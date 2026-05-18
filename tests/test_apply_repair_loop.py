import time
from pathlib import Path

from api import main as api_main
from api.main import ApplyPreviewRequest


class SequenceChecker:
    calls = 0

    def __init__(self, project_root: str):
        self.project_root = project_root

    def run(self):
        SequenceChecker.calls += 1
        if SequenceChecker.calls == 1:
            return {
                "status": "failed",
                "message": "Verification failed.",
                "commands": [{
                    "name": "python_compileall",
                    "command": ["python", "-m", "compileall"],
                    "status": "failed",
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "broken.py:1: error",
                    "error": "",
                    "duration_ms": 1,
                    "reason": "test",
                }],
            }
        return {"status": "passed", "message": "ok", "commands": []}


class FakeRepairAgent:
    def __init__(self, project_root: str):
        self.project_root = project_root

    def repair_sync(self, verification, changed_files):
        return {
            "status": "success",
            "message": "repaired",
            "files_repaired": changed_files,
            "llm_provider": "fake",
            "errors": [],
        }


def test_apply_writes_files_and_runs_single_check(tmp_path: Path, monkeypatch):
    """After the refactor, /apply does ONE checker pass (no repair loop).
    Repair already happened in the preview-stream sandbox."""
    target = tmp_path / "app.py"
    original = "value = 1\n"
    patched = "value = 2\n"
    target.write_text(original, encoding="utf-8")

    SequenceChecker.calls = 0
    monkeypatch.setattr(api_main, "ProjectChecker", SequenceChecker)

    session_id = "preview_test_apply_simple"
    api_main.PREVIEW_SESSIONS[session_id] = {
        "session_id": session_id,
        "folder_path": str(tmp_path),
        "package_info": {"file_path": str(target), "name": "demo"},
        "files_original": {"app.py": original},
        "files_patched": {"app.py": patched},
        "created_at": time.time(),
    }

    response = api_main.apply_preview(
        ApplyPreviewRequest(
            session_id=session_id,
            decisions={"app.py": {"file_decision": "accept"}},
        )
    )

    # File was written
    assert target.read_text(encoding="utf-8") == patched
    # Only ONE checker call (no repair loop)
    assert SequenceChecker.calls == 1
    # repair is always skipped — it ran in the sandbox during preview
    assert response["repair"]["status"] == "skipped"
    assert response["repair"]["attempts"] == []
