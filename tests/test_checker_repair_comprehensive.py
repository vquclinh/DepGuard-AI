"""
Comprehensive tests for ProjectChecker and RepairAgent.

Unit tests use fake/mock LLMs and run instantly.
Integration tests (marked `llm`) call real Qwen via OpenRouter and verify
the full checker → repair → checker loop end-to-end.

Run all:        pytest tests/test_checker_repair_comprehensive.py -v
Run unit only:  pytest tests/test_checker_repair_comprehensive.py -v -m "not llm"
Run LLM only:   pytest tests/test_checker_repair_comprehensive.py -v -m llm
"""

import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from agents.checker import ProjectChecker
from agents.repair import RepairAgent
from tools.llm_router import LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_verification(stderr: str, stdout: str = "", status: str = "failed") -> dict:
    return {
        "status": status,
        "commands": [{
            "name": "python_compileall",
            "command": [sys.executable, "-m", "compileall", "-q", "."],
            "status": status,
            "exit_code": 1 if status == "failed" else 0,
            "stderr": stderr,
            "stdout": stdout,
            "error": "",
        }],
    }


class FixedRouter:
    """Fake LLM router that returns a pre-baked replacement."""

    def __init__(self, replacement_fn):
        self._fn = replacement_fn

    async def complete(self, system_prompt, user_prompt, max_tokens=2000, task_type="general"):
        import re, json as _json
        m = re.search(r"\[\{.*?\}\]", user_prompt, re.DOTALL)
        blocks = _json.loads(m.group(0)) if m else []
        replacements = [self._fn(b) for b in blocks if self._fn(b) is not None]
        return LLMResponse(
            content=_json.dumps({"replacements": replacements}),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )

    async def close(self):
        pass


# ============================================================================
# SECTION 1 — ProjectChecker unit tests
# ============================================================================

