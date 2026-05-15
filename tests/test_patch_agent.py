from pathlib import Path

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
