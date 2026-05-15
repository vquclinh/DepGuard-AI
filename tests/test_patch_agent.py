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
        return LLMResponse(
            content=f"```rust\n{self.patched_content}\n```",
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
