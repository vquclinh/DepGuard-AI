import ast
import json
import logging
import os
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from tools.ast_scanner import ASTScanner as MultiLanguageScanner
except ImportError:
    MultiLanguageScanner = None


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

    Python keeps the standard ast extractor for richer data-flow details. All
    other supported extensions use Tree-sitter when a grammar is installed, with
    a conservative module-level fallback so non-Python projects still show up in
    the graph.
    """

    GENERIC_FUNCTION_TYPES = {
        "function_definition", "function_declaration", "function_item",
        "method_definition", "method_declaration", "function_declarator",
        "arrow_function", "generator_function_declaration",
        "async_function_declaration", "constructor_declaration",
    }
    GENERIC_CLASS_TYPES = {
        "class_definition", "class_declaration", "struct_item",
        "struct_declaration", "interface_declaration", "enum_declaration",
        "impl_item", "object_declaration", "trait_item", "record_declaration",
    }
    GENERIC_IDENTIFIER_TYPES = {
        "identifier", "type_identifier", "property_identifier",
        "field_identifier", "constant", "scoped_identifier",
        "qualified_identifier", "simple_identifier", "name",
    }
    GENERIC_SKIP_TYPES = {
        "comment", "line_comment", "block_comment", "string", "string_literal",
        "raw_string_literal", "template_string", "char_literal",
        "character_literal",
    }

    def __init__(self, project_root: str | None = None):
        self.project_root = Path(project_root).resolve() if project_root else None
        self.parsers: dict[str, Any] = {}
        self.language_scanner = MultiLanguageScanner() if MultiLanguageScanner else None
        if self.language_scanner:
            self.parsers.update(self.language_scanner.parsers)
        else:
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

        if language in self.parsers:
            return self._parse_generic_tree_sitter(path, source, language)

        return self._parse_lexical_nodes(path, source, language)

    def adapter_summary(self) -> dict[str, str]:
        return {
            "python": "Python AST adapter with calls, classes, defaults, decorators, and return-value data flow.",
            "rust": "Rust Tree-sitter/generic adapter plus lexical fallback for fn/struct/enum/trait/impl, calls, macros, use statements, and let-chain data flow.",
            "go": "Go Tree-sitter/generic adapter plus lexical fallback for funcs, methods, structs/interfaces, selector calls, imports, and := data flow.",
            "javascript": "JavaScript Tree-sitter/generic adapter plus lexical fallback for functions, classes, arrow functions, imports, member calls, and const/let data flow.",
            "typescript": "TypeScript adapter shares JavaScript behavior and handles TS/TSX extensions.",
            "java": "Java Tree-sitter/generic adapter plus lexical fallback for classes, methods, imports, member calls, and typed local data flow.",
        }

    def get_function_source(self, file_path: str, start_line: int, end_line: int) -> str:
        try:
            lines = Path(file_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[start_line - 1:end_line])

    def detect_language(self, file_path: str) -> str | None:
        if self.language_scanner:
            return self.language_scanner.detect_language(file_path)

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

    def _parse_generic_tree_sitter(self, path: Path, source: str, language: str) -> list[GraphNode]:
        parser = self.parsers.get(language)
        if not parser:
            return self._parse_lexical_nodes(path, source, language)

        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception as exc:
            logger.debug("Could not tree-sitter parse %s: %s", path, exc)
            return self._parse_lexical_nodes(path, source, language)

        file_id = self._display_path(path)
        lines = source.splitlines()
        nodes: list[GraphNode] = []
        used_ids: set[str] = set()

        def add_node(ts_node: Any, context_type: str, name: str, parent: str | None = None) -> None:
            start = _point_row(ts_node.start_point) + 1
            end = _point_row(ts_node.end_point) + 1
            calls, references = self._generic_symbols_and_calls(ts_node, source)
            lexical_calls, lexical_references, return_usage = self._lexical_block_analysis(
                _source_range(lines, start, end),
                language,
            )
            for call in lexical_calls:
                if call not in calls:
                    calls.append(call)
            references.update(lexical_references)
            calls = [
                call for call in calls
                if call not in {name, f"{parent}.{name}" if parent else name}
            ]
            defines = {name}
            if parent:
                defines.add(f"{parent}.{name}")

            qualified_name = f"{parent}.{name}" if parent else name
            node_id = self._unique_node_id(f"{file_id}::{qualified_name}", used_ids, start)
            nodes.append(GraphNode(
                id=node_id,
                location=CodeLocation(
                    file=file_id,
                    start_line=start,
                    end_line=end,
                    source=_source_range(lines, start, end),
                    context_type=context_type,
                    name=name,
                    parent=parent,
                ),
                calls=calls,
                references_symbols=sorted(references),
                call_return_usage=_sorted_usage(return_usage),
                defines_symbols=sorted(defines),
            ))

        def walk(ts_node: Any, parent: str | None = None) -> None:
            if self._is_generic_class(ts_node):
                name = self._generic_node_name(ts_node, source)
                if name:
                    add_node(ts_node, "class", name)
                    for child in getattr(ts_node, "children", []):
                        walk(child, name)
                    return

            if self._is_generic_function(ts_node):
                name = self._generic_node_name(ts_node, source)
                if name:
                    add_node(ts_node, "method" if parent else "function", name, parent)

            for child in getattr(ts_node, "children", []):
                walk(child, parent)

        walk(tree.root_node)

        module_children = [
            child for child in getattr(tree.root_node, "named_children", tree.root_node.children)
            if not self._is_generic_function(child) and not self._is_generic_class(child)
        ]
        if module_children:
            start = min(_point_row(child.start_point) + 1 for child in module_children)
            end = max(_point_row(child.end_point) + 1 for child in module_children)
            calls: list[str] = []
            references: set[str] = set()
            for child in module_children:
                child_calls, child_refs = self._generic_symbols_and_calls(child, source)
                for call in child_calls:
                    if call not in calls:
                        calls.append(call)
                references.update(child_refs)
            lexical_calls, lexical_references, return_usage = self._lexical_block_analysis(
                _source_range(lines, start, end),
                language,
            )
            for call in lexical_calls:
                if call not in calls:
                    calls.append(call)
            references.update(lexical_references)

            node_id = self._unique_node_id(f"{file_id}::module_level::{start}-{end}", used_ids, start)
            nodes.append(GraphNode(
                id=node_id,
                location=CodeLocation(
                    file=file_id,
                    start_line=start,
                    end_line=end,
                    source=_source_range(lines, start, end),
                    context_type="module_level",
                    name=None,
                    parent=None,
                ),
                calls=calls,
                references_symbols=sorted(references),
                call_return_usage=_sorted_usage(return_usage),
                defines_symbols=[],
            ))

        if not nodes:
            return self._parse_lexical_nodes(path, source, language)

        return nodes

    def _parse_lexical_nodes(self, path: Path, source: str, language: str | None) -> list[GraphNode]:
        file_id = self._display_path(path)
        lines = source.splitlines()
        if not lines:
            return []

        nodes: list[GraphNode] = []
        used_ids: set[str] = set()
        definitions = self._lexical_definitions(source, language)
        covered_lines: set[int] = set()

        for definition in definitions:
            start_line, end_line, name, context_type = definition
            node_source = _source_range(lines, start_line, end_line)
            calls, references, return_usage = self._lexical_block_analysis(node_source, language)
            calls = [call for call in calls if call != name]
            node_id = self._unique_node_id(f"{file_id}::{name}", used_ids, start_line)
            nodes.append(GraphNode(
                id=node_id,
                location=CodeLocation(
                    file=file_id,
                    start_line=start_line,
                    end_line=end_line,
                    source=node_source,
                    context_type=context_type,
                    name=name,
                    parent=None,
                ),
                calls=calls,
                references_symbols=sorted(references),
                call_return_usage=_sorted_usage(return_usage),
                defines_symbols=[name],
            ))
            covered_lines.update(range(start_line, end_line + 1))

        module_lines = [
            (line_no, line)
            for line_no, line in enumerate(lines, start=1)
            if line_no not in covered_lines and line.strip()
        ]
        if module_lines:
            start_line = module_lines[0][0]
            end_line = module_lines[-1][0]
            module_source = _source_range(lines, start_line, end_line)
            calls, references, return_usage = self._lexical_block_analysis(module_source, language)
            nodes.append(GraphNode(
                id=self._unique_node_id(f"{file_id}::module_level::{start_line}-{end_line}", used_ids, start_line),
                location=CodeLocation(
                    file=file_id,
                    start_line=start_line,
                    end_line=end_line,
                    source=module_source,
                    context_type="module_level",
                    name=None,
                    parent=None,
                ),
                calls=calls,
                references_symbols=sorted(references),
                call_return_usage=_sorted_usage(return_usage),
                defines_symbols=[],
            ))

        if nodes:
            return nodes

        calls, references, return_usage = self._lexical_block_analysis(source, language)
        return [GraphNode(
            id=f"{file_id}::module_level::1-{len(lines)}",
            location=CodeLocation(
                file=file_id,
                start_line=1,
                end_line=len(lines),
                source=source,
                context_type="module_level",
                name=None,
                parent=None,
            ),
            calls=calls,
            references_symbols=sorted(references),
            call_return_usage=_sorted_usage(return_usage),
            defines_symbols=[],
        )]

    def _is_generic_function(self, ts_node: Any) -> bool:
        node_type = getattr(ts_node, "type", "")
        return node_type in self.GENERIC_FUNCTION_TYPES or (
            "function" in node_type and "type" not in node_type
        )

    def _is_generic_class(self, ts_node: Any) -> bool:
        node_type = getattr(ts_node, "type", "")
        return node_type in self.GENERIC_CLASS_TYPES or (
            any(token in node_type for token in ("class", "struct", "interface", "enum", "trait"))
            and "body" not in node_type
        )

    def _generic_node_name(self, ts_node: Any, source: str) -> str | None:
        for field in ("name", "declarator", "type"):
            child = ts_node.child_by_field_name(field)
            if child:
                name = self._first_identifier_text(child, source)
                if name:
                    return name
        return self._first_identifier_text(ts_node, source)

    def _generic_symbols_and_calls(self, ts_node: Any, source: str) -> tuple[list[str], set[str]]:
        calls: list[str] = []
        references: set[str] = set()

        def walk(node: Any) -> None:
            node_type = getattr(node, "type", "")
            if node_type in self.GENERIC_SKIP_TYPES or "comment" in node_type:
                return

            if self._is_generic_call(node):
                call_name = self._generic_call_name(node, source)
                if call_name:
                    if call_name not in calls:
                        calls.append(call_name)
                    references.add(call_name)
                    references.add(call_name.split(".")[-1].split("::")[-1])

            if node_type in self.GENERIC_IDENTIFIER_TYPES:
                symbol = self._clean_symbol(self._node_text(node, source))
                if symbol:
                    references.add(symbol)

            for child in getattr(node, "children", []):
                walk(child)

        walk(ts_node)
        return calls, references

    def _is_generic_call(self, ts_node: Any) -> bool:
        node_type = getattr(ts_node, "type", "")
        return (
            node_type in {"call_expression", "call", "method_invocation", "macro_invocation", "invocation_expression"}
            or "call_expression" in node_type
            or "invocation" in node_type
        )

    def _generic_call_name(self, ts_node: Any, source: str) -> str | None:
        target = (
            ts_node.child_by_field_name("function")
            or ts_node.child_by_field_name("name")
            or ts_node.child_by_field_name("method")
        )
        if not target:
            for child in getattr(ts_node, "children", []):
                if child.type not in {"arguments", "argument_list", "parameters"}:
                    target = child
                    break
        if not target:
            return None

        raw = self._node_text(target, source)
        raw = re.sub(r"\s+", "", raw)
        raw = raw.split("(", 1)[0]
        raw = raw.replace("::", ".")
        if len(raw) > 120 or not raw:
            return self._first_identifier_text(target, source)
        return raw

    def _first_identifier_text(self, ts_node: Any, source: str) -> str | None:
        if getattr(ts_node, "type", "") in self.GENERIC_IDENTIFIER_TYPES:
            return self._clean_symbol(self._node_text(ts_node, source))
        for child in getattr(ts_node, "children", []):
            name = self._first_identifier_text(child, source)
            if name:
                return name
        return None

    def _clean_symbol(self, value: str) -> str | None:
        value = value.strip()
        if not value or len(value) > 120:
            return None
        if not re.search(r"[A-Za-z_$]", value):
            return None
        return value

    def _node_text(self, ts_node: Any, source: str) -> str:
        return source[ts_node.start_byte:ts_node.end_byte]

    def _lexical_definitions(self, source: str, language: str | None) -> list[tuple[int, int, str, str]]:
        patterns = self._lexical_definition_patterns(language)
        if not patterns:
            return []

        definitions: list[tuple[int, int, str, str]] = []
        seen: set[tuple[int, int, str, str]] = set()
        scan_source = self._strip_comments_and_strings(source)

        for pattern, context_type in patterns:
            for match in re.finditer(pattern, scan_source, re.MULTILINE):
                name = match.group("name")
                brace_index = scan_source.find("{", match.end() - 1)
                start_line = source.count("\n", 0, match.start()) + 1
                if brace_index >= 0 and brace_index < match.end() + 500:
                    end_line = self._matching_brace_end_line(scan_source, brace_index)
                else:
                    end_line = start_line

                key = (start_line, end_line, name, context_type)
                if key in seen:
                    continue
                seen.add(key)
                definitions.append((start_line, end_line, name, context_type))

        return sorted(definitions, key=lambda item: (item[0], item[1], item[2]))

    def _lexical_definition_patterns(self, language: str | None) -> list[tuple[str, str]]:
        common_function = r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\b[^{;]*\{"
        patterns_by_language = {
            "rust": [
                (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]+\"\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "function"),
                (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "class"),
            ],
            "go": [
                (r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "function"),
                (r"^\s*type\s+(?P<name>[A-Za-z_]\w*)\s+(?:struct|interface)\b[^{;]*\{", "class"),
            ],
            "javascript": [
                (common_function, "function"),
                (r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b[^{;]*\{", "class"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", "function"),
            ],
            "typescript": [
                (common_function, "function"),
                (r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b[^{;]*\{", "class"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", "function"),
            ],
            "tsx": [
                (common_function, "function"),
                (r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b[^{;]*\{", "class"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", "function"),
            ],
            "java": [
                (r"^\s*(?:public|private|protected|static|final|abstract|synchronized|\s)+[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*(?:throws\s+[^{]+)?\{", "function"),
                (r"^\s*(?:public|private|protected|abstract|final|\s)*(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "class"),
            ],
            "c": [
                (r"^\s*(?:static\s+|inline\s+|extern\s+)?[A-Za-z_][\w\s*]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", "function"),
            ],
            "cpp": [
                (r"^\s*(?:template\s*<[^>]+>\s*)?(?:static\s+|inline\s+|virtual\s+|constexpr\s+)?[A-Za-z_:~][\w:\s*&<>~]+\s+(?P<name>[A-Za-z_:~]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?\{", "function"),
                (r"^\s*(?:class|struct|enum)\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "class"),
            ],
            "c_sharp": [
                (r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|partial|\s)+[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^)]*\)\s*\{", "function"),
                (r"^\s*(?:public|private|protected|internal|abstract|sealed|partial|\s)*(?:class|interface|enum|struct|record)\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{", "class"),
            ],
        }
        return patterns_by_language.get(language or "", [])

    def _matching_brace_end_line(self, source: str, open_brace_index: int) -> int:
        depth = 0
        for index in range(open_brace_index, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source.count("\n", 0, index) + 1
        return source.count("\n") + 1

    def _strip_comments_and_strings(self, source: str) -> str:
        patterns = [
            r"//[^\n]*",
            r"/\*.*?\*/",
            r"#(?!\[)[^\n]*",
            r'"(?:\\.|[^"\\])*"',
            r"'(?:\\.|[^'\\])*'",
            r"`(?:\\.|[^`\\])*`",
        ]

        def blank(match: re.Match) -> str:
            return re.sub(r"[^\n]", " ", match.group(0))

        stripped = source
        for pattern in patterns:
            stripped = re.sub(pattern, blank, stripped, flags=re.DOTALL)
        return stripped

    def _lexical_block_analysis(
        self,
        source: str,
        language: str | None,
    ) -> tuple[list[str], set[str], dict[str, list[str]]]:
        calls, references = self._lexical_symbols_and_calls(source)
        scan_source = self._strip_comments_and_strings(source)
        variable_origin: dict[str, str] = {}
        call_return_usage: dict[str, list[str]] = defaultdict(list)

        def remember_call(call_name: str | None) -> None:
            if not call_name:
                return
            references.add(call_name)
            references.add(call_name.split(".")[-1])
            if call_name not in calls:
                calls.append(call_name)

        for variable, expression in self._lexical_assignments(scan_source, language):
            origin = self._origin_from_lexical_expression(expression, variable_origin)
            if origin:
                variable_origin[variable] = origin
                remember_call(origin)
            elif expression.strip() in variable_origin:
                variable_origin[variable] = variable_origin[expression.strip()]

        ignored_attrs = {
            "await", "unwrap", "expect", "ok", "err", "as_ref", "as_mut",
            "clone", "into", "iter", "next",
        }

        for variable, origin in variable_origin.items():
            escaped = re.escape(variable)

            for match in re.finditer(rf"\b{escaped}\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", scan_source):
                attr = match.group(1)
                suffix = f".{attr}()"
                self._add_lexical_return_usage(call_return_usage, origin, suffix)
                remember_call(f"{variable}.{attr}")

            for match in re.finditer(rf"\b{escaped}\s*\.\s*([A-Za-z_$][\w$]*)\b", scan_source):
                attr = match.group(1)
                after = scan_source[match.end():match.end() + 1]
                if after == "(" or attr in ignored_attrs:
                    continue
                self._add_lexical_return_usage(call_return_usage, origin, f".{attr}")

            for match in re.finditer(rf"\b{escaped}\s*\[\s*([^\]]+)\s*\]", scan_source):
                key = match.group(1).strip()
                if len(key) <= 80:
                    self._add_lexical_return_usage(call_return_usage, origin, f"[{key}]")

        return calls, references, call_return_usage

    def _lexical_assignments(self, source: str, language: str | None) -> list[tuple[str, str]]:
        patterns = [
            r"\b(?:let|const|var|final|val|auto|mut)\s+(?:mut\s+)?(?P<var>[A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*(?P<expr>[^;\n]+)",
            r"\b(?P<var>[A-Za-z_$][\w$]*)\s*:=\s*(?P<expr>[^\n;]+)",
            r"\b(?P<var>[A-Za-z_$][\w$]*)\s*=\s*(?P<expr>[^;\n]+)",
        ]

        if language in {"java", "c_sharp", "go", "c", "cpp"}:
            patterns.insert(
                0,
                r"\b[A-Za-z_$][\w$<>\[\]., ?]*\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*(?P<expr>[^;\n]+)",
            )

        assignments: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, source):
                variable = match.group("var")
                expression = match.group("expr").strip()
                if variable in {"if", "for", "while", "return", "match", "switch"}:
                    continue
                key = (variable, expression)
                if key not in seen:
                    seen.add(key)
                    assignments.append(key)
        return assignments

    def _origin_from_lexical_expression(
        self,
        expression: str,
        variable_origin: dict[str, str],
    ) -> str | None:
        expression = expression.strip()
        if expression in variable_origin:
            return variable_origin[expression]

        call_matches = list(re.finditer(
            r"\b([A-Za-z_$][\w$]*(?:(?:::|\.)[A-Za-z_$][\w$]*)*)\s*[(!]",
            expression,
        ))
        if not call_matches:
            return None

        origin = call_matches[-1].group(1).replace("::", ".")
        if origin in {"if", "for", "while", "match", "switch", "return"}:
            return None
        return origin

    def _add_lexical_return_usage(
        self,
        usage: dict[str, list[str]],
        origin: str,
        suffix: str,
    ) -> None:
        keys = {origin, origin.split(".")[-1]}
        for key in keys:
            if suffix not in usage[key]:
                usage[key].append(suffix)

    def _lexical_symbols_and_calls(self, source: str) -> tuple[list[str], set[str]]:
        scan_source = self._strip_comments_and_strings(source)
        references = set(re.findall(r"\b[A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*|\.[A-Za-z_$][\w$]*)*\b", scan_source))
        calls: list[str] = []
        for match in re.finditer(r"\b([A-Za-z_$][\w$]*(?:::[A-Za-z_$][\w$]*|\.[A-Za-z_$][\w$]*)*)\s*[(!]", scan_source):
            call = match.group(1).replace("::", ".")
            if call in {"if", "for", "while", "match", "switch", "return", "fn", "func", "function"}:
                continue
            if call not in calls:
                calls.append(call)
        return calls, references

    def _unique_node_id(self, base_id: str, used_ids: set[str], start_line: int) -> str:
        node_id = base_id
        if node_id in used_ids:
            node_id = f"{base_id}@{start_line}"
        counter = 2
        while node_id in used_ids:
            node_id = f"{base_id}@{start_line}-{counter}"
            counter += 1
        used_ids.add(node_id)
        return node_id

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
    CACHE_VERSION = 4
    IGNORE_DIRS = {
        "venv", ".venv", "env", "node_modules", "__pycache__",
        ".git", "dist", "build", ".pytest_cache", ".depguard_cache",
        "target", ".next", ".turbo", "coverage",
    }

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.cache_dir = self.project_root / ".depguard_cache"
        self.cache_file = self.cache_dir / "impact_graph.json"
        self.parser = TreeSitterParser(str(self.project_root))

    def build(self, force_rebuild: bool = False) -> ImpactGraph:
        files = self._supported_files()
        cache = self._load_cache() if not force_rebuild else {"files": {}}
        if cache.get("version") != self.CACHE_VERSION:
            cache = {"version": self.CACHE_VERSION, "files": {}}
        cached_files = cache.get("files", {})
        next_cache: dict[str, Any] = {"version": self.CACHE_VERSION, "files": {}}
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
            return {"version": self.CACHE_VERSION, "files": {}}

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


def _point_row(point: Any) -> int:
    return getattr(point, "row", point[0])


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