class TestCheckerDetection:
    def test_empty_dir_skipped(self, tmp_path):
        r = ProjectChecker(str(tmp_path)).run()
        assert r["status"] == "skipped"
        assert r["commands"] == []

    def test_nonexistent_root_skipped(self):
        r = ProjectChecker("/nonexistent/path/xyz").run()
        assert r["status"] == "skipped"

    def test_python_pyproject_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any(c.name in ("python_compileall", "pytest") for c in checks)

    def test_python_requirements_txt_detected(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests\n")
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any("python" in c.name or "pytest" in c.name for c in checks)

    def test_python_bare_py_file_detected(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any("python" in c.name or "pytest" in c.name for c in checks)

    def test_python_tests_folder_prefers_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_ok(): pass\n")
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        python_check = next(c for c in checks if "python" in c.name or "pytest" in c.name)
        # If pytest is importable it should prefer pytest, else compileall
        assert python_check.name in ("pytest", "python_compileall")

    def test_rust_detected_when_cargo_present(self, tmp_path, monkeypatch):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/{cmd}" if cmd == "cargo" else None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert checks[0].name == "cargo_check"
        assert "cargo" in checks[0].command

    def test_rust_not_detected_without_cargo(self, tmp_path, monkeypatch):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
        monkeypatch.setattr(shutil, "which", lambda cmd: None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert not any(c.name == "cargo_check" for c in checks)

    def test_go_detected_when_go_present(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module example.com/x\ngo 1.21\n")
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/{cmd}" if cmd == "go" else None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any(c.name == "go_test" for c in checks)

    def test_typescript_local_tsc_preferred(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}\n")
        pkg = {"scripts": {}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        fake_tsc = bin_dir / "tsc"
        fake_tsc.write_text("#!/bin/sh\nexit 0\n")
        fake_tsc.chmod(0o755)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any(c.name == "typescript_check" for c in checks)

    def test_package_json_with_typecheck_script(self, tmp_path, monkeypatch):
        pkg = {"scripts": {"typecheck": "tsc --noEmit"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/{cmd}" if cmd == "npm" else None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any(c.name in ("npm_typecheck", "typescript_check") for c in checks)

    def test_package_json_with_no_test_script(self, tmp_path):
        pkg = {"scripts": {"start": "node index.js"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert not any(c.name == "npm_test" for c in checks)

    def test_maven_detected_when_mvn_present(self, tmp_path, monkeypatch):
        (tmp_path / "pom.xml").write_text("<project/>\n")
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/{cmd}" if cmd == "mvn" else None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        assert any(c.name == "maven_test" for c in checks)

    def test_gradle_wrapper_preferred_over_system_gradle(self, tmp_path, monkeypatch):
        (tmp_path / "build.gradle").write_text("plugins {}\n")
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\nexit 0\n")
        gradlew.chmod(0o755)
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/{cmd}" if cmd == "gradle" else None)
        checks = ProjectChecker(str(tmp_path)).detect_checks()
        gradle = next(c for c in checks if c.name == "gradle_test")
        assert str(gradlew) in gradle.command

    def test_max_checks_limits_results(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "app.py").write_text("x = 1\n")
        r = ProjectChecker(str(tmp_path)).run(max_checks=0)
        assert r["status"] == "skipped"


class TestCheckerExecution:
    def test_valid_python_passes(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "app.py").write_text("x = 1\nprint(x)\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        assert r["status"] == "passed"

    def test_syntax_error_fails(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "broken.py").write_text("def f(:\n    pass\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        assert r["status"] == "failed"
        assert r["commands"][0]["exit_code"] != 0

    def test_multiple_files_one_broken_fails(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "ok.py").write_text("x = 1\n")
        (tmp_path / "bad.py").write_text("def f(:\n    pass\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        assert r["status"] == "failed"

    def test_multiple_files_all_valid_passes(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        for i in range(5):
            (tmp_path / f"mod{i}.py").write_text(f"VAL_{i} = {i}\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        assert r["status"] == "passed"

    def test_deeply_nested_package_syntax_error_caught(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        pkg = tmp_path / "myapp" / "core" / "utils"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "helper.py").write_text("class Bad(:\n    pass\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        assert r["status"] == "failed"

    def test_output_contains_command_details(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "app.py").write_text("x = 1\n")
        r = ProjectChecker(str(tmp_path), timeout_seconds=30).run()
        cmd = r["commands"][0]
        assert "name" in cmd
        assert "status" in cmd
        assert "exit_code" in cmd
        assert "duration_ms" in cmd
        assert cmd["duration_ms"] >= 0


# ============================================================================
# SECTION 2 — RepairAgent unit tests (fake LLM)
# ============================================================================

import agents.repair as repair_module


class TestRepairUnit:
    def test_skips_when_verification_passed(self, tmp_path):
        r = RepairAgent(str(tmp_path)).repair_sync(
            {"status": "passed", "commands": []}, []
        )
        assert r["status"] == "not_needed"

    def test_skips_when_no_candidates(self, tmp_path):
        r = RepairAgent(str(tmp_path)).repair_sync(
            make_verification("some error with no file paths"), []
        )
        assert r["status"] == "skipped"

    def test_skips_non_utf8_file(self, tmp_path, monkeypatch):
        bad = tmp_path / "binary.py"
        bad.write_bytes(b"\xff\xfe" + b"x = 1\n")
        # Fake candidate extraction to include the binary file
        monkeypatch.setattr(
            RepairAgent, "_candidate_files",
            lambda self, err, changed_files: [str(bad)],
        )
        r = RepairAgent(str(tmp_path)).repair_sync(
            make_verification(f'File "{bad}", line 1'), [str(bad)]
        )
        # Should have an error entry for the binary file, not crash
        assert r["status"] in ("skipped", "failed", "partial")
        assert any("UTF-8" in e.get("error", "") for e in r.get("errors", []))

    def test_returns_failed_when_llm_raises(self, tmp_path, monkeypatch):
        app = tmp_path / "app.py"
        app.write_text("x = undefined_var\n")

        class BrokenRouter:
            async def complete(self, *a, **kw):
                raise RuntimeError("LLM offline")
            async def close(self): pass

        monkeypatch.setattr(repair_module, "LLMRouter", lambda: BrokenRouter())
        r = RepairAgent(str(tmp_path)).repair_sync(
            make_verification(f'File "{app}", line 1, NameError'), [str(app)]
        )
        assert r["status"] in ("failed", "partial")
        assert r["files_repaired"] == []

    def test_no_change_is_not_reported_as_repaired(self, tmp_path, monkeypatch):
        app = tmp_path / "app.py"
        original = "x = 1\ny = 2\n"
        app.write_text(original)

        class NoChangeRouter:
            async def complete(self, system_prompt, user_prompt, **kw):
                # Return the exact same source unchanged
                import re, json as _j
                m = re.search(r"\[\{.*?\}\]", user_prompt, re.DOTALL)
                blocks = _j.loads(m.group(0)) if m else []
                b = blocks[0] if blocks else {"start_line": 1, "end_line": 1, "source": "x = 1"}
                return LLMResponse(
                    content=_j.dumps({"replacements": [{
                        "start_line": b["start_line"],
                        "end_line": b["end_line"],
                        "replacement": b["source"],
                    }]}),
                    provider="fake", model="test", latency_ms=1, fallback_used=False,
                )
            async def close(self): pass

        monkeypatch.setattr(repair_module, "LLMRouter", lambda: NoChangeRouter())
        r = RepairAgent(str(tmp_path)).repair_sync(
            make_verification(f'File "{app}", line 1'), [str(app)]
        )
        assert "app.py" not in r["files_repaired"]

    def test_parse_replacements_handles_multiple_json_objects(self, tmp_path):
        """Regression test for the greedy-regex bug: LLM explains with an inline
        JSON snippet before the actual replacements object."""
        agent = RepairAgent(str(tmp_path))
        messy_response = textwrap.dedent("""
            The problem is {"type": "NameError"} in the first line.
            Here is the fix {"status": "not_real"}.
            ```json
            {"replacements": [{"start_line": 1, "end_line": 1, "replacement": "x = 1"}]}
            ```
        """)
        replacements = agent._parse_replacements(messy_response)
        assert len(replacements) == 1
        assert replacements[0]["replacement"] == "x = 1"

    def test_parse_replacements_balanced_braces_no_fence(self, tmp_path):
        """Balanced-brace fallback handles JSON without a fenced block."""
        agent = RepairAgent(str(tmp_path))
        raw = 'Fixed it. {"replacements": [{"start_line": 2, "end_line": 3, "replacement": "pass"}]}'
        replacements = agent._parse_replacements(raw)
        assert replacements[0]["start_line"] == 2

    def test_parse_replacements_empty_response(self, tmp_path):
        agent = RepairAgent(str(tmp_path))
        assert agent._parse_replacements("") == []
        assert agent._parse_replacements("No JSON here at all.") == []

    def test_parse_replacements_invalid_json(self, tmp_path):
        agent = RepairAgent(str(tmp_path))
        assert agent._parse_replacements("{not valid json}") == []

    def test_parse_replacements_wrong_types_skipped(self, tmp_path):
        agent = RepairAgent(str(tmp_path))
        payload = json.dumps({"replacements": [
            {"start_line": "one", "end_line": 2, "replacement": "x"},  # str start_line
            {"start_line": 1, "end_line": 2, "replacement": 99},       # int replacement
            {"start_line": 1, "end_line": 2, "replacement": "good"},   # valid
        ]})
        result = agent._parse_replacements(payload)
        assert len(result) == 1
        assert result[0]["replacement"] == "good"

    def test_apply_response_reverse_order_preserves_lines(self, tmp_path):
        """Replacements applied in reverse order so earlier line numbers stay valid."""
        agent = RepairAgent(str(tmp_path))
        original = "line1\nline2\nline3\nline4\nline5\n"
        from agents.repair import RepairTarget
        targets = [RepairTarget(file="", start_line=1, end_line=1, source="line1", reason=""), RepairTarget(file="", start_line=3, end_line=3, source="line3", reason="")]
        payload = json.dumps({"replacements": [
            {"start_line": 1, "end_line": 1, "replacement": "FIRST"},
            {"start_line": 3, "end_line": 3, "replacement": "THIRD"},
        ]})
        result = agent._apply_response(original, payload, targets)
        lines = result.splitlines()
        assert lines[0] == "FIRST"
        assert lines[2] == "THIRD"
        assert lines[1] == "line2"

    def test_apply_response_rejects_out_of_range_replacements(self, tmp_path):
        """Replacements that don't match an allowed target are silently dropped."""
        agent = RepairAgent(str(tmp_path))
        original = "line1\nline2\nline3\n"
        from agents.repair import RepairTarget
        targets = [RepairTarget(file="", start_line=1, end_line=1, source="line1", reason="")]  # only line 1 allowed
        payload = json.dumps({"replacements": [
            {"start_line": 2, "end_line": 2, "replacement": "SHOULD_NOT_APPLY"},
        ]})
        result = agent._apply_response(original, payload, targets)
        assert result == original  # unchanged

    def test_repair_preserves_trailing_newline(self, tmp_path, monkeypatch):
        app = tmp_path / "app.py"
        app.write_text("x = bad\n")

        class OneLineRouter:
            async def complete(self, sys_p, usr_p, **kw):
                import re, json as _j
                m = re.search(r"\[\{.*?\}\]", usr_p, re.DOTALL)
                blocks = _j.loads(m.group(0)) if m else []
                b = blocks[0]
                return LLMResponse(
                    content=_j.dumps({"replacements": [{
                        "start_line": b["start_line"],
                        "end_line": b["end_line"],
                        "replacement": "x = 42",
                    }]}),
                    provider="fake", model="test", latency_ms=1, fallback_used=False,
                )
            async def close(self): pass

        monkeypatch.setattr(repair_module, "LLMRouter", lambda: OneLineRouter())
        RepairAgent(str(tmp_path)).repair_sync(
            make_verification(f'File "{app}", line 1, NameError: bad'), [str(app)]
        )
        assert app.read_text().endswith("\n")


# ============================================================================
# SECTION 3 — Integration tests: real LLM (Qwen via OpenRouter)
# Each test verifies the full checker → repair → checker loop.
# ============================================================================

pytestmark_llm = pytest.mark.llm


def run_full_loop(tmp_path: Path, files: dict[str, str], expect_pass_after_repair: bool = True):
    """Helper: write files, run checker, run repair, run checker again."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")

    checker = ProjectChecker(str(tmp_path), timeout_seconds=60)
    before = checker.run()
    assert before["status"] == "failed", f"Expected checker to fail before repair, got: {before['status']}\n{before}"

    agent = RepairAgent(str(tmp_path))
    # Only pass source files (not test/config files) to the repair agent so the
    # LLM doesn't inadvertently rewrite test assertions or build configs.
    source_files = [
        str(tmp_path / f)
        for f in files
        if not Path(f).name.startswith("test_")
        and not f.startswith("tests/")
        and Path(f).suffix == ".py"
    ]
    repair_report = agent.repair_sync(before, source_files)

    after = checker.run()
    if expect_pass_after_repair:
        assert after["status"] == "passed", (
            f"Checker still failing after repair.\n"
            f"Repair report: {json.dumps(repair_report, indent=2)}\n"
            f"Checker after: {json.dumps(after, indent=2)}"
        )
    return repair_report, after


@pytest.mark.llm
def test_llm_repair_name_error(tmp_path):
    """NameError: undefined variable — LLM should replace with a valid value."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "app.py": """\
            def greet(name):
                return msg  # NameError: msg is not defined
        """,
        "test_app.py": """\
            from app import greet
            def test_greet():
                result = greet("Alice")
                assert result is not None
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_attribute_error_method_renamed(tmp_path):
    """Simulates a package upgrade where a method was renamed."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "processor.py": """\
            class DataProcessor:
                def process(self, data):
                    # Calling a non-existent method (simulates renamed API)
                    return data.strip().encode_utf8()  # AttributeError
        """,
        "test_processor.py": """\
            from processor import DataProcessor
            def test_process():
                dp = DataProcessor()
                result = dp.process("hello")
                assert isinstance(result, bytes)
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_import_error(tmp_path):
    """ImportError: MutableMapping moved to collections.abc in Python 3.10.
    The import fails at collection-time; fix is to change the import path."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "utils.py": """\
            from collections import MutableMapping  # removed in Python 3.10+

            def is_mapping(obj) -> bool:
                return isinstance(obj, MutableMapping)
        """,
        "test_utils.py": """\
            from utils import is_mapping
            def test_is_mapping():
                assert is_mapping({}) is True
                assert is_mapping([]) is False
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_type_error_wrong_args(tmp_path):
    """TypeError from calling a function with wrong number of arguments.
    The module-level bad call triggers TypeError when pytest imports math_utils."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "math_utils.py": """\
            def add(a, b):
                return a + b

            result = add(1, 2, 3)  # TypeError: too many arguments — executed at import time
        """,
        "test_math_utils.py": """\
            from math_utils import add
            def test_add():
                assert add(1, 2) == 3
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_syntax_error_in_patch(tmp_path):
    """Syntax error left by a bad patch — LLM should correct the syntax."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "config.py": """\
            def load_config(path):
                with open(path) as f
                    return f.read()
        """,
    })


@pytest.mark.llm
def test_llm_repair_indentation_error(tmp_path):
    """IndentationError — common after automated patching."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "handler.py": """\
            def handle(request):
                if request:
                return "ok"  # wrong indent
        """,
    })


@pytest.mark.llm
def test_llm_repair_multiple_files(tmp_path):
    """Two files both with syntax errors — repair agent must fix both.
    Uses compileall-detectable syntax errors so the checker reliably fails."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "module_a.py": """\
            def compute(x, y)
                return x + y
        """,
        "module_b.py": """\
            def transform(data):
                if data
                    return str(data)
                return ""
        """,
    })


@pytest.mark.llm
def test_llm_repair_cryptography_api_migration(tmp_path):
    """Simulates a cryptography package upgrade breaking change:
    Fernet.encrypt() now requires bytes, not str."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "crypto_utils.py": """\
            # Simulated post-patch code where the LLM forgot to encode the string
            def encrypt_message(key_bytes, message: str) -> bytes:
                from cryptography.fernet import Fernet
                f = Fernet(key_bytes)
                # BUG: message must be bytes, not str
                return f.encrypt(message)
        """,
        "test_crypto.py": """\
            from cryptography.fernet import Fernet
            from crypto_utils import encrypt_message

            def test_encrypt():
                key = Fernet.generate_key()
                token = encrypt_message(key, "hello world")
                assert isinstance(token, bytes)
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_requests_to_httpx_migration(tmp_path):
    """Simulates migrating from requests to httpx: import was changed but the
    call site still uses `requests.get` (NameError). LLM should change to httpx.get."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "client.py": """\
            import httpx

            def fetch_data(url: str) -> dict:
                # BUG: should use httpx.get not requests.get (requests not imported)
                response = requests.get(url)
                return response.json()
        """,
        "test_client.py": """\
            from unittest.mock import MagicMock, patch
            from client import fetch_data

            def test_fetch_data():
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"ok": True}
                with patch("httpx.get", return_value=mock_resp):
                    result = fetch_data("http://test.example.com/api")
                assert result == {"ok": True}
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_class_method_wrong_return(tmp_path):
    """Method return type is wrong — simulates an LLM patch that changed
    a function to return None instead of a value."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "service.py": """\
            class UserService:
                def get_user(self, user_id: int):
                    # BUG: forgot return
                    user = {"id": user_id, "name": "Alice"}

                def create_user(self, name: str) -> dict:
                    return {"id": 1, "name": name}
        """,
        "test_service.py": """\
            from service import UserService

            def test_get_user():
                svc = UserService()
                user = svc.get_user(1)
                assert user is not None
                assert user["id"] == 1
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_dict_api_change(tmp_path):
    """Simulates dict-like API change where .has_key() was removed in Python 3."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "legacy.py": """\
            def check_key(d: dict, key: str) -> bool:
                # Python 2 style — AttributeError in Python 3
                return d.has_key(key)
        """,
        "test_legacy.py": """\
            from legacy import check_key

            def test_check_key():
                d = {"a": 1}
                assert check_key(d, "a") is True
                assert check_key(d, "b") is False
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_nested_class_error(tmp_path):
    """Error inside a nested class — LLM must correctly target the right scope."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "models.py": """\
            class Outer:
                class Inner:
                    def compute(self):
                        return undefined_inner  # NameError inside nested class
        """,
        "test_models.py": """\
            from models import Outer

            def test_inner():
                result = Outer.Inner().compute()
                assert result is not None
        """,
        "tests/__init__.py": "",
    })


@pytest.mark.llm
def test_llm_repair_decorator_usage_error(tmp_path):
    """Wrong decorator usage — common after library upgrades."""
    run_full_loop(tmp_path, {
        "pyproject.toml": "[project]\nname='x'\n",
        "routes.py": """\
            from functools import wraps

            def require_auth(f):
                @wraps(f)
                def wrapper(*args, **kwargs):
                    # BUG: forgot to call and return the wrapped function
                    f(*args, **kwargs)
                return wrapper

            @require_auth
            def protected_view():
                return {"status": "ok"}
        """,
        "test_routes.py": """\
            from routes import protected_view

            def test_protected_view():
                result = protected_view()
                assert result == {"status": "ok"}
        """,
        "tests/__init__.py": "",
    })
