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
    from anthropic import AsyncAnthropic
except ImportError:
    print("anthropic is required. Install with: pip install anthropic")
    sys.exit(1)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

class PatchAgent:
    def __init__(self):
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.anthropic_client = AsyncAnthropic(api_key=self.anthropic_api_key) if self.anthropic_api_key else None
        self.model = "claude-sonnet-4-20250514"

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

    def _extract_code(self, response_text: str) -> str:
        # If wrapped in markdown
        match = re.search(r'```python\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        match = re.search(r'```\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        return response_text.strip()

    async def _patch_file(self, filepath: str, matches: list, scout_context: dict) -> tuple[bool, str]:
        if not self.anthropic_client:
            logger.error("Anthropic API key not set. Cannot patch file.")
            return False, "Missing API Key"

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
            return False, f"Could not read file: {e}"

        system_prompt = (
            "You are an expert code migration assistant.\n"
            "Fix ONLY the deprecated API usages listed.\n"
            "Do NOT change any other code, logic, or formatting.\n"
            "Return ONLY the complete fixed file content, no explanation."
        )

        matches_str = json.dumps(matches, indent=2)
        bc_str = json.dumps(scout_context.get("breaking_changes", []), indent=2)

        prompt = f"""
Package Migration Context: {scout_context.get("package")} {scout_context.get("from_version")} -> {scout_context.get("to_version")}
Breaking Changes:
{bc_str}

File: {filepath}
Matches to fix:
{matches_str}

Code:
```python
{original_content}
```
"""

        try:
            response = await self.anthropic_client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            response_text = response.content[0].text
            patched_content = self._extract_code(response_text)

            # Validate AST
            try:
                ast.parse(patched_content, filename=filepath)
            except SyntaxError as e:
                logger.error(f"Validation failed for {filepath}: {e}")
                return False, f"Syntax error in LLM output: {e}"

            # Write file
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(patched_content)

            return True, ""
        except Exception as e:
            logger.error(f"Error patching {filepath}: {e}")
            return False, str(e)

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
            "overall_status": "in_progress"
        }

        if not matches_by_file:
            report["overall_status"] = "success"
            return report

        checkpoint_id, commit_made = self._create_checkpoint(package)
        report["checkpoint_id"] = checkpoint_id

        overall_success = True
        
        for filepath, matches in matches_by_file.items():
            success, error_msg = await self._patch_file(filepath, matches, scout_output)
            
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

def main():
    parser = argparse.ArgumentParser(description="DepGuard AI Patch Agent")
    parser.add_argument("--scout", required=True, help="Path to scout_output.json")
    parser.add_argument("--ast", required=True, help="Path to ast_output.json")
    parser.add_argument("--dep", required=True, help="Path to dependency file (e.g. requirements.txt)")
    args = parser.parse_args()

    try:
        with open(args.scout, 'r') as f:
            scout_output = json.load(f)
        with open(args.ast, 'r') as f:
            ast_output = json.load(f)
    except Exception as e:
        logger.error(f"Error reading input files: {e}")
        sys.exit(1)

    agent = PatchAgent()
    report = agent.run_sync(scout_output, ast_output, args.dep)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1] == "test":
        main()
    else:
        import unittest
        from unittest.mock import patch, MagicMock, AsyncMock
        import tempfile
        import shutil

        class TestPatchAgent(unittest.TestCase):
            def setUp(self):
                self.agent = PatchAgent()
                self.agent.anthropic_api_key = "test"
                self.agent.anthropic_client = MagicMock()
                self.agent.anthropic_client.messages.create = AsyncMock()
                self.test_dir = tempfile.mkdtemp()
                
                self.scout_output = {
                    "package": "numpy",
                    "from_version": "1.21.0",
                    "to_version": "1.26.4",
                    "breaking_changes": [{"type": "removed", "old_api": "np.bool", "new_api": "np.bool_"}]
                }
                
                self.test_file = os.path.join(self.test_dir, "test_file.py")
                with open(self.test_file, "w") as f:
                    f.write("import numpy as np\nmask = np.bool(1)\n")
                    
                self.dep_file = os.path.join(self.test_dir, "requirements.txt")
                with open(self.dep_file, "w") as f:
                    f.write("numpy==1.21.0\nrequests==2.28.0\n")
                    
                self.ast_output = {
                    "matches_by_file": {
                        self.test_file: [{"line": 2, "old_api": "np.bool"}]
                    }
                }

            def tearDown(self):
                shutil.rmtree(self.test_dir)

            @patch('subprocess.run')
            def test_successful_patch(self, mock_subprocess):
                mock_res = MagicMock()
                mock_res.returncode = 0
                mock_subprocess.return_value = mock_res
                
                mock_msg = MagicMock()
                mock_msg.content = [MagicMock(text='```python\nimport numpy as np\nmask = np.bool_(1)\n```')]
                self.agent.anthropic_client.messages.create.return_value = mock_msg
                
                report = self.agent.run_sync(self.scout_output, self.ast_output, self.dep_file)
                
                self.assertEqual(report["overall_status"], "success")
                self.assertEqual(len(report["files_patched"]), 1)
                self.assertEqual(report["dependency_file_updated"], "requirements.txt")
                
                with open(self.test_file, "r") as f:
                    content = f.read()
                self.assertIn("np.bool_", content)
                
                with open(self.dep_file, "r") as f:
                    dep_content = f.read()
                self.assertIn("numpy==1.26.4", dep_content)

            @patch('subprocess.run')
            def test_syntax_error_rollback(self, mock_subprocess):
                mock_msg = MagicMock()
                mock_msg.content = [MagicMock(text='```python\nimport numpy as np\nmask = invalid syntax\n```')]
                self.agent.anthropic_client.messages.create.return_value = mock_msg
                
                report = self.agent.run_sync(self.scout_output, self.ast_output, self.dep_file)
                
                self.assertEqual(report["overall_status"], "rolled_back")
                
                with open(self.test_file, "r") as f:
                    content = f.read()
                self.assertIn("np.bool(1)", content)

        sys.argv = [sys.argv[0]]
        print("Running Patch Agent Unit Tests...\n")
        unittest.main()
