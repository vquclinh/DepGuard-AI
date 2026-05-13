import asyncio
import logging
import json
import argparse
import sys
import re
from typing import List, Dict, Any, Optional
from urllib.parse import quote

try:
    import httpx
except ImportError:
    print("httpx is required. Please install it using `pip install httpx`")
    sys.exit(1)

try:
    from tools.lockfile_resolver import LockfileResolver
except ImportError:
    LockfileResolver = None  # type: ignore

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

class WatchdogAgent:
    def __init__(self, max_concurrent: int = 10):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._lockfile_resolver = LockfileResolver() if LockfileResolver else None

    # ------------------------------- For deps.dev -------------------------------
    def _map_deps_dev_ecosystem(self, ecosystem: str) -> str:
        mapping = {
            "pip": "PYPI",
            "npm": "NPM",
            "go": "GO",
            "cargo": "CARGO",
            "maven": "MAVEN"
        }
        return mapping.get(ecosystem, "")
    
    # ---------------------------------- For osv ----------------------------------
    def _map_osv_ecosystem(self, ecosystem: str) -> str:
        mapping = {
            "pip": "PyPI",
            "npm": "npm",
            "go": "Go",
            "cargo": "crates.io",
            "maven": "Maven"
        }
        return mapping.get(ecosystem, "")

    # ----------------------------- Get Latest Version ----------------------------
    async def _fetch_latest_version(self, client: httpx.AsyncClient, name: str, ecosystem: str) -> str:
        system = self._map_deps_dev_ecosystem(ecosystem)
        if not system:
            return "unknown"
        
        encoded_name = quote(name, safe='')
        url = f"https://api.deps.dev/v3/systems/{system}/packages/{encoded_name}"
        
        async with self.semaphore:
            try:
                response = await client.get(url, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    versions = data.get("versions", [])
                    # Try to find the default version first
                    for v in versions:
                        if v.get("isDefault"):
                            return v.get("versionKey", {}).get("version", "unknown")
                    # Fallback to the last version in the list if no default is found
                    if versions:
                        return versions[-1].get("versionKey", {}).get("version", "unknown")
            except Exception as e:
                logger.debug(f"Error fetching latest version for {name}: {e}")
        return "unknown"

    # ------------------------------- Get Vulneribilities --------------------------
    async def _fetch_cves_batch(self, client: httpx.AsyncClient, packages: List[Dict]) -> Dict[str, List[Dict]]:
        url = "https://api.osv.dev/v1/querybatch"
        queries = []
        mapping_keys = []

        for pkg in packages:
            osv_eco = self._map_osv_ecosystem(pkg["ecosystem"])
            version = pkg.get("version")
            
            # Only check CVE if not unknown
            if osv_eco and version and version != "unknown":
                queries.append({
                    "version": version,
                    "package": {"name": pkg["name"], "ecosystem": osv_eco}
                })
                mapping_keys.append(f"{pkg['ecosystem']}::{pkg['name']}::{version}")

        if not queries:
            return {}

        results_map = {}
        
        chunk_size = 500
        for i in range(0, len(queries), chunk_size):
            chunk_queries = queries[i:i + chunk_size]
            chunk_keys = mapping_keys[i:i + chunk_size]

            try:
                response = await client.post(url, json={"queries": chunk_queries}, timeout=20.0)
                if response.status_code == 200:
                    data = response.json()
                    batch_results = data.get("results", [])

                    for idx, res in enumerate(batch_results):
                        vulns = res.get("vulns", [])
                        parsed_vulns = [{"id": v.get("id"), "summary": v.get("summary", "")} for v in vulns]
                        results_map[chunk_keys[idx]] = parsed_vulns
            except Exception as e:
                logger.debug(f"Error fetching OSV batch: {e}")

        return results_map

    # --------------------------------- Risk Scoring System --------------------------
    def _classify_severity(self, current_version: str, latest_version: str, cves: list) -> str:
        if cves:
            return "CRITICAL"
        if current_version == "unknown" or latest_version == "unknown":
            return "OK"
        
        def parse_version(v: str) -> List[int]:
            # Simple semver parser ignoring pre-release tags for major/minor comparison
            # e.g., "3.2.18" -> [3, 2, 18], "^29.0.0" -> [29, 0, 0]
            # Strip anything that's not a digit or dot at the start (like ^, ~, v)
            v = re.sub(r'^[^\d]+', '', v)
            match = re.match(r'^(\d+)(?:\.(\d+))?(?:\.(\d+))?', v)
            if not match:
                return [0, 0, 0]
            return [int(x) if x else 0 for x in match.groups()]

        curr_parts = parse_version(current_version)
        latest_parts = parse_version(latest_version)

        if curr_parts[0] < latest_parts[0]:
            return "HIGH"
        elif curr_parts[0] == latest_parts[0] and curr_parts[1] < latest_parts[1]:
            return "MEDIUM"
        elif curr_parts[0] == latest_parts[0] and curr_parts[1] == latest_parts[1] and curr_parts[2] < latest_parts[2]:
            return "LOW"
        
        return "OK"

    # -------------------------- Full Process For Each Package ------------------------
    async def _process_package(
        self,
        client: httpx.AsyncClient,
        pkg: dict,
        cves_list: list,
        progress_callback = None
    ) -> Optional[Dict[str, Any]]:
        name = pkg.get("name")
        current_version = str(pkg.get("version", "unknown"))
        ecosystem = str(pkg.get("ecosystem", "unknown")) 
        pinned = pkg.get("pinned")
        resolved_from = pkg.get("resolved_from", "manifest")
        file_path = pkg.get("file_path")

        if not isinstance(name, str) or not name:
            return None

        if progress_callback:
            await progress_callback({"phase": "Fetching updates", "package": name})

        latest_version = await self._fetch_latest_version(client, name, ecosystem)

        # Unpinned Case
        if not pinned or current_version == "unknown":
            return {
                "name": name,
                "current_version": "unknown",
                "latest_version": latest_version,
                "ecosystem": ecosystem,
                "severity": "UNPINNED",
                "pinned": False,
                "resolved_from": "none",
                "cves": [],
                "file_path": file_path,
                "message": (
                    f"No version pinned. Installing latest ({latest_version}). "
                    "Scanning full changelog for breaking changes."
                )
            }

        severity = self._classify_severity(current_version, latest_version, cves_list)
        
        return {
            "name": name,
            "current_version": current_version,
            "latest_version": latest_version,
            "ecosystem": ecosystem,
            "severity": severity,
            "pinned": pinned,
            "resolved_from": resolved_from,
            "cves": cves_list,
            "file_path": file_path
        }

    async def run(self, scanner_output: List[Dict], project_root: str = "", progress_callback = None) -> List[Dict]:
        all_packages = []
        
        # Step 1: handle lockfile
        for file_data in scanner_output:
            file_path = file_data.get("file_path")
            ecosystem = file_data.get("ecosystem")
            packages = file_data.get("packages", [])
            
            if not isinstance(file_path, str) or not isinstance(ecosystem, str) or not isinstance(packages, list):
                continue
                
            for pkg in packages:
                name = pkg.get("name")
                current_version = pkg.get("version")
                
                if not isinstance(current_version, str):
                    current_version = "unknown"
                    
                pinned = pkg.get("pinned", current_version != "unknown")
                resolved_from = "manifest"

                if (not pinned or current_version == "unknown") and self._lockfile_resolver and project_root:
                    try:
                        lock_v = self._lockfile_resolver.resolve(name, project_root)
                        if lock_v:
                            current_version = lock_v
                            pinned = True
                            resolved_from = "lockfile"
                    except Exception as e:
                        logger.debug(f"LockfileResolver error for {name}: {e}")

                all_packages.append({
                    "name": name,
                    "version": current_version,
                    "pinned": pinned,
                    "ecosystem": ecosystem,
                    "file_path": file_path,
                    "resolved_from": resolved_from
                })

        # Call fetch cves batch
        tasks = []
        async with httpx.AsyncClient() as client:
            if progress_callback and all_packages:
                await progress_callback({
                    "phase": "Checking Vulnerabilities", 
                    "message": f"Batch querying OSV for {len(all_packages)} packages..."
                })
                
            cves_map = await self._fetch_cves_batch(client, all_packages)
            
            for pkg in all_packages:
                key = f"{pkg['ecosystem']}::{pkg['name']}::{pkg['version']}"
                pkg_cves = cves_map.get(key, [])
                
                tasks.append(
                    self._process_package(client, pkg, pkg_cves, progress_callback)
                )
                    
            results = await asyncio.gather(*tasks)
            
        return [r for r in results if r is not None]

    def run_sync(self, scanner_output: List[Dict], project_root: str = "", progress_callback = None) -> List[Dict]:
        return asyncio.run(self.run(scanner_output, project_root, progress_callback))
