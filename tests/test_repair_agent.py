import json
from pathlib import Path

import pytest

from agents import repair as repair_module
from agents.repair import RepairAgent
from tools.llm_router import LLMResponse


class FakeRepairRouter:
    def __init__(self):
        self.last_prompt = ""

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        self.last_prompt = user_prompt
        marker = "Target code ranges:"
        block_text = user_prompt.split(marker, 1)[1].split("Return complete replacement", 1)[0]
        blocks = json.loads(block_text)
        block = blocks[0]
        replacement = block["source"].replace("return missing_name", "return 1")
        return LLMResponse(
            content=json.dumps({
                "replacements": [{
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "replacement": replacement,
                }]
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


def test_repair_agent_repairs_file_from_checker_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = tmp_path / "app.py"
    app.write_text("def value():\n    return missing_name\n", encoding="utf-8")
    router = FakeRepairRouter()
    monkeypatch.setattr(repair_module, "LLMRouter", lambda: router)

    verification = {
        "status": "failed",
        "commands": [{
            "status": "failed",
            "command": ["python", "-m", "pytest"],
            "exit_code": 1,
            "stderr": f'File "{app}", line 2, in value\nNameError: name missing_name is not defined',
            "stdout": "",
            "error": "",
        }],
    }

    report = RepairAgent(str(tmp_path)).repair_sync(verification, ["app.py"])

    assert report["status"] == "success"
    assert report["files_repaired"] == ["app.py"]
    assert "return 1" in app.read_text(encoding="utf-8")
    assert "Verification errors" in router.last_prompt


def test_repair_agent_skips_when_verification_passed(tmp_path: Path):
    report = RepairAgent(str(tmp_path)).repair_sync({"status": "passed", "commands": []}, ["app.py"])

    assert report["status"] == "not_needed"
