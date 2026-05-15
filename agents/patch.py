import os
import re
import json
import asyncio
import logging
import argparse
import subprocess
import ast
import uuid
import sys
import time
from pathlib import Path
from typing import Dict, List, Any
from dotenv import load_dotenv

try:
    from tools.llm_router import LLMRouter
except ImportError:
    print("llm_router is required. Ensure you are running from the project root.")
    sys.exit(1)

try:
    from tools.impact_graph import ImpactFinder
except ImportError:
    ImpactFinder = None

try:
    from tools.ast_scanner import ASTScanner as TreeSitterScanner
except ImportError:
    TreeSitterScanner = None

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# -------------------------------- Patch Agent ----------------------------------
class PatchAgent:
    def __init__(self, project_root: str | None = None):
        self.router = LLMRouter()
        self.project_root = project_root or os.getcwd()
        self._impact_finder = None
        self.syntax_scanner = TreeSitterScanner() if TreeSitterScanner else None

    def _get_changed_lines(self, matches: list) -> list[int]:
        return sorted({
            match.get("line")
            for match in matches
            if isinstance(match.get("line"), int) and match.get("line") > 0
        })

    def _get_impact_result(self, filepath: str, matches: list):
        if ImpactFinder is None:
            return None

        changed_lines = self._get_changed_lines(matches)
        if not changed_lines:
            return None

        try:
            if self._impact_finder is None:
                self._impact_finder = ImpactFinder(self.project_root)
            return self._impact_finder.find_impact(filepath, changed_lines, max_depth=2)
        except Exception as e:
            logger.debug(f"Could not build impact context for {filepath}: {e}")
            return None

    def _get_impact_context(self, filepath: str, matches: list) -> str:
        impact = self._get_impact_result(filepath, matches)
        if impact:
            return impact.to_llm_context()
        return ""

    def _line_window_block(self, filepath: str, lines: list[str], line: int, radius: int = 18) -> dict:
        start_line = max(1, line - radius)
        end_line = min(len(lines), line + radius)
        return {
            "file": filepath,
            "start_line": start_line,
            "end_line": end_line,
            "context_type": "line_window",
            "name": None,
            "parent": None,
            "source": "\n".join(lines[start_line - 1:end_line]),
        }

    def _target_blocks(self, filepath: str, matches: list, lines: list[str], impact_result) -> list[dict]:
        blocks_by_range: dict[tuple[int, int], dict] = {}

        for line in self._get_changed_lines(matches):
            node = None
            if self._impact_finder:
                try:
                    node = self._impact_finder.get_node_at_line(filepath, line)
                except Exception:
                    node = None

            if node:
                location = node.location
                blocks_by_range[(location.start_line, location.end_line)] = {
                    "file": filepath,
                    "start_line": location.start_line,
                    "end_line": location.end_line,
                    "context_type": location.context_type,
                    "name": location.name,
                    "parent": location.parent,
                    "source": location.source,
                }
            else:
                block = self._line_window_block(filepath, lines, line)
                blocks_by_range[(block["start_line"], block["end_line"])] = block

        if not blocks_by_range and impact_result:
            for node in impact_result.changed_nodes:
                location = node.location
                blocks_by_range[(location.start_line, location.end_line)] = {
                    "file": filepath,
                    "start_line": location.start_line,
                    "end_line": location.end_line,
                    "context_type": location.context_type,
                    "name": location.name,
                    "parent": location.parent,
                    "source": location.source,
                }

        return [blocks_by_range[key] for key in sorted(blocks_by_range)]

    def _top_level_context(self, lines: list[str], max_lines: int = 80) -> str:
        context_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                context_lines.append(line)
                continue
            if stripped.startswith((
                "import ", "from ", "use ", "mod ", "pub mod ", "package ",
                "#![", "#[", "extern crate ", "using ", "namespace ",
            )):
                context_lines.append(line)
                continue
            if context_lines and len(context_lines) < max_lines:
                context_lines.append(line)
            break

        return "\n".join(context_lines[:max_lines]).rstrip()

    def _compact_impact_context(self, impact_result, current_file: str, max_chars: int = 12000) -> str:
        if not impact_result:
            return ""

        current_path = Path(current_file).resolve()
        parts = [impact_result.summary]

        def add_node(prefix: str, node, reason: str = "", attrs: list[str] | None = None) -> None:
            location = node.location
            node_file = self._absolute_project_file(location.file)
            try:
                display_file = Path(node_file).resolve().relative_to(Path(self.project_root).resolve()).as_posix()
            except ValueError:
                display_file = location.file
            header = (
                f"{prefix}: {display_file}:{location.start_line}-{location.end_line} "
                f"{location.context_type} {location.name or 'module level'}"
            )
            if reason:
                header += f" | reason: {reason}"
            if attrs:
                header += f" | attrs: {', '.join(attrs)}"
            parts.append(header)

            # The current file receives full target blocks separately. For other
            # related files, include only a compact excerpt because those files
            # are expanded into their own LLM review targets when needed.
            if Path(node_file).resolve() != current_path:
                excerpt = "\n".join(location.source.splitlines()[:12])
                if excerpt:
                    parts.append(excerpt)

        for node in impact_result.changed_nodes:
            add_node("Changed", node)

        for impacted in impact_result.impacted_nodes:
            add_node(
                f"Related depth {impacted.depth}",
                impacted.node,
                impacted.impact_reason,
                impacted.affected_attributes,
            )
            if len("\n".join(parts)) > max_chars:
                parts.append("... impact context truncated; additional related nodes are reviewed as separate targets.")
                break

        return "\n".join(parts)[:max_chars]

    def _build_sliced_patch_prompt(
        self,
        filepath: str,
        matches: list,
        scout_context: dict,
        original_content: str,
    ) -> tuple[str, str, list[dict]]:
        lines = original_content.splitlines()
        impact_result = self._get_impact_result(filepath, matches)
        impact_context = self._compact_impact_context(impact_result, filepath)
        target_blocks = self._target_blocks(filepath, matches, lines, impact_result)
        code_language = self._code_fence_language(filepath)
        matches_str = json.dumps(matches, indent=2)
        bc_str = json.dumps(scout_context.get("breaking_changes", []), indent=2)
        blocks_str = json.dumps(
            [
                {
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "context_type": block.get("context_type"),
                    "name": block.get("name"),
                    "parent": block.get("parent"),
                    "source": block["source"],
                }
                for block in target_blocks
            ],
            indent=2,
        )
        import_context = self._top_level_context(lines)

        system_prompt = (
            "You are an expert code migration assistant.\n"
            "Use the provided Tree-sitter/LSP impact context to patch only the target line ranges.\n"
            "Do NOT change unrelated code, logic, formatting, or comments.\n"
            "If a target range does not require a code change, return its original source unchanged.\n"
            "Return ONLY JSON with this shape: "
            "{\"replacements\":[{\"start_line\":1,\"end_line\":2,\"replacement\":\"code\"}]}."
        )

        prompt = f"""
            Package Migration Context: {scout_context.get("package")} {scout_context.get("from_version")} -> {scout_context.get("to_version")}
            Breaking Changes:
            {bc_str}

            File: {filepath}
            Matches to fix:
            {matches_str}

            Top-level imports/module context:
            ```{code_language}
            {import_context or "(none)"}
            ```

            TARGET CODE BLOCKS TO PATCH:
            {blocks_str}

            Related Tree-sitter/LSP Impact Context:
            {impact_context or "No related impact context available."}

            Instructions:
            - Return replacements for the target blocks only.
            - Keep each replacement as complete code for the exact start_line/end_line range.
            - Include all necessary edits within those ranges if related code in the same block must change.
            - Do not return markdown.
            """
        return system_prompt, prompt, target_blocks

    def _parse_replacements(self, response_text: str) -> list[dict] | None:
        text = response_text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None

        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

        replacements = data.get("replacements")
        if not isinstance(replacements, list):
            return None

        valid = []
        for replacement in replacements:
            if not isinstance(replacement, dict):
                continue
            start_line = replacement.get("start_line")
            end_line = replacement.get("end_line")
            source = replacement.get("replacement")
            if isinstance(start_line, int) and isinstance(end_line, int) and isinstance(source, str):
                valid.append({
                    "start_line": start_line,
                    "end_line": end_line,
                    "replacement": source,
                })
        return valid

    def _apply_replacements(self, original_content: str, replacements: list[dict], allowed_blocks: list[dict]) -> str:
        lines = original_content.splitlines()
        trailing_newline = "\n" if original_content.endswith("\n") else ""
        allowed_ranges = {
            (block["start_line"], block["end_line"])
            for block in allowed_blocks
        }

        normalized = []
        for replacement in replacements:
            key = (replacement["start_line"], replacement["end_line"])
            if key not in allowed_ranges:
                logger.debug("Ignoring replacement outside target block: %s", key)
                continue
            normalized.append(replacement)

        for replacement in sorted(normalized, key=lambda item: item["start_line"], reverse=True):
            start_index = max(0, replacement["start_line"] - 1)
            end_index = min(len(lines), replacement["end_line"])
            replacement_lines = replacement["replacement"].splitlines()
            lines[start_index:end_index] = replacement_lines

        return "\n".join(lines) + trailing_newline

    def _patch_response_to_full_file(
        self,
        response_text: str,
        original_content: str,
        target_blocks: list[dict],
    ) -> str:
        replacements = self._parse_replacements(response_text)
        if replacements is not None:
            return self._apply_replacements(original_content, replacements, target_blocks)

        # Backward compatibility for older prompts/tests/providers that return a
        # complete file instead of JSON replacements.
        return self._extract_code(response_text)

    def _absolute_project_file(self, file_path: str) -> str:
        path = Path(file_path)
        if path.is_absolute():
            return str(path)
        return str((Path(self.project_root) / path).resolve())

    def _expand_matches_with_impacted_nodes(self, matches_by_file: dict) -> dict:
        """Add LSP/Tree-sitter related nodes as review targets."""
        expanded = {
            filepath: list(matches)
            for filepath, matches in matches_by_file.items()
        }
        seen = {
            (str(filepath), match.get("line"), match.get("type"), match.get("old_api"))
            for filepath, matches in expanded.items()
            for match in matches
        }

        for filepath, matches in list(expanded.items()):
            impact = self._get_impact_result(filepath, matches)
            if not impact:
                continue

            for impacted in impact.impacted_nodes:
                location = impacted.node.location
                absolute_file = self._absolute_project_file(location.file)
                if not os.path.exists(absolute_file):
                    continue
                key = (absolute_file, location.start_line, "impact_review", impacted.node.id)
                if key in seen:
                    continue
                seen.add(key)
                expanded.setdefault(absolute_file, []).append({
                    "file": absolute_file,
                    "line": location.start_line,
                    "col": 0,
                    "old_api": impacted.node.id,
                    "new_api": "",
                    "description": (
                        f"Related code may need review because it {impacted.impact_reason}. "
                        f"Review the full {location.context_type} block."
                    ),
                    "code_snippet": location.source.splitlines()[0] if location.source else "",
                    "type": "impact_review",
                    "impact_reason": impacted.impact_reason,
                    "affected_attributes": impacted.affected_attributes,
                })

        return expanded

    # -------------------------- Create A Commit Before Update Code ---------------------
    def _create_checkpoint(self, package: str) -> tuple[str, bool]:
        checkpoint_id = f"depguard_checkpoint_{uuid.uuid4().hex[:8]}"
        try:
            subprocess.run(["git", "add", "."], capture_output=True, check=False)
            res = subprocess.run(
                ["git", "commit", "-m", f"depguard: checkpoint before patching {package} {checkpoint_id}"],
                capture_output=True,
                check=False
            )
            commit_made = res.returncode == 0
            return checkpoint_id, commit_made
        except Exception as e:
            logger.warning(f"Failed to create git checkpoint: {e}")
            return checkpoint_id, False

    # ------------------- Rollback to the last commit (no change by LLM) ----------------
    def _rollback(self, commit_made: bool):
        try:
            logger.info("Rolling back changes...")
            # Restore working directory
            subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
            if commit_made:
                # Remove the commit we just made
                subprocess.run(["git", "reset", "--hard", "HEAD~1"], capture_output=True, check=False)
        except Exception as e:
            logger.warning(f"Failed to rollback: {e}")

    # -------------------- Extract code out of markdown form by LLM -----------------------
    def _extract_code(self, response_text: str) -> str:
        # If wrapped in markdown
        match = re.search(r'```(?:[A-Za-z0-9_+\-.#]+)?\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        return response_text.strip()

    def _code_fence_language(self, filepath: str) -> str:
        extension = os.path.splitext(filepath)[1].lower()
        return {
            ".py": "python",
            ".pyw": "python",
            ".js": "javascript",
            ".jsx": "jsx",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".mts": "typescript",
            ".cts": "typescript",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".kt": "kotlin",
            ".kts": "kotlin",
            ".c": "c",
            ".h": "c",
            ".cc": "cpp",
            ".cpp": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
            ".cs": "csharp",
            ".php": "php",
            ".rb": "ruby",
            ".swift": "swift",
            ".dart": "dart",
            ".lua": "lua",
            ".ex": "elixir",
            ".exs": "elixir",
            ".hs": "haskell",
            ".html": "html",
            ".htm": "html",
            ".css": "css",
            ".scss": "scss",
            ".sass": "scss",
        }.get(extension, "")

    def _validate_patched_content(self, filepath: str, patched_content: str) -> str | None:
        if self.syntax_scanner:
            validation_error = self.syntax_scanner.validate_source(filepath, patched_content)
            if validation_error:
                return validation_error

            language = self.syntax_scanner.detect_language(filepath)
            if language and self.syntax_scanner.parsers.get(language):
                return None

        if filepath.endswith((".py", ".pyw")):
            try:
                ast.parse(patched_content, filename=filepath)
            except SyntaxError as e:
                return f"Syntax error in LLM output: {e}"

        return None

    async def _generate_patched_content(self, filepath: str, matches: list, scout_context: dict, task_type: str) -> tuple[bool, str, dict, str, str]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
                return False, f"Could not read file: {e}", {}, "", ""

        system_prompt, prompt, target_blocks = self._build_sliced_patch_prompt(
            filepath,
            matches,
            scout_context,
            original_content,
        )

        try:
            # Call LLM
            response = await self.router.complete(system_prompt, prompt, max_tokens=2000, task_type=task_type)
            response_text = response.content
            patched_content = self._patch_response_to_full_file(response_text, original_content, target_blocks)
            llm_info = {"provider": response.provider, "fallback_used": response.fallback_used}

            validation_error = self._validate_patched_content(filepath, patched_content)
            if validation_error:
                logger.error(f"Validation failed for {filepath}: {validation_error}")
                return False, validation_error, llm_info, original_content, patched_content

            return True, "", llm_info, original_content, patched_content
        except Exception as e:
            logger.error(f"Error patching {filepath}: {e}")
            return False, str(e), {}, original_content, ""


    async def _patch_file(self, filepath: str, matches: list, scout_context: dict, task_type: str) -> tuple[bool, str, dict]:
        success, error_msg, llm_info, _original_content, patched_content = await self._generate_patched_content(filepath, matches, scout_context, task_type)
        if not success:
            return False, error_msg, llm_info

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(patched_content)
            return True, "", llm_info
        except Exception as e:
            logger.error(f"Error writing patched file {filepath}: {e}")
            return False, str(e), llm_info

    # Update dependencies version
    def _update_dependency_file(self, dep_file_path: str, package: str, from_v: str, to_v: str):
        try:
            with open(dep_file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # regex to replace version string only on the line containing the package name
            # Works for `package==1.2.3` or `"package": "^1.2.3"`
            # \g<1> refers to the prefix before the version
            new_content = re.sub(rf'({package}[^\d\n]*){re.escape(from_v)}', rf'\g<1>{to_v}', content, flags=re.IGNORECASE)
            
            with open(dep_file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        except Exception as e:
            logger.warning(f"Failed to update dependency file {dep_file_path}: {e}")
            return False

    async def run(self, scout_output: dict, ast_scanner_output: dict, dep_file_path: str) -> dict:
        package = scout_output.get("package", "unknown")
        from_v = scout_output.get("from_version", "")
        to_v = scout_output.get("to_version", "")
        
        matches_by_file = ast_scanner_output.get("matches_by_file", {})
        if "matches_by_file" not in ast_scanner_output:
            if isinstance(ast_scanner_output, dict) and not "total_files_scanned" in ast_scanner_output:
                matches_by_file = ast_scanner_output
        matches_by_file = self._expand_matches_with_impacted_nodes(matches_by_file)

        report = {
            "package": package,
            "from_version": from_v,
            "to_version": to_v,
            "files_patched": [],
            "dependency_file_updated": "",
            "checkpoint_id": "",
            "overall_status": "in_progress",
            "llm_provider": "none",
            "fallback_used": False
        }

        if not matches_by_file:
            report["overall_status"] = "success"
            return report

        breaking_changes = scout_output.get("breaking_changes", [])
        if breaking_changes and all(c.get("type") == "renamed" for c in breaking_changes):
            task_type = "patch_simple"
        else:
            task_type = "patch_complex"

        checkpoint_id, commit_made = self._create_checkpoint(package)
        report["checkpoint_id"] = checkpoint_id

        overall_success = True
        
        for filepath, matches in matches_by_file.items():
            success, error_msg, llm_info = await self._patch_file(filepath, matches, scout_output, task_type)
            if llm_info:
                report["llm_provider"] = llm_info.get("provider", report["llm_provider"])
                report["fallback_used"] = report["fallback_used"] or llm_info.get("fallback_used", False)
            
            lines_changed = list(set([m.get("line", 0) for m in matches]))
            
            file_report = {
                "file": filepath,
                "lines_changed": lines_changed,
                "status": "success" if success else "failed"
            }
            if not success:
                file_report["error"] = error_msg
                overall_success = False
                
            report["files_patched"].append(file_report)
            
            if not overall_success:
                break

        if not overall_success:
            self._rollback(commit_made)
            report["overall_status"] = "rolled_back"
            return report

        # Update dependency file
        if dep_file_path and os.path.exists(dep_file_path):
            if self._update_dependency_file(dep_file_path, package, from_v, to_v):
                report["dependency_file_updated"] = os.path.basename(dep_file_path)

        report["overall_status"] = "success"
        return report

    def run_sync(self, scout_output: dict, ast_scanner_output: dict, dep_file_path: str) -> dict:
        return asyncio.run(self.run(scout_output, ast_scanner_output, dep_file_path))

    async def preview(self, scout_output: dict, ast_scanner_output: dict) -> dict:
        package = scout_output.get("package", "unknown")
        from_v = scout_output.get("from_version", "")
        to_v = scout_output.get("to_version", "")

        matches_by_file = ast_scanner_output.get("matches_by_file", {})
        if "matches_by_file" not in ast_scanner_output:
            if isinstance(ast_scanner_output, dict) and not "total_files_scanned" in ast_scanner_output:
                matches_by_file = ast_scanner_output
        matches_by_file = self._expand_matches_with_impacted_nodes(matches_by_file)

        breaking_changes = scout_output.get("breaking_changes", [])
        if breaking_changes and all(c.get("type") == "renamed" for c in breaking_changes):
            task_type = "patch_simple"
        else:
            task_type = "patch_complex"

        report = {
            "package": package,
            "from_version": from_v,
            "to_version": to_v,
            "files": [],
            "llm_provider": "none",
            "fallback_used": False,
        }

        for filepath, matches in matches_by_file.items():
            success, error_msg, llm_info, original_content, patched_content = await self._generate_patched_content(filepath, matches, scout_output, task_type)
            if llm_info:
                report["llm_provider"] = llm_info.get("provider", report["llm_provider"])
                report["fallback_used"] = report["fallback_used"] or llm_info.get("fallback_used", False)

            report["files"].append({
                "file": filepath,
                "status": "success" if success else "failed",
                "error": error_msg,
                "original": original_content,
                "patched": patched_content if success else original_content,
            })
            if not success and "All configured LLM providers failed" in error_msg:
                break

        return report

    def preview_sync(self, scout_output: dict, ast_scanner_output: dict) -> dict:
        return asyncio.run(self.preview(scout_output, ast_scanner_output))
