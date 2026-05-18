import os
import re
import json
import asyncio
import logging
import argparse
import subprocess
import ast
import builtins
import inspect
import textwrap
import uuid
import sys
import time
from pathlib import Path
from typing import Dict, List, Any
from dotenv import load_dotenv

try:
    from tools.llm_router import LLMRouter
except ImportError:
    print("llm_router is required. Ensure you are running from the project root.")
    sys.exit(1)

try:
    from tools.impact_graph import ImpactFinder
except ImportError:
    ImpactFinder = None

try:
    from tools.ast_scanner import ASTScanner as TreeSitterScanner
except ImportError:
    TreeSitterScanner = None

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class PatchResponseError(ValueError):
    """Raised when an LLM patch response violates DepGuard's patch contract."""


# -------------------------------- Patch Agent ----------------------------------
class PatchAgent:
    def __init__(self, project_root: str | None = None):
        self.router = LLMRouter()
        self.project_root = project_root or os.getcwd()
        self._impact_finder = None
        self.syntax_scanner = TreeSitterScanner() if TreeSitterScanner else None
        self.target_block_max_lines = self._env_int("PATCH_TARGET_MAX_LINES", 140)
        self.target_block_max_chars = self._env_int("PATCH_TARGET_MAX_CHARS", 14000)
        self.impact_context_max_chars = self._env_int("PATCH_IMPACT_CONTEXT_MAX_CHARS", 6000)
        self.import_context_max_lines = self._env_int("PATCH_IMPORT_CONTEXT_MAX_LINES", 80)
        self.module_level_max_lines = self._env_int("PATCH_MODULE_LEVEL_MAX_LINES", 90)
        self.llm_response_max_attempts = max(1, self._env_int("PATCH_LLM_RESPONSE_MAX_ATTEMPTS", 2))
        self.retry_response_max_chars = self._env_int("PATCH_RETRY_RESPONSE_MAX_CHARS", 6000)

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    def _get_changed_lines(self, matches: list) -> list[int]:
        return sorted({
            match.get("line")
            for match in matches
            if isinstance(match.get("line"), int) and match.get("line") > 0
        })

    def _get_impact_result(self, filepath: str, matches: list):
        if ImpactFinder is None:
            return None

        changed_lines = self._get_changed_lines(matches)
        if not changed_lines:
            return None

        try:
            if self._impact_finder is None:
                self._impact_finder = ImpactFinder(self.project_root)
            return self._impact_finder.find_impact(filepath, changed_lines, max_depth=2)
        except Exception as e:
            logger.debug(f"Could not build impact context for {filepath}: {e}")
            return None

    def _get_impact_context(self, filepath: str, matches: list) -> str:
        impact = self._get_impact_result(filepath, matches)
        if impact:
            return impact.to_llm_context()
        return ""

    def _line_window_block(self, filepath: str, lines: list[str], line: int, radius: int = 18) -> dict:
        start_line = max(1, line - radius)
        end_line = min(len(lines), line + radius)
        return {
            "file": filepath,
            "start_line": start_line,
            "end_line": end_line,
            "context_type": "line_window",
            "name": None,
            "parent": None,
            "source": "\n".join(lines[start_line - 1:end_line]),
        }

    def _bounded_node_block(self, filepath: str, lines: list[str], line: int, location) -> dict:
        source = location.source
        line_count = location.end_line - location.start_line + 1
        if location.context_type == "module_level" and line_count > self.module_level_max_lines:
            radius = max(18, min(45, self.module_level_max_lines // 2))
            block = self._line_window_block(filepath, lines, line, radius=radius)
            block["context_type"] = "module_level_slice"
            block["name"] = location.name
            block["parent"] = location.parent
            return block

        if line_count > self.target_block_max_lines or len(source) > self.target_block_max_chars:
            radius = max(12, min(45, self.target_block_max_lines // 2))
            block = self._line_window_block(filepath, lines, line, radius=radius)
            block["context_type"] = f"{location.context_type}_slice"
            block["name"] = location.name
            block["parent"] = location.parent
            return block

        return {
            "file": filepath,
            "start_line": location.start_line,
            "end_line": location.end_line,
            "context_type": location.context_type,
            "name": location.name,
            "parent": location.parent,
            "source": source,
        }

    def _target_blocks(self, filepath: str, matches: list, lines: list[str], impact_result) -> list[dict]:
        blocks_by_range: dict[tuple[int, int], dict] = {}

        for line in self._get_changed_lines(matches):
            node = None
            if self._impact_finder:
                try:
                    node = self._impact_finder.get_node_at_line(filepath, line)
                except Exception:
                    node = None

            if node:
                location = node.location
                block = self._bounded_node_block(filepath, lines, line, location)
                blocks_by_range[(block["start_line"], block["end_line"])] = block
            else:
                block = self._line_window_block(filepath, lines, line)
                blocks_by_range[(block["start_line"], block["end_line"])] = block

        if not blocks_by_range and impact_result:
            for node in impact_result.changed_nodes:
                location = node.location
                block = self._bounded_node_block(filepath, lines, location.start_line, location)
                blocks_by_range[(block["start_line"], block["end_line"])] = block

        return self._merge_overlapping_target_blocks(
            [blocks_by_range[key] for key in sorted(blocks_by_range)],
            filepath,
            lines,
        )

    def _merge_overlapping_target_blocks(self, blocks: list[dict], filepath: str, lines: list[str]) -> list[dict]:
        if not blocks:
            return []

        merged: list[dict] = []
        for block in sorted(blocks, key=lambda item: (item["start_line"], item["end_line"])):
            if not merged:
                merged.append(block)
                continue

            previous = merged[-1]
            if block["start_line"] <= previous["end_line"]:
                previous["end_line"] = max(previous["end_line"], block["end_line"])
                previous["start_line"] = min(previous["start_line"], block["start_line"])
                previous["source"] = "\n".join(lines[previous["start_line"] - 1:previous["end_line"]])
                previous["context_type"] = self._merged_context_type(previous, block)
                previous["name"] = previous.get("name") if previous.get("name") == block.get("name") else None
                previous["parent"] = previous.get("parent") if previous.get("parent") == block.get("parent") else None
                previous["file"] = filepath
            else:
                merged.append(block)

        return merged

    def _merged_context_type(self, left: dict, right: dict) -> str:
        left_type = str(left.get("context_type") or "")
        right_type = str(right.get("context_type") or "")
        if left_type == right_type:
            return left_type
        if "module_level" in left_type or "module_level" in right_type:
            return "module_level_slice"
        return "merged_block"

    def _top_level_context(self, lines: list[str], max_lines: int = 80) -> str:
        context_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                context_lines.append(line)
                continue
            if stripped.startswith((
                "import ", "from ", "use ", "mod ", "pub mod ", "package ",
                "#![", "#[", "extern crate ", "using ", "namespace ",
            )):
                context_lines.append(line)
                continue
            if context_lines and len(context_lines) < max_lines:
                context_lines.append(line)
            break

        return "\n".join(context_lines[:max_lines]).rstrip()

    def _compact_impact_context(self, impact_result, current_file: str, max_chars: int | None = None) -> str:
        if not impact_result:
            return ""

        max_chars = max_chars or self.impact_context_max_chars
        current_path = Path(current_file).resolve()
        parts = [impact_result.summary]

        def add_node(prefix: str, node, reason: str = "", attrs: list[str] | None = None) -> None:
            location = node.location
            node_file = self._absolute_project_file(location.file)
            try:
                display_file = Path(node_file).resolve().relative_to(Path(self.project_root).resolve()).as_posix()
            except ValueError:
                display_file = location.file
            header = (
                f"{prefix}: {display_file}:{location.start_line}-{location.end_line} "
                f"{location.context_type} {location.name or 'module level'}"
            )
            if reason:
                header += f" | reason: {reason}"
            if attrs:
                header += f" | attrs: {', '.join(attrs)}"
            parts.append(header)

            # The current file receives full target blocks separately. For other
            # related files, include only a compact excerpt because those files
            # are expanded into their own LLM review targets when needed.
            if Path(node_file).resolve() != current_path:
                excerpt = "\n".join(location.source.splitlines()[:8])
                if excerpt:
                    parts.append(excerpt)

        for node in impact_result.changed_nodes:
            add_node("Changed", node)

        for impacted in impact_result.impacted_nodes:
            add_node(
                f"Related depth {impacted.depth}",
                impacted.node,
                impacted.impact_reason,
                impacted.affected_attributes,
            )
            if len("\n".join(parts)) > max_chars:
                parts.append("... impact context truncated; additional related nodes are reviewed as separate targets.")
                break

        return "\n".join(parts)[:max_chars]

    def _build_sliced_patch_prompt(
        self,
        filepath: str,
        matches: list,
        scout_context: dict,
        original_content: str,
    ) -> tuple[str, str, list[dict]]:
        lines = original_content.splitlines()
        impact_result = self._get_impact_result(filepath, matches)
        impact_context = self._compact_impact_context(impact_result, filepath)
        target_blocks = self._target_blocks(filepath, matches, lines, impact_result)
        code_language = self._code_fence_language(filepath)
        compact_matches = [
            {
                "line": match.get("line"),
                "old_api": match.get("old_api"),
                "new_api": match.get("new_api"),
                "type": match.get("type"),
                "description": match.get("description"),
            }
            for match in matches[:16]
        ]
        target_source = "\n".join(b.get("source", "") for b in target_blocks)
        compact_breaking_changes = [
            {
                **bc,
                "parameters_changed": [
                    p for p in (bc.get("parameters_changed") or [])
                    if re.search(rf'\b{re.escape(str(p.get("old_param", "") or ""))}\s*=', target_source)
                ] or None,
            }
            for bc in scout_context.get("breaking_changes", [])[:16]
        ]
        # Strip None parameters_changed to keep prompt compact
        compact_breaking_changes = [
            {k: v for k, v in bc.items() if v is not None} for bc in compact_breaking_changes
        ]
        matches_str = json.dumps(compact_matches, separators=(",", ":"))
        bc_str = json.dumps(compact_breaking_changes, separators=(",", ":"))
        api_evidence_str = self._format_api_evidence(scout_context.get("api_evidence", []))
        api_semantics_str = self._format_api_semantics(scout_context.get("api_semantics", []))
        references_str = self._format_scout_references(
            scout_context.get("evidence_references") or scout_context.get("references", [])
        )
        blocks_str = json.dumps(
            [
                {
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "context_type": block.get("context_type"),
                    "name": block.get("name"),
                    "parent": block.get("parent"),
                    "source": block["source"],
                }
                for block in target_blocks
            ],
            separators=(",", ":"),
        )
        import_context = self._top_level_context(lines, max_lines=self.import_context_max_lines)
        migration_obligations = self._format_migration_obligations(
            target_blocks,
            scout_context.get("api_evidence", []),
            scout_context.get("evidence_references") or scout_context.get("references", []),
        )
        response_schema = json.dumps(self._patch_response_schema(), indent=2)
        allowed_ranges = [
            {
                "start_line": block["start_line"],
                "end_line": block["end_line"],
            }
            for block in target_blocks
        ]
        llm_prior_fallback = self._allows_llm_prior_fallback(scout_context)
        evidence_policy = (
            "Scout did not find usable external migration evidence for this fallback path. "
            "You may use your general dependency migration knowledge, but only for the exact matched APIs and version range, "
            "and only when the code change is clear from the shown source. If uncertain, return no_change."
            if llm_prior_fallback else
            "Prefer documented Scout evidence over model memory. When new_api is empty, only infer a migration if the attached evidence or source block makes the replacement unambiguous. "
            "For migration_review items, do not patch from model memory; return no_change unless Scout evidence documents a concrete required edit."
        )
        review_instruction = (
            "- This is an explicit no-evidence migration fallback: you may use general package migration knowledge for the exact matched API only, but return no_change if the required edit is not clear."
            if llm_prior_fallback else
            "- For migration_review matches with no structured API evidence, return no_change with replacements []."
        )

        system_prompt = (
            "You are DepGuard's deterministic code migration patch engine.\n"
            "Your job is to produce a machine-parseable patch plan, not a narrative.\n"
            "Use the package/version migration context, matched API usages, source code, and DepGuard API evidence to decide whether a code edit is needed.\n"
            f"{evidence_policy}\n"
            "Do not make unrelated stylistic rewrites or speculative edits outside the matched migration surface.\n"
            "Patch only exact target ranges supplied by DepGuard. Never return line-level edits inside a larger target block.\n"
            "Preserve unrelated code, behavior, formatting, comments, imports, and indentation unless the migration evidence requires a change. "
            "A replacement for a larger target range must keep every unchanged line in that range.\n"
            "If the top-level import area is outside the editable range and a migrated call needs a helper, inject the import locally inside the edited scope. "
            "Example: if replacing engine.execute(\"SELECT 1\") with connection.execute(text(\"SELECT 1\")) and top-level imports are blocked, write `def get_user_count(db_url):\\n    from sqlalchemy import text\\n    ...`.\n"
            "IMPORTANT — keyword argument migration via parameters_changed: "
            "Each breaking change may carry a 'parameters_changed' list. For each entry in that list: "
            "(1) Search the original source code for the exact text '<old_param>=' (e.g. 'always='). "
            "(2) Only if that exact text appears in the matched call, remove it and apply the replacement. "
            "(3) If the text does not appear, skip the entry — do NOT add the replacement argument. "
            "Never invent or add a keyword argument that was not already present in the original code.\n"
            "IMPORTANT — never insert a bare `import` or `from X import Y` statement at an indented level (inside a function or class body). "
            "If a new import is needed and the module-level import block is outside the target range, either (a) add a local import at the start of the enclosing function body, or (b) if the symbol is already imported at module level per the Import Context, do not add it again.\n"
            "If you migrate an API call that binds its return value to a variable, and evidence or the package migration context shows the library shifted from dictionaries to Pydantic/object models, audit the entire target block for downstream uses of that variable and convert dictionary lookups like `response['choices'][0]['message']['content']` into attribute access like `response.choices[0].message.content`.\n"
            "If evidence is insufficient, or no target block needs a change, return status no_change with an empty replacements array.\n"
            "Return one JSON object only. Do not return markdown, prose, code fences, or explanations outside JSON.\n"
            "The JSON object must follow this schema:\n"
            f"{response_schema}"
        )

        prompt = f"""
            Package Migration Context: {scout_context.get("package")} {scout_context.get("from_version")} -> {scout_context.get("to_version")}
            Breaking Changes:
            {bc_str}

            DepGuard API Evidence:
            {api_evidence_str}

            DepGuard Old API Semantics:
            {api_semantics_str}

            DepGuard Scout References:
            {references_str}

            Evidence-Derived Patch Obligations:
            {migration_obligations}

            File: {filepath}
            Matches to fix:
            {matches_str}

            Top-level imports/module context:
            ```{code_language}
            {import_context or "(none)"}
            ```

            TARGET CODE BLOCKS TO PATCH:
            {blocks_str}

            Related Tree-sitter/LSP Impact Context:
            {impact_context or "No related impact context available."}

            DepGuard Patch Contract:
            - allowed_ranges: {json.dumps(allowed_ranges, separators=(",", ":"))}
            - schema_version must be "depguard.patch.v1".
            - status must be "patched" only when at least one replacement changes code.
            - status must be "no_change" when replacements is empty.
            - Each replacement start_line/end_line must exactly match one allowed range.
            - Each replacement must contain the complete replacement source for that whole allowed range.
            - Do not include unchanged target blocks in replacements.
            - If a necessary edit is outside the allowed ranges, return no_change and explain that in target_decisions.

            Instructions:
            - Return replacements for the target blocks only.
            - Keep each replacement as complete code for the exact start_line/end_line range.
            - Valid replacement ranges are: {[(block["start_line"], block["end_line"]) for block in target_blocks]}.
            - Do not return smaller line edits inside a target block.
            - Preserve every unrelated line inside each returned target block; if only an import or decorator changes, return the full block with the rest intact.
            - If new_api is empty, infer the migration only when the evidence/fallback policy above permits it and the replacement is clear; otherwise return no_change.
            {review_instruction}
            - Apply all coupled requirements documented by the same evidence chunk for the matched expression, including required argument wrappers/conversions and transaction/context-manager changes.
            - If a helper/import is required but import lines are outside the allowed ranges, prefer a local import inside the edited target block when that is valid for the language; otherwise return no_change and explain why.
            - Local import few-shot: def get_user_count(db_url):\n    from sqlalchemy import text\n    ...
            - If a migrated call's assigned return object is used later in the target block, update coupled downstream access patterns documented by the migration evidence or explicit fallback policy, including dictionary-to-object response access when a library moved to object/Pydantic responses.
            - Include all necessary edits within those ranges if related code in the same block must change.
            - If a target block does not require a code change, omit it from replacements.
            - If no target block requires a code change, return status no_change with replacements [].
            - Do not return original unchanged blocks.
            - Do not return markdown.
            """
        return system_prompt, prompt, target_blocks

    def _format_api_evidence(self, api_evidence: list, max_chars: int = 10000) -> str:
        if not api_evidence:
            return "No structured API evidence was attached by Scout."
        blocks = []
        for index, item in enumerate(api_evidence[:12], start=1):
            if not isinstance(item, dict):
                continue
            evidence_lines = []
            for evidence in item.get("evidence", [])[:3]:
                if not isinstance(evidence, dict):
                    continue
                quote = str(evidence.get("quote", "") or "").strip()
                evidence_lines.append(
                    " | ".join(filter(None, [
                        str(evidence.get("source", "") or ""),
                        str(evidence.get("url", "") or ""),
                        quote[:500],
                    ]))
                )
            block = [
                f"[{index}] api: {item.get('api', '')}",
                f"change_type: {item.get('change_type', '')}",
                f"replacement: {item.get('replacement', '')}",
                f"confidence: {item.get('confidence', '')}",
                f"reason: {item.get('reason', '')}",
            ]
            if evidence_lines:
                block.extend(["evidence:", "\n".join(evidence_lines)])
            blocks.append("\n".join(block))
            if len("\n\n".join(blocks)) > max_chars:
                blocks.append("... API evidence truncated ...")
                break
        return "\n\n".join(blocks)[:max_chars]

    def _format_api_semantics(self, api_semantics: list, max_chars: int = 8000) -> str:
        if not api_semantics:
            return "No old-version API semantics were attached by Scout."
        blocks = []
        for index, item in enumerate(api_semantics[:12], start=1):
            if not isinstance(item, dict):
                continue
            block = [
                f"[{index}] api: {item.get('api', '')}",
                f"source: {item.get('source', '')}",
                f"confidence: {item.get('confidence', '')}",
                f"purpose: {item.get('purpose', '')}",
                f"behavior: {str(item.get('behavior', '') or '')[:900]}",
                f"parameters: {str(item.get('parameters', '') or '')[:500]}",
                f"returns: {str(item.get('returns', '') or '')[:400]}",
                f"semantic_search_terms: {', '.join(str(term) for term in (item.get('search_terms', []) or [])[:10])}",
            ]
            docs = []
            for doc in item.get("docs", [])[:2]:
                if isinstance(doc, dict):
                    docs.append(" | ".join(filter(None, [
                        str(doc.get("title", "") or ""),
                        str(doc.get("url", "") or ""),
                    ])))
            if docs:
                block.extend(["docs:", "\n".join(docs)])
            blocks.append("\n".join(block))
            if len("\n\n".join(blocks)) > max_chars:
                blocks.append("... API semantics truncated ...")
                break
        return "\n\n".join(blocks)[:max_chars]

    def _format_scout_references(self, references: list, max_chars: int = 12000) -> str:
        if not references:
            return "No external references were attached by Scout."
        blocks = []
        for index, reference in enumerate(references[:12], start=1):
            if not isinstance(reference, dict):
                continue
            content = str(reference.get("content", "") or "").strip()
            block = [
                f"[{index}] {reference.get('title') or reference.get('source') or 'Reference'}",
                f"source: {reference.get('source', '')}",
                f"url: {reference.get('url', '')}",
            ]
            if reference.get("document_kind"):
                block.append(f"document_kind: {reference.get('document_kind')}")
            if reference.get("matched_terms"):
                block.append(f"matched_terms: {', '.join(reference.get('matched_terms', []))}")
            if content:
                block.extend(["excerpt:", content[:2000]])
            blocks.append("\n".join(block))
            if len("\n\n".join(blocks)) > max_chars:
                blocks.append("... references truncated ...")
                break
        return "\n\n".join(blocks)[:max_chars]

    def _format_migration_obligations(
        self,
        target_blocks: list[dict],
        api_evidence: list,
        references: list,
        max_chars: int = 5000,
    ) -> str:
        evidence_text = self._combined_evidence_text(api_evidence, references)
        target_source = "\n\n".join(str(block.get("source", "") or "") for block in target_blocks)
        obligations = []

        string_arg_obligations = self._string_argument_obligations(evidence_text, target_source)
        obligations.extend(string_arg_obligations)

        if self._evidence_mentions_context_manager(evidence_text) and re.search(r"\.\w+\s*\(", target_source):
            obligations.append(
                "Evidence documents a context-manager or explicit connection/transaction style. "
                "When replacing a call target, also preserve resource lifetime and transaction behavior documented in the evidence."
            )

        if not obligations:
            return "No additional coupled obligations detected beyond the listed API replacement evidence."

        deduped = []
        seen = set()
        for obligation in obligations:
            key = obligation.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(f"- {obligation}")
        return "\n".join(deduped)[:max_chars]

    def _combined_evidence_text(self, api_evidence: list, references: list) -> str:
        parts = []
        for item in api_evidence or []:
            if not isinstance(item, dict):
                continue
            parts.extend([
                str(item.get("reason", "") or ""),
                str(item.get("replacement", "") or ""),
                str(item.get("change_type", "") or ""),
            ])
            for evidence in item.get("evidence", []) or []:
                if isinstance(evidence, dict):
                    parts.extend([
                        str(evidence.get("quote", "") or ""),
                        str(evidence.get("url", "") or ""),
                    ])
        for reference in references or []:
            if not isinstance(reference, dict):
                continue
            parts.extend([
                str(reference.get("title", "") or ""),
                str(reference.get("url", "") or ""),
                str(reference.get("content", "") or ""),
            ])
        return "\n".join(part for part in parts if part)

    def _string_argument_obligations(self, evidence_text: str, target_source: str) -> list[str]:
        normalized_evidence = self._normalize_text(evidence_text)
        if not normalized_evidence or not re.search(r"\w+\s*\(\s*(['\"])", target_source):
            return []

        string_arg_markers = [
            "passing a string",
            "string to",
            "raw string",
            "textual sql",
            "textual statement",
            "string statements require",
            "sql string",
        ]
        migration_markers = [
            "deprecated",
            "removed",
            "will be removed",
            "no longer",
            "requires",
            "must",
            "use ",
        ]
        if not any(marker in normalized_evidence for marker in string_arg_markers):
            return []
        if not any(marker in normalized_evidence for marker in migration_markers):
            return []

        helpers = self._documented_string_argument_helpers(evidence_text)
        call_names = self._deprecated_string_argument_call_names(evidence_text)
        snippets = self._string_argument_evidence_snippets(evidence_text)
        helper_text = f" Documented helper/alternative mentions: {', '.join(helpers)}." if helpers else ""
        call_text = f" Affected call names from evidence: {', '.join(sorted(call_names))}." if call_names else ""
        snippet_text = f" Supporting evidence: {self._compact_evidence_sentence(snippets[0])}." if snippets else ""
        return [
            "Target code passes a string literal to a migrated call, and the evidence documents a string-argument migration. "
            "Do not only rename/move the call; also migrate the string argument exactly as documented. "
            "The replacement must not leave the migrated call receiving a bare string literal. "
            "CRITICAL: If the replacement uses any documented helper as a bare function call, it must also make that helper available in scope; "
            "when import lines are outside the allowed range, add the local import inside the target block."
            f"{call_text}{helper_text}{snippet_text}"
        ]

    def _compact_evidence_sentence(self, text: str, max_chars: int = 420) -> str:
        compact = re.sub(r"\s+", " ", text or "").strip()
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars].rstrip() + "..."

    def _documented_string_argument_helpers(self, evidence_text: str) -> list[str]:
        helpers = []
        snippets = self._string_argument_evidence_snippets(evidence_text)
        for pattern in [
            r"\b[Uu]se\s+(?:the\s+)?([A-Za-z_][\w.]*)(?:\(\))?\s+(?:construct|method|function)",
            r"\bor\s+(?:the\s+)?([A-Za-z_][\w.]*)(?:\(\))?\s+(?:construct|method|function)",
            r"\brequire[s]?\s+(?:the\s+)?([A-Za-z_][\w.]*)(?:\(\))?\s+(?:construct|method|function)",
            r"\bunless\s+(?:the\s+)?([A-Za-z_][\w.]*)(?:\(\))?\s+(?:construct|method|function)",
        ]:
            for snippet in snippets:
                for match in re.finditer(pattern, snippet, flags=re.IGNORECASE):
                    helper = self._short_api_name(match.group(1))
                    if helper.lower() in {"use", "method", "function", "construct", "execute"}:
                        continue
                    helpers.append(helper)
        seen = set()
        deduped = []
        for helper in helpers:
            key = helper.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(helper)
        return deduped[:6]

    def _string_argument_evidence_snippets(self, evidence_text: str) -> list[str]:
        cleaned = self._clean_rst_api_roles(evidence_text or "")
        if not cleaned:
            return []

        markers = [
            "passing a string",
            "raw string",
            "textual sql",
            "textual statement",
            "string statements require",
            "sql string",
        ]
        parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
        snippets = []
        for index, part in enumerate(parts):
            normalized = self._normalize_text(part)
            if not any(marker in normalized for marker in markers):
                continue
            window = " ".join(parts[max(0, index - 1):index + 2]).strip()
            if window:
                snippets.append(window)
        return snippets or [cleaned]

    def _clean_rst_api_roles(self, text: str) -> str:
        cleaned = re.sub(r":[A-Za-z_]+:`~?([^`]+)`", r"\1", text or "")
        cleaned = cleaned.replace("``", "").replace("`", "")
        return cleaned

    def _evidence_mentions_context_manager(self, evidence_text: str) -> bool:
        normalized = self._normalize_text(evidence_text)
        return (
            re.search(r"\bwith\s+[^.\n]{0,120}\bas\b", normalized) is not None
            or any(marker in normalized for marker in [
                "context manager",
                "connect() as",
                "begin() as",
                "explicit transaction",
            ])
        )

    def _normalize_text(self, text: str) -> str:
        value = (text or "").lower()
        value = value.replace("``", "")
        value = value.replace("`", "")
        return re.sub(r"\s+", " ", value)

    def _string_argument_migration_policy(self, evidence_text: str, target_source: str) -> dict:
        if not self._string_argument_obligations(evidence_text, target_source):
            return {"required": False, "call_names": []}

        call_names = self._deprecated_string_argument_call_names(evidence_text)
        return {
            "required": bool(call_names),
            "call_names": sorted(call_names),
        }

    def _deprecated_string_argument_call_names(self, evidence_text: str) -> set[str]:
        call_names: set[str] = set()
        snippets = self._string_argument_evidence_snippets(evidence_text)
        patterns = [
            r"\b[Pp]assing\s+a\s+string\s+to\s+(?:the\s+)?([A-Za-z_][\w.]*)(?:\(\))?",
            r"\b([A-Za-z_][\w.]*\.[A-Za-z_]\w*)(?:\(\))?\s+(?:function/method|method|function)\s+is\s+(?:considered\s+)?(?:legacy|deprecated|removed)",
        ]
        for pattern in patterns:
            for snippet in snippets:
                for match in re.finditer(pattern, snippet):
                    dotted_name = self._shortenable_api_name(match.group(1))
                    if not dotted_name:
                        continue
                    call_names.add(dotted_name)
                    call_names.add(dotted_name.rsplit(".", 1)[-1])

        helpers = {helper.lower() for helper in self._documented_string_argument_helpers(evidence_text)}
        return {
            name
            for name in call_names
            if name and name.lower() not in helpers
        }

    def _shortenable_api_name(self, name: str) -> str:
        value = (name or "").strip().strip(".")
        value = re.sub(r"\(\)$", "", value)
        parts = [part for part in value.split(".") if part]
        while parts and parts[0].startswith("_"):
            parts.pop(0)
        return ".".join(parts)

    def _short_api_name(self, name: str) -> str:
        value = self._shortenable_api_name(name)
        return value.rsplit(".", 1)[-1] if value else ""

    def _validate_replacements_against_migration_obligations(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        scout_context: dict | None,
        original_content: str = "",
    ) -> None:
        if not replacements or not scout_context:
            return

        evidence_text = self._combined_evidence_text(
            scout_context.get("api_evidence", []),
            scout_context.get("evidence_references") or scout_context.get("references", []),
        )
        target_source = "\n\n".join(str(block.get("source", "") or "") for block in target_blocks)
        policy = self._string_argument_migration_policy(evidence_text, target_source)
        if not policy.get("required"):
            return

        call_names = set(policy.get("call_names") or [])
        helpers = self._documented_string_argument_helpers(evidence_text)
        blocks_by_range = {
            (block["start_line"], block["end_line"]): block
            for block in target_blocks
        }
        for replacement in replacements:
            block = blocks_by_range.get((replacement["start_line"], replacement["end_line"]))
            if not block:
                continue
            original_source = str(block.get("source", "") or "")
            replacement_source = str(replacement.get("replacement", "") or "")
            if not self._source_has_deprecated_string_call(original_source, call_names):
                continue
            if self._source_has_deprecated_string_call(replacement_source, call_names):
                names = ", ".join(sorted(call_names))
                helper_text = f" Documented helpers/alternatives: {', '.join(helpers)}." if helpers else ""
                raise PatchResponseError(
                    "Replacement leaves a raw string literal passed to a migrated call "
                    f"({names}) even though the evidence requires a string-argument migration."
                    f"{helper_text}"
                )
            missing_helpers = self._missing_bare_helper_imports(
                replacement_source,
                helpers,
                original_content,
            )
            if missing_helpers:
                helper_text = ", ".join(missing_helpers)
                raise PatchResponseError(
                    "Replacement uses documented migration helper(s) as bare function calls without making them available in scope: "
                    f"{helper_text}. Add an import or definition inside the target block when the existing import section is outside the allowed range."
                )

    def _source_has_deprecated_string_call(self, source: str, call_names: set[str]) -> bool:
        if not source or not call_names:
            return False

        try:
            tree = ast.parse(textwrap.dedent(source))
        except SyntaxError:
            return self._text_has_deprecated_string_call(source, call_names)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
                continue
            call_name = self._ast_call_name(node.func)
            if call_name and self._call_name_matches(call_name, call_names):
                return True
        return False

    def _text_has_deprecated_string_call(self, source: str, call_names: set[str]) -> bool:
        for name in call_names:
            basename = name.rsplit(".", 1)[-1]
            if re.search(rf"(?:\.|\b){re.escape(basename)}\s*\(\s*['\"]", source):
                return True
        return False

    def _ast_call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._ast_call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def _call_name_matches(self, call_name: str, expected_names: set[str]) -> bool:
        basename = call_name.rsplit(".", 1)[-1]
        for expected in expected_names:
            expected_basename = expected.rsplit(".", 1)[-1]
            if call_name == expected or basename == expected_basename or call_name.endswith(f".{expected}"):
                return True
        return False

    def _missing_bare_helper_imports(
        self,
        replacement_source: str,
        helpers: list[str],
        original_content: str = "",
    ) -> list[str]:
        helper_set = {helper for helper in helpers if helper and helper.isidentifier()}
        if not helper_set:
            return []

        used_helpers = self._bare_helper_calls(replacement_source, helper_set)
        if not used_helpers:
            return []

        available = self._defined_python_names(replacement_source)
        available.update(self._top_level_python_names(original_content))
        return sorted(helper for helper in used_helpers if helper not in available)

    def _bare_helper_calls(self, source: str, helpers: set[str]) -> set[str]:
        try:
            tree = ast.parse(textwrap.dedent(source or ""))
        except SyntaxError:
            return {
                helper
                for helper in helpers
                if re.search(rf"(?<![\w.]){re.escape(helper)}\s*\(", source or "")
            }

        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
                used.add(node.func.id)
        return used

    def _defined_python_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(textwrap.dedent(source or ""))
        except SyntaxError:
            return set()
        return self._python_names_defined_in_tree(tree, top_level_only=False)

    def _top_level_python_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(source or "")
        except SyntaxError:
            return set()
        return self._python_names_defined_in_tree(tree, top_level_only=True)

    def _python_names_defined_in_tree(self, tree: ast.AST, top_level_only: bool) -> set[str]:
        names = set()
        nodes = getattr(tree, "body", []) if top_level_only else ast.walk(tree)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    names.update(self._python_assigned_names(target))
            elif isinstance(node, ast.AnnAssign):
                names.update(self._python_assigned_names(node.target))
        return names

    def _python_assigned_names(self, node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            names = set()
            for element in node.elts:
                names.update(self._python_assigned_names(element))
            return names
        return set()

    def _patch_response_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "status", "replacements"],
            "properties": {
                "schema_version": {"type": "string", "const": "depguard.patch.v1"},
                "status": {"type": "string", "enum": ["patched", "no_change"]},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_line", "end_line", "replacement"],
                        "properties": {
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                            "replacement": {"type": "string"},
                        },
                    },
                },
                "target_decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start_line", "end_line", "decision", "reason"],
                        "properties": {
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                            "decision": {"type": "string", "enum": ["changed", "unchanged", "blocked"]},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        }

    def _decode_json_candidate(self, text: str):
        decoder = json.JSONDecoder()
        stripped = text.strip()
        if not stripped:
            return None

        try:
            data, end = decoder.raw_decode(stripped)
            if not stripped[end:].strip():
                return data
        except json.JSONDecodeError:
            pass

        for index, char in enumerate(stripped):
            if char not in "{[":
                continue
            try:
                data, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, list) or (isinstance(data, dict) and ("replacements" in data or "status" in data)):
                return data
        return None

    def _extract_patch_json(self, response_text: str):
        text = response_text.strip()

        fenced_blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        for fenced in fenced_blocks:
            data = self._decode_json_candidate(fenced)
            if data is not None:
                return data

        data = self._decode_json_candidate(text)
        if data is not None:
            return data

        raise PatchResponseError("LLM response did not contain JSON replacements")

    def _parse_replacements(self, response_text: str) -> list[dict]:
        data = self._extract_patch_json(response_text)

        status = None
        if isinstance(data, list):
            replacements = data
        elif isinstance(data, dict):
            status = data.get("status")
            schema_version = data.get("schema_version")
            if schema_version is not None and schema_version != "depguard.patch.v1":
                raise PatchResponseError(f"LLM response used unsupported schema_version: {schema_version}")
            if status is not None and status not in {"patched", "no_change"}:
                raise PatchResponseError(f"LLM response used invalid status: {status}")
            if "replacements" not in data:
                raise PatchResponseError("LLM response JSON did not contain a replacements array")
            replacements = data.get("replacements")
        else:
            raise PatchResponseError("LLM response JSON must be an object or an array")

        if not isinstance(replacements, list):
            raise PatchResponseError("LLM response replacements must be an array")
        if status == "patched" and not replacements:
            raise PatchResponseError("LLM response status was patched but replacements was empty")
        if status == "no_change" and replacements:
            raise PatchResponseError("LLM response status was no_change but replacements was not empty")

        valid = []
        for index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict):
                raise PatchResponseError(f"Replacement {index} must be an object")
            start_line = replacement.get("start_line")
            end_line = replacement.get("end_line")
            source = replacement.get("replacement")
            if not isinstance(start_line, int) or not isinstance(end_line, int):
                raise PatchResponseError(f"Replacement {index} start_line/end_line must be integers")
            if start_line < 1 or end_line < start_line:
                raise PatchResponseError(f"Replacement {index} has invalid line range {start_line}-{end_line}")
            if not isinstance(source, str):
                raise PatchResponseError(f"Replacement {index} replacement must be a string")
            if "```" in source:
                raise PatchResponseError(f"Replacement {index} contains a markdown code fence")
            valid.append({
                "start_line": start_line,
                "end_line": end_line,
                "replacement": source,
            })

        return valid

    def _apply_replacements(self, original_content: str, replacements: list[dict], allowed_blocks: list[dict]) -> str:
        lines = original_content.splitlines()
        trailing_newline = "\n" if original_content.endswith("\n") else ""
        allowed_ranges = {
            (block["start_line"], block["end_line"])
            for block in allowed_blocks
        }
        allowed_sources = {
            (block["start_line"], block["end_line"]): block["source"]
            for block in allowed_blocks
        }
        invalid_ranges = []
        duplicate_ranges = []
        seen_ranges = set()

        normalized = []
        for replacement in replacements:
            key = (replacement["start_line"], replacement["end_line"])
            if key not in allowed_ranges:
                invalid_ranges.append(key)
                continue
            if key in seen_ranges:
                duplicate_ranges.append(key)
                continue
            seen_ranges.add(key)
            if replacement["replacement"].splitlines() == allowed_sources[key].splitlines():
                continue
            normalized.append(replacement)

        if invalid_ranges:
            allowed = ", ".join(f"{start}-{end}" for start, end in sorted(allowed_ranges))
            invalid = ", ".join(f"{start}-{end}" for start, end in invalid_ranges)
            raise PatchResponseError(
                "LLM returned replacement ranges outside the target blocks. "
                f"Invalid: {invalid}. Allowed exact ranges: {allowed}."
            )
        if duplicate_ranges:
            duplicate = ", ".join(f"{start}-{end}" for start, end in duplicate_ranges)
            raise PatchResponseError(f"LLM returned duplicate replacement ranges: {duplicate}.")

        for replacement in sorted(normalized, key=lambda item: item["start_line"], reverse=True):
            start_index = max(0, replacement["start_line"] - 1)
            end_index = min(len(lines), replacement["end_line"])
            replacement_lines = replacement["replacement"].splitlines()
            lines[start_index:end_index] = replacement_lines

        return "\n".join(lines) + trailing_newline


    _PY_KEYWORDS: frozenset = frozenset(dir(builtins)) | frozenset({
        "if", "else", "elif", "for", "while", "try", "except", "finally",
        "with", "return", "raise", "import", "from", "as", "class", "def",
        "and", "or", "not", "in", "is", "None", "True", "False", "pass",
        "break", "continue", "lambda", "yield", "del", "global", "nonlocal",
        "assert", "async", "await", "self", "cls",
    })

    def _name_tokens(self, text: str) -> set:
        # Strip string literals so words inside them don't appear as meaningful tokens
        stripped = re.sub(r'''[bruf]*(?:'[^'\\]*(?:\\.[^'\\]*)*'|"[^"\\]*(?:\\.[^"\\]*)*")''', '', text)
        return {t for t in re.findall(r'\b([a-zA-Z_]\w*)\b', stripped) if t not in self._PY_KEYWORDS}

    def _names_removed_from_matched_lines(
        self,
        block_source: str,
        block_start_line: int,
        matched_lines: set,
        replacement_text: str,
    ) -> set:
        source_lines = block_source.splitlines()
        matched_names: set = set()
        for offset, line in enumerate(source_lines):
            if block_start_line + offset in matched_lines:
                matched_names |= self._name_tokens(line)
        if not matched_names:
            return set()
        return matched_names - self._name_tokens(replacement_text)

    def _expand_matched_for_decorator_scopes(
        self, block_source: str, block_start_line: int, matched_lines: set
    ) -> set:
        """When a matched line is a decorator (@...), also mark its decorated
        function definition and body as removable — they are logically coupled."""
        expanded = set(matched_lines)
        source_lines = block_source.splitlines()
        for line_no in sorted(matched_lines):
            offset = line_no - block_start_line
            if offset < 0 or offset >= len(source_lines):
                continue
            if not source_lines[offset].lstrip().startswith("@"):
                continue
            # Walk past any additional decorators to find the def
            next_off = offset + 1
            while next_off < len(source_lines):
                nl = source_lines[next_off]
                stripped = nl.strip()
                if not stripped:
                    next_off += 1
                    continue
                if stripped.startswith("@"):
                    expanded.add(block_start_line + next_off)
                    next_off += 1
                    continue
                if re.match(r"^(async\s+)?def\s+", stripped):
                    expanded.add(block_start_line + next_off)
                    def_indent = len(nl) - len(nl.lstrip())
                    body_off = next_off + 1
                    while body_off < len(source_lines):
                        bl = source_lines[body_off]
                        if not bl.strip():
                            body_off += 1
                            continue
                        if len(bl) - len(bl.lstrip()) <= def_indent:
                            break
                        expanded.add(block_start_line + body_off)
                        body_off += 1
                    break
                break
        return expanded

    def _allowed_keywords_from_breaking_changes(self, scout_context, target_source: str = "") -> set:
        if not scout_context:
            return set()
        allowed: set = set()
        for bc in scout_context.get("breaking_changes", []):
            for text in [str(bc.get("new_api", "") or ""), str(bc.get("description", "") or "")]:
                for token in re.findall(r'\b([a-zA-Z_]\w*)\b', text):
                    if len(token) > 2:
                        allowed.add(token.lower())
            # Only allow replacement tokens for parameters that are actually present in the target code
            for p in (bc.get("parameters_changed") or []):
                old_param = str(p.get("old_param", "") or "")
                if not old_param:
                    continue
                if target_source and not re.search(rf'\b{re.escape(old_param)}\s*=', target_source):
                    continue
                for token in re.findall(r'\b([a-zA-Z_]\w*)\b', str(p.get("replacement", "") or "")):
                    if len(token) > 2:
                        allowed.add(token.lower())
        return allowed

    def _validate_replacements_preserve_unchanged_code(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        matches: list,
    ) -> None:
        if not replacements:
            return

        blocks_by_range = {
            (block["start_line"], block["end_line"]): block
            for block in target_blocks
        }
        matched_lines = {
            int(match.get("line"))
            for match in matches or []
            if isinstance(match, dict) and isinstance(match.get("line"), int)
        }

        for replacement in replacements:
            key = (replacement["start_line"], replacement["end_line"])
            block = blocks_by_range.get(key)
            if not block:
                continue
            protected_lines = []
            start_line = block["start_line"]
            block_source = str(block.get("source", "") or "")
            # Expand matched lines to include bodies of matched decorator functions
            effective_matched = self._expand_matched_for_decorator_scopes(
                block_source, start_line, matched_lines
            )
            for offset, line in enumerate(block_source.splitlines()):
                absolute_line = start_line + offset
                if absolute_line in effective_matched:
                    continue
                if not line.strip():
                    continue
                protected_lines.append((absolute_line, line.strip()))

            if not protected_lines:
                continue

            replacement_stripped = [
                line.strip()
                for line in str(replacement.get("replacement", "") or "").splitlines()
                if line.strip()
            ]
            search_from = 0
            missing = []
            for absolute_line, expected in protected_lines:
                try:
                    found_at = replacement_stripped.index(expected, search_from)
                except ValueError:
                    missing.append(absolute_line)
                    continue
                search_from = found_at + 1

            if missing:
                removed_names = self._names_removed_from_matched_lines(
                    str(block.get("source", "") or ""),
                    start_line,
                    matched_lines,
                    str(replacement.get("replacement", "") or ""),
                )
                if removed_names:
                    block_source_lines = str(block.get("source", "") or "").splitlines()
                    truly_missing = []
                    for line_no in missing:
                        offset = line_no - start_line
                        line_src = (
                            block_source_lines[offset].strip()
                            if 0 <= offset < len(block_source_lines) else ""
                        )
                        line_names = self._name_tokens(line_src)
                        if line_names and line_names.issubset(removed_names):
                            continue  # orphaned — all names came from removed matched lines
                        # Also orphan if line has no user-defined names but references
                        # a removed name in raw source (e.g. in an f-string)
                        if not line_names and any(name in line_src for name in removed_names):
                            continue
                        truly_missing.append(line_no)
                    missing = truly_missing
            if missing:
                sample = ", ".join(str(line) for line in missing[:8])
                raise PatchResponseError(
                    "Replacement removed or rewrote unchanged code inside an allowed range. "
                    f"Protected original line(s) missing from replacement: {sample}. "
                    "Return the complete target block with unrelated code preserved."
                )

    def _validate_replacements_do_not_introduce_unresolved_bare_calls(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        original_content: str,
    ) -> None:
        if not replacements:
            return

        available = self._top_level_python_names(original_content)
        for replacement in replacements:
            available.update(self._defined_python_names(str(replacement.get("replacement", "") or "")))

        builtin_names = set(dir(builtins))
        blocks_by_range = {
            (block["start_line"], block["end_line"]): block
            for block in target_blocks
        }

        missing_names = set()
        for replacement in replacements:
            block = blocks_by_range.get((replacement["start_line"], replacement["end_line"]))
            if not block:
                continue
            original_calls = self._bare_call_names(str(block.get("source", "") or ""))
            replacement_calls = self._bare_call_names(str(replacement.get("replacement", "") or ""))
            introduced = replacement_calls - original_calls
            missing_names.update(
                name
                for name in introduced
                if name not in available and name not in builtin_names
            )

        if missing_names:
            names = ", ".join(sorted(missing_names))
            raise PatchResponseError(
                "Replacement introduced new bare call(s) without making them available in scope: "
                f"{names}. Add an import or definition inside the returned target block or another returned block."
            )

    def _extract_import_names(self, import_line: str) -> set:
        """Extract imported symbol names from a single import statement line."""
        stripped = import_line.strip()
        names: set = set()
        try:
            import ast as _ast
            tree = _ast.parse(stripped)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    for alias in node.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, _ast.ImportFrom):
                    for alias in node.names:
                        names.add(alias.asname or alias.name)
        except SyntaxError:
            pass
        return names

    def _validate_replacements_do_not_add_imports_in_function_bodies(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        original_content: str,
    ) -> None:
        """Reject replacements that re-import at an indented level a symbol already
        available at module level. Local imports for genuinely new helpers are allowed."""
        if not replacements:
            return
        module_names = self._top_level_python_names(original_content)
        for replacement in replacements:
            repl_text = str(replacement.get("replacement", "") or "")
            for line in repl_text.splitlines():
                stripped = line.lstrip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                indent = len(line) - len(stripped)
                if indent == 0:
                    continue  # module-level import is fine
                imported = self._extract_import_names(stripped)
                duplicates = imported & module_names
                if duplicates:
                    names_str = ", ".join(sorted(duplicates))
                    raise PatchResponseError(
                        f"Replacement added a redundant import inside an indented block: "
                        f"{stripped!r}. "
                        f"These names are already available at module level: {names_str}. "
                        "Do not re-import symbols already imported at module level. "
                        "Remove the local import from the replacement."
                    )


    def _repair_replacements_with_documented_local_imports(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        original_content: str,
        scout_context: dict | None,
    ) -> list[dict]:
        if not replacements or not scout_context:
            return replacements

        evidence_text = self._combined_evidence_text(
            scout_context.get("api_evidence", []),
            scout_context.get("evidence_references") or scout_context.get("references", []),
        )
        documented_imports = self._documented_helper_imports(evidence_text)
        if not documented_imports:
            return replacements

        available_global = self._top_level_python_names(original_content)
        builtin_names = set(dir(builtins))
        blocks_by_range = {
            (block["start_line"], block["end_line"]): block
            for block in target_blocks
        }

        repaired = []
        for replacement in replacements:
            block = blocks_by_range.get((replacement["start_line"], replacement["end_line"]))
            if not block:
                repaired.append(replacement)
                continue

            replacement_source = str(replacement.get("replacement", "") or "")
            original_calls = self._bare_call_names(str(block.get("source", "") or ""))
            replacement_calls = self._bare_call_names(replacement_source)
            available = set(available_global)
            available.update(self._defined_python_names(replacement_source))
            missing_names = {
                name
                for name in replacement_calls - original_calls
                if name not in available and name not in builtin_names
            }
            import_lines = [
                documented_imports[name]
                for name in sorted(missing_names)
                if name in documented_imports
            ]
            if not import_lines:
                repaired.append(replacement)
                continue

            repaired.append({
                **replacement,
                "replacement": self._insert_local_imports(replacement_source, import_lines),
            })

        return repaired

    def _documented_helper_imports(self, evidence_text: str) -> dict[str, str]:
        imports: dict[str, str] = {}
        for match in re.finditer(
            r"(?m)^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+([A-Za-z_][\w]*(?:\s+as\s+[A-Za-z_][\w]*)?(?:\s*,\s*[A-Za-z_][\w]*(?:\s+as\s+[A-Za-z_][\w]*)?)*)",
            evidence_text or "",
        ):
            module = match.group(1)
            for item in match.group(2).split(","):
                item = item.strip()
                if not item:
                    continue
                parts = re.split(r"\s+as\s+", item, maxsplit=1)
                imported = parts[0].strip()
                local_name = parts[1].strip() if len(parts) > 1 else imported
                if imported and local_name and imported.isidentifier() and local_name.isidentifier():
                    imports.setdefault(local_name, f"from {module} import {item}")
        return imports

    def _insert_local_imports(self, source: str, import_lines: list[str]) -> str:
        lines = (source or "").splitlines()
        unique_imports = []
        seen = {line.strip() for line in lines}
        for import_line in import_lines:
            stripped = import_line.strip()
            if stripped and stripped not in seen and stripped not in unique_imports:
                unique_imports.append(stripped)
        if not unique_imports:
            return source

        insert_at = 0
        import_indent = ""
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                base_indent = line[:len(line) - len(stripped)]
                import_indent = base_indent + "    "
                insert_at = index + 1
                break
        else:
            while insert_at < len(lines) and (
                not lines[insert_at].strip()
                or lines[insert_at].lstrip().startswith(("#!", "# -*-", "# coding"))
            ):
                insert_at += 1

        insertion = [f"{import_indent}{import_line}" for import_line in unique_imports]
        return "\n".join(lines[:insert_at] + insertion + lines[insert_at:])

    def _validate_replacements_do_not_introduce_undocumented_keywords(
        self,
        replacements: list[dict],
        target_blocks: list[dict],
        scout_context: dict | None,
    ) -> None:
        if not replacements or not scout_context or self._allows_llm_prior_fallback(scout_context):
            return

        evidence_text = self._combined_evidence_text(
            scout_context.get("api_evidence", []),
            scout_context.get("evidence_references") or scout_context.get("references", []),
        )
        blocks_by_range = {
            (block["start_line"], block["end_line"]): block
            for block in target_blocks
        }
        target_source = "\n".join(str(b.get("source", "") or "") for b in target_blocks)
        allowed_from_new_api = self._allowed_keywords_from_breaking_changes(scout_context, target_source)
        undocumented = set()
        for replacement in replacements:
            block = blocks_by_range.get((replacement["start_line"], replacement["end_line"]))
            if not block:
                continue
            original_keywords = self._call_keyword_names(str(block.get("source", "") or ""))
            replacement_keywords = self._call_keyword_names(str(replacement.get("replacement", "") or ""))
            for keyword in replacement_keywords - original_keywords:
                if keyword.lower() in allowed_from_new_api:
                    continue
                if not self._evidence_mentions_keyword(evidence_text, keyword):
                    undocumented.add(keyword)

        if undocumented:
            names = ", ".join(sorted(undocumented))
            raise PatchResponseError(
                "Replacement introduced undocumented keyword argument(s): "
                f"{names}. Only add keyword arguments that appear in Scout evidence."
            )

    def _bare_call_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(textwrap.dedent(source or ""))
        except SyntaxError:
            return set()
        return {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    def _call_keyword_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(textwrap.dedent(source or ""))
        except SyntaxError:
            return set()
        keywords = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg:
                    keywords.add(keyword.arg)
        return keywords

    def _evidence_mentions_keyword(self, evidence_text: str, keyword: str) -> bool:
        if not keyword:
            return False
        raw = evidence_text or ""
        normalized = self._normalize_text(raw)
        escaped = re.escape(keyword.lower())
        return (
            re.search(rf"\b{escaped}\s*=", normalized) is not None
            or re.search(rf"(``|`|'|\"){escaped}(``|`|'|\")", raw, flags=re.IGNORECASE) is not None
            or re.search(rf"\b{escaped}\s+(parameter|argument|option|keyword)\b", normalized) is not None
        )

    def _allows_llm_prior_fallback(self, scout_context: dict | None) -> bool:
        if not scout_context:
            return False
        return bool(
            scout_context.get("llm_prior_fallback")
            or scout_context.get("migration_review_fallback")
        )

    def _patch_response_to_full_file(
        self,
        response_text: str,
        original_content: str,
        target_blocks: list[dict],
        scout_context: dict | None = None,
        matches: list | None = None,
    ) -> str:
        replacements = self._parse_replacements(response_text)
        self._validate_replacements_preserve_unchanged_code(
            replacements,
            target_blocks,
            matches or [],
        )
        replacements = self._repair_replacements_with_documented_local_imports(
            replacements,
            target_blocks,
            original_content,
            scout_context,
        )
        self._validate_replacements_do_not_introduce_unresolved_bare_calls(
            replacements,
            target_blocks,
            original_content,
        )
        self._validate_replacements_do_not_add_imports_in_function_bodies(
            replacements,
            target_blocks,
            original_content,
        )
        self._validate_replacements_do_not_introduce_undocumented_keywords(
            replacements,
            target_blocks,
            scout_context,
        )
        self._validate_replacements_against_migration_obligations(
            replacements,
            target_blocks,
            scout_context,
            original_content,
        )
        return self._apply_replacements(original_content, replacements, target_blocks)

    def _build_patch_retry_prompt(
        self,
        original_prompt: str,
        error_message: str,
        response_text: str,
        target_blocks: list[dict],
    ) -> str:
        allowed_ranges = [
            {"start_line": block["start_line"], "end_line": block["end_line"]}
            for block in target_blocks
        ]
        previous = response_text.strip()
        if len(previous) > self.retry_response_max_chars:
            previous = previous[:self.retry_response_max_chars] + "\n... previous response truncated ..."
        obligation_retry_hint = ""
        if "string-argument migration" in error_message:
            obligation_retry_hint = (
                "\n            - The previous patch only changed the call target. "
                "Also migrate the literal/string argument using the documented helper or alternative named in the parser error."
            )
        if "without making them available in scope" in error_message:
            obligation_retry_hint += (
                "\n            - If you use a helper as a bare function call, add its import or definition inside the returned target block "
                "unless it already exists in the shown code context. Example: add `from sqlalchemy import text` inside the edited function before `conn.execute(text(...))` when top-level imports are outside the allowed range."
            )

        return f"""{original_prompt}

            Previous LLM response was rejected by DepGuard's parser/validator.
            Parser error:
            {error_message}

            Allowed exact replacement ranges:
            {json.dumps(allowed_ranges, separators=(",", ":"))}

            Rejected response:
            {previous or "(empty response)"}

            Retry instructions:
            - Return one corrected JSON object only.
            - Use schema_version "depguard.patch.v1".
            - Use status "patched" only if replacements is non-empty.
            - Use status "no_change" with replacements [] if no safe documented code edit is required.
            - Every replacement range must exactly match one allowed range.
            - Do not include markdown or prose.
            {obligation_retry_hint}
            """

    def _absolute_project_file(self, file_path: str) -> str:
        path = Path(file_path)
        if path.is_absolute():
            return str(path)
        return str((Path(self.project_root) / path).resolve())

    def _expand_matches_with_impacted_nodes(self, matches_by_file: dict, scout_context: dict | None = None) -> dict:
        """Add LSP/Tree-sitter related nodes as review targets."""
        expanded = {
            filepath: list(matches)
            for filepath, matches in matches_by_file.items()
        }

        seen = {
            (str(filepath), match.get("line"), match.get("type"), match.get("old_api"))
            for filepath, matches in expanded.items()
            for match in matches
        }

        for filepath, matches in list(expanded.items()):
            impact = self._get_impact_result(filepath, matches)
            if not impact:
                continue

            for impacted in impact.impacted_nodes:
                location = impacted.node.location
                absolute_file = self._absolute_project_file(location.file)
                if not os.path.exists(absolute_file):
                    continue
                key = (absolute_file, location.start_line, "impact_review", impacted.node.id)
                if key in seen:
                    continue
                seen.add(key)
                expanded.setdefault(absolute_file, []).append({
                    "file": absolute_file,
                    "line": location.start_line,
                    "col": 0,
                    "old_api": impacted.node.id,
                    "new_api": "",
                    "description": (
                        f"Related code may need review because it {impacted.impact_reason}. "
                        f"Review the full {location.context_type} block."
                    ),
                    "code_snippet": location.source.splitlines()[0] if location.source else "",
                    "type": "impact_review",
                    "impact_reason": impacted.impact_reason,
                    "affected_attributes": impacted.affected_attributes,
                })

        return expanded

    # -------------------------- Create A Commit Before Update Code ---------------------
    def _create_checkpoint(self, package: str) -> tuple[str, bool]:
        checkpoint_id = f"depguard_checkpoint_{uuid.uuid4().hex[:8]}"
        try:
            subprocess.run(["git", "add", "."], capture_output=True, check=False)
            res = subprocess.run(
                ["git", "commit", "-m", f"depguard: checkpoint before patching {package} {checkpoint_id}"],
                capture_output=True,
                check=False
            )
            commit_made = res.returncode == 0
            return checkpoint_id, commit_made
        except Exception as e:
            logger.warning(f"Failed to create git checkpoint: {e}")
            return checkpoint_id, False

    # ------------------- Rollback to the last commit (no change by LLM) ----------------
    def _rollback(self, commit_made: bool):
        try:
            logger.info("Rolling back changes...")
            # Restore working directory
            subprocess.run(["git", "reset", "--hard", "HEAD"], capture_output=True, check=False)
            if commit_made:
                # Remove the commit we just made
                subprocess.run(["git", "reset", "--hard", "HEAD~1"], capture_output=True, check=False)
        except Exception as e:
            logger.warning(f"Failed to rollback: {e}")

    # -------------------- Extract code out of markdown form by LLM -----------------------
    def _extract_code(self, response_text: str) -> str:
        # If wrapped in markdown
        match = re.search(r'```(?:[A-Za-z0-9_+\-.#]+)?\n(.*?)\n```', response_text, re.DOTALL)
        if match:
            return match.group(1)
        return response_text.strip()

    def _extract_fenced_code(self, response_text: str) -> str | None:
        match = re.search(r'```(?:[A-Za-z0-9_+\-.#]+)?\n(.*?)\n```', response_text, re.DOTALL)
        return match.group(1) if match else None

    def _code_fence_language(self, filepath: str) -> str:
        extension = os.path.splitext(filepath)[1].lower()
        return {
            ".py": "python",
            ".pyw": "python",
            ".js": "javascript",
            ".jsx": "jsx",
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
            ".c": "c",
            ".h": "c",
            ".cc": "cpp",
            ".cpp": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
            ".cs": "csharp",
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
        }.get(extension, "")

    def _validate_patched_content(self, filepath: str, patched_content: str) -> str | None:
        if self.syntax_scanner:
            validation_error = self.syntax_scanner.validate_source(filepath, patched_content)
            if validation_error:
                return validation_error

            language = self.syntax_scanner.detect_language(filepath)
            if language and self.syntax_scanner.parsers.get(language):
                return None

        if filepath.endswith((".py", ".pyw")):
            try:
                ast.parse(patched_content, filename=filepath)
            except SyntaxError as e:
                return f"Syntax error in LLM output: {e}"

        return None

    async def _generate_patched_content(self, filepath: str, matches: list, scout_context: dict, task_type: str) -> tuple[bool, str, dict, str, str]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
                return False, f"Could not read file: {e}", {}, "", ""

        if self._should_skip_speculative_review(matches, scout_context):
            return True, "", {}, original_content, original_content

        system_prompt, prompt, target_blocks = self._build_sliced_patch_prompt(
            filepath,
            matches,
            scout_context,
            original_content,
        )

        max_tokens = self._env_int("PATCH_LLM_MAX_TOKENS", 8000)
        prompt_for_attempt = prompt
        response_text = ""
        llm_info: dict = {}
        last_error = ""

        for attempt in range(1, self.llm_response_max_attempts + 1):
            try:
                response = await self.router.complete(
                    system_prompt,
                    prompt_for_attempt,
                    max_tokens=max_tokens,
                    task_type=task_type,
                )
                response_text = response.content
                llm_info = {"provider": response.provider, "fallback_used": response.fallback_used}
                patched_content = self._patch_response_to_full_file(
                    response_text,
                    original_content,
                    target_blocks,
                    scout_context,
                    matches,
                )

                validation_error = self._validate_patched_content(filepath, patched_content)
                if validation_error:
                    raise PatchResponseError(f"Patched content failed syntax validation: {validation_error}")

                return True, "", llm_info, original_content, patched_content
            except PatchResponseError as e:
                last_error = str(e)
                if attempt < self.llm_response_max_attempts:
                    logger.warning(
                        "LLM patch response rejected for %s on attempt %s/%s: %s",
                        filepath,
                        attempt,
                        self.llm_response_max_attempts,
                        last_error,
                    )
                    prompt_for_attempt = self._build_patch_retry_prompt(
                        prompt,
                        last_error,
                        response_text,
                        target_blocks,
                    )
                    continue
                logger.error(f"Error patching {filepath}: {last_error}")
                return False, last_error, llm_info, original_content, ""
            except Exception as e:
                logger.error(f"Error patching {filepath}: {e}")
                return False, str(e), llm_info, original_content, ""

        return False, last_error or "LLM patch failed", llm_info, original_content, ""

    def _should_skip_speculative_review(self, matches: list, scout_context: dict | None) -> bool:
        """Keep ordinary review-only findings deterministic.

        ``migration_review`` and ``impact_review`` are not evidence by
        themselves. The API layer may explicitly set ``llm_prior_fallback`` (or
        its legacy marker ``migration_review_fallback``) when retrieval found no
        docs and the user still wants the LLM to try from general migration
        knowledge. All other review-only findings remain no-change.
        """
        if not matches or self._allows_llm_prior_fallback(scout_context):
            return False

        review_types = {"migration_review", "impact_review"}
        if any(str(match.get("type", "") or "") not in review_types for match in matches):
            return False

        if not scout_context:
            return True

        actionable_types = {"removed", "renamed", "changed_signature"}
        actionable_apis = {
            str(change.get("old_api", "") or "").strip()
            for change in scout_context.get("breaking_changes", []) or []
            if str(change.get("type", "") or "") in actionable_types
        }
        if actionable_apis:
            matched_apis = {
                str(match.get("old_api", "") or "").strip()
                for match in matches
                if str(match.get("old_api", "") or "").strip()
            }
            return not any(
                matched == actionable
                or matched.startswith(f"{actionable}.")
                or actionable.startswith(f"{matched}.")
                for matched in matched_apis
                for actionable in actionable_apis
            )

        matched_apis = {
            str(match.get("old_api", "") or "").strip()
            for match in matches
            if str(match.get("old_api", "") or "").strip()
        }
        for item in scout_context.get("api_evidence", []) or []:
            if not isinstance(item, dict):
                continue
            api = str(item.get("api", "") or "").strip()
            if not api:
                continue
            if not (
                item.get("evidence")
                or str(item.get("replacement", "") or "").strip()
                or str(item.get("reason", "") or "").strip()
            ):
                continue
            if any(
                matched == api
                or matched.startswith(f"{api}.")
                or api.startswith(f"{matched}.")
                for matched in matched_apis
            ):
                return False

        return True


    async def _patch_file(self, filepath: str, matches: list, scout_context: dict, task_type: str) -> tuple[bool, str, dict]:
        success, error_msg, llm_info, _original_content, patched_content = await self._generate_patched_content(filepath, matches, scout_context, task_type)
        if not success:
            return False, error_msg, llm_info

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(patched_content)
            return True, "", llm_info
        except Exception as e:
            logger.error(f"Error writing patched file {filepath}: {e}")
            return False, str(e), llm_info

    # Update dependencies version
    def _update_dependency_file(self, dep_file_path: str, package: str, from_v: str, to_v: str):
        try:
            with open(dep_file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # regex to replace version string only on the line containing the package name
            # Works for `package==1.2.3` or `"package": "^1.2.3"`
            # \g<1> refers to the prefix before the version
            new_content = re.sub(rf'({package}[^\d\n]*){re.escape(from_v)}', rf'\g<1>{to_v}', content, flags=re.IGNORECASE)
            
            with open(dep_file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        except Exception as e:
            logger.warning(f"Failed to update dependency file {dep_file_path}: {e}")
            return False

    async def run(self, scout_output: dict, ast_scanner_output: dict, dep_file_path: str) -> dict:
        package = scout_output.get("package", "unknown")
        from_v = scout_output.get("from_version", "")
        to_v = scout_output.get("to_version", "")
        
        matches_by_file = ast_scanner_output.get("matches_by_file", {})
        if "matches_by_file" not in ast_scanner_output:
            if isinstance(ast_scanner_output, dict) and not "total_files_scanned" in ast_scanner_output:
                matches_by_file = ast_scanner_output
        matches_by_file = self._expand_matches_with_impacted_nodes(matches_by_file, scout_output)

        report = {
            "package": package,
            "from_version": from_v,
            "to_version": to_v,
            "files_patched": [],
            "dependency_file_updated": "",
            "checkpoint_id": "",
            "overall_status": "in_progress",
            "llm_provider": "none",
            "fallback_used": False
        }

        if not matches_by_file:
            report["overall_status"] = "success"
            return report

        breaking_changes = scout_output.get("breaking_changes", [])
        if breaking_changes and all(c.get("type") == "renamed" for c in breaking_changes):
            task_type = "patch_simple"
        else:
            task_type = "patch_complex"

        checkpoint_id, commit_made = self._create_checkpoint(package)
        report["checkpoint_id"] = checkpoint_id

        overall_success = True
        
        for filepath, matches in matches_by_file.items():
            success, error_msg, llm_info = await self._patch_file(filepath, matches, scout_output, task_type)
            if llm_info:
                report["llm_provider"] = llm_info.get("provider", report["llm_provider"])
                report["fallback_used"] = report["fallback_used"] or llm_info.get("fallback_used", False)
            
            lines_changed = list(set([m.get("line", 0) for m in matches]))
            
            file_report = {
                "file": filepath,
                "lines_changed": lines_changed,
                "status": "success" if success else "failed"
            }
            if not success:
                file_report["error"] = error_msg
                overall_success = False
                
            report["files_patched"].append(file_report)
            
            if not overall_success:
                break

        if not overall_success:
            self._rollback(commit_made)
            report["overall_status"] = "rolled_back"
            return report

        # Update dependency file
        if dep_file_path and os.path.exists(dep_file_path):
            if self._update_dependency_file(dep_file_path, package, from_v, to_v):
                report["dependency_file_updated"] = os.path.basename(dep_file_path)

        report["overall_status"] = "success"
        return report

    async def _close_router(self) -> None:
        close = getattr(self.router, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def run_sync(self, scout_output: dict, ast_scanner_output: dict, dep_file_path: str) -> dict:
        async def _run_and_close() -> dict:
            try:
                return await self.run(scout_output, ast_scanner_output, dep_file_path)
            finally:
                await self._close_router()

        return asyncio.run(_run_and_close())

    async def preview(self, scout_output: dict, ast_scanner_output: dict) -> dict:
        package = scout_output.get("package", "unknown")
        from_v = scout_output.get("from_version", "")
        to_v = scout_output.get("to_version", "")

        matches_by_file = ast_scanner_output.get("matches_by_file", {})
        if "matches_by_file" not in ast_scanner_output:
            if isinstance(ast_scanner_output, dict) and not "total_files_scanned" in ast_scanner_output:
                matches_by_file = ast_scanner_output
        matches_by_file = self._expand_matches_with_impacted_nodes(matches_by_file, scout_output)

        report = {
            "package": package,
            "from_version": from_v,
            "to_version": to_v,
            "files": [],
            "llm_provider": "none",
            "fallback_used": False,
        }

        if not matches_by_file:
            return report

        breaking_changes = scout_output.get("breaking_changes", [])
        if breaking_changes and all(c.get("type") == "renamed" for c in breaking_changes):
            task_type = "patch_simple"
        else:
            task_type = "patch_complex"

        for filepath, matches in matches_by_file.items():
            success, error_msg, llm_info, original_content, patched_content = await self._generate_patched_content(filepath, matches, scout_output, task_type)
            if llm_info:
                report["llm_provider"] = llm_info.get("provider", report["llm_provider"])
                report["fallback_used"] = report["fallback_used"] or llm_info.get("fallback_used", False)

            report["files"].append({
                "file": filepath,
                "status": "success" if success else "failed",
                "error": error_msg,
                "original": original_content,
                "patched": patched_content if success else original_content,
            })
            if not success and "All configured LLM providers failed" in error_msg:
                break

        return report

    def preview_sync(self, scout_output: dict, ast_scanner_output: dict) -> dict:
        async def _preview_and_close() -> dict:
            try:
                return await self.preview(scout_output, ast_scanner_output)
            finally:
                await self._close_router()

        return asyncio.run(_preview_and_close())
