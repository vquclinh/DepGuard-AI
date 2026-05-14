import ast
import json
import logging
import os
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CodeLocation:
    file: str
    start_line: int
    end_line: int
    source: str
    context_type: str
    name: str | None
    parent: str | None


@dataclass
class GraphNode:
    id: str
    location: CodeLocation
    calls: list[str] = field(default_factory=list)
    references_symbols: list[str] = field(default_factory=list)
    call_return_usage: dict[str, list[str]] = field(default_factory=dict)
    defines_symbols: list[str] = field(default_factory=list)


@dataclass
class ImpactGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    calls: dict[str, list[str]] = field(default_factory=dict)
    called_by: dict[str, list[str]] = field(default_factory=dict)
    symbol_defined_by: dict[str, str] = field(default_factory=dict)
    symbol_used_by: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ImpactedNode:
    node: GraphNode
    depth: int
    impact_reason: str
    affected_attributes: list[str] = field(default_factory=list)


@dataclass
class ImpactResult:
    changed_nodes: list[GraphNode]
    impacted_nodes: list[ImpactedNode]
    summary: str

    def to_llm_context(self) -> str:
        parts = ["=== CHANGED CODE ==="]
        for node in self.changed_nodes:
            parts.extend(_format_node_block(node))

        by_depth: dict[int, list[ImpactedNode]] = defaultdict(list)
        module_level: list[ImpactedNode] = []
        for impacted in self.impacted_nodes:
            if impacted.node.location.context_type == "module_level":
                module_level.append(impacted)
            else:
                by_depth[impacted.depth].append(impacted)

        for depth in sorted(by_depth):
            label = "DIRECTLY AFFECTED" if depth == 1 else "INDIRECTLY AFFECTED"
            parts.append(f"=== {label} (depth {depth}) ===")
            for impacted in sorted(by_depth[depth], key=lambda item: item.node.id):
                parts.extend(_format_node_block(impacted.node, impacted))

        if module_level:
            parts.append("=== MODULE-LEVEL CODE AFFECTED ===")
            for impacted in sorted(module_level, key=lambda item: item.node.id):
                parts.extend(_format_node_block(impacted.node, impacted))

        if len(parts) == 1:
            parts.append("No changed code nodes were found.")

        return "\n".join(parts).rstrip()


