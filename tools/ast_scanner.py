import ast
import os
import argparse
import json
import logging
from pathlib import Path
from collections import defaultdict

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --------------------------------- Find all the old API ------------------------------- 
class DeprecatedAPIVisitor(ast.NodeVisitor):
    # Constructor
    def __init__(self, breaking_changes: list, file_lines: list, filepath: str):
        self.breaking_changes = breaking_changes
        self.file_lines = file_lines
        self.filepath = filepath
        self.matches = []
        
        self.aliases = {}
        self.from_imports = {}

    # Handle import alias: "import numpy as np"
    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                self.aliases[alias.name] = alias.name
        self.generic_visit(node)

    # Handle from import: "from django.utils.encoding import force_text"
    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            name = alias.name
            asname = alias.asname or name
            full_name = f"{module}.{name}" if module else name
            self.from_imports[asname] = full_name
            
            self._check_api_usage(full_name, node)
        self.generic_visit(node)

    # Convert alias to the real name: make "np.bool" to "numpy.bool"
    def _get_full_name_for_attribute(self, node) -> str:
        if isinstance(node, ast.Name):
            base = node.id
            return self.aliases.get(base, base)
        elif isinstance(node, ast.Attribute):
            base = self._get_full_name_for_attribute(node.value)
            if base:
                return f"{base}.{node.attr}"
        return ""

    # Handle object.attribute: "np.bool" or "pandas.DataFrame.append"
    def visit_Attribute(self, node):
        full_name = self._get_full_name_for_attribute(node)
        if full_name:
            self._check_api_usage(full_name, node)
        self.generic_visit(node)

    # Handle "like_this()"
    def visit_Name(self, node):
        name = node.id
        if name in self.from_imports:
            full_name = self.from_imports[name]
            self._check_api_usage(full_name, node)
        else:
            self._check_api_usage(name, node)
        self.generic_visit(node)

    def _check_api_usage(self, full_name: str, node: ast.AST):
        for bc in self.breaking_changes:
            old_api = bc.get("old_api", "")
            if not old_api:
                continue

            resolved_old_api = old_api
            parts = old_api.split('.')
            if parts[0] in self.aliases:
                resolved_old_api = f"{self.aliases[parts[0]]}." + ".".join(parts[1:]) if len(parts) > 1 else self.aliases[parts[0]]

            match = False
            if full_name == old_api or full_name == resolved_old_api:
                match = True
            elif isinstance(node, ast.Attribute) and old_api.endswith("." + node.attr):
                match = True
            elif isinstance(node, ast.Name) and old_api == node.id:
                match = True
            elif old_api == full_name.split('.')[-1] and full_name.endswith(old_api):
                match = True

            if match:
                line = getattr(node, "lineno", -1)
                col = getattr(node, "col_offset", -1)
                if any(m["line"] == line and m["col"] == col for m in self.matches):
                    continue
                    
                snippet = ""
                if line > 0 and line <= len(self.file_lines):
                    snippet = self.file_lines[line - 1].rstrip()
                    
                self.matches.append({
                    "file": self.filepath,
                    "line": line,
                    "col": col,
                    "old_api": old_api,
                    "new_api": bc.get("new_api", ""),
                    "description": bc.get("description", ""),
                    "code_snippet": snippet,
                    "type": bc.get("type", "")
                })

# -------------------------------- Find API that using in project --------------------------
class APIUsageVisitor(ast.NodeVisitor):
    def __init__(self, target_package: str):
        self.target_package = target_package
        self.used_apis = set()
        self.aliases = {}
        self.from_imports = {}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name.startswith(self.target_package):
                asname = alias.asname or alias.name
                self.aliases[asname] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if module.startswith(self.target_package):
            for alias in node.names:
                name = alias.name
                asname = alias.asname or name
                full_name = f"{module}.{name}"
                self.from_imports[asname] = full_name
                self.used_apis.add(full_name)
        self.generic_visit(node)

    def _get_full_name_for_attribute(self, node) -> str:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        elif isinstance(node, ast.Attribute):
            base = self._get_full_name_for_attribute(node.value)
            if base:
                return f"{base}.{node.attr}"
        return ""

    def visit_Attribute(self, node):
        full_name = self._get_full_name_for_attribute(node)
        if full_name and full_name.startswith(self.target_package):
            self.used_apis.add(full_name)
        self.generic_visit(node)

    def visit_Name(self, node):
        name = node.id
        if name in self.from_imports:
            self.used_apis.add(self.from_imports[name])
        self.generic_visit(node)


class ASTScanner:
    def __init__(self):
        pass

    def find_api_usages(self, root_folder: str, package_name: str) -> list[str]:
        root = Path(root_folder)
        ignore_dirs = {'venv', '.venv', '__pycache__', 'node_modules', '.git'}
        all_used_apis = set()

        if not root.exists() or not root.is_dir():
            logger.error(f"Invalid directory: {root_folder}")
            return []

        for filepath in root.rglob("*.py"):
            if any(part in ignore_dirs for part in filepath.parts):
                continue
                
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                tree = ast.parse(content, filename=str(filepath))
                visitor = APIUsageVisitor(package_name)
                visitor.visit(tree)
                all_used_apis.update(visitor.used_apis)
            except Exception as e:
                logger.debug(f"Error parsing {filepath} for API usage: {e}")

        # Try to clean up standard base module usage if a sub-attribute is more specific
        # e.g., if we have both "requests" and "requests.get", keep both for safety, 
        # but in many cases we just pass the raw list.
        return sorted(list(all_used_apis))

    def scan(self, root_folder: str, breaking_changes: list) -> dict:
        root = Path(root_folder)
        ignore_dirs = {'venv', '.venv', '__pycache__', 'node_modules', '.git'}
        
        matches_by_file = defaultdict(list)
        total_files_scanned = 0
        total_matches = 0
        
        if not root.exists() or not root.is_dir():
            logger.error(f"Invalid directory: {root_folder}")
            return {
                "total_files_scanned": 0,
                "total_files_affected": 0,
                "total_matches": 0,
                "matches_by_file": {}
            }

        for filepath in root.rglob("*.py"):
            if any(part in ignore_dirs for part in filepath.parts):
                continue
                
            total_files_scanned += 1
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                lines = content.split('\n')
                tree = ast.parse(content, filename=str(filepath))
                
                visitor = DeprecatedAPIVisitor(breaking_changes, lines, str(filepath))
                visitor.visit(tree)
                
                if visitor.matches:
                    matches_by_file[str(filepath)].extend(visitor.matches)
                    total_matches += len(visitor.matches)
                    
            except SyntaxError as e:
                logger.warning(f"SyntaxError in {filepath}, skipping. Error: {e}")
            except Exception as e:
                logger.warning(f"Error parsing {filepath}: {e}")

        summary = {
            "total_files_scanned": total_files_scanned,
            "total_files_affected": len(matches_by_file),
            "total_matches": total_matches,
            "matches_by_file": dict(matches_by_file)
        }
        
        return summary
