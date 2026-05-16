import os
import sys
import logging
import subprocess
import re
import asyncio
import difflib
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel
from dotenv import load_dotenv

# Ensure the root directory is in the python path to import agents
sys.path.append(str(Path(__file__).parent.parent))

try:
    from agents.scanner import ScannerAgent
    from agents.watchdog import WatchdogAgent
    from agents.scout import ScoutAgent
    from agents.patch import PatchAgent
    from agents.checker import ProjectChecker
    from agents.repair import RepairAgent
    from tools.ast_scanner import ASTScanner
    from tools.impact_graph import ImpactGraphBuilder
    from tools.llm_router import LLMRouter
except ImportError as e:
    print(f"Warning: Could not import agents. Ensure you are running from the project root. ({e})")

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="DepGuard AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    logger.info("DepGuard AI API running on http://localhost:8000")

# --------------------------------- Request Forming ----------------------------------
class ScanRequest(BaseModel):
    folder_path: str

class PackageInfo(BaseModel):
    name: str
    current_version: str
    latest_version: str
    ecosystem: str
    file_path: str

class UpdateRequest(BaseModel):
    folder_path: str
    package_info: PackageInfo

class RollbackRequest(BaseModel):
    checkpoint_id: str
    folder_path: str

class ImpactGraphRequest(BaseModel):
    folder_path: str
    force_rebuild: bool = False

class FileContentRequest(BaseModel):
    folder_path: str
    file_path: str

class ApplyPreviewRequest(BaseModel):
    session_id: str
    decisions: dict

PREVIEW_SESSIONS: dict[str, dict] = {}
PREVIEW_SESSION_TTL_SECONDS = 30 * 60

FILE_EXPLORER_IGNORE_DIRS = {
    "venv", ".venv", "env", "node_modules", "__pycache__",
    ".git", "dist", "build", ".pytest_cache", ".depguard_cache",
}

FILE_EXPLORER_BINARY_EXTENSIONS = {
    ".7z", ".a", ".ai", ".avi", ".bin", ".bmp", ".class", ".dll", ".dmg",
    ".doc", ".docx", ".dylib", ".eot", ".exe", ".gif", ".gz", ".ico",
    ".jar", ".jpeg", ".jpg", ".mov", ".mp3", ".mp4", ".o", ".obj", ".otf",
    ".pdf", ".png", ".pyc", ".rar", ".so", ".sqlite", ".sqlite3", ".tar",
    ".ttf", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".zip",
}

def _safe_project_path(folder_path: Path, file_path: str) -> Path:
    root = folder_path.resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="File path is outside the project")
    return resolved

def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

def _is_explorer_file(path: Path) -> bool:
    if path.suffix.lower() in FILE_EXPLORER_BINARY_EXTENSIONS:
        return False
    try:
        if path.stat().st_size > 2_000_000:
            return False
        with open(path, "rb") as f:
            chunk = f.read(2048)
        return b"\x00" not in chunk
    except OSError:
        return False

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

def _capture_files(folder_path: Path, file_paths: list[str]) -> dict[str, str]:
    captured = {}
    for file_path in file_paths:
        try:
            resolved = _safe_project_path(folder_path, file_path)
        except HTTPException:
            continue
        if resolved.exists() and resolved.is_file():
            try:
                relative = resolved.relative_to(folder_path.resolve()).as_posix()
            except ValueError:
                relative = str(resolved)
            captured[relative] = _read_text_if_exists(resolved)
    return captured

def _cleanup_preview_sessions():
    now = time.time()
    expired = [
        session_id for session_id, session in PREVIEW_SESSIONS.items()
        if now - session.get("created_at", 0) > PREVIEW_SESSION_TTL_SECONDS
    ]
    for session_id in expired:
        PREVIEW_SESSIONS.pop(session_id, None)

def _relative_path(folder_path: Path, file_path: str) -> str:
    resolved = _safe_project_path(folder_path, file_path)
    return resolved.relative_to(folder_path.resolve()).as_posix()

