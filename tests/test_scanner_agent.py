from pathlib import Path

from agents.scanner import ScannerAgent


def test_scan_prunes_build_and_hidden_directories(tmp_path: Path):
    (tmp_path / "target" / "debug").mkdir(parents=True)
    (tmp_path / ".git").mkdir()

    (tmp_path / "Cargo.toml").write_text(
        "\n".join([
            "[package]",
            'name = "demo"',
            'version = "0.1.0"',
            "",
            "[dependencies]",
            'anyhow = "1.0.80"',
            "",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "target" / "debug" / "Cargo.toml").write_text(
        "\n".join([
            "[dependencies]",
            'should-not-scan = "9.9.9"',
            "",
        ]),
        encoding="utf-8",
    )
    (tmp_path / ".git" / "package.json").write_text(
        '{"dependencies": {"also-skipped": "1.0.0"}}',
        encoding="utf-8",
    )

    results = ScannerAgent(str(tmp_path)).scan()

    assert len(results) == 1
    assert results[0]["ecosystem"] == "cargo"
    assert results[0]["packages"] == [
        {"name": "anyhow", "version": "1.0.80", "pinned": True}
    ]
