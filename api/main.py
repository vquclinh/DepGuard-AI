import os
import sys
import logging
import subprocess
import re
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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

# ------------------------------ Scan Dependencies Project -------------------------------
@app.post("/scan")
def scan_project(req: ScanRequest):
    folder_path = Path(req.folder_path)
    if not folder_path.exists() or not folder_path.is_dir():
        logger.error(f"Scan failed: Folder path {folder_path} does not exist")
        raise HTTPException(status_code=400, detail="Folder path does not exist or is not a directory")

    try:
        # Phase 1: Scan
        scanner = ScannerAgent(str(folder_path))
        scanner_output = scanner.scan()
        
        # Phase 2: Watchdog
        watchdog = WatchdogAgent()
        watchdog_report = watchdog.run_sync(scanner_output)
        
        # Compute health score
        total_packages = len(watchdog_report)
        counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "OK": 0}
        
        for pkg in watchdog_report:
            sev = pkg.get("severity", "OK")
            counts[sev] = counts.get(sev, 0) + 1
            
        health_score = 100
        if total_packages > 0:
            score = (counts["OK"] * 100 + counts["LOW"] * 75 + counts["MEDIUM"] * 50 + counts["HIGH"] * 25 + counts["CRITICAL"] * 0)
            health_score = int(score / total_packages)
            
        return {
            "folder_path": str(folder_path),
            "health_score": health_score,
            "total_packages": total_packages,
            "critical": counts["CRITICAL"],
            "high": counts["HIGH"],
            "medium": counts["MEDIUM"],
            "low": counts["LOW"],
            "ok": counts["OK"],
            "packages": watchdog_report
        }
    except Exception as e:
        logger.error(f"Error in /scan: {e}")
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")

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
        
        # Phase 3: Scout
        scout = ScoutAgent()
        scout_output = scout.run_sync(pkg_info_dict)
        
        breaking_changes = scout_output.get("breaking_changes", [])
        
        if not breaking_changes:
            # Just update version in dep file directly
            pkg = req.package_info

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
                    "files_patched": [],
                    "checkpoint_id": "",
                    "llm_provider": scout_output.get("llm_provider", "none"),
                    "fallback_used": False,
                    "latency_ms": int((time.time() - start_time) * 1000)
                }
            except Exception as e:
                logger.error(f"Error updating dependency file: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to update dependency file: {e}")

        # If breaking changes exist, run ASTScanner and PatchAgent
        ast_scanner = ASTScanner()
        ast_output = ast_scanner.scan(str(folder_path), breaking_changes)
        
        # Run patch agent
        patch_agent = PatchAgent()
        patch_report = patch_agent.run_sync(scout_output, ast_output, req.package_info.file_path)
        
        return {
            "package": patch_report.get("package"),
            "status": patch_report.get("overall_status"),
            "files_patched": patch_report.get("files_patched", []),
            "checkpoint_id": patch_report.get("checkpoint_id", ""),
            "llm_provider": patch_report.get("llm_provider", "none"),
            "fallback_used": patch_report.get("fallback_used", False),
            "latency_ms": int((time.time() - start_time) * 1000)
        }
        
    except Exception as e:
        logger.error(f"Error in /update: {e}")
        raise HTTPException(status_code=500, detail=f"Agent Error: {str(e)}")

# -------------------------------------- Rollback ---------------------------------------
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
