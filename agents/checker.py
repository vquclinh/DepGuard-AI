import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CheckCommand:
    name: str
    command: list[str]
    reason: str


@dataclass
class CheckCommandResult:
    name: str
    command: list[str]
    reason: str
    status: str
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    error: str


class ProjectChecker:
    """Run best-effort project verification after patches are applied.

    This is intentionally separate from LSP. LSP answers "where is related
    code?"; this checker answers "does the project still build or test?"
    """

    OUTPUT_LIMIT = 8000
    IGNORE_COMPILEALL_PATTERN = (
        r"(/\.git/|/\.venv/|/venv/|/env/|/node_modules/|/target/|/build/|/dist/|/\.depguard_cache/)"
    )

    def __init__(self, project_root: str, timeout_seconds: int = 120):
        self.project_root = Path(project_root).resolve()
        self.timeout_seconds = timeout_seconds

    def detect_checks(self) -> list[CheckCommand]:
        checks: list[CheckCommand] = []

        if (self.project_root / "Cargo.toml").exists() and self._has_command("cargo"):
            checks.append(CheckCommand(
                name="cargo_check",
                command=["cargo", "check", "--quiet"],
                reason="Rust project detected from Cargo.toml.",
            ))

        if (self.project_root / "go.mod").exists() and self._has_command("go"):
            checks.append(CheckCommand(
                name="go_test",
                command=["go", "test", "./..."],
                reason="Go module detected from go.mod.",
            ))

        package_json = self.project_root / "package.json"
        if package_json.exists():
            checks.extend(self._javascript_checks(package_json))

        if self._is_python_project():
            checks.append(self._python_check())

        if (self.project_root / "pom.xml").exists() and self._has_command("mvn"):
            checks.append(CheckCommand(
                name="maven_test",
                command=["mvn", "test", "-q"],
                reason="Maven project detected from pom.xml.",
            ))

        gradlew = self.project_root / "gradlew"
        if (self.project_root / "build.gradle").exists() or (self.project_root / "build.gradle.kts").exists():
            if gradlew.exists():
                checks.append(CheckCommand(
                    name="gradle_test",
                    command=[str(gradlew), "test"],
                    reason="Gradle project detected; using project wrapper.",
                ))
            elif self._has_command("gradle"):
                checks.append(CheckCommand(
                    name="gradle_test",
                    command=["gradle", "test"],
                    reason="Gradle project detected.",
                ))

        return checks

    def run(self, max_checks: int | None = None) -> dict[str, Any]:
        if not self.project_root.exists() or not self.project_root.is_dir():
            return {
                "status": "skipped",
                "message": "Project root does not exist.",
                "commands": [],
            }

        checks = self.detect_checks()
        if max_checks is not None:
            checks = checks[:max_checks]

        if not checks:
            return {
                "status": "skipped",
                "message": "No supported verification command was detected.",
                "commands": [],
            }

        results = [self._run_command(check) for check in checks]
        statuses = {result.status for result in results}
        if "failed" in statuses or "timeout" in statuses:
            status = "failed"
        elif statuses == {"skipped"}:
            status = "skipped"
        else:
            status = "passed"

        return {
            "status": status,
            "message": self._summary_message(status, results),
            "commands": [asdict(result) for result in results],
        }

    def _javascript_checks(self, package_json: Path) -> list[CheckCommand]:
        checks: list[CheckCommand] = []
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return checks

        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        local_tsc = self.project_root / "node_modules" / ".bin" / "tsc"
        if local_tsc.exists() and ((self.project_root / "tsconfig.json").exists() or "typecheck" in scripts):
            checks.append(CheckCommand(
                name="typescript_check",
                command=[str(local_tsc), "--noEmit"],
                reason="TypeScript project detected with local tsc.",
            ))
            return checks

        if "typecheck" in scripts and self._has_command("npm"):
            checks.append(CheckCommand(
                name="npm_typecheck",
                command=["npm", "run", "typecheck"],
                reason="package.json has a typecheck script.",
            ))
            return checks

        test_script = str(scripts.get("test", ""))
        has_real_test = test_script and "no test specified" not in test_script.lower()
        if has_real_test and self._has_command("npm"):
            checks.append(CheckCommand(
                name="npm_test",
                command=["npm", "test", "--", "--watch=false"],
                reason="package.json has a test script.",
            ))

        return checks

    def _is_python_project(self) -> bool:
        markers = [
            "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
        ]
        if any((self.project_root / marker).exists() for marker in markers):
            return True
        return any(self.project_root.glob("*.py")) or (self.project_root / "tests").exists()

    def _python_check(self) -> CheckCommand:
        if (self.project_root / "tests").exists() and self._python_module_exists("pytest"):
            return CheckCommand(
                name="pytest",
                command=[sys.executable, "-m", "pytest", "-q"],
                reason="Python project with tests detected.",
            )

        return CheckCommand(
            name="python_compileall",
            command=[
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "-x",
                self.IGNORE_COMPILEALL_PATTERN,
                ".",
            ],
            reason="Python project detected; running syntax compilation.",
        )

    def _run_command(self, check: CheckCommand) -> CheckCommandResult:
        start = time.time()
        env = os.environ.copy()
        env.update({
            "CI": "true",
            "NO_COLOR": "1",
        })

        try:
            result = subprocess.run(
                check.command,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
            status = "passed" if result.returncode == 0 else "failed"
            return CheckCommandResult(
                name=check.name,
                command=check.command,
                reason=check.reason,
                status=status,
                exit_code=result.returncode,
                duration_ms=int((time.time() - start) * 1000),
                stdout=self._trim(result.stdout),
                stderr=self._trim(result.stderr),
                error="",
            )
        except subprocess.TimeoutExpired as exc:
            return CheckCommandResult(
                name=check.name,
                command=check.command,
                reason=check.reason,
                status="timeout",
                exit_code=None,
                duration_ms=int((time.time() - start) * 1000),
                stdout=self._trim(exc.stdout or ""),
                stderr=self._trim(exc.stderr or ""),
                error=f"Timed out after {self.timeout_seconds}s.",
            )
        except OSError as exc:
            logger.debug("Verification command failed to start: %s", exc)
            return CheckCommandResult(
                name=check.name,
                command=check.command,
                reason=check.reason,
                status="skipped",
                exit_code=None,
                duration_ms=int((time.time() - start) * 1000),
                stdout="",
                stderr="",
                error=str(exc),
            )

    def _summary_message(self, status: str, results: list[CheckCommandResult]) -> str:
        if status == "passed":
            return f"{len(results)} verification command(s) passed."
        if status == "failed":
            failed = [result.name for result in results if result.status in {"failed", "timeout"}]
            return f"Verification failed: {', '.join(failed)}."
        return "Verification skipped."

    def _has_command(self, command: str) -> bool:
        return shutil.which(command) is not None

    def _python_module_exists(self, module: str) -> bool:
        try:
            __import__(module)
            return True
        except Exception:
            return False

    def _trim(self, value: str | bytes) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if len(value) <= self.OUTPUT_LIMIT:
            return value
        return value[:self.OUTPUT_LIMIT] + "\n... output truncated ..."