class TreeSitterParser:
    """Parse project files into graph nodes.

    Python files use the standard ast module as a reliable fallback and primary
    extractor for semantic details. When tree-sitter packages are installed, the
    parser objects are initialized so JavaScript/TypeScript files can be parsed
    and unsupported languages can be skipped cleanly.
    """

    def __init__(self, project_root: str | None = None):
        self.project_root = Path(project_root).resolve() if project_root else None
        self.parsers: dict[str, Any] = {}
        self._init_tree_sitter()

    def _init_tree_sitter(self) -> None:
        try:
            from tree_sitter import Language, Parser
        except Exception:
            return

        for language_name, package_name in (
            ("python", "tree_sitter_python"),
            ("javascript", "tree_sitter_javascript"),
        ):
            try:
                module = __import__(package_name)
                raw_language = module.language()
                
                if isinstance(raw_language, Language):
                    language = raw_language
                else:
                    language = Language(raw_language)

                parser = Parser(language)
                
                self.parsers[language_name] = parser
                if language_name == "javascript":
                    self.parsers["typescript"] = parser
            except Exception as exc:
                logger.debug("Could not initialize tree-sitter %s parser: %s", language_name, exc)

    def parse_file(self, file_path: str) -> list[GraphNode]:
        language = self.detect_language(file_path)
        if not language:
            return []

        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.debug("Could not read %s: %s", file_path, exc)
            return []

        if language == "python":
            return self._parse_python_ast(path, source)

        if language in {"javascript", "typescript"} and "javascript" in self.parsers:
            return self._parse_javascript_tree_sitter(path, source, language)

        return []

    def get_function_source(self, file_path: str, start_line: int, end_line: int) -> str:
        try:
            lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[start_line - 1:end_line])

    def detect_language(self, file_path: str) -> str | None:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix in {".js", ".jsx"}:
            return "javascript"
        if suffix in {".ts", ".tsx"}:
            return "typescript"
        return None

    def _display_path(self, path: Path) -> str:
        resolved = path.resolve()
        if self.project_root:
            try:
                return resolved.relative_to(self.project_root).as_posix()
            except ValueError:
                pass
        return path.name

    def _parse_python_ast(self, path: Path, source: str) -> list[GraphNode]:
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            logger.debug("SyntaxError in %s, skipping impact parse: %s", path, exc)
            return []

        file_id = self._display_path(path)
        lines = source.splitlines()
        nodes: list[GraphNode] = []

        module_statements = [
            stmt for stmt in tree.body
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if module_statements:
            start_line = min(getattr(stmt, "lineno", 1) for stmt in module_statements)
            end_line = max(getattr(stmt, "end_lineno", getattr(stmt, "lineno", start_line)) for stmt in module_statements)
            analyzer = _PythonNodeAnalyzer(file_id=file_id, lines=lines)
            analyzer.visit_statements(module_statements)
            node_id = f"{file_id}::module_level::{start_line}-{end_line}"
            nodes.append(GraphNode(
                id=node_id,
                location=CodeLocation(
                    file=file_id,
                    start_line=start_line,
                    end_line=end_line,
                    source=_source_range(lines, start_line, end_line),
                    context_type="module_level",
                    name=None,
                    parent=None,
                ),
                calls=analyzer.calls,
                references_symbols=sorted(analyzer.references_symbols),
                call_return_usage=_sorted_usage(analyzer.call_return_usage),
                defines_symbols=sorted(analyzer.defines_symbols),
            ))

        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nodes.append(self._function_node(file_id, lines, stmt, parent=None))
            elif isinstance(stmt, ast.ClassDef):
                nodes.append(self._class_node(file_id, lines, stmt))
                for child in stmt.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nodes.append(self._function_node(file_id, lines, child, parent=stmt.name))

        return nodes

    def _function_node(
        self,
        file_id: str,
        lines: list[str],
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        parent: str | None,
    ) -> GraphNode:
        start_line = _decorated_start_line(node)
        end_line = getattr(node, "end_lineno", getattr(node, "lineno", start_line))
        analyzer = _PythonNodeAnalyzer(file_id=file_id, lines=lines, parent=parent)
        analyzer.defines_symbols.add(node.name)
        if parent:
            analyzer.defines_symbols.add(f"{parent}.{node.name}")
        analyzer.visit_decorators_and_arguments(node)
        analyzer.visit_statements(node.body)

        name = f"{parent}.{node.name}" if parent else node.name
        node_id = f"{file_id}::{name}"
        return GraphNode(
            id=node_id,
            location=CodeLocation(
                file=file_id,
                start_line=start_line,
                end_line=end_line,
                source=_source_range(lines, start_line, end_line),
                context_type="method" if parent else "function",
                name=node.name,
                parent=parent,
            ),
            calls=analyzer.calls,
            references_symbols=sorted(analyzer.references_symbols),
            call_return_usage=_sorted_usage(analyzer.call_return_usage),
            defines_symbols=sorted(analyzer.defines_symbols),
        )

    def _class_node(self, file_id: str, lines: list[str], node: ast.ClassDef) -> GraphNode:
        start_line = _decorated_start_line(node)
        end_line = getattr(node, "end_lineno", getattr(node, "lineno", start_line))
        analyzer = _PythonNodeAnalyzer(file_id=file_id, lines=lines, parent=node.name)
        analyzer.defines_symbols.add(node.name)
        for decorator in node.decorator_list:
            analyzer.visit(decorator)
        for base in node.bases:
            analyzer.visit(base)
        for keyword in node.keywords:
            analyzer.visit(keyword.value)
        class_statements = [
            stmt for stmt in node.body
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        analyzer.visit_statements(class_statements)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                analyzer.defines_symbols.add(f"{node.name}.{child.name}")

        return GraphNode(
            id=f"{file_id}::{node.name}",
            location=CodeLocation(
                file=file_id,
                start_line=start_line,
                end_line=end_line,
                source=_source_range(lines, start_line, end_line),
                context_type="class",
                name=node.name,
                parent=None,
            ),
            calls=analyzer.calls,
            references_symbols=sorted(analyzer.references_symbols),
            call_return_usage=_sorted_usage(analyzer.call_return_usage),
            defines_symbols=sorted(analyzer.defines_symbols),
        )

    def _parse_javascript_tree_sitter(self, path: Path, source: str, language: str) -> list[GraphNode]:
        parser = self.parsers.get("javascript")
        if not parser:
            return []

        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception as exc:
            logger.debug("Could not tree-sitter parse %s: %s", path, exc)
            return []

        file_id = self._display_path(path)
        lines = source.splitlines()
        nodes: list[GraphNode] = []

        def text(ts_node: Any) -> str:
            return source[ts_node.start_byte:ts_node.end_byte]

        def walk(ts_node: Any) -> None:
            if ts_node.type in {"function_declaration", "method_definition"}:
                name_node = ts_node.child_by_field_name("name")
                name = text(name_node) if name_node else None
                if name:
                    start = ts_node.start_point[0] + 1
                    end = ts_node.end_point[0] + 1
                    nodes.append(GraphNode(
                        id=f"{file_id}::{name}",
                        location=CodeLocation(
                            file=file_id,
                            start_line=start,
                            end_line=end,
                            source=_source_range(lines, start, end),
                            context_type="function",
                            name=name,
                            parent=None,
                        ),
                        calls=[],
                        references_symbols=[],
                        call_return_usage={},
                        defines_symbols=[name],
                    ))
            for child in ts_node.children:
                walk(child)

        walk(tree.root_node)
        return nodes


class ImpactGraphBuilder:
    IGNORE_DIRS = {
        "venv", ".venv", "node_modules", "__pycache__",
        ".git", "dist", "build", ".pytest_cache", ".depguard_cache",
    }

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.cache_dir = self.project_root / ".depguard_cache"
        self.cache_file = self.cache_dir / "impact_graph.json"
        self.parser = TreeSitterParser(str(self.project_root))

    def build(self, force_rebuild: bool = False) -> ImpactGraph:
        files = self._supported_files()
        cache = self._load_cache() if not force_rebuild else {"files": {}}
        cached_files = cache.get("files", {})
        next_cache: dict[str, Any] = {"files": {}}
        all_nodes: dict[str, GraphNode] = {}

        for path in files:
            rel_path = path.relative_to(self.project_root).as_posix()
            mtime = path.stat().st_mtime
            cached = cached_files.get(rel_path)
            if cached and cached.get("mtime") == mtime:
                nodes = [_graph_node_from_dict(item) for item in cached.get("nodes", [])]
            else:
                nodes = self.parser.parse_file(str(path))

            next_cache["files"][rel_path] = {
                "mtime": mtime,
                "nodes": [asdict(node) for node in nodes],
            }
            for node in nodes:
                all_nodes[node.id] = node

        graph = self._build_graph_indexes(all_nodes)
        self._save_cache(next_cache)
        return graph

    def _supported_files(self) -> list[Path]:
        supported: list[Path] = []
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [item for item in dirs if item not in self.IGNORE_DIRS]
            for filename in files:
                path = Path(root) / filename
                if self.parser.detect_language(str(path)):
                    supported.append(path)
        return sorted(supported)

    def _load_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"files": {}}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not save impact graph cache: %s", exc)

    def _build_graph_indexes(self, all_nodes: dict[str, GraphNode]) -> ImpactGraph:
        symbol_defined_by: dict[str, str] = {}
        symbol_used_by: dict[str, list[str]] = defaultdict(list)

        for node in all_nodes.values():
            for symbol in node.defines_symbols:
                if symbol not in symbol_defined_by or node.location.context_type == "module_level":
                    symbol_defined_by[symbol] = node.id
            for symbol in node.references_symbols:
                if node.id not in symbol_used_by[symbol]:
                    symbol_used_by[symbol].append(node.id)

        calls: dict[str, list[str]] = {}
        called_by: dict[str, list[str]] = defaultdict(list)
        for node in all_nodes.values():
            resolved_calls: list[str] = []
            for call_name in node.calls:
                resolved = self._resolve_call_to_node_id(call_name, node, all_nodes)
                if resolved and resolved not in resolved_calls:
                    resolved_calls.append(resolved)
                    if node.id not in called_by[resolved]:
                        called_by[resolved].append(node.id)
            node.calls = resolved_calls
            calls[node.id] = resolved_calls

        return ImpactGraph(
            nodes=all_nodes,
            calls=calls,
            called_by=dict(called_by),
            symbol_defined_by=symbol_defined_by,
            symbol_used_by=dict(symbol_used_by),
        )

    def _resolve_call_to_node_id(
        self,
        call_name: str,
        caller_node: GraphNode,
        all_nodes: dict[str, GraphNode],
    ) -> str | None:
        if call_name in all_nodes:
            return call_name

        candidates: list[str] = []
        simple_name = call_name.split(".")[-1]

        for node_id, node in all_nodes.items():
            defined = set(node.defines_symbols)
            qualified = f"{node.location.parent}.{node.location.name}" if node.location.parent and node.location.name else None
            if call_name in defined or simple_name in defined or call_name == qualified:
                candidates.append(node_id)
            elif node.location.name in {call_name, simple_name}:
                candidates.append(node_id)

        same_file = [node_id for node_id in candidates if all_nodes[node_id].location.file == caller_node.location.file]
        if same_file:
            return sorted(same_file)[0]
        if candidates:
            return sorted(candidates)[0]
        return None


class ImpactFinder:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        builder = ImpactGraphBuilder(str(self.project_root))
        self.graph = builder.build()
        self.parser = TreeSitterParser(str(self.project_root))

    def find_impact(
        self,
        file_path: str,
        changed_lines: list[int],
        max_depth: int = 3,
    ) -> ImpactResult:
        changed_nodes: list[GraphNode] = []
        for line in changed_lines:
            node = self.get_node_at_line(file_path, line)
            if node and node.id not in {item.id for item in changed_nodes}:
                changed_nodes.append(node)

        changed_ids = {node.id for node in changed_nodes}
        impacted: dict[str, ImpactedNode] = {}

        queue: deque[tuple[str, int]] = deque((node.id, 0) for node in changed_nodes)
        while queue:
            node_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for caller_id in self.graph.called_by.get(node_id, []):
                next_depth = depth + 1
                if caller_id in changed_ids:
                    continue
                caller = self.graph.nodes[caller_id]
                changed_node = self.graph.nodes[node_id]
                attrs = self._return_attributes_used(caller, changed_node)
                reason = "uses return value" if attrs else "calls changed function"
                self._add_impacted(impacted, caller, next_depth, reason, attrs)
                queue.append((caller_id, next_depth))

        changed_symbols = sorted({symbol for node in changed_nodes for symbol in self._exported_symbols(node)})
        for symbol in changed_symbols:
            for user_id in self.graph.symbol_used_by.get(symbol, []):
                if user_id in changed_ids:
                    continue
                user = self.graph.nodes[user_id]
                reason = "module level dependency" if user.location.context_type == "module_level" else "uses defined symbol"
                attrs: list[str] = []
                for changed_node in changed_nodes:
                    attrs.extend(self._return_attributes_used(user, changed_node))
                self._add_impacted(impacted, user, 1, reason, sorted(set(attrs)))

        impacted_nodes = sorted(impacted.values(), key=lambda item: (item.depth, item.node.id))
        summary = self._summary(changed_nodes, impacted_nodes)
        return ImpactResult(changed_nodes=changed_nodes, impacted_nodes=impacted_nodes, summary=summary)

    def get_node_at_line(self, file_path: str, line: int) -> GraphNode | None:
        normalized = self._normalize_file(file_path)
        candidates = [
            node for node in self.graph.nodes.values()
            if node.location.file == normalized and node.location.start_line <= line <= node.location.end_line
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda node: (
            node.location.context_type == "module_level",
            node.location.end_line - node.location.start_line,
        ))
        return candidates[0]

    def _normalize_file(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.is_absolute():
            path = self.project_root / path
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return path.name

    def _return_attributes_used(self, caller: GraphNode, changed_node: GraphNode) -> list[str]:
        keys = set(self._exported_symbols(changed_node))
        if changed_node.location.name:
            keys.add(changed_node.location.name)
            if changed_node.location.parent:
                keys.add(f"{changed_node.location.parent}.{changed_node.location.name}")

        attrs: list[str] = []
        for key in keys:
            attrs.extend(caller.call_return_usage.get(key, []))
        return sorted(set(attrs))

    def _exported_symbols(self, node: GraphNode) -> set[str]:
        location = node.location
        if location.context_type in {"function", "method"} and location.name:
            symbols = {location.name}
            if location.parent:
                symbols.add(f"{location.parent}.{location.name}")
            return symbols
        if location.context_type == "class" and location.name:
            return {symbol for symbol in node.defines_symbols if symbol == location.name or symbol.startswith(f"{location.name}.")}
        return set(node.defines_symbols)

    def _add_impacted(
        self,
        impacted: dict[str, ImpactedNode],
        node: GraphNode,
        depth: int,
        reason: str,
        attrs: list[str],
    ) -> None:
        existing = impacted.get(node.id)
        if not existing:
            impacted[node.id] = ImpactedNode(
                node=node,
                depth=depth,
                impact_reason=reason,
                affected_attributes=sorted(set(attrs)),
            )
            return

        if depth < existing.depth:
            existing.depth = depth
        existing.affected_attributes = sorted(set(existing.affected_attributes + attrs))
        if existing.affected_attributes:
            existing.impact_reason = "uses return value"

    def _summary(self, changed_nodes: list[GraphNode], impacted_nodes: list[ImpactedNode]) -> str:
        changed = ", ".join(node.id for node in changed_nodes) or "no matching nodes"
        return f"Changed nodes: {changed}. Impacted nodes found: {len(impacted_nodes)}."


class _PythonNodeAnalyzer(ast.NodeVisitor):
    def __init__(self, file_id: str, lines: list[str], parent: str | None = None):
        self.file_id = file_id
        self.lines = lines
        self.parent = parent
        self.calls: list[str] = []
        self.references_symbols: set[str] = set()
        self.call_return_usage: dict[str, list[str]] = defaultdict(list)
        self.defines_symbols: set[str] = set()
        self.variable_origin: dict[str, str] = {}
        self.variable_class: dict[str, str] = {}
        self._call_attribute_nodes: set[int] = set()

    def visit_statements(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._record_definition(statement)
                continue
            self.visit(statement)

    def visit_decorators_and_arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        args = node.args
        for default in list(args.defaults) + list(args.kw_defaults):
            if default is not None:
                self.visit(default)
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            if arg.annotation:
                self.visit(arg.annotation)
        if args.vararg and args.vararg.annotation:
            self.visit(args.vararg.annotation)
        if args.kwarg and args.kwarg.annotation:
            self.visit(args.kwarg.annotation)
        if node.returns:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_definition(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.references_symbols.add(alias.name)
            self.references_symbols.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module:
            self.references_symbols.add(module)
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.references_symbols.add(local_name)
            self.references_symbols.add(alias.name)
            if module:
                self.references_symbols.add(f"{module}.{alias.name}")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._record_target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.annotation:
            self.visit(node.annotation)
        if node.value:
            self.visit(node.value)
        self._record_target(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._record_target(node.target, None)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._record_target(node.target, None)
        self.visit_statements(node.body)
        self.visit_statements(node.orelse)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With | ast.AsyncWith ) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._record_target(item.optional_vars, item.context_expr)
        self.visit_statements(node.body)

    def visit_AsyncWith(self, node: ast.AsyncWith ) -> None:
        self.visit_With(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node.func)
        if call_name:
            self._add_call(call_name)

        if isinstance(node.func, ast.Attribute):
            self._call_attribute_nodes.add(id(node.func))
            origin = self._origin_from_expr(node.func.value)
            if origin:
                self._add_return_usage(origin, f".{node.func.attr}()")

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        full_name = _expr_name(node)
        if full_name:
            self.references_symbols.add(full_name)
        if isinstance(node.value, ast.Name):
            base_name = node.value.id
            if self.parent and base_name == "self":
                self.references_symbols.add(f"{self.parent}.{node.attr}")
            if base_name in self.variable_class:
                self.references_symbols.add(f"{self.variable_class[base_name]}.{node.attr}")
            if id(node) not in self._call_attribute_nodes and base_name in self.variable_origin:
                self._add_return_usage(self.variable_origin[base_name], f".{node.attr}")
        elif id(node) not in self._call_attribute_nodes:
            origin = self._origin_from_expr(node.value)
            if origin:
                self._add_return_usage(origin, f".{node.attr}")

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        origin = self._origin_from_expr(node.value)
        if origin:
            self._add_return_usage(origin, _subscript_suffix(node.slice))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references_symbols.add(node.id)

    def _record_definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.defines_symbols.add(node.name)
        if self.parent:
            self.defines_symbols.add(f"{self.parent}.{node.name}")

    def _record_target(self, target: ast.AST, value: ast.AST | None) -> None:
        if isinstance(target, ast.Name):
            self.defines_symbols.add(target.id)
            origin = self._origin_from_expr(value) if value else None
            if origin:
                self.variable_origin[target.id] = origin
                self.variable_class[target.id] = origin.split(".")[-1]
            elif isinstance(value, ast.Name):
                if value.id in self.variable_origin:
                    self.variable_origin[target.id] = self.variable_origin[value.id]
                if value.id in self.variable_class:
                    self.variable_class[target.id] = self.variable_class[value.id]
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._record_target(item, None)
        elif isinstance(target, ast.Attribute):
            if self.parent and isinstance(target.value, ast.Name) and target.value.id == "self":
                self.defines_symbols.add(f"{self.parent}.{target.attr}")
            self.visit(target)

    def _origin_from_expr(self, node: ast.AST | None) -> str | None:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return self.variable_origin.get(node.id)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return self._origin_from_expr(node.func.value) or _call_name(node.func)
            return _call_name(node.func)
        if isinstance(node, ast.Attribute):
            return self._origin_from_expr(node.value)
        if isinstance(node, ast.Subscript):
            return self._origin_from_expr(node.value)
        return None

    def _add_call(self, call_name: str) -> None:
        if call_name not in self.calls:
            self.calls.append(call_name)
        self.references_symbols.add(call_name)
        self.references_symbols.add(call_name.split(".")[-1])

    def _add_return_usage(self, origin: str, suffix: str) -> None:
        if suffix not in self.call_return_usage[origin]:
            self.call_return_usage[origin].append(suffix)


def _graph_node_from_dict(data: dict[str, Any]) -> GraphNode:
    location = CodeLocation(**data["location"])
    return GraphNode(
        id=data["id"],
        location=location,
        calls=list(data.get("calls", [])),
        references_symbols=list(data.get("references_symbols", [])),
        call_return_usage={key: list(value) for key, value in data.get("call_return_usage", {}).items()},
        defines_symbols=list(data.get("defines_symbols", [])),
    )


def _source_range(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1:end_line])


def _decorated_start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    line_numbers = [getattr(node, "lineno", 1)]
    line_numbers.extend(getattr(decorator, "lineno", line_numbers[0]) for decorator in node.decorator_list)
    return min(line_numbers)


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Subscript):
        base = _expr_name(node.value)
        return f"{base}{_subscript_suffix(node.slice)}" if base else None
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _subscript_suffix(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        return f"[{node.value!r}]"
    if isinstance(node, ast.Slice):
        return "[:]"
    name = _expr_name(node)
    return f"[{name}]" if name else "[]"


def _sorted_usage(usage: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: sorted(set(value)) for key, value in sorted(usage.items())}


def _format_node_block(node: GraphNode, impacted: ImpactedNode | None = None) -> list[str]:
    location = node.location
    name = location.name or "module_level"
    if location.parent:
        name = f"{location.parent}.{name}"

    block = [
        f"File: {location.file}, function: {name}, lines {location.start_line}-{location.end_line}",
    ]
    if impacted:
        note = f"Note: {impacted.impact_reason}"
        if impacted.affected_attributes:
            note += f"; uses return value attributes: [{', '.join(impacted.affected_attributes)}]"
        block.append(note)
    block.extend([location.source, ""])
    return block
