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


def test_find_api_usages_tracks_python_constructor_method_dataflow(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import pandas as pd",
            "",
            "def dummy_pandas_trap():",
            "    df1 = pd.DataFrame({'A': [1, 2]})",
            "    df2 = pd.DataFrame({'A': [3, 4]})",
            "    result = df1.append(df2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    usages = ASTScanner().find_api_usages(str(tmp_path), "pandas")

    assert "pandas.DataFrame" in usages
    assert "pandas.DataFrame.append" in usages


def test_find_api_usage_contexts_for_scout_retrieval(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import pandas as pd",
            "",
            "def dummy_pandas_trap():",
            "    df1 = pd.DataFrame({'A': [1, 2]})",
            "    df2 = pd.DataFrame({'A': [3, 4]})",
            "    result = df1.append(df2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    contexts = ASTScanner().find_api_usage_contexts(str(tmp_path), "pandas")

    assert any(context["api"] == "pandas.DataFrame.append" for context in contexts)
    assert any("df1.append" in context["context"] for context in contexts)


def test_scan_matches_python_constructor_method_dataflow_target(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import pandas as pd",
            "",
            "def dummy_pandas_trap():",
            "    df1 = pd.DataFrame({'A': [1, 2]})",
            "    df2 = pd.DataFrame({'A': [3, 4]})",
            "    result = df1.append(df2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    result = ASTScanner().scan(
        str(tmp_path),
        [{"old_api": "pandas.DataFrame.append", "new_api": "pandas.concat"}],
    )
    matches = next(iter(result["matches_by_file"].values()))

    assert len(matches) == 1
    assert matches[0]["line"] == 6
    assert matches[0]["old_api"] == "pandas.DataFrame.append"
    assert matches[0]["matched_text"] == "df1.append("


def test_scan_does_not_match_native_list_append_as_pandas_append(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import pandas as pd",
            "",
            "def sanitize_features(features):",
            "    clean = []",
            "    for f in features:",
            "        clean.append(f)",
            "    return clean",
            "",
            "def dummy_pandas_trap():",
            "    df1 = pd.DataFrame({'A': [1, 2]})",
            "    df2 = pd.DataFrame({'A': [3, 4]})",
            "    result = df1.append(df2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    result = ASTScanner().scan(
        str(tmp_path),
        [{"old_api": "pandas.DataFrame.append", "new_api": "pandas.concat"}],
    )
    matches = [
        match
        for file_matches in result["matches_by_file"].values()
        for match in file_matches
    ]

    assert len(matches) == 1
    assert matches[0]["line"] == 12
    assert matches[0]["code_snippet"].strip().startswith("result = df1.append")


def test_find_api_usages_uses_aliases_declared_in_code(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import examplepkg as ex",
            "",
            "def dummy_usage():",
            "    item1 = ex.Widget({'A': [1, 2]})",
            "    item2 = ex.Widget({'A': [3, 4]})",
            "    result = item1.merge(item2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    usages = ASTScanner().find_api_usages(str(tmp_path), "examplepkg")

    assert "examplepkg.Widget" in usages
    assert "examplepkg.Widget.merge" in usages


def test_find_api_usages_uses_distribution_import_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "from importroot import Image",
            "",
            "def resize(path):",
            "    img = Image.open(path)",
            "    return img.resize((100, 100), Image.OLD_CONSTANT)",
            "",
        ]),
        encoding="utf-8",
    )

    scanner = ASTScanner()
    monkeypatch.setattr(scanner, "_distribution_import_roots", lambda package_name: {"importroot"})

    usages = scanner.find_api_usages(str(tmp_path), "example-dist")

    assert "importroot.Image" in usages
    assert "importroot.Image.open" in usages
    assert "importroot.Image.OLD_CONSTANT" in usages


def test_find_api_usages_can_match_clear_import_root_prefix(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "from sam import Tool",
            "",
            "result = Tool.OLD_CONSTANT",
            "",
        ]),
        encoding="utf-8",
    )

    usages = ASTScanner().find_api_usages(str(tmp_path), "sampledist")

    assert "sam.Tool.OLD_CONSTANT" in usages


