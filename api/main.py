import os
import sys
import logging
import subprocess
import re
import asyncio
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
    from tools.ast_scanner import ASTScanner
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
        
        if not breaking_changes:
            # Just update version in dep file directly
            dep_file = pkg.file_path
            package = pkg.name
            from_v = pkg.current_version
            to_v = pkg.latest_version
            
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
                    "latency_ms": int((time.time() - start_time) * 1000)
                }
            except Exception as e:
                logger.error(f"Error updating dependency file: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to update dependency file: {e}")

        # If breaking changes exist, run full ASTScanner for lines/cols
        ast_output = ast_scanner.scan(str(folder_path), breaking_changes)
        
        # Run patch agent
        patch_agent = PatchAgent()
        patch_report = patch_agent.run_sync(scout_output, ast_output, req.package_info.file_path)
        
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
