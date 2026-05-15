import shutil
from pathlib import Path

from agents.checker import ProjectChecker


def test_python_compileall_passes_for_valid_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    report = ProjectChecker(str(tmp_path), timeout_seconds=20).run()

    assert report["status"] == "passed"
    assert report["commands"][0]["name"] == "python_compileall"


def test_python_compileall_reports_failure(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    report = ProjectChecker(str(tmp_path), timeout_seconds=20).run()

    assert report["status"] == "failed"
    assert report["commands"][0]["status"] == "failed"
    assert report["commands"][0]["exit_code"] != 0


def test_detects_rust_check_when_cargo_is_available(tmp_path: Path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/bin/{command}" if command == "cargo" else None)

    checks = ProjectChecker(str(tmp_path)).detect_checks()

    assert checks[0].name == "cargo_check"
    assert checks[0].command == ["cargo", "check", "--quiet"]


def test_no_supported_project_is_skipped(tmp_path: Path):
    report = ProjectChecker(str(tmp_path)).run()

    assert report["status"] == "skipped"
    assert report["commands"] == []
