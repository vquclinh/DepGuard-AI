from pathlib import Path

from tools.impact_graph import ImpactFinder, TreeSitterParser


def write_project(root: Path) -> None:
    (root / "config.py").write_text(
        "\n".join([
            "DEFAULT_TIMEOUT = 30",
            'BASE_URL = "http://api"',
            "",
        ]),
        encoding="utf-8",
    )
    (root / "fetcher.py").write_text(
        "\n".join([
            "import requests",
            "from config import DEFAULT_TIMEOUT, BASE_URL",
            "",
            "def fetch_data(url):",
            "    resp = requests.get(url, timeout=DEFAULT_TIMEOUT)",
            "    return resp",
            "",
            "def fetch_json(url):",
            "    resp = fetch_data(url)",
            "    return resp.json()",
            "",
        ]),
        encoding="utf-8",
    )
    (root / "processor.py").write_text(
        "\n".join([
            "from fetcher import fetch_data",
            'result = fetch_data("http://x")',
            "print(result.status_code)",
            "",
        ]),
        encoding="utf-8",
    )
    (root / "handler.py").write_text(
        "\n".join([
            "from fetcher import fetch_data, fetch_json",
            "from config import BASE_URL",
            "",
            '@app.route("/")',
            "def handle():",
            "    data = fetch_data(BASE_URL)",
            "    text = data.text",
            "    code = data.status_code",
            "    more = fetch_json(BASE_URL)",
            "    return text",
            "",
        ]),
        encoding="utf-8",
    )
    (root / "chain.py").write_text(
        "\n".join([
            "from fetcher import fetch_json",
            "",
            "def run():",
            '    return fetch_json("http://x")',
            "",
        ]),
        encoding="utf-8",
    )


def impact_for(tmp_path: Path, file_name: str, lines: list[int], max_depth: int = 3):
    write_project(tmp_path)
    finder = ImpactFinder(str(tmp_path))
    return finder.find_impact(str(tmp_path / file_name), lines, max_depth=max_depth)


def impacted_by_id(result):
    return {item.node.id: item for item in result.impacted_nodes}


def test_change_fetch_data_finds_callers_module_level_and_return_usage(tmp_path):
    result = impact_for(tmp_path, "fetcher.py", [5], max_depth=2)
    impacted = impacted_by_id(result)

    assert "fetcher.py::fetch_json" in impacted
    assert "processor.py::module_level::1-3" in impacted
    assert "handler.py::handle" in impacted

    assert impacted["fetcher.py::fetch_json"].impact_reason == "uses return value"
    assert impacted["fetcher.py::fetch_json"].affected_attributes == [".json()"]
    assert impacted["processor.py::module_level::1-3"].affected_attributes == [".status_code"]
    assert impacted["handler.py::handle"].affected_attributes == [".status_code", ".text"]


def test_change_module_level_symbol_finds_symbol_users(tmp_path):
    result = impact_for(tmp_path, "config.py", [1], max_depth=2)
    impacted = impacted_by_id(result)

    assert "fetcher.py::fetch_data" in impacted
    assert impacted["fetcher.py::fetch_data"].impact_reason == "uses defined symbol"
    assert "DEFAULT_TIMEOUT" in result.changed_nodes[0].defines_symbols


def test_change_fetch_json_finds_callers_but_not_callees(tmp_path):
    result = impact_for(tmp_path, "fetcher.py", [9], max_depth=2)
    impacted = impacted_by_id(result)

    assert "handler.py::handle" in impacted
    assert "chain.py::run" in impacted
    assert "fetcher.py::fetch_data" not in impacted


def test_change_module_level_code_reports_changed_node_only(tmp_path):
    result = impact_for(tmp_path, "processor.py", [2], max_depth=2)

    assert [node.id for node in result.changed_nodes] == ["processor.py::module_level::1-3"]
    assert result.impacted_nodes == []


def test_depth_limit_excludes_deeper_callers(tmp_path):
    result = impact_for(tmp_path, "fetcher.py", [5], max_depth=1)
    impacted = impacted_by_id(result)

    assert "fetcher.py::fetch_json" in impacted
    assert "chain.py::run" not in impacted


def test_to_llm_context_is_readable(tmp_path):
    result = impact_for(tmp_path, "fetcher.py", [5], max_depth=1)
    context = result.to_llm_context()

    assert "=== CHANGED CODE ===" in context
    assert "=== DIRECTLY AFFECTED (depth 1) ===" in context
    assert "File: fetcher.py, function: fetch_data, lines 4-6" in context
    assert "Note: uses return value; uses return value attributes: [.json()]" in context


def test_parser_tracks_decorators_defaults_reassignment_and_class_fields(tmp_path):
    source = "\n".join([
        "DEFAULT_CONFIG = {}",
        "",
        "class Loader:",
        "    def __init__(self):",
        "        self.value = 1",
        "",
        "@app.route('/x')",
        "def view(config=DEFAULT_CONFIG):",
        "    first = fetch()",
        "    second = first",
        "    loader = Loader()",
        "    return second.text, loader.value",
        "",
    ])
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    parser = TreeSitterParser(str(tmp_path))
    nodes = {node.id: node for node in parser.parse_file(str(path))}

    view = nodes["sample.py::view"]
    init = nodes["sample.py::Loader.__init__"]

    assert "app.route" in view.references_symbols
    assert "DEFAULT_CONFIG" in view.references_symbols
    assert view.call_return_usage["fetch"] == [".text"]
    assert "Loader.value" in view.references_symbols
    assert "Loader.value" in init.defines_symbols
