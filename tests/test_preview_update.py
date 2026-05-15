from pathlib import Path

import pytest

from api import main as api_main
from api.main import PackageInfo, UpdateRequest


class EmptyScout:
    def run_sync(self, package_info: dict, api_usages: list[str]):
        return {
            "package": package_info["name"],
            "from_version": package_info["current_version"],
            "to_version": package_info["latest_version"],
            "breaking_changes": [],
            "confidence_score": 0.0,
            "llm_provider": "fake",
        }


class FallbackPatchAgent:
    def __init__(self, project_root: str | None = None):
        self.project_root = project_root

    def preview_sync(self, scout_output: dict, ast_output: dict):
        assert scout_output["migration_review_fallback"] is True
        assert ast_output["matches_by_file"]

        files = []
        for file_path in ast_output["matches_by_file"]:
            original = Path(file_path).read_text(encoding="utf-8")
            patched = original.replace("request_token(&mut oauth);", "oauth.prompt_for_token(&url)?;")
            files.append({
                "file": file_path,
                "status": "success",
                "error": "",
                "original": original,
                "patched": patched,
            })
        return {"files": files, "llm_provider": "fake", "fallback_used": False}


def test_preview_uses_api_usage_fallback_when_scout_finds_no_breaking_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    src = tmp_path / "src"
    src.mkdir()
    cargo = tmp_path / "Cargo.toml"
    rust_file = src / "main.rs"

    cargo.write_text(
        "\n".join([
            "[package]",
            'name = "spotify-demo"',
            'version = "0.1.0"',
            "",
            "[dependencies]",
            'rspotify = "0.10.0"',
            "",
        ]),
        encoding="utf-8",
    )
    rust_file.write_text(
        "\n".join([
            "use rspotify::{",
            "    oauth2::SpotifyOAuth,",
            "    util::request_token,",
            "};",
            "",
            "fn main() {",
            "    let mut oauth = SpotifyOAuth::default();",
            "    request_token(&mut oauth);",
            "}",
            "",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(api_main, "ScoutAgent", lambda: EmptyScout())
    monkeypatch.setattr(api_main, "PatchAgent", FallbackPatchAgent)

    response = api_main.preview_update(
        UpdateRequest(
            folder_path=str(tmp_path),
            package_info=PackageInfo(
                name="rspotify",
                current_version="0.10.0",
                latest_version="0.16.1",
                ecosystem="cargo",
                file_path=str(cargo),
            ),
        )
    )

    changed_paths = {file["relative_path"] for file in response["files"]}

    assert "Cargo.toml" in changed_paths
    assert "src/main.rs" in changed_paths
    assert response["summary"]["total_files_changed"] == 2
    assert rust_file.read_text(encoding="utf-8").count("request_token") == 2