def _dependency_preview_content(dep_file_path: str, package: str, from_v: str, to_v: str) -> tuple[str, str]:
    original = _read_text_if_exists(Path(dep_file_path))
    patched = re.sub(
        rf'({re.escape(package)}[^\d\n]*){re.escape(from_v)}',
        rf'\g<1>{to_v}',
        original,
        flags=re.IGNORECASE
    )
    return original, patched

def _migration_review_breaking_changes(package: str, from_v: str, to_v: str, api_usages: list[str], limit: int = 80) -> list[dict]:
    """Build API review targets when Scout cannot extract exact changes.

    The patch agent still has to produce a real diff for a file to appear in the
    review UI, so this broad fallback remains review-first rather than direct
    write behavior.
    """
    concrete_usages = [
        usage for usage in sorted(set(api_usages), key=lambda item: (-item.count("."), -len(item), item))
        if usage and usage != package and (usage.startswith(f"{package}.") or usage.startswith(f"{package}/"))
    ]
    if not concrete_usages:
        concrete_usages = [
            usage for usage in sorted(set(api_usages))
            if usage and usage != package
        ]

    return [
        {
            "type": "migration_review",
            "old_api": usage,
            "new_api": "",
            "description": (
                f"Review this {package} API usage while migrating {package} "
                f"from {from_v} to {to_v}. Apply a code change only if the new version requires it."
            ),
        }
        for usage in concrete_usages[:limit]
    ]

def _scan_breaking_changes_with_review_fallback(
    ast_scanner,
    folder_path: Path,
    scout_output: dict,
    package: str,
    from_v: str,
    to_v: str,
    api_usages: list[str],
) -> tuple[dict, dict]:
    """Return scanner output, using broad API usages for preview when Scout is silent.

    Changelog extraction can be sparse for non-Python ecosystems or for packages
    with incomplete release notes. The review UI is the right place to surface
    candidate code edits because the user can reject any hunk before writing.
    """
    breaking_changes = scout_output.get("breaking_changes", []) or []
    ast_output = {"matches_by_file": {}, "total_matches": 0, "total_files_scanned": 0}

    if breaking_changes:
        ast_output = ast_scanner.scan(str(folder_path), breaking_changes)

    if ast_output.get("total_matches", 0) > 0:
        return scout_output, ast_output

    fallback_changes = _migration_review_breaking_changes(package, from_v, to_v, api_usages)
    if not fallback_changes:
        return scout_output, ast_output

    fallback_ast_output = ast_scanner.scan(str(folder_path), fallback_changes)
    if fallback_ast_output.get("total_matches", 0) <= 0:
        return scout_output, ast_output

    fallback_scout_output = {
        **scout_output,
        "breaking_changes": fallback_changes,
        "migration_review_fallback": True,
    }
    logger.info(
        "Using migration review fallback for %s: %s API target(s), %s code file(s)",
        package,
        len(fallback_changes),
        len(fallback_ast_output.get("matches_by_file", {})),
    )
    return fallback_scout_output, fallback_ast_output

def _build_hunks(original: str, patched: str) -> tuple[list[dict], int, int]:
    original_lines = original.splitlines()
    patched_lines = patched.splitlines()
    matcher = difflib.SequenceMatcher(None, original_lines, patched_lines)
    hunks = []
    additions = 0
    deletions = 0

    for hunk_index, group in enumerate(matcher.get_grouped_opcodes(3), start=1):
        old_start = group[0][1] + 1
        old_end = group[-1][2]
        new_start = group[0][3] + 1
        new_end = group[-1][4]
        changes = []

        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset, line in enumerate(original_lines[i1:i2]):
                    changes.append({
                        "type": "context",
                        "line_number_old": i1 + offset + 1,
                        "line_number_new": j1 + offset + 1,
                        "content": line,
                    })
            elif tag in {"replace", "delete"}:
                for offset, line in enumerate(original_lines[i1:i2]):
                    deletions += 1
                    changes.append({
                        "type": "deletion",
                        "line_number_old": i1 + offset + 1,
                        "line_number_new": None,
                        "content": line,
                    })
                if tag == "replace":
                    for offset, line in enumerate(patched_lines[j1:j2]):
                        additions += 1
                        changes.append({
                            "type": "addition",
                            "line_number_old": None,
                            "line_number_new": j1 + offset + 1,
                            "content": line,
                        })
            elif tag == "insert":
                for offset, line in enumerate(patched_lines[j1:j2]):
                    additions += 1
                    changes.append({
                        "type": "addition",
                        "line_number_old": None,
                        "line_number_new": j1 + offset + 1,
                        "content": line,
                    })

        hunks.append({
            "hunk_id": f"hunk_{hunk_index:03d}",
            "old_start": old_start,
            "old_lines": max(0, old_end - (old_start - 1)),
            "new_start": new_start,
            "new_lines": max(0, new_end - (new_start - 1)),
            "changes": changes,
        })

    return hunks, additions, deletions

