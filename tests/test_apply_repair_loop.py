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


def test_apply_runs_repair_loop_when_verification_fails(tmp_path: Path, monkeypatch):
    target = tmp_path / "app.py"
    original = "value = 1\n"
    patched = "value = 2\n"
    target.write_text(original, encoding="utf-8")

    SequenceChecker.calls = 0
    monkeypatch.setattr(api_main, "ProjectChecker", SequenceChecker)
    monkeypatch.setattr(api_main, "RepairAgent", FakeRepairAgent)
    monkeypatch.setenv("DEPGUARD_AUTO_REPAIR", "true")
    monkeypatch.setenv("DEPGUARD_REPAIR_MAX_ATTEMPTS", "1")

    session_id = "preview_test_repair"
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

    assert response["verification"]["status"] == "passed"
    assert response["repair"]["status"] == "success"
    assert response["repair"]["attempts"][0]["status"] == "success"
    assert response["repair"]["attempts"][0]["files_repaired"] == ["app.py"]
    assert target.read_text(encoding="utf-8") == patched
