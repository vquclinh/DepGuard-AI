"""Debug script: trace the full update_package pipeline for pydantic on GuideDIS."""
import sys, json, logging, asyncio, os
from pathlib import Path

# ── project root on sys.path ──────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

# verbose logging so every agent step prints
logging.basicConfig(level=logging.DEBUG, format="%(name)s | %(levelname)s | %(message)s")
# quiet noisy libraries
for noisy in ("httpx", "httpcore", "openai", "anthropic", "google", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from tools.ast_scanner import ASTScanner
from agents.scout import ScoutAgent
from agents.patch import PatchAgent

FOLDER      = "/mnt/vquclinh/PROJECT-CMAKE/TEST/GuideDIS"
PACKAGE     = "pydantic"
FROM_V      = "1.10.2"
TO_V        = "2.13.4"
DEP_FILE    = f"{FOLDER}/requirements.txt"

PKG_INFO = {
    "name": PACKAGE,
    "current_version": FROM_V,
    "latest_version": TO_V,
    "ecosystem": "pypi",
    "file_path": DEP_FILE,
}

SEP = "─" * 80

def _j(obj):
    return json.dumps(obj, indent=2, default=str)

def section(title: str, body: str):
    print(f"\n{SEP}\n◆  {title}\n{SEP}")
    print(body)


# ── Step 1: ASTScanner.find_api_usages ────────────────────────────────────────
print(f"\n{'═'*80}\nDEBUG PATCH PIPELINE  •  {PACKAGE}  {FROM_V} → {TO_V}\n{'═'*80}")

scanner = ASTScanner()

raw_usages = scanner.find_api_usages(FOLDER, PACKAGE)
section("RAW API usages (find_api_usages)", _j(sorted(raw_usages)))

# mimic _prefer_specific_api_usages from api/main.py
from api.main import _prefer_specific_api_usages, _scan_breaking_changes_with_review_fallback
api_usages = _prefer_specific_api_usages(raw_usages)
section("FILTERED API usages (_prefer_specific_api_usages)", _j(sorted(api_usages)))

api_contexts = scanner.find_api_usage_contexts(FOLDER, PACKAGE)
section("API usage CONTEXTS (first 3)", _j(api_contexts[:3]))


# ── Step 2: Scout ─────────────────────────────────────────────────────────────
print(f"\n{SEP}\n◆  Running ScoutAgent …\n{SEP}")
scout = ScoutAgent()

# Monkey-patch to capture LLM calls inside Scout
_original_analyze = scout._analyze_changelog_with_llm.__func__  # unbound

def _patched_analyze(self_inner, *args, **kwargs):
    result = _original_analyze(self_inner, *args, **kwargs)
    section("SCOUT LLM response (breaking_changes)", _j(result))
    return result

import types
scout._analyze_changelog_with_llm = types.MethodType(_patched_analyze, scout)

scout_output = scout.run_sync(PKG_INFO, api_usages, api_contexts)

section("SCOUT OUTPUT (breaking_changes only)",
        _j(scout_output.get("breaking_changes", [])))


# ── Step 3: Scan with partial fallback ────────────────────────────────────────
print(f"\n{SEP}\n◆  Running _scan_breaking_changes_with_review_fallback …\n{SEP}")

scout_output, ast_output = _scan_breaking_changes_with_review_fallback(
    scanner,
    Path(FOLDER),
    scout_output,
    PACKAGE,
    FROM_V,
    TO_V,
    api_usages,
)

breaking_changes = scout_output.get("breaking_changes", [])
section(f"Breaking changes after fallback ({len(breaking_changes)} total)",
        _j(breaking_changes))

section("AST SCAN OUTPUT (matches_by_file)",
        _j({fp: [{"line": m["line"], "old_api": m.get("old_api"), "type": m.get("type"), "matched_text": m.get("matched_text")}
                  for m in ms]
            for fp, ms in ast_output.get("matches_by_file", {}).items()}))
print(f"total_matches = {ast_output.get('total_matches', 0)}")
print(f"partial_fallback_used = {scout_output.get('partial_fallback_used', False)}")
print(f"llm_prior_fallback    = {scout_output.get('llm_prior_fallback', False)}")

if not ast_output.get("matches_by_file"):
    print("⚠  ASTScanner found 0 matches → PatchAgent would have nothing to patch.")
    sys.exit(0)


# ── Step 4: PatchAgent — capture prompt & response ───────────────────────────
patch_agent = PatchAgent(project_root=FOLDER)

# Monkey-patch LLMRouter.complete to capture prompts/responses
router = patch_agent.router
_original_complete = router.complete.__func__  # unbound

_call_count = [0]
async def _patched_complete(self_r, system_prompt, user_prompt, *args, **kwargs):
    _call_count[0] += 1
    section(f"LLM CALL #{_call_count[0]} — SYSTEM", str(system_prompt)[:3000])
    section(f"LLM CALL #{_call_count[0]} — USER",   str(user_prompt)[:5000])
    llm_resp = await _original_complete(self_r, system_prompt, user_prompt, *args, **kwargs)
    section(f"LLM CALL #{_call_count[0]} — RESPONSE ({llm_resp.provider})", str(llm_resp.content)[:5000])
    return llm_resp

import types
router.complete = types.MethodType(_patched_complete, router)

print(f"\n{SEP}\n◆  Running PatchAgent.preview_sync …\n{SEP}")
try:
    # Use preview so we don't actually write files
    patch_report = patch_agent.preview_sync(scout_output, ast_output)
    section("PATCH REPORT (files)", _j(patch_report.get("files", [])))
    for fp in patch_report.get("files", []):
        if fp.get("status") == "success":
            orig    = fp.get("original", "")
            patched = fp.get("patched",  orig)
            if orig != patched:
                import difflib
                diff = list(difflib.unified_diff(
                    orig.splitlines(keepends=True),
                    patched.splitlines(keepends=True),
                    fromfile=f"a/{fp['file']}",
                    tofile=f"b/{fp['file']}",
                    n=3,
                ))
                section(f"DIFF  {fp['file']}", "".join(diff))
            else:
                section(f"NO CHANGE  {fp['file']}", "(original == patched)")
        else:
            section(f"FAILED  {fp.get('file')}", fp.get("error", ""))
except Exception as exc:
    import traceback
    section("PATCH ERROR", traceback.format_exc())

print(f"\n{'═'*80}\nDone.\n{'═'*80}\n")