def _preview_response(session: dict) -> dict:
    files = []
    total_additions = 0
    total_deletions = 0
    folder_path = Path(session["folder_path"])

    for relative_path, original in session["files_original"].items():
        patched = session["files_patched"].get(relative_path, original)
        if original == patched:
            continue
        hunks, additions, deletions = _build_hunks(original, patched)
        total_additions += additions
        total_deletions += deletions
        files.append({
            "file_path": relative_path,
            "relative_path": relative_path,
            "status": "modified",
            "additions": additions,
            "deletions": deletions,
            "hunks": hunks,
        })

    package_info = session["package_info"]
    return {
        "session_id": session["session_id"],
        "package": package_info.get("name", ""),
        "from_version": package_info.get("current_version", ""),
        "to_version": package_info.get("latest_version", ""),
        "summary": {
            "total_files_changed": len(files),
            "total_additions": total_additions,
            "total_deletions": total_deletions,
        },
        "files": files,
    }

def _apply_partial_hunks(original: str, patched: str, accepted_hunk_ids: set[str]) -> str:
    hunks, _additions, _deletions = _build_hunks(original, patched)
    original_lines = original.splitlines()
    merged_lines = []
    cursor = 1

    for hunk in hunks:
        old_start = hunk["old_start"]
        old_end = old_start + hunk["old_lines"] - 1
        while cursor < old_start and cursor <= len(original_lines):
            merged_lines.append(original_lines[cursor - 1])
            cursor += 1

        if hunk["hunk_id"] in accepted_hunk_ids:
            for change in hunk["changes"]:
                if change["type"] in {"context", "addition"}:
                    merged_lines.append(change["content"])
        else:
            while cursor <= old_end and cursor <= len(original_lines):
                merged_lines.append(original_lines[cursor - 1])
                cursor += 1
            continue

        cursor = old_end + 1

    while cursor <= len(original_lines):
        merged_lines.append(original_lines[cursor - 1])
        cursor += 1

    trailing_newline = "\n" if original.endswith("\n") or patched.endswith("\n") else ""
    return "\n".join(merged_lines) + trailing_newline

# ------------------------------ Health Check Endpoint ---------------------------------
@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}

# ------------------------------ Check Providers Active --------------------------------
@app.get("/providers")
def get_providers():
    router = LLMRouter()
    status = router.get_providers_status()
    active_provider = next((p["name"] for p in status if p["status"] == "available"), "none")
    return {"providers": status, "active_provider": active_provider}