def test_scan_matches_imported_constant_attribute_target(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "from importroot import Image",
            "",
            "def resize(img):",
            "    return img.resize((100, 100), Image.OLD_CONSTANT)",
            "",
        ]),
        encoding="utf-8",
    )

    result = ASTScanner().scan(
        str(tmp_path),
        [{"old_api": "importroot.Image.OLD_CONSTANT", "new_api": "importroot.Image.New.OLD_CONSTANT"}],
    )
    matches = [
        match
        for file_matches in result["matches_by_file"].values()
        for match in file_matches
    ]

    assert len(matches) == 1
    assert matches[0]["old_api"] == "importroot.Image.OLD_CONSTANT"
    assert matches[0]["matched_text"] == "Image.OLD_CONSTANT"


def test_find_api_usages_tracks_subclass_classmethod_and_instance_methods(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "from examplepkg import BaseThing",
            "from typing import Any",
            "",
            "class Payload(BaseThing):",
            "    user_id: int",
            "",
            "def process(raw_data: dict[str, Any]) -> dict:",
            "    item = Payload.load(raw_data)",
            "    return item.export(exclude={'secret'})",
            "",
        ]),
        encoding="utf-8",
    )

    usages = ASTScanner().find_api_usages(str(tmp_path), "examplepkg")

    assert "examplepkg.BaseThing" in usages
    assert "examplepkg.BaseThing.load" in usages
    assert "examplepkg.BaseThing.export" in usages


def test_scan_matches_subclass_inherited_method_targets(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "from examplepkg import BaseThing",
            "",
            "class Payload(BaseThing):",
            "    user_id: int",
            "",
            "def process(raw_data):",
            "    item = Payload.load(raw_data)",
            "    return item.export(exclude={'secret'})",
            "",
        ]),
        encoding="utf-8",
    )

    result = ASTScanner().scan(
        str(tmp_path),
        [
            {"old_api": "examplepkg.BaseThing.load", "new_api": "examplepkg.BaseThing.validate"},
            {"old_api": "examplepkg.BaseThing.export", "new_api": "examplepkg.BaseThing.dump"},
        ],
    )
    matches = [
        match
        for file_matches in result["matches_by_file"].values()
        for match in file_matches
    ]

    assert {match["old_api"] for match in matches} == {
        "examplepkg.BaseThing.load",
        "examplepkg.BaseThing.export",
    }
    assert {match["matched_text"] for match in matches} == {
        "Payload.load(",
        "item.export(",
    }


def test_find_api_usages_handles_unicode_comments_before_method_calls(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "\n".join([
            "import pandas as pd",
            "",
            "def dummy_pandas_trap():",
            "    df1 = pd.DataFrame({'A': [1, 2]})",
            "    df2 = pd.DataFrame({'A': [3, 4]})",
            "    # BẪY Ở ĐÂY: Dùng hàm append (đã bị xóa trong Pandas 2.0)",
            "    result = df1.append(df2, ignore_index=True)",
            "    return result",
            "",
        ]),
        encoding="utf-8",
    )

    usages = ASTScanner().find_api_usages(str(tmp_path), "pandas")

    assert "pandas.DataFrame.append" in usages


def test_find_api_usages_expands_nested_rust_use_trees(tmp_path: Path):
    (tmp_path / "main.rs").write_text(
        "\n".join([
            "use rspotify::{",
            "  client::Spotify,",
            "  oauth2::{SpotifyOAuth, TokenInfo},",
            "  util::{process_token, request_token},",
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

    usages = ASTScanner().find_api_usages(str(tmp_path), "rspotify")

    assert "rspotify.client.Spotify" in usages
    assert "rspotify.oauth2.SpotifyOAuth" in usages
    assert "rspotify.oauth2.TokenInfo" in usages
    assert "rspotify.util.process_token" in usages
    assert "rspotify.util.request_token" in usages


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
