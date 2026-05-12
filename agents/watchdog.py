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

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

class WatchdogAgent:
    def __init__(self, max_concurrent: int = 10):
        self.semaphore = asyncio.Semaphore(max_concurrent)

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
    async def _fetch_cves(self, client: httpx.AsyncClient, name: str, version: str, ecosystem: str) -> List[Dict]:
        osv_eco = self._map_osv_ecosystem(ecosystem)
        if not osv_eco:
            return []
        
        url = "https://api.osv.dev/v1/query"
        payload = {
            "version": version,
            "package": {"name": name, "ecosystem": osv_eco}
        }
        async with self.semaphore:
            try:
                response = await client.post(url, json=payload, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    vulns = data.get("vulns", [])
                    return [{"id": v.get("id"), "summary": v.get("summary", "")} for v in vulns]
            except Exception as e:
                logger.debug(f"Error fetching CVEs for {name} {version}: {e}")
        return []

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
    async def _process_package(self, client: httpx.AsyncClient, pkg: dict, ecosystem: str, file_path: str) -> Optional[Dict[str, Any]]:
        name = pkg.get("name")
        current_version = pkg.get("version")

        if not isinstance(name, str) or not name:
            return None

        if not isinstance(current_version, str) or current_version == "unknown":
            return None
        
        # Skip if current version is unknown
        if not current_version or current_version == "unknown":
            return None
            
        # Fetch latest version and CVEs concurrently
        latest_task = self._fetch_latest_version(client, name, ecosystem)
        cves_task = self._fetch_cves(client, name, current_version, ecosystem)
        
        latest_version, cves = await asyncio.gather(latest_task, cves_task)
        
        severity = self._classify_severity(current_version, latest_version, cves)
        
        return {
            "name": name,
            "current_version": current_version,
            "latest_version": latest_version,
            "ecosystem": ecosystem,
            "severity": severity,
            "cves": cves,
            "file_path": file_path
        }

    async def run(self, scanner_output: List[Dict]) -> List[Dict]:
        tasks = []
        async with httpx.AsyncClient() as client:
            for file_data in scanner_output:
                file_path = file_data.get("file_path")
                ecosystem = file_data.get("ecosystem")
                packages = file_data.get("packages", [])
                
                if not isinstance(file_path, str):
                    continue

                if not isinstance(ecosystem, str):
                    continue

                if not isinstance(packages, list):
                    continue
                
                for pkg in packages:
                    tasks.append(self._process_package(client, pkg, ecosystem, file_path))
                    
            results = await asyncio.gather(*tasks)
            
        return [r for r in results if r is not None]

    def run_sync(self, scanner_output: List[Dict]) -> List[Dict]:
        return asyncio.run(self.run(scanner_output))