# ------------------------------ Project Impact Graph ----------------------------------
@app.post("/impact-graph")
def get_impact_graph(req: ImpactGraphRequest):
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    try:
        builder = ImpactGraphBuilder(str(folder_path))
        graph = builder.build(force_rebuild=req.force_rebuild)

        nodes = []
        edges = []
        for node_id, node in graph.nodes.items():
            location = node.location
            node_type = location.context_type
            nodes.append({
                "id": node_id,
                "label": location.name or "module level",
                "file": location.file,
                "type": node_type,
                "parent": location.parent,
                "startLine": location.start_line,
                "endLine": location.end_line,
                "source": location.source,
                "calls": node.calls,
                "referencesSymbols": node.references_symbols,
                "definesSymbols": node.defines_symbols,
                "callReturnUsage": node.call_return_usage,
            })

        for caller_id, callees in graph.calls.items():
            for callee_id in callees:
                if caller_id in graph.nodes and callee_id in graph.nodes:
                    edges.append({
                        "id": f"{caller_id}-->{callee_id}",
                        "source": caller_id,
                        "target": callee_id,
                        "type": "calls",
                    })

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "files": len({node["file"] for node in nodes}),
            },
        }
    except Exception as e:
        logger.error(f"Error building impact graph: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to build impact graph: {e}")

# ------------------------------ IDE File Explorer --------------------------------------
@app.get("/files")
def list_project_files(folder_path: str):
    folder_path_obj = Path(folder_path)
    if not folder_path_obj.exists() or not folder_path_obj.is_dir():
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    root = folder_path_obj.resolve()
    files = []
    try:
        for current_root, dirs, filenames in os.walk(root):
            dirs[:] = [name for name in dirs if name not in FILE_EXPLORER_IGNORE_DIRS]
            for filename in filenames:
                path = Path(current_root) / filename
                if not _is_explorer_file(path):
                    continue
                relative = path.relative_to(root).as_posix()
                files.append({
                    "path": relative,
                    "name": filename,
                    "extension": path.suffix.lower(),
                    "size": path.stat().st_size,
                })
        return {"files": sorted(files, key=lambda item: item["path"])}
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {e}")

