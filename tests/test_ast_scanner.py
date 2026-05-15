from pathlib import Path

import pytest

from tools.ast_scanner import ASTScanner


def test_scan_supports_popular_non_python_languages(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "cmd").mkdir()

    (tmp_path / "src" / "main.rs").write_text(
        "fn main() {\n    legacy::crate::old_call();\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "App.tsx").write_text(
        'import legacy from "legacy-lib";\nlegacy.oldThing();\n',
        encoding="utf-8",
    )
    (tmp_path / "cmd" / "main.go").write_text(
        'package main\n\nimport oldpkg "example.com/old/pkg"\n\nfunc main() {\n    oldpkg.DoThing()\n}\n',
        encoding="utf-8",
    )

    scanner = ASTScanner()
    result = scanner.scan(
        str(tmp_path),
        [
            {"old_api": "legacy.crate.old_call", "new_api": "modern.crate.new_call"},
            {"old_api": "legacy-lib.oldThing", "new_api": "legacy-lib.newThing"},
            {"old_api": "example.com/old/pkg.DoThing", "new_api": "example.com/old/pkg.DoThingNew"},
        ],
    )

    matches = [
        match
        for file_matches in result["matches_by_file"].values()
        for match in file_matches
    ]

    assert result["total_files_scanned"] == 3
    assert {match["old_api"] for match in matches} == {
        "legacy.crate.old_call",
        "legacy-lib.oldThing",
        "example.com/old/pkg.DoThing",
    }


def test_find_api_usages_resolves_aliases_across_languages(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import numpy as np\nmask = np.bool_(condition)\n",
        encoding="utf-8",
    )
    (tmp_path / "ui.tsx").write_text(
        'import React from "react";\nReact.createElement("div");\n',
        encoding="utf-8",
    )

    scanner = ASTScanner()

    assert "numpy.bool_" in scanner.find_api_usages(str(tmp_path), "numpy")
    assert "react.createElement" in scanner.find_api_usages(str(tmp_path), "react")


def test_tree_sitter_ignores_python_comments_and_strings(tmp_path: Path):
    scanner = ASTScanner()
    if "python" not in scanner.parsers:
        pytest.skip("python tree-sitter grammar is not installed")

    (tmp_path / "app.py").write_text(
        "\n".join([
            "import numpy as np",
            "# np.bool should not count here",
            'message = "np.bool should not count here either"',
            "mask = np.bool(condition)",
            "",
        ]),
        encoding="utf-8",
    )

    result = scanner.scan(
        str(tmp_path),
        [{"old_api": "numpy.bool", "new_api": "numpy.bool_"}],
    )
    matches = next(iter(result["matches_by_file"].values()))

    assert len(matches) == 1
    assert matches[0]["line"] == 4


def test_validate_source_uses_tree_sitter_when_available():
    scanner = ASTScanner()
    if "python" not in scanner.parsers:
        pytest.skip("python tree-sitter grammar is not installed")

    assert scanner.validate_source("example.py", "value = 1\n") is None
    assert scanner.validate_source("example.py", "def broken(:\n") is not None
