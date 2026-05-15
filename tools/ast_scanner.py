import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ASTScanner:
    """Tree-sitter-first multi-language API scanner.

    The public class name stays ASTScanner for compatibility with the existing
    DepGuard pipeline, but the implementation is no longer Python-AST-only.
    Tree-sitter is used when a grammar is installed; otherwise the scanner falls
    back to a conservative lexical scan so non-Python projects remain visible.
    """

    IGNORE_DIRS = {
        "venv", ".venv", "env", "node_modules", "__pycache__",
        ".git", "dist", "build", ".pytest_cache", ".depguard_cache",
        "target", ".next", ".turbo", "coverage",
    }

    LANGUAGE_BY_EXTENSION = {
        ".py": "python",
        ".pyw": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".mts": "typescript",
        ".cts": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".scala": "scala",
        ".sc": "scala",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".hxx": "cpp",
        ".cs": "c_sharp",
        ".php": "php",
        ".rb": "ruby",
        ".swift": "swift",
        ".dart": "dart",
        ".lua": "lua",
        ".ex": "elixir",
        ".exs": "elixir",
        ".hs": "haskell",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".scss": "scss",
        ".sass": "scss",
        ".less": "css",
        ".vue": "vue",
        ".svelte": "svelte",
        ".astro": "astro",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".fish": "fish",
        ".ps1": "powershell",
        ".r": "r",
        ".R": "r",
        ".jl": "julia",
        ".clj": "clojure",
        ".cljs": "clojure",
        ".cljc": "clojure",
        ".erl": "erlang",
        ".hrl": "erlang",
        ".fs": "f_sharp",
        ".fsx": "f_sharp",
        ".fsi": "f_sharp",
        ".ml": "ocaml",
        ".mli": "ocaml",
        ".m": "objc",
        ".mm": "objc",
        ".zig": "zig",
        ".nim": "nim",
        ".pl": "perl",
        ".pm": "perl",
        ".groovy": "groovy",
        ".sol": "solidity",
    }

    GRAMMAR_MODULES = {
        "python": ("tree_sitter_python", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
        "tsx": ("tree_sitter_typescript", "language_tsx"),
        "rust": ("tree_sitter_rust", "language"),
        "go": ("tree_sitter_go", "language"),
        "java": ("tree_sitter_java", "language"),
        "c": ("tree_sitter_c", "language"),
        "cpp": ("tree_sitter_cpp", "language"),
        "c_sharp": ("tree_sitter_c_sharp", "language"),
        "php": ("tree_sitter_php", "language_php"),
        "ruby": ("tree_sitter_ruby", "language"),
        "kotlin": ("tree_sitter_kotlin", "language"),
        "swift": ("tree_sitter_swift", "language"),
        "scala": ("tree_sitter_scala", "language"),
        "lua": ("tree_sitter_lua", "language"),
        "elixir": ("tree_sitter_elixir", "language"),
        "haskell": ("tree_sitter_haskell", "language"),
        "dart": ("tree_sitter_dart", "language"),
        "html": ("tree_sitter_html", "language"),
        "css": ("tree_sitter_css", "language"),
        "scss": ("tree_sitter_scss", "language"),
        "vue": ("tree_sitter_vue", "language"),
        "svelte": ("tree_sitter_svelte", "language"),
        "astro": ("tree_sitter_astro", "language"),
        "bash": ("tree_sitter_bash", "language"),
        "fish": ("tree_sitter_fish", "language"),
        "powershell": ("tree_sitter_powershell", "language"),
        "r": ("tree_sitter_r", "language"),
        "julia": ("tree_sitter_julia", "language"),
        "clojure": ("tree_sitter_clojure", "language"),
        "erlang": ("tree_sitter_erlang", "language"),
        "f_sharp": ("tree_sitter_fsharp", "language"),
        "ocaml": ("tree_sitter_ocaml", "language_ocaml"),
        "objc": ("tree_sitter_objc", "language"),
        "zig": ("tree_sitter_zig", "language"),
        "nim": ("tree_sitter_nim", "language"),
        "perl": ("tree_sitter_perl", "language"),
        "groovy": ("tree_sitter_groovy", "language"),
        "solidity": ("tree_sitter_solidity", "language"),
    }

    BINARY_EXTENSIONS = {
        ".7z", ".a", ".ai", ".avi", ".bin", ".bmp", ".class", ".dll", ".dmg",
        ".doc", ".docx", ".dylib", ".eot", ".exe", ".gif", ".gz", ".ico",
        ".jar", ".jpeg", ".jpg", ".mov", ".mp3", ".mp4", ".o", ".obj", ".otf",
        ".pdf", ".png", ".pyc", ".rar", ".so", ".sqlite", ".sqlite3", ".tar",
        ".ttf", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".zip",
    }

    COMMENT_OR_STRING_TYPES = {
        "comment", "line_comment", "block_comment", "string", "string_literal",
        "raw_string_literal", "interpreted_string_literal", "template_string",
        "template_substitution", "char_literal", "character_literal",
    }

    def __init__(self):
        self.parsers: dict[str, Any] = {}
        self._tree_sitter_language: Any = None
        self._tree_sitter_parser: Any = None
        self._language_pack_get_parser: Any = None
        self._init_tree_sitter()

    def _init_tree_sitter(self) -> None:
        try:
            from tree_sitter import Language, Parser
            self._tree_sitter_language = Language
            self._tree_sitter_parser = Parser
        except Exception as exc:
            logger.debug("tree-sitter is not available: %s", exc)
            return

        try:
            from tree_sitter_language_pack import get_parser
            self._language_pack_get_parser = get_parser
        except Exception:
            self._language_pack_get_parser = None

        for language in sorted(set(self.LANGUAGE_BY_EXTENSION.values())):
            parser = self._load_parser(language)
            if parser:
                self.parsers[language] = parser

    def _load_parser(self, language: str) -> Any | None:
        if self._language_pack_get_parser:
            try:
                parser = self._language_pack_get_parser(language)
                if self._is_usable_parser(parser):
                    return parser
                logger.debug("tree-sitter language pack returned an unusable %s parser", language)
            except Exception as exc:
                logger.debug("Could not load %s from tree-sitter language pack: %s", language, exc)

        grammar = self.GRAMMAR_MODULES.get(language)
        language_type = self._tree_sitter_language
        if not grammar or language_type is None or self._tree_sitter_parser is None:
            return None

        module_name, function_name = grammar
        try:
            module = __import__(module_name)
            raw_language = getattr(module, function_name)()
            tree_sitter_language = (
                raw_language
                if isinstance(raw_language, language_type)
                else language_type(raw_language)
            )
            parser = self._make_parser(tree_sitter_language)
            return parser if self._is_usable_parser(parser) else None
        except Exception as exc:
            logger.debug("Could not load tree-sitter grammar %s: %s", language, exc)
            return None

    def _make_parser(self, language: Any) -> Any | None:
        parser_factory = self._tree_sitter_parser
        if parser_factory is None:
            return None

        try:
            return parser_factory(language)
        except Exception:
            pass

        try:
            parser: Any = parser_factory()
            set_language = getattr(parser, "set_language", None)
            if callable(set_language):
                set_language(language)
            else:
                parser.language = language
            return parser
        except Exception as exc:
            logger.debug("Could not create tree-sitter parser: %s", exc)
            return None

    def _is_usable_parser(self, parser: Any) -> bool:
        return parser is not None and callable(getattr(parser, "parse", None))

    def find_api_usages(self, root_folder: str, package_name: str) -> list[str]:
        root = Path(root_folder)
        all_used_apis = set()

        if not root.exists() or not root.is_dir():
            logger.error("Invalid directory: %s", root_folder)
            return []

        for filepath in self._iter_source_files(root):
            try:
                content = self._read_text(filepath)
            except OSError as exc:
                logger.debug("Could not read %s: %s", filepath, exc)
                continue

            language = self.detect_language(str(filepath))
            ignored_ranges = self._ignored_ranges(language, content)
            aliases = self._extract_aliases(content)
            all_used_apis.update(self._find_package_usages(content, package_name, aliases, ignored_ranges))

        return sorted(all_used_apis)

    def scan(self, root_folder: str, breaking_changes: list) -> dict:
        root = Path(root_folder)
        matches_by_file = defaultdict(list)
        total_files_scanned = 0
        total_matches = 0

        if not root.exists() or not root.is_dir():
            logger.error("Invalid directory: %s", root_folder)
            return {
                "total_files_scanned": 0,
                "total_files_affected": 0,
                "total_matches": 0,
                "matches_by_file": {},
            }

        for filepath in self._iter_source_files(root):
            total_files_scanned += 1
            try:
                content = self._read_text(filepath)
            except OSError as exc:
                logger.debug("Could not read %s: %s", filepath, exc)
                continue

            language = self.detect_language(str(filepath))
            ignored_ranges = self._ignored_ranges(language, content)
            aliases = self._extract_aliases(content)
            file_matches = self._scan_content(str(filepath), content, breaking_changes, aliases, ignored_ranges)

            if file_matches:
                matches_by_file[str(filepath)].extend(file_matches)
                total_matches += len(file_matches)

        return {
            "total_files_scanned": total_files_scanned,
            "total_files_affected": len(matches_by_file),
            "total_matches": total_matches,
            "matches_by_file": dict(matches_by_file),
        }

    def validate_source(self, file_path: str, content: str) -> str | None:
        language = self.detect_language(file_path)
        parser = self.parsers.get(language or "")
        if not parser:
            return None
        try:
            tree = parser.parse(content.encode("utf-8"))
            if getattr(tree.root_node, "has_error", False):
                return f"tree-sitter parse error in {file_path}"
        except Exception as exc:
            return f"tree-sitter validation failed for {file_path}: {exc}"
        return None

    def detect_language(self, file_path: str) -> str | None:
        return self.LANGUAGE_BY_EXTENSION.get(Path(file_path).suffix.lower())

    def _iter_source_files(self, root: Path):
        for filepath in root.rglob("*"):
            if not filepath.is_file():
                continue
            if any(part in self.IGNORE_DIRS for part in filepath.parts):
                continue
            if not self.detect_language(str(filepath)):
                continue
            if not self._looks_text_file(filepath):
                continue
            yield filepath

    def _looks_text_file(self, path: Path) -> bool:
        if path.suffix.lower() in self.BINARY_EXTENSIONS:
            return False
        try:
            if path.stat().st_size > 2_000_000:
                return False
            with open(path, "rb") as f:
                return b"\x00" not in f.read(2048)
        except OSError:
            return False

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="ignore")

    def _ignored_ranges(self, language: str | None, content: str) -> list[tuple[int, int]]:
        parser = self.parsers.get(language or "")
        if not parser:
            return []

        ignored: list[tuple[int, int]] = []
        try:
            tree = parser.parse(content.encode("utf-8"))
        except Exception:
            return []

        def walk(node: Any) -> None:
            node_type = getattr(node, "type", "").lower()
            if node_type in self.COMMENT_OR_STRING_TYPES or "comment" in node_type:
                ignored.append((node.start_byte, node.end_byte))
                return
            for child in getattr(node, "children", []):
                walk(child)

        walk(tree.root_node)
        return ignored

    def _scan_content(
        self,
        filepath: str,
        content: str,
        breaking_changes: list,
        aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> list[dict]:
        lines = content.splitlines()
        matches = []
        seen = set()
        seen_spans: list[tuple[str, int, int]] = []

        for bc in breaking_changes:
            old_api = bc.get("old_api", "")
            if not old_api:
                continue
            for variant in sorted(self._api_variants(old_api, aliases), key=len, reverse=True):
                for match in self._find_variant_matches(content, variant):
                    start = match.start()
                    end = match.end()
                    if self._is_ignored_offset(start, ignored_ranges):
                        continue
                    if any(
                        seen_old_api == old_api and start < seen_end and end > seen_start
                        for seen_old_api, seen_start, seen_end in seen_spans
                    ):
                        continue
                    line, col = self._line_col(content, start)
                    key = (line, col, old_api)
                    if key in seen:
                        continue
                    seen.add(key)
                    seen_spans.append((old_api, start, end))
                    snippet = lines[line - 1].rstrip() if 0 < line <= len(lines) else ""
                    matches.append({
                        "file": filepath,
                        "line": line,
                        "col": col,
                        "old_api": old_api,
                        "new_api": bc.get("new_api", ""),
                        "description": bc.get("description", ""),
                        "code_snippet": snippet,
                        "type": bc.get("type", ""),
                        "matched_text": match.group(0),
                    })

        return matches

    def _find_package_usages(
        self,
        content: str,
        package_name: str,
        aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> set[str]:
        if not package_name:
            return set()

        usages = set()
        escaped = re.escape(package_name)
        for match in re.finditer(rf"(?<![\w$]){escaped}(?:[.:/][A-Za-z_$][\w$-]*)*", content):
            if self._is_ignored_offset(match.start(), ignored_ranges):
                continue
            usages.add(match.group(0))

        for alias, real_name in aliases.items():
            if real_name == package_name or real_name.startswith(f"{package_name}.") or real_name.startswith(f"{package_name}/"):
                usages.add(real_name)
                for match in re.finditer(rf"(?<![\w$]){re.escape(alias)}((?:\.[A-Za-z_$][\w$]*)+)", content):
                    if self._is_ignored_offset(match.start(), ignored_ranges):
                        continue
                    usages.add(f"{real_name}{match.group(1)}")

        return usages

    def _extract_aliases(self, content: str) -> dict[str, str]:
        aliases: dict[str, str] = {}

        for module, alias in re.findall(r"^\s*import\s+([A-Za-z_][\w.]*)(?:\s+as\s+([A-Za-z_]\w*))?", content, re.MULTILINE):
            aliases[alias or module.split(".")[0]] = module

        for module, names in re.findall(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+([^\n]+)", content, re.MULTILINE):
            for item in names.split(","):
                item = item.strip()
                if not item or item == "*":
                    continue
                parts = re.split(r"\s+as\s+", item)
                name = parts[0].strip()
                alias = parts[1].strip() if len(parts) > 1 else name
                aliases[alias] = f"{module}.{name}"

        for default_or_ns, module in re.findall(r"import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]", content):
            aliases[default_or_ns] = module

        for imports, module in re.findall(r"import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]", content):
            for item in imports.split(","):
                item = item.strip()
                if not item:
                    continue
                parts = re.split(r"\s+as\s+", item)
                name = parts[0].strip()
                alias = parts[1].strip() if len(parts) > 1 else name
                aliases[alias] = f"{module}.{name}"

        for alias, module in re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(['\"]([^'\"]+)['\"]\)", content):
            aliases[alias] = module

        for full, name, alias in re.findall(r"use\s+([A-Za-z_][\w:]+)::([A-Za-z_]\w*)\s+as\s+([A-Za-z_]\w*)", content):
            aliases[alias] = f"{full}::{name}"

        for full, name in re.findall(r"use\s+([A-Za-z_][\w:]+)::([A-Za-z_]\w*)\s*;", content):
            aliases[name] = f"{full}::{name}"

        aliases.update(self._extract_rust_use_tree_aliases(content))

        for alias, module in re.findall(r"^\s*(?:import\s+)?([A-Za-z_]\w*)\s+['\"]([^'\"]+)['\"]", content, re.MULTILINE):
            aliases[alias] = module

        for imported in re.findall(r"^\s*import\s+([A-Za-z_][\w.]*);", content, re.MULTILINE):
            aliases[imported.split(".")[-1]] = imported

        for imported in re.findall(r"^\s*using\s+([A-Za-z_][\w.]*);", content, re.MULTILINE):
            aliases[imported.split(".")[-1]] = imported

        for imported in re.findall(r"^\s*use\s+([A-Za-z_\\][\w\\]+);", content, re.MULTILINE):
            aliases[imported.split("\\")[-1]] = imported.replace("\\", ".")

        return aliases

    def _extract_rust_use_tree_aliases(self, content: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for use_body in self._rust_use_statements(content):
            self._walk_rust_use_tree(use_body.strip(), [], aliases)
        return aliases

    def _rust_use_statements(self, content: str) -> list[str]:
        statements: list[str] = []
        index = 0
        while True:
            match = re.search(r"\buse\s+", content[index:])
            if not match:
                break
            start = index + match.end()
            cursor = start
            depth = 0
            while cursor < len(content):
                char = content[cursor]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                elif char == ";" and depth == 0:
                    statements.append(content[start:cursor])
                    cursor += 1
                    break
                cursor += 1
            index = cursor
        return statements

    def _walk_rust_use_tree(self, text: str, prefix: list[str], aliases: dict[str, str]) -> None:
        text = text.strip()
        if not text:
            return

        brace_index = self._top_level_char(text, "{")
        if brace_index >= 0:
            base = text[:brace_index].strip().rstrip(":")
            inner_end = text.rfind("}")
            inner = text[brace_index + 1:inner_end if inner_end >= 0 else len(text)]
            next_prefix = prefix + self._rust_path_parts(base)
            for item in self._split_rust_use_items(inner):
                self._walk_rust_use_tree(item, next_prefix, aliases)
            return

        for item in self._split_rust_use_items(text):
            item = item.strip()
            if not item:
                continue
            alias_match = re.match(r"(.+?)\s+as\s+([A-Za-z_]\w*)$", item)
            if alias_match:
                path_text = alias_match.group(1).strip()
                local_name = alias_match.group(2)
            else:
                path_text = item
                local_name = path_text.split("::")[-1].strip()

            if local_name in {"self", "super", "crate", "*"}:
                continue
            full_parts = prefix + self._rust_path_parts(path_text)
            if full_parts:
                aliases[local_name] = ".".join(full_parts)

    def _rust_path_parts(self, path_text: str) -> list[str]:
        return [
            part.strip()
            for part in path_text.strip().strip(":").split("::")
            if part.strip() and part.strip() not in {"self", "*"}
        ]

    def _split_rust_use_items(self, text: str) -> list[str]:
        items: list[str] = []
        start = 0
        depth = 0
        for index, char in enumerate(text):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            elif char == "," and depth == 0:
                item = text[start:index].strip()
                if item:
                    items.append(item)
                start = index + 1
        tail = text[start:].strip()
        if tail:
            items.append(tail)
        return items

    def _top_level_char(self, text: str, needle: str) -> int:
        depth = 0
        for index, char in enumerate(text):
            if char == needle and depth == 0:
                return index
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        return -1

    def _api_variants(self, old_api: str, aliases: dict[str, str]) -> set[str]:
        variants = {old_api, old_api.replace(".", "::")}
        if "." in old_api:
            variants.add(old_api.replace(".", "/"))
            final_segment = old_api.split(".")[-1]
            if len(final_segment) > 2:
                variants.add(final_segment)

        for alias, real_name in aliases.items():
            if old_api == real_name:
                variants.add(alias)
            elif old_api.startswith(f"{real_name}."):
                variants.add(f"{alias}{old_api[len(real_name):]}")
            elif old_api.startswith(f"{real_name}::"):
                variants.add(f"{alias}{old_api[len(real_name):]}")

        return {variant for variant in variants if variant}

    def _find_variant_matches(self, content: str, variant: str):
        escaped = re.escape(variant)
        return re.finditer(rf"(?<![\w$]){escaped}(?![\w$])", content)

    def _is_ignored_offset(self, offset: int, ignored_ranges: list[tuple[int, int]]) -> bool:
        return any(start <= offset < end for start, end in ignored_ranges)

    def _line_col(self, content: str, offset: int) -> tuple[int, int]:
        line = content.count("\n", 0, offset) + 1
        last_newline = content.rfind("\n", 0, offset)
        col = offset if last_newline < 0 else offset - last_newline - 1
        return line, col