@app.post("/file-content")
def get_file_content(req: FileContentRequest):
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    path = _safe_project_path(folder_path, req.file_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        relative = path.relative_to(folder_path.resolve()).as_posix()
        return {
            "path": relative,
            "content": _read_text_if_exists(path),
            "size": path.stat().st_size,
        }
    except Exception as e:
        logger.error(f"Error reading file content: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")

# ------------------------------ Preview and Review --------------------------------------
@app.post("/preview")
def preview_update(req: UpdateRequest):
    _cleanup_preview_sessions()
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    try:
        pkg_info_dict = req.package_info.dict()
        pkg = req.package_info
        package = pkg.name
        from_v = pkg.current_version
        to_v = pkg.latest_version

        files_original: dict[str, str] = {}
        files_patched: dict[str, str] = {}

        ast_scanner = ASTScanner()
        api_usages = ast_scanner.find_api_usages(str(folder_path), package)
        scout = ScoutAgent()
        scout_output = scout.run_sync(pkg_info_dict, api_usages)
        scout_output, ast_output = _scan_breaking_changes_with_review_fallback(
            ast_scanner,
            folder_path,
            scout_output,
            package,
            from_v,
            to_v,
            api_usages,
        )

        dep_relative = _relative_path(folder_path, pkg.file_path)
        dep_original, dep_patched = _dependency_preview_content(pkg.file_path, package, from_v, to_v)
        if dep_original != dep_patched:
            files_original[dep_relative] = dep_original
            files_patched[dep_relative] = dep_patched

        if ast_output.get("matches_by_file"):
            patch_agent = PatchAgent(project_root=str(folder_path))
            preview_report = patch_agent.preview_sync(scout_output, ast_output)
            for file_preview in preview_report.get("files", []):
                if file_preview.get("status") != "success":
                    continue
                relative = _relative_path(folder_path, file_preview.get("file", ""))
                original = file_preview.get("original", "")
                patched = file_preview.get("patched", original)
                if original != patched:
                    files_original[relative] = original
                    files_patched[relative] = patched

        session_id = f"preview_{uuid.uuid4().hex[:10]}"
        session = {
            "session_id": session_id,
            "folder_path": str(folder_path.resolve()),
            "package_info": pkg_info_dict,
            "files_original": files_original,
            "files_patched": files_patched,
            "created_at": time.time(),
        }
        PREVIEW_SESSIONS[session_id] = session
        return _preview_response(session)
    except Exception as e:
        logger.error(f"Error creating preview: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create preview: {e}")

@app.post("/apply")
def apply_preview(req: ApplyPreviewRequest):
    _cleanup_preview_sessions()
    session = PREVIEW_SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Preview session not found or expired")

    folder_path = Path(session["folder_path"])
    files_accepted = []
    files_rejected = []
    dependency_file_updated = ""

    try:
        for relative_path, original in session["files_original"].items():
            patched = session["files_patched"].get(relative_path, original)
            file_decision = req.decisions.get(relative_path, {})
            decision = file_decision.get("file_decision", "reject")

            if decision == "accept":
                final_content = patched
                files_accepted.append(relative_path)
            elif decision == "partial":
                accepted_hunks = {
                    hunk_id for hunk_id, hunk_decision in file_decision.get("hunks", {}).items()
                    if hunk_decision == "accept"
                }
                if not accepted_hunks:
                    files_rejected.append(relative_path)
                    continue
                final_content = _apply_partial_hunks(original, patched, accepted_hunks)
                files_accepted.append(relative_path)
            else:
                files_rejected.append(relative_path)
                continue

            target = _safe_project_path(folder_path, relative_path)
            target.write_text(final_content, encoding="utf-8")
            package_info = session["package_info"]
            try:
                dep_relative = _relative_path(folder_path, package_info.get("file_path", ""))
                if relative_path == dep_relative:
                    dependency_file_updated = Path(relative_path).name
            except Exception:
                pass

        PREVIEW_SESSIONS.pop(req.session_id, None)
        verification = ProjectChecker(str(folder_path)).run()
        repair = {
            "status": "skipped",
            "attempts": [],
            "final_verification": verification,
        }
        if (
            verification.get("status") == "failed"
            and files_accepted
            and _env_bool("DEPGUARD_AUTO_REPAIR", True)
        ):
            max_attempts = max(0, _env_int("DEPGUARD_REPAIR_MAX_ATTEMPTS", 1))
            repair_agent = RepairAgent(str(folder_path))
            attempts = []
            for attempt_number in range(1, max_attempts + 1):
                repair_report = repair_agent.repair_sync(verification, files_accepted)
                verification = ProjectChecker(str(folder_path)).run()
                attempts.append({
                    "attempt": attempt_number,
                    "status": repair_report.get("status", "failed"),
                    "files_repaired": repair_report.get("files_repaired", []),
                    "error": "; ".join(
                        item.get("error", "")
                        for item in repair_report.get("errors", [])
                        if item.get("error")
                    ) or None,
                    "final_verification": verification,
                })
                if verification.get("status") != "failed":
                    break
                if repair_report.get("status") not in {"success", "partial"}:
                    break
            repair = {
                "status": "success" if verification.get("status") == "passed" else "failed",
                "attempts": attempts,
                "final_verification": verification,
            }
        return {
            "status": "success",
            "files_accepted": files_accepted,
            "files_rejected": files_rejected,
            "dependency_file_updated": dependency_file_updated,
            "verification": verification,
            "repair": repair,
        }
    except Exception as e:
        logger.error(f"Error applying preview: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to apply preview: {e}")

@app.delete("/preview/{session_id}")
def discard_preview(session_id: str):
    PREVIEW_SESSIONS.pop(session_id, None)
    return {"status": "discarded"}

# ------------------------------ Browse Directory --------------------------------------
def _open_native_dialog():
    try:
        # Try zenity (Linux)
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Select Project Directory"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
            
        # Fallback to kdialog if zenity fails or user cancels
        if result.returncode != 1:  # 1 usually means cancel in zenity
            result = subprocess.run(
                ["kdialog", "--getexistingdirectory"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout.strip()
    except Exception as e:
        logger.error(f"Error with native dialog: {e}")
    return ""

@app.get("/browse")
async def browse_directory():
    try:
        folder = await asyncio.to_thread(_open_native_dialog)
        return {"path": folder or ""}
    except Exception as e:
        logger.error(f"Error opening folder dialog: {e}")
        return {"path": ""}

# ------------------------------ Scan Dependencies Project -------------------------------
@app.get("/scan-stream")
async def scan_project_stream(folder_path: str):
    folder_path_obj = Path(folder_path)
    if not folder_path_obj.exists() or not folder_path_obj.is_dir():
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    async def event_generator():
        try:
            # Phase 1: Scan
            yield f"data: {json.dumps({'phase': 'Scanning files', 'message': 'Finding manifests...'})}\n\n"
            scanner = ScannerAgent(str(folder_path_obj))
            
            # We run the scanner in a thread since it's synchronous
            scanner_output = await asyncio.to_thread(scanner.scan)
            
            total_packages = sum(len(f.get("packages", [])) for f in scanner_output)
            yield f"data: {json.dumps({'phase': 'Resolving versions', 'message': f'Found {total_packages} packages', 'total_packages': total_packages})}\n\n"
            
            queue = asyncio.Queue()
            async def progress_callback(msg):
                await queue.put(msg)
                
            async def run_watchdog():
                try:
                    watchdog = WatchdogAgent()
                    report = await watchdog.run(scanner_output, project_root=str(folder_path_obj), progress_callback=progress_callback)
                    await queue.put({"done": True, "report": report})
                except Exception as e:
                    await queue.put({"error": str(e)})
                
            task = asyncio.create_task(run_watchdog())
            
            while True:
                msg = await queue.get()
                if "error" in msg:
                    yield f"data: {json.dumps({'error': msg['error']})}\n\n"
                    break
                    
                if "done" in msg:
                    watchdog_report = msg["report"]
                    
                    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNPINNED": 0, "OK": 0}
                    for pkg in watchdog_report:
                        sev = pkg.get("severity", "OK")
                        counts[sev] = counts.get(sev, 0) + 1
                        
                    health_score = 100
                    if total_packages > 0:
                        score = (counts["OK"] * 100 + counts["LOW"] * 75 + counts["MEDIUM"] * 50 + counts["HIGH"] * 25 + counts["UNPINNED"] * 25 + counts["CRITICAL"] * 0)
                        health_score = int(score / total_packages)
                        
                    final_data = {
                        "phase": "Completed",
                        "folder_path": str(folder_path_obj),
                        "health_score": health_score,
                        "total_packages": total_packages,
                        "critical": counts["CRITICAL"],
                        "high": counts["HIGH"],
                        "medium": counts["MEDIUM"],
                        "low": counts["LOW"],
                        "unpinned": counts["UNPINNED"],
                        "ok": counts["OK"],
                        "packages": watchdog_report
                    }
                    yield f"data: {json.dumps(final_data)}\n\n"
                    break
                    
                yield f"data: {json.dumps(msg)}\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

# --------------------------------- Update Endpoint -----------------------------------
@app.post("/update")
def update_package(req: UpdateRequest):
    import time
    start_time = time.time()
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        logger.error(f"Update failed: Folder path {folder_path} does not exist")
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    try:
        pkg_info_dict = req.package_info.dict()
        
        ast_scanner = ASTScanner()
        
        # Step 1: Find API usages in codebase first
        api_usages = ast_scanner.find_api_usages(str(folder_path), pkg_info_dict.get("name", ""))
        
        # Step 2: Run targeted Scout
        scout = ScoutAgent()
        scout_output = scout.run_sync(pkg_info_dict, api_usages)
        
        breaking_changes = scout_output.get("breaking_changes", [])
        
        pkg = req.package_info
        is_unknown = pkg.current_version == "unknown"
        changed_file_candidates = {pkg.file_path}
        
        if not breaking_changes:
            # Just update version in dep file directly
            dep_file = pkg.file_path
            package = pkg.name
            from_v = pkg.current_version
            to_v = pkg.latest_version
            before_files = _capture_files(folder_path, [dep_file])
            
            # Simple version update implementation
            try:
                with open(dep_file, "r", encoding="utf-8") as f:
                    content = f.read()

                new_content = re.sub(
                    rf'({re.escape(package)}[^\d\n]*){re.escape(from_v)}',
                    rf'\g<1>{to_v}',
                    content,
                    flags=re.IGNORECASE
                )

                with open(dep_file, "w", encoding="utf-8") as f:
                    f.write(new_content)

                after_files = _capture_files(folder_path, [dep_file])
                changed_files = [
                    {"file": file_path, "before": before, "after": after_files.get(file_path, ""), "status": "modified"}
                    for file_path, before in before_files.items()
                    if before != after_files.get(file_path, "")
                ]
                
                return {
                    "package": package,
                    "status": "updated_version_only",
                    "api_usages_found": api_usages,
                    "version_was_unknown": is_unknown,
                    "pinned_to": to_v if is_unknown else "",
                    "files_patched": [],
                    "checkpoint_id": "",
                    "llm_provider": scout_output.get("llm_provider", "none"),
                    "fallback_used": False,
                    "changed_files": changed_files,
                    "latency_ms": int((time.time() - start_time) * 1000)
                }
            except Exception as e:
                logger.error(f"Error updating dependency file: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to update dependency file: {e}")

        # If breaking changes exist, run full ASTScanner for lines/cols
        ast_output = ast_scanner.scan(str(folder_path), breaking_changes)
        changed_file_candidates.update(ast_output.get("matches_by_file", {}).keys())
        before_files = _capture_files(folder_path, list(changed_file_candidates))
        
        # Run patch agent
        patch_agent = PatchAgent()
        patch_report = patch_agent.run_sync(scout_output, ast_output, req.package_info.file_path)
        patched_files = [item.get("file", "") for item in patch_report.get("files_patched", []) if item.get("file")]
        changed_file_candidates.update(patched_files)
        after_files = _capture_files(folder_path, list(changed_file_candidates))
        changed_files = []
        for file_path in sorted(set(before_files) | set(after_files)):
            before = before_files.get(file_path, "")
            after = after_files.get(file_path, "")
            if before != after:
                changed_files.append({
                    "file": file_path,
                    "before": before,
                    "after": after,
                    "status": "modified",
                })
        
        return {
            "package": patch_report.get("package"),
            "status": patch_report.get("overall_status"),
            "api_usages_found": api_usages,
            "version_was_unknown": is_unknown,
            "pinned_to": pkg.latest_version if is_unknown else "",
            "files_patched": patch_report.get("files_patched", []),
            "checkpoint_id": patch_report.get("checkpoint_id", ""),
            "llm_provider": patch_report.get("llm_provider", "none"),
            "fallback_used": patch_report.get("fallback_used", False),
            "changed_files": changed_files,
            "latency_ms": int((time.time() - start_time) * 1000)
        }
        
    except Exception as e:
        logger.error(f"Error in /update: {e}")
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")

# ---------------------------------- Rollback Endpoint -----------------------------------
@app.post("/rollback")
def rollback(req: RollbackRequest):
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        logger.error(f"Rollback failed: Folder path {folder_path} does not exist")
        raise HTTPException(status_code=400, detail="Folder path does not exist")
        
    try:
        # Find the checkpoint commit hash
        process = subprocess.run(
            ["git", "log", f"--grep={req.checkpoint_id}", "--format=%H", "-n", "1"],
            cwd=str(folder_path),
            capture_output=True,
            text=True
        )
        commit_hash = process.stdout.strip()
        
        if not commit_hash:
            logger.error(f"Rollback failed: Checkpoint {req.checkpoint_id} not found in git log")
            raise HTTPException(status_code=400, detail="Checkpoint not found in git log")
            
        # Reset to that commit exactly (the state BEFORE patching)
        subprocess.run(["git", "reset", "--hard", commit_hash], cwd=str(folder_path), check=True)
        
        return {
            "status": "success",
            "message": f"Successfully rolled back to checkpoint {req.checkpoint_id}"
        }
    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {e}")
        raise HTTPException(status_code=500, detail=f"Git command failed: {e.stderr}")
    except Exception as e:
        logger.error(f"Error in /rollback: {e}")
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")
