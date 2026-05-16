from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from agents import patch as patch_module
from agents.patch import PatchAgent
from tools.ast_scanner import ASTScanner
from tools.llm_router import LLMResponse


class FakeRouter:
    def __init__(self, patched_content: str):
        self.patched_content = patched_content
        self.last_prompt = ""

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        self.last_prompt = user_prompt
        marker = "TARGET CODE BLOCKS TO PATCH:"
        block_text = user_prompt.split(marker, 1)[1].split("Related Tree-sitter/LSP Impact Context:", 1)[0]
        block = json.loads(block_text)[0]
        patched_lines = self.patched_content.splitlines()
        replacement = "\n".join(patched_lines[block["start_line"] - 1:block["end_line"]])
        return LLMResponse(
            content=json.dumps({
                "schema_version": "depguard.patch.v1",
                "status": "patched",
                "replacements": [{
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "replacement": replacement,
                }],
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class JsonReplacementRouter:
    def __init__(self):
        self.last_prompt = ""

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        self.last_prompt = user_prompt
        marker = "TARGET CODE BLOCKS TO PATCH:"
        block_text = user_prompt.split(marker, 1)[1].split("Related Tree-sitter/LSP Impact Context:", 1)[0]
        blocks = json.loads(block_text)
        block = blocks[0]
        replacement = block["source"].replace("oldpkg::do_thing()", "newpkg::do_thing()")
        return LLMResponse(
            content=json.dumps({
                "schema_version": "depguard.patch.v1",
                "status": "patched",
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


class TextOnlyRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        return LLMResponse(
            content="I changed the deprecated call. The code should now use newpkg::do_thing().",
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class NoChangeRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        return LLMResponse(
            content='{"schema_version":"depguard.patch.v1","status":"no_change","replacements":[]}',
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class TopLevelArrayRouter:
    def __init__(self):
        self.last_prompt = ""

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        self.last_prompt = user_prompt
        marker = "TARGET CODE BLOCKS TO PATCH:"
        block_text = user_prompt.split(marker, 1)[1].split("Related Tree-sitter/LSP Impact Context:", 1)[0]
        block = json.loads(block_text)[0]
        replacement = block["source"].replace("oldpkg::do_thing()", "newpkg::do_thing()")
        return LLMResponse(
            content=json.dumps([{
                "start_line": block["start_line"],
                "end_line": block["end_line"],
                "replacement": replacement,
            }]),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class InvalidRangeRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        return LLMResponse(
            content=json.dumps({
                "replacements": [{
                    "start_line": 2,
                    "end_line": 2,
                    "replacement": "    newpkg::do_thing();",
                }]
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class UnexpectedRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        raise AssertionError("LLM should not be called")


class RetryThenValidRouter:
    def __init__(self):
        self.calls = 0
        self.last_prompt = ""

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        self.calls += 1
        self.last_prompt = user_prompt
        if self.calls == 1:
            return LLMResponse(
                content="I changed it:\n```json\n{\"replacements\":[{\"start_line\":2,\"end_line\":2,\"replacement\":\"    newpkg::do_thing();\"}]}\n```",
                provider="fake",
                model="test",
                latency_ms=1,
                fallback_used=False,
            )

        marker = "TARGET CODE BLOCKS TO PATCH:"
        block_text = user_prompt.split(marker, 1)[1].split("Related Tree-sitter/LSP Impact Context:", 1)[0]
        block = json.loads(block_text)[0]
        return LLMResponse(
            content=json.dumps({
                "schema_version": "depguard.patch.v1",
                "status": "patched",
                "replacements": [{
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "replacement": block["source"].replace("oldpkg::do_thing()", "newpkg::do_thing()"),
                }],
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class MalformedReplacementRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        return LLMResponse(
            content=json.dumps({
                "schema_version": "depguard.patch.v1",
                "status": "patched",
                "replacements": [{"start_line": "2", "end_line": 2, "replacement": "x"}],
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


def test_patch_agent_changes_rust_code_and_updates_cargo_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src = tmp_path / "src"
    src.mkdir()
    cargo = tmp_path / "Cargo.toml"
    rust_file = src / "auth.rs"

    cargo.write_text(
        "\n".join([
            "[package]",
            'name = "demo"',
            'version = "0.1.0"',
            "",
            "[dependencies]",
            'rspotify = "0.11.0"',
            "",
        ]),
        encoding="utf-8",
    )
    original = "\n".join([
        "use rspotify::AuthCodePkceSpotify;",
        "",
        "pub fn create_auth_client(creds: Credentials, oauth: OAuth, config: Config) -> AuthCodePkceSpotify {",
        "    AuthCodePkceSpotify::with_config(creds, oauth, config)",
        "}",
        "",
    ])
    patched = original.replace(
        "AuthCodePkceSpotify::with_config(creds, oauth, config)",
        "AuthCodePkceSpotify::new(creds, oauth, config)",
    )
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "AuthCodePkceSpotify.with_config",
        "new_api": "AuthCodePkceSpotify.new",
        "description": "rspotify 0.12 renamed the PKCE constructor.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    assert str(rust_file) in scan_output["matches_by_file"]
    assert scan_output["matches_by_file"][str(rust_file)][0]["line"] == 4

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: FakeRouter(patched))
    monkeypatch.setattr(PatchAgent, "_create_checkpoint", lambda self, package: ("test_checkpoint", False))
    monkeypatch.setattr(PatchAgent, "_rollback", lambda self, commit_made: None)
    monkeypatch.setattr(PatchAgent, "_get_impact_context", lambda self, filepath, matches: "related impact context")

    agent = PatchAgent(project_root=str(tmp_path))
    report = agent.run_sync(
        {
            "package": "rspotify",
            "from_version": "0.11.0",
            "to_version": "0.12.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
        str(cargo),
    )

    assert report["overall_status"] == "success"
    assert report["llm_provider"] == "fake"
    assert report["dependency_file_updated"] == "Cargo.toml"
    assert rust_file.read_text(encoding="utf-8") == patched
    assert 'rspotify = "0.12.0"' in cargo.read_text(encoding="utf-8")


def test_patch_preview_returns_rust_patch_without_writing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    patched = "fn main() {\n    newpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: FakeRouter(patched))
    monkeypatch.setattr(PatchAgent, "_get_impact_context", lambda self, filepath, matches: "related impact context")

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "success"
    assert report["files"][0]["original"] == original
    assert report["files"][0]["patched"] == patched
    assert rust_file.read_text(encoding="utf-8") == original


def test_patch_preview_sends_code_slice_not_full_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "\n".join([
        "fn main() {",
        "    oldpkg::do_thing();",
        "}",
        "",
        *[f"// filler {index}" for index in range(50)],
        "fn unrelated() {",
        "    println!(\"do not send this whole function\");",
        "}",
        "",
    ])
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)
    router = JsonReplacementRouter()

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: router)
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "success"
    assert "newpkg::do_thing()" in report["files"][0]["patched"]
    assert "fn unrelated" in report["files"][0]["patched"]
    assert "TARGET CODE BLOCKS TO PATCH" in router.last_prompt
    assert "fn unrelated" not in router.last_prompt


def test_patch_preview_rejects_text_only_llm_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: TextOnlyRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "failed"
    assert "did not contain JSON replacements" in report["files"][0]["error"]
    assert report["files"][0]["patched"] == original


def test_patch_preview_allows_migration_review_empty_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "migration_review",
        "old_api": "oldpkg.do_thing",
        "new_api": "",
        "description": "Review the usage and change only if needed.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: NoChangeRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "1.0.1",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "success"
    assert report["files"][0]["patched"] == original
    assert rust_file.read_text(encoding="utf-8") == original


def test_patch_preview_accepts_top_level_replacement_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: TopLevelArrayRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "success"
    assert "newpkg::do_thing()" in report["files"][0]["patched"]


def test_patch_preview_rejects_replacement_ranges_inside_target_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: InvalidRangeRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "failed"
    assert "outside the target blocks" in report["files"][0]["error"]
    assert report["files"][0]["patched"] == original


def test_patch_preview_retries_invalid_llm_response_with_contract_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)
    router = RetryThenValidRouter()

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: router)
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "success"
    assert "newpkg::do_thing()" in report["files"][0]["patched"]
    assert router.calls == 2
    assert "Previous LLM response was rejected" in router.last_prompt


def test_patch_preview_rejects_malformed_replacement_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rust_file = tmp_path / "main.rs"
    original = "fn main() {\n    oldpkg::do_thing();\n}\n"
    rust_file.write_text(original, encoding="utf-8")

    breaking_changes = [{
        "type": "renamed",
        "old_api": "oldpkg.do_thing",
        "new_api": "newpkg.do_thing",
        "description": "The function moved to newpkg.",
    }]
    scan_output = ASTScanner().scan(str(tmp_path), breaking_changes)

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: MalformedReplacementRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "oldpkg",
            "from_version": "1.0.0",
            "to_version": "2.0.0",
            "breaking_changes": breaking_changes,
        },
        scan_output,
    )

    assert report["files"][0]["status"] == "failed"
    assert "start_line/end_line must be integers" in report["files"][0]["error"]


def test_patch_prompt_slices_large_module_level_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    py_file = tmp_path / "sweep.py"
    lines = [
        "import optuna",
        "",
        *[f"VALUE_{index} = {index}" for index in range(120)],
        "study = optuna.create_study(direction='maximize')",
        "study.optimize(lambda trial: 1.0)",
        "",
    ]
    py_file.write_text("\n".join(lines), encoding="utf-8")

    breaking_changes = [{
        "type": "migration_review",
        "old_api": "optuna.create_study",
        "new_api": "",
        "description": "Review create_study usage.",
    }]
    matches = [{
        "file": str(py_file),
        "line": 123,
        "old_api": "optuna.create_study",
        "new_api": "",
        "type": "migration_review",
        "description": "Review create_study usage.",
    }]

    agent = PatchAgent(project_root=str(tmp_path))
    agent.module_level_max_lines = 30

    location = SimpleNamespace(
        source=py_file.read_text(encoding="utf-8"),
        start_line=1,
        end_line=len(lines),
        context_type="module_level",
        name=None,
        parent=None,
    )
    node = SimpleNamespace(location=location)
    monkeypatch.setattr(agent, "_get_impact_result", lambda filepath, scan_matches: None)
    monkeypatch.setattr(agent, "_impact_finder", SimpleNamespace(get_node_at_line=lambda filepath, line: node))

    _system, prompt, blocks = agent._build_sliced_patch_prompt(
        str(py_file),
        matches,
        {
            "package": "optuna",
            "from_version": "3.6.1",
            "to_version": "4.9.0",
            "breaking_changes": breaking_changes,
        },
        py_file.read_text(encoding="utf-8"),
    )

    assert blocks[0]["context_type"] == "module_level_slice"
    assert blocks[0]["end_line"] - blocks[0]["start_line"] + 1 <= 37
    assert "Valid replacement ranges are" in prompt
    assert "VALUE_1 = 1" not in blocks[0]["source"]


def test_patch_prompt_merges_overlapping_target_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    py_file = tmp_path / "sweep.py"
    lines = [f"line_{index} = {index}" for index in range(1, 80)]
    py_file.write_text("\n".join(lines), encoding="utf-8")

    agent = PatchAgent(project_root=str(tmp_path))
    blocks = agent._merge_overlapping_target_blocks(
        [
            {
                "file": str(py_file),
                "start_line": 20,
                "end_line": 40,
                "context_type": "module_level_slice",
                "name": None,
                "parent": None,
                "source": "\n".join(lines[19:40]),
            },
            {
                "file": str(py_file),
                "start_line": 32,
                "end_line": 50,
                "context_type": "function",
                "name": "objective",
                "parent": None,
                "source": "\n".join(lines[31:50]),
            },
        ],
        str(py_file),
        lines,
    )

    assert len(blocks) == 1
    assert blocks[0]["start_line"] == 20
    assert blocks[0]["end_line"] == 50
    assert blocks[0]["context_type"] == "module_level_slice"


def test_patch_agent_expands_related_impact_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    caller_file = tmp_path / "caller.rs"
    caller_file.write_text("fn caller() {\n    changed();\n}\n", encoding="utf-8")

    agent = PatchAgent(project_root=str(tmp_path))
    location = SimpleNamespace(
        file="caller.rs",
        start_line=1,
        end_line=3,
        context_type="function",
        source=caller_file.read_text(encoding="utf-8"),
    )
    impacted_node = SimpleNamespace(id="caller.rs::caller", location=location)
    impacted = SimpleNamespace(
        node=impacted_node,
        impact_reason="calls changed function",
        affected_attributes=[],
    )
    impact = SimpleNamespace(impacted_nodes=[impacted])
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: impact)

    expanded = agent._expand_matches_with_impacted_nodes({
        str(tmp_path / "changed.rs"): [{
            "file": str(tmp_path / "changed.rs"),
            "line": 1,
            "old_api": "changed",
            "type": "renamed",
        }]
    })

    assert str(caller_file) in expanded
    assert expanded[str(caller_file)][0]["type"] == "impact_review"


def test_patch_agent_expands_migration_review_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    caller_file = tmp_path / "caller.py"
    caller_file.write_text("def caller():\n    pass\n", encoding="utf-8")

    agent = PatchAgent(project_root=str(tmp_path))
    location = SimpleNamespace(
        file="caller.py",
        start_line=1,
        end_line=2,
        context_type="function",
        source=caller_file.read_text(encoding="utf-8"),
    )
    impacted_node = SimpleNamespace(id="caller.py::caller", location=location)
    impacted = SimpleNamespace(
        node=impacted_node,
        impact_reason="module level dependency",
        affected_attributes=[],
    )
    impact = SimpleNamespace(impacted_nodes=[impacted])
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: impact)

    original_file = str(tmp_path / "sweep.py")
    matches_by_file = {
        original_file: [{
            "file": original_file,
            "line": 10,
            "old_api": "optuna.create_study",
            "new_api": "",
            "type": "migration_review",
        }]
    }
    scout_context = {
        "package": "optuna",
        "from_version": "3.6.1",
        "to_version": "4.9.0",
        "breaking_changes": [{
            "type": "migration_review",
            "old_api": "optuna.create_study",
            "new_api": "",
        }],
    }

    expanded = agent._expand_matches_with_impacted_nodes(matches_by_file, scout_context)

    assert original_file in expanded
    assert str(caller_file) in expanded
    assert expanded[str(caller_file)][0]["type"] == "impact_review"


def test_patch_preview_allows_migration_review_no_change_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    py_file = tmp_path / "sweep.py"
    original = "import optuna\nraise optuna.exceptions.TrialPruned()\n"
    py_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: NoChangeRouter())
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).preview_sync(
        {
            "package": "optuna",
            "from_version": "3.6.1",
            "to_version": "4.9.0",
            "breaking_changes": [{
                "type": "migration_review",
                "old_api": "optuna.exceptions.TrialPruned",
                "new_api": "",
            }],
        },
        {
            "matches_by_file": {
                str(py_file): [{
                    "file": str(py_file),
                    "line": 2,
                    "old_api": "optuna.exceptions.TrialPruned",
                    "new_api": "",
                    "type": "migration_review",
                }]
            }
        },
    )

    assert report["files"][0]["status"] == "success"
    assert report["files"][0]["patched"] == original
    assert py_file.read_text(encoding="utf-8") == original


def test_patch_run_updates_dependency_and_reviews_code_for_migration_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("optuna==3.6.1\n", encoding="utf-8")
    py_file = tmp_path / "sweep.py"
    py_file.write_text("import optuna\nraise optuna.exceptions.TrialPruned()\n", encoding="utf-8")

    monkeypatch.setattr(patch_module, "LLMRouter", lambda: NoChangeRouter())
    monkeypatch.setattr(PatchAgent, "_create_checkpoint", lambda self, package: ("test_checkpoint", False))
    monkeypatch.setattr(PatchAgent, "_get_impact_result", lambda self, filepath, matches: None)

    report = PatchAgent(project_root=str(tmp_path)).run_sync(
        {
            "package": "optuna",
            "from_version": "3.6.1",
            "to_version": "4.9.0",
            "breaking_changes": [{
                "type": "migration_review",
                "old_api": "optuna.exceptions.TrialPruned",
                "new_api": "",
            }],
        },
        {
            "matches_by_file": {
                str(py_file): [{
                    "file": str(py_file),
                    "line": 2,
                    "old_api": "optuna.exceptions.TrialPruned",
                    "new_api": "",
                    "type": "migration_review",
                }]
            }
        },
        str(req_file),
    )

    assert report["overall_status"] == "success"
    assert report["files_patched"][0]["status"] == "success"
    assert report["dependency_file_updated"] == "requirements.txt"
    assert "optuna==4.9.0" in req_file.read_text(encoding="utf-8")
