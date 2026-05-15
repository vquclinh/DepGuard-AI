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

    def _get_impact_context(self, filepath: str, matches: list) -> str:
        if ImpactFinder is None:
            return ""

        changed_lines = sorted({
            match.get("line")
            for match in matches
            if isinstance(match.get("line"), int) and match.get("line") > 0
        })
        if not changed_lines:
            return ""

        try:
            if self._impact_finder is None:
                self._impact_finder = ImpactFinder(self.project_root)
            impact = self._impact_finder.find_impact(filepath, changed_lines, max_depth=2)
            return impact.to_llm_context()
        except Exception as e:
            logger.debug(f"Could not build impact context for {filepath}: {e}")
            return ""

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
        match = re.search(r'```python\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        match = re.search(r'```\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        return response_text.strip()

    async def _generate_patched_content(self, filepath: str, matches: list, scout_context: dict, task_type: str) -> tuple[bool, str, dict, str, str]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
                return False, f"Could not read file: {e}", {}, "", ""

        system_prompt = (
            "You are an expert code migration assistant.\n"
            "Fix ONLY the deprecated API usages listed.\n"
            "Do NOT change any other code, logic, or formatting.\n"
            "Return ONLY the complete fixed file content, no explanation."
        )

        matches_str = json.dumps(matches, indent=2)
        bc_str = json.dumps(scout_context.get("breaking_changes", []), indent=2)
        impact_context = self._get_impact_context(filepath, matches)

        prompt = f"""
            Package Migration Context: {scout_context.get("package")} {scout_context.get("from_version")} -> {scout_context.get("to_version")}
            Breaking Changes:
            {bc_str}

            File: {filepath}
            Matches to fix:
            {matches_str}

            Related Impact Context:
            {impact_context or "No related impact context available."}

            Code:
            ```python
            {original_content}
            ```
            """

        try:
            # Call LLM
            response = await self.router.complete(system_prompt, prompt, max_tokens=2000, task_type=task_type)
            response_text = response.content
            patched_content = self._extract_code(response_text)
            llm_info = {"provider": response.provider, "fallback_used": response.fallback_used}

            # Validate syntax
            try:
                ast.parse(patched_content, filename=filepath)
            except SyntaxError as e:
                logger.error(f"Validation failed for {filepath}: {e}")
                return False, f"Syntax error in LLM output: {e}", llm_info, original_content, patched_content

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

        return report

    def preview_sync(self, scout_output: dict, ast_scanner_output: dict) -> dict:
        return asyncio.run(self.preview(scout_output, ast_scanner_output))
