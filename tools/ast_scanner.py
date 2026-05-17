import logging
import re
from collections import defaultdict
from importlib import metadata as importlib_metadata
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

    KNOWN_IMPORT_ALIASES = {
        "PyJWT": {"jwt"},
        "PyYAML": {"yaml"},
        "scikit-learn": {"sklearn"},
        "beautifulsoup4": {"bs4"},
        "Pillow": {"PIL"},
        "python-dotenv": {"dotenv"},
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
        package_roots = self._package_import_roots(package_name)

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
            all_used_apis.update(self._find_package_usages(content, package_name, package_roots, aliases, ignored_ranges))

        return sorted(all_used_apis)

    def find_api_usage_contexts(self, root_folder: str, package_name: str, limit: int = 40) -> list[dict]:
        """Return compact code snippets for package API usages.

        Scout uses these snippets to build better retrieval terms without asking
        an LLM to guess which APIs are deprecated.
        """
        usages = self.find_api_usages(root_folder, package_name)
        if not usages:
            return []

        synthetic_changes = [
            {"old_api": usage, "new_api": "", "description": "", "type": "usage_context"}
            for usage in usages
        ]
        scan_result = self.scan(root_folder, synthetic_changes)
        contexts = []
        for filepath, matches in scan_result.get("matches_by_file", {}).items():
            try:
                lines = self._read_text(Path(filepath)).splitlines()
            except OSError:
                continue
            for match in matches:
                line = int(match.get("line") or 0)
                if line <= 0:
                    continue
                start = max(1, line - 2)
                end = min(len(lines), line + 2)
                snippet = "\n".join(lines[start - 1:end])
                contexts.append({
                    "file": filepath,
                    "line": line,
                    "api": match.get("old_api", ""),
                    "matched_text": match.get("matched_text", ""),
                    "code_snippet": match.get("code_snippet", ""),
                    "context": snippet,
                })
                if len(contexts) >= limit:
                    return contexts
        return contexts

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
            encoded = content.encode("utf-8")
            tree = parser.parse(encoded)
        except Exception:
            return []

        def byte_to_char(byte_offset: int) -> int:
            return len(encoded[:byte_offset].decode("utf-8", errors="ignore"))

        def walk(node: Any) -> None:
            node_type = getattr(node, "type", "").lower()
            if node_type in self.COMMENT_OR_STRING_TYPES or "comment" in node_type:
                ignored.append((byte_to_char(node.start_byte), byte_to_char(node.end_byte)))
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
            for match in self._find_dataflow_api_matches(content, old_api, aliases, ignored_ranges):
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
        package_roots: set[str],
        aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> set[str]:
        if not package_name:
            return set()

        usages = set()
        for package_root in package_roots:
            escaped = re.escape(package_root)
            for match in re.finditer(rf"(?<![\w$]){escaped}(?:[.:/][A-Za-z_$][\w$-]*)*", content):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                usages.add(match.group(0))

        for alias, real_name in aliases.items():
            if self._real_name_matches_package(real_name, package_name, package_roots):
                usages.add(real_name)
                for match in re.finditer(rf"(?<![\w$]){re.escape(alias)}((?:\.[A-Za-z_$][\w$]*)+)", content):
                    if self._is_ignored_offset(match.start(), ignored_ranges):
                        continue
                    usages.add(f"{real_name}{match.group(1)}")

        usages.update(self._find_alias_dataflow_usages(content, package_name, package_roots, aliases, ignored_ranges))
        return usages

    def _package_import_roots(self, package_name: str) -> set[str]:
        roots = {
            package_name,
            package_name.replace("-", "_"),
            package_name.replace("_", "-"),
        }
        package_norm = self._normalize_package_token(package_name)
        for known_name, aliases in self.KNOWN_IMPORT_ALIASES.items():
            if self._normalize_package_token(known_name) == package_norm:
                roots.update(aliases)
        roots.update(self._distribution_import_roots(package_name))
        return {root for root in roots if root}

    def _distribution_import_roots(self, package_name: str) -> set[str]:
        """Read generic distribution -> import root metadata when available.

        Python distribution names and import package names do not always match.
        Wheels commonly expose that mapping through top_level.txt, so using it
        lets DepGuard resolve packages like any other Python tool would without
        carrying package-specific aliases.
        """
        roots: set[str] = set()
        try:
            distribution = importlib_metadata.distribution(package_name)
        except importlib_metadata.PackageNotFoundError:
            return roots
        except Exception as exc:
            logger.debug("Could not inspect distribution metadata for %s: %s", package_name, exc)
            return roots

        try:
            top_level = distribution.read_text("top_level.txt") or ""
        except Exception as exc:
            logger.debug("Could not read top_level.txt for %s: %s", package_name, exc)
            top_level = ""

        for line in top_level.splitlines():
            root = line.strip()
            if re.match(r"^[A-Za-z_][\w$-]*$", root):
                roots.add(root)
        return roots

    def _real_name_matches_package(
        self,
        real_name: str,
        package_name: str,
        package_roots: set[str],
    ) -> bool:
        real_root = re.split(r"[./:]+", real_name, maxsplit=1)[0]
        if self._real_name_has_known_root(real_name, package_roots):
            return True

        root_norm = self._normalize_package_token(real_root)
        package_norm = self._normalize_package_token(package_name)
        if len(root_norm) < 3 or not package_norm:
            return False
        return package_norm.startswith(root_norm)

    def _real_name_has_known_root(self, real_name: str, package_roots: set[str]) -> bool:
        real_root = re.split(r"[./:]+", real_name, maxsplit=1)[0]
        real_root_norm = self._normalize_package_token(real_root)
        for root in package_roots:
            if (
                real_name == root
                or real_name.startswith(f"{root}.")
                or real_name.startswith(f"{root}/")
                or real_name.startswith(f"{root}::")
            ):
                return True
            if real_root_norm and real_root_norm == self._normalize_package_token(root):
                return True
        return False

    def _normalize_package_token(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    def _find_alias_dataflow_usages(
        self,
        content: str,
        package_name: str,
        package_roots: set[str],
        aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> set[str]:
        """Infer package type method usages from simple local assignments.

        This keeps migration-review targets specific. For example:
            import package as pkg
            value = pkg.Type(...)
            value.method(...)

        becomes:
            package.Type
            package.Type.method

        The inference is intentionally conservative and lexical: it handles
        straightforward constructor assignments and reassignment chains without
        pretending to be a full type checker.
        """
        package_aliases = {root: root for root in package_roots}
        for alias, real_name in aliases.items():
            if self._real_name_matches_package(real_name, package_name, package_roots):
                package_aliases[alias] = real_name

        usages: set[str] = set()
        class_bases = self._infer_package_class_bases(
            content,
            package_aliases,
            ignored_ranges,
        )
        usages.update(class_bases.values())

        variable_types = self._infer_package_variable_types(
            content,
            package_roots,
            package_aliases,
            class_bases,
            ignored_ranges,
        )
        usages.update(variable_types.values())

        for class_name, resolved_base in class_bases.items():
            for match in re.finditer(rf"(?<![\w$]){re.escape(class_name)}\.([A-Za-z_$][\w$]*)\s*\(", content):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                usages.add(f"{resolved_base}.{match.group(1)}")

        for variable, resolved_type in variable_types.items():
            for match in re.finditer(rf"(?<![\w$]){re.escape(variable)}\.([A-Za-z_$][\w$]*)\s*\(", content):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                usages.add(f"{resolved_type}.{match.group(1)}")

        for alias, real_name in package_aliases.items():
            alias_pattern = re.escape(alias)
            for match in re.finditer(
                rf"(?<![\w$]){alias_pattern}\.([A-Z][\w$]*)\s*\([^)]*\)\.([A-Za-z_$][\w$]*)\s*\(",
                content,
                re.DOTALL,
            ):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                usages.add(f"{real_name}.{match.group(1)}.{match.group(2)}")

        return usages

    def _infer_package_class_bases(
        self,
        content: str,
        package_aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> dict[str, str]:
        class_bases: dict[str, str] = {}
        class_pattern = re.compile(
            r"(?m)^\s*class\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*:"
        )

        for match in class_pattern.finditer(content):
            if self._is_ignored_offset(match.start(), ignored_ranges):
                continue
            class_name = match.group(1)
            bases = self._split_top_level_commas(match.group(2))
            for base in bases:
                resolved = self._resolve_package_expression(base.strip(), package_aliases, class_bases)
                if resolved:
                    class_bases[class_name] = resolved
                    break

        for _ in range(3):
            changed = False
            for class_name, resolved_base in list(class_bases.items()):
                chained = class_bases.get(resolved_base)
                if chained and chained != resolved_base:
                    class_bases[class_name] = chained
                    changed = True
            if not changed:
                break

        return class_bases

    def _resolve_package_expression(
        self,
        expression: str,
        package_aliases: dict[str, str],
        class_bases: dict[str, str] | None = None,
    ) -> str:
        expression = expression.strip()
        expression = re.sub(r"\[.*\]$", "", expression).strip()
        expression = expression.split("(", 1)[0].strip()
        if not expression:
            return ""

        class_bases = class_bases or {}
        if expression in class_bases:
            return class_bases[expression]
        if expression in package_aliases:
            return package_aliases[expression]

        for alias, real_name in package_aliases.items():
            if expression.startswith(f"{alias}."):
                return f"{real_name}{expression[len(alias):]}"
            if expression.startswith(f"{alias}::"):
                return f"{real_name}{expression[len(alias):]}"
        return ""

    def _infer_package_variable_types(
        self,
        content: str,
        package_roots: set[str],
        package_aliases: dict[str, str],
        class_bases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> dict[str, str]:
        variable_types: dict[str, str] = {}
        assignment_patterns: list[tuple[str, str, str]] = []
        assignment_target = r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"

        for alias, real_name in package_aliases.items():
            alias_pattern = re.escape(alias)
            assignment_patterns.append((
                rf"(?<![\w$.]){assignment_target}\s*=\s*{alias_pattern}\.([A-Z][\w$]*)\s*\(",
                f"{real_name}.{{constructor}}",
                "",
            ))
            if self._real_name_has_known_root(real_name, package_roots):
                assignment_patterns.append((
                    rf"(?<![\w$.]){assignment_target}\s*=\s*{alias_pattern}\s*\(",
                    real_name,
                    real_name.rsplit(".", 1)[-1],
                ))
        for class_name, resolved_base in class_bases.items():
            class_pattern = re.escape(class_name)
            assignment_patterns.append((
                rf"(?<![\w$.]){assignment_target}\s*=\s*{class_pattern}\s*\(",
                resolved_base,
                "",
            ))
            assignment_patterns.append((
                rf"(?<![\w$.]){assignment_target}\s*=\s*{class_pattern}\.[A-Za-z_$][\w$]*\s*\(",
                resolved_base,
                "",
            ))

        for pattern, type_template, callable_name in assignment_patterns:
            for match in re.finditer(pattern, content):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                variable = match.group(1)
                if callable_name and not self._assignment_target_matches_callable(variable, callable_name):
                    continue
                constructor = match.group(2) if "{constructor}" in type_template else ""
                variable_types[variable] = type_template.format(constructor=constructor)

        variable_types.update(
            self._infer_annotated_package_variable_types(
                content,
                package_aliases,
                class_bases,
                ignored_ranges,
            )
        )

        # Follow simple reassignment chains such as result = df.
        for _ in range(3):
            changed = False
            for match in re.finditer(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\b(?!\s*[.(])", content):
                if self._is_ignored_offset(match.start(), ignored_ranges):
                    continue
                target, source = match.group(1), match.group(2)
                if source in variable_types and variable_types.get(target) != variable_types[source]:
                    variable_types[target] = variable_types[source]
                    changed = True
            if not changed:
                break

        return variable_types

    def _infer_annotated_package_variable_types(
        self,
        content: str,
        package_aliases: dict[str, str],
        class_bases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ) -> dict[str, str]:
        variable_types: dict[str, str] = {}

        for match in re.finditer(r"(?m)^\s*def\s+[A-Za-z_$][\w$]*\s*\((.*?)\)\s*(?:->[^:]+)?\s*:", content, re.DOTALL):
            if self._is_ignored_offset(match.start(), ignored_ranges):
                continue
            for param in self._split_top_level_commas(match.group(1)):
                param = param.strip()
                if ":" not in param:
                    continue
                name, annotation = param.split(":", 1)
                name = name.strip().lstrip("*")
                annotation = annotation.split("=", 1)[0].strip()
                if not re.match(r"^[A-Za-z_$][\w$]*$", name):
                    continue
                resolved = self._resolve_package_expression(annotation, package_aliases, class_bases)
                if resolved:
                    variable_types[name] = resolved

        for match in re.finditer(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*:\s*([^=\n]+)(?:=.*)?$", content):
            if self._is_ignored_offset(match.start(), ignored_ranges):
                continue
            resolved = self._resolve_package_expression(match.group(2).strip(), package_aliases, class_bases)
            if resolved:
                variable_types[match.group(1)] = resolved

        return variable_types

    def _assignment_target_matches_callable(self, variable: str, callable_name: str) -> bool:
        """Keep lowercase factory dataflow useful without treating all returns as package objects."""
        callable_leaf = callable_name.rsplit(".", 1)[-1]
        # Class instantiations (PascalCase callables) always produce an instance — track them
        # regardless of how the variable is named (e.g. app = Flask(), engine = SQLAlchemy()).
        if callable_leaf and callable_leaf[0].isupper():
            return True
        variable_leaf = variable.rsplit(".", 1)[-1].lower()
        callable_leaf_lower = callable_leaf.lower()
        callable_terms = {term for term in re.split(r"[_\W]+", callable_leaf_lower) if term}
        if not variable_leaf or not callable_terms:
            return False
        if variable_leaf in callable_terms:
            return True
        return any(
            len(term) >= 4 and (variable_leaf.endswith(term) or term.endswith(variable_leaf))
            for term in callable_terms
        )

    def _package_aliases_for_old_api(self, old_api: str, aliases: dict[str, str]) -> tuple[str, dict[str, str]]:
        package_name = re.split(r"[./:]", old_api, maxsplit=1)[0]
        package_aliases = {package_name: package_name}

        for alias, real_name in aliases.items():
            if real_name == package_name or real_name.startswith(f"{package_name}.") or real_name.startswith(f"{package_name}/"):
                package_aliases[alias] = real_name

        return package_name, package_aliases

    def _find_dataflow_api_matches(
        self,
        content: str,
        old_api: str,
        aliases: dict[str, str],
        ignored_ranges: list[tuple[int, int]],
    ):
        if "." not in old_api:
            return []

        parts = old_api.split(".")
        if len(parts) < 3:
            return []

        type_path = ".".join(parts[:-1])
        method_name = parts[-1]
        package_name, package_aliases = self._package_aliases_for_old_api(old_api, aliases)
        variable_types = self._infer_package_variable_types(
            content,
            {package_name},
            package_aliases,
            self._infer_package_class_bases(content, package_aliases, ignored_ranges),
            ignored_ranges,
        )

        matches = []
        for variable, resolved_type in variable_types.items():
            if resolved_type != type_path:
                continue
            # Method call: variable.method(...)
            pattern = rf"(?<![\w$]){re.escape(variable)}\.{re.escape(method_name)}\s*\("
            matches.extend(re.finditer(pattern, content))
            # Attribute/decorator access without call parens (e.g. @app.before_first_request)
            attr_pattern = rf"(?<![\w$]){re.escape(variable)}\.{re.escape(method_name)}(?![\w$\(])"
            matches.extend(re.finditer(attr_pattern, content))

        class_bases = self._infer_package_class_bases(content, package_aliases, ignored_ranges)
        for class_name, resolved_base in class_bases.items():
            if resolved_base != type_path:
                continue
            pattern = rf"(?<![\w$]){re.escape(class_name)}\.{re.escape(method_name)}\s*\("
            matches.extend(re.finditer(pattern, content))
            attr_pattern = rf"(?<![\w$]){re.escape(class_name)}\.{re.escape(method_name)}(?![\w$\(])"
            matches.extend(re.finditer(attr_pattern, content))

        constructor = parts[-2]
        for alias, real_name in package_aliases.items():
            if type_path != f"{real_name}.{constructor}":
                continue
            pattern = (
                rf"(?<![\w$]){re.escape(alias)}\.{re.escape(constructor)}"
                rf"\s*\([^)]*\)\.{re.escape(method_name)}\s*\("
            )
            matches.extend(re.finditer(pattern, content, re.DOTALL))

        return matches

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

    def _split_top_level_commas(self, text: str) -> list[str]:
        items: list[str] = []
        start = 0
        depth = 0
        pairs = {"(": ")", "[": "]", "{": "}"}
        closing = {")": "(", "]": "[", "}": "{"}
        stack: list[str] = []
        for index, char in enumerate(text):
            if char in pairs:
                stack.append(char)
                depth += 1
            elif char in closing and stack and stack[-1] == closing[char]:
                stack.pop()
                depth = max(0, depth - 1)
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
