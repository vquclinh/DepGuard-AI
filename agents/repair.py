import asyncio
import inspect
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.llm_router import LLMRouter

try:
    from tools.ast_scanner import ASTScanner
except ImportError:
    ASTScanner = None

logger = logging.getLogger(__name__)


@dataclass
class RepairTarget:
    file: str
    start_line: int
    end_line: int
    source: str
    reason: str


class RepairAgent:
    """Repair code after ProjectChecker reports compiler/test errors.

    The patch agent performs dependency migration. This agent is narrower: it
    reads concrete verifier errors, sends focused code slices to the LLM, applies
    JSON line-range replacements, and leaves the checker loop to the caller.
    """

    OUTPUT_LIMIT = 12000
    MAX_TARGET_FILES = 8

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.router = LLMRouter()
        self.syntax_scanner = ASTScanner() if ASTScanner else None

    async def repair(self, verification: dict[str, Any], changed_files: list[str]) -> dict[str, Any]:
        if verification.get("status") != "failed":
            return {
                "status": "not_needed",
                "message": "Verification did not fail.",
                "files_repaired": [],
                "llm_provider": "none",
                "errors": [],
            }

        error_text = self._verification_error_text(verification)
        candidates = self._candidate_files(error_text, changed_files)
        if not candidates:
            return {
                "status": "skipped",
                "message": "No repair candidate files found.",
                "files_repaired": [],
                "llm_provider": "none",
                "errors": [],
            }

        files_repaired: list[str] = []
        errors: list[dict[str, str]] = []
        provider = "none"
        fallback_used = False

        for file_path in candidates[:self.MAX_TARGET_FILES]:
            try:
                original = Path(file_path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original = Path(file_path).read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                errors.append({"file": file_path, "error": str(exc)})
                continue

            targets = self._targets_for_file(file_path, original, error_text)
            if not targets:
                continue

            system_prompt, prompt = self._build_repair_prompt(file_path, targets, error_text)
            try:
                response = await self.router.complete(system_prompt, prompt, max_tokens=2000, task_type="patch_complex")
                provider = response.provider
                fallback_used = fallback_used or response.fallback_used
                patched = self._apply_response(original, response.content, targets)
                if patched == original:
                    continue

                validation_error = self._validate(file_path, patched)
                if validation_error:
                    errors.append({"file": file_path, "error": validation_error})
                    continue

                Path(file_path).write_text(patched, encoding="utf-8")
                files_repaired.append(self._relative(file_path))
            except Exception as exc:
                logger.error("Repair failed for %s: %s", file_path, exc)
                errors.append({"file": self._relative(file_path), "error": str(exc)})

        status = "success" if files_repaired and not errors else "partial" if files_repaired else "failed"
        return {
            "status": status,
            "message": self._message(status, files_repaired, errors),
            "files_repaired": files_repaired,
            "llm_provider": provider,
            "fallback_used": fallback_used,
            "errors": errors,
        }

    def repair_sync(self, verification: dict[str, Any], changed_files: list[str]) -> dict[str, Any]:
        async def _repair_and_close() -> dict[str, Any]:
            try:
                return await self.repair(verification, changed_files)
            finally:
                close = getattr(self.router, "aclose", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result

        return asyncio.run(_repair_and_close())

    def _verification_error_text(self, verification: dict[str, Any]) -> str:
        chunks = []
        for command in verification.get("commands", []):
            if not isinstance(command, dict):
                continue
            if command.get("status") not in {"failed", "timeout"}:
                continue
            chunks.append(f"COMMAND: {' '.join(command.get('command', []))}")
            chunks.append(f"STATUS: {command.get('status')} EXIT: {command.get('exit_code')}")
            for key in ("error", "stderr", "stdout"):
                value = command.get(key)
                if value:
                    chunks.append(str(value))
        return "\n".join(chunks)[: self.OUTPUT_LIMIT]

    def _candidate_files(self, error_text: str, changed_files: list[str]) -> list[str]:
        candidates: list[str] = []

        def add(file_path: str) -> None:
            absolute = self._absolute(file_path)
            if absolute.exists() and absolute.is_file():
                value = str(absolute)
                if value not in candidates:
                    candidates.append(value)

        for file_path, _line in self._error_locations(error_text):
            add(file_path)

        for file_path in changed_files:
            add(file_path)

        return candidates

    def _error_locations(self, text: str) -> list[tuple[str, int]]:
        locations: list[tuple[str, int]] = []
        patterns = [
            r"-->\s+(.+?):(\d+):\d+",                      # Rust
            r'File "(.+?)", line (\d+)',                    # Python
            r"([A-Za-z0-9_./\\-]+\.(?:ts|tsx|js|jsx))\((\d+),\d+\)",  # TS
            r"([A-Za-z0-9_./\\-]+\.(?:go|rs|java|py|ts|tsx|js|jsx)):(\d+):\d+",
            r"([A-Za-z0-9_./\\-]+\.java):\[(\d+),\d+\]",   # Maven javac
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                try:
                    locations.append((match.group(1), int(match.group(2))))
                except (IndexError, ValueError):
                    continue
        return locations

    def _targets_for_file(self, file_path: str, content: str, error_text: str) -> list[RepairTarget]:
        lines = content.splitlines()
        line_numbers = [
            line
            for candidate, line in self._error_locations(error_text)
            if self._absolute(candidate) == Path(file_path).resolve()
        ]
        if not line_numbers:
            line_numbers = [1]

        targets: dict[tuple[int, int], RepairTarget] = {}
        for line in line_numbers[:6]:
            start_line = max(1, line - 30)
            end_line = min(len(lines), line + 30)
            targets[(start_line, end_line)] = RepairTarget(
                file=file_path,
                start_line=start_line,
                end_line=end_line,
                source="\n".join(lines[start_line - 1:end_line]),
                reason=f"Verification error near line {line}.",
            )
        return [targets[key] for key in sorted(targets)]

    def _build_repair_prompt(self, file_path: str, targets: list[RepairTarget], error_text: str) -> tuple[str, str]:
        blocks = [
            {
                "start_line": target.start_line,
                "end_line": target.end_line,
                "reason": target.reason,
                "source": target.source,
            }
            for target in targets
        ]
        system_prompt = (
            "You are a build/test repair agent.\n"
            "Fix ONLY the verification errors shown.\n"
            "Patch only the provided line ranges.\n"
            "Preserve unrelated formatting and behavior.\n"
            "Return ONLY JSON: {\"replacements\":[{\"start_line\":1,\"end_line\":2,\"replacement\":\"code\"}]}."
        )
        prompt = f"""
            Project root: {self.project_root}
            File: {file_path}

            Verification errors:
            {error_text}

            Target code ranges:
            {json.dumps(blocks, indent=2)}

            Return complete replacement source for each modified target range.
            If no repair is needed in a range, return the original source for that range.
            """
        return system_prompt, prompt

    def _apply_response(self, original: str, response_text: str, targets: list[RepairTarget]) -> str:
        replacements = self._parse_replacements(response_text)
        if not replacements:
            return original

        lines = original.splitlines()
        trailing_newline = "\n" if original.endswith("\n") else ""
        allowed = {(target.start_line, target.end_line) for target in targets}
        for replacement in sorted(replacements, key=lambda item: item["start_line"], reverse=True):
            key = (replacement["start_line"], replacement["end_line"])
            if key not in allowed:
                continue
            lines[replacement["start_line"] - 1:replacement["end_line"]] = replacement["replacement"].splitlines()
        return "\n".join(lines) + trailing_newline

    def _parse_replacements(self, response_text: str) -> list[dict[str, Any]]:
        text = response_text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        replacements = data.get("replacements")
        if not isinstance(replacements, list):
            return []
        valid = []
        for item in replacements:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("start_line"), int) and isinstance(item.get("end_line"), int) and isinstance(item.get("replacement"), str):
                valid.append(item)
        return valid

    def _validate(self, file_path: str, content: str) -> str | None:
        if not self.syntax_scanner:
            return None
        return self.syntax_scanner.validate_source(file_path, content)

    def _absolute(self, file_path: str) -> Path:
        path = Path(file_path)
        if path.is_absolute():
            return path.resolve()
        return (self.project_root / path).resolve()

    def _relative(self, file_path: str) -> str:
        try:
            return Path(file_path).resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(file_path)

    def _message(self, status: str, files_repaired: list[str], errors: list[dict[str, str]]) -> str:
        if status == "success":
            return f"Repaired {len(files_repaired)} file(s)."
        if status == "partial":
            return f"Repaired {len(files_repaired)} file(s), with {len(errors)} remaining repair error(s)."
        return "Repair failed; no files were changed."
