import asyncio
import logging
import json
import argparse
import sys
import re
from typing import List, Dict, Any
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

    def _map_deps_dev_ecosystem(self, ecosystem: str) -> str:
        mapping = {
            "pip": "PYPI",
            "npm": "NPM",
            "go": "GO",
            "cargo": "CARGO",
            "maven": "MAVEN"
        }
        return mapping.get(ecosystem, "")

    def _map_osv_ecosystem(self, ecosystem: str) -> str:
        mapping = {
            "pip": "PyPI",
            "npm": "npm",
            "go": "Go",
            "cargo": "crates.io",
            "maven": "Maven"
        }
        return mapping.get(ecosystem, "")

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

    def _classify_severity(self, current_version: str, latest_version: str, cves: list) -> str:
        if cves:
            return "CRITICAL"
        if current_version == "unknown" or latest_version == "unknown":
            return "OK" # Can't determine
        
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

    async def _process_package(self, client: httpx.AsyncClient, pkg: dict, ecosystem: str, file_path: str) -> Dict[str, Any]:
        name = pkg.get("name")
        current_version = pkg.get("version")
        
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
                
                for pkg in packages:
                    tasks.append(self._process_package(client, pkg, ecosystem, file_path))
                    
            results = await asyncio.gather(*tasks)
            
        return [r for r in results if r is not None]

    def run_sync(self, scanner_output: List[Dict]) -> List[Dict]:
        return asyncio.run(self.run(scanner_output))


def main():
    parser = argparse.ArgumentParser(description="DepGuard AI Watchdog Agent")
    parser.add_argument("path", help="Root folder path to scan")
    args = parser.parse_args()

    # Import ScannerAgent here to avoid circular imports and keep dependencies clean
    try:
        from agents.scanner import ScannerAgent
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.append(str(Path(__file__).parent.parent))
        from agents.scanner import ScannerAgent

    logger.info(f"Scanning directory: {args.path}")
    scanner = ScannerAgent(args.path)
    scanner_output = scanner.scan()
    
    logger.info("Watchdog is verifying packages with OSV.dev and deps.dev...")
    watchdog = WatchdogAgent()
    report = watchdog.run_sync(scanner_output)
    
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] != "test":
        main()
    else:
        # Run tests
        import unittest
        from unittest.mock import patch, MagicMock, AsyncMock

        class TestWatchdogAgent(unittest.TestCase):
            def setUp(self):
                self.agent = WatchdogAgent()
                self.scanner_output = [
                  {
                    "file_path": "requirements.txt",
                    "ecosystem": "pip",
                    "packages": [
                      {"name": "django", "version": "3.2.18"},
                      {"name": "requests", "version": "2.28.0"},
                      {"name": "unknown_pkg", "version": "unknown"}
                    ]
                  }
                ]

            @patch('httpx.AsyncClient.get', new_callable=AsyncMock)
            @patch('httpx.AsyncClient.post', new_callable=AsyncMock)
            def test_run_sync(self, mock_post, mock_get):
                # Setup mock for get (deps.dev)
                def get_side_effect(url, **kwargs):
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    if "django" in url:
                        mock_resp.json.return_value = {"versions": [{"isDefault": True, "versionKey": {"version": "5.0.6"}}]}
                    elif "requests" in url:
                        mock_resp.json.return_value = {"versions": [{"isDefault": True, "versionKey": {"version": "2.32.2"}}]}
                    else:
                        mock_resp.status_code = 404
                    return mock_resp
                mock_get.side_effect = get_side_effect

                # Setup mock for post (osv.dev)
                def post_side_effect(url, json=None, **kwargs):
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    if json and json["package"]["name"] == "requests":
                        mock_resp.json.return_value = {"vulns": [{"id": "CVE-2024-XXXX", "summary": "Test Vuln"}]}
                    else:
                        mock_resp.json.return_value = {}
                    return mock_resp
                mock_post.side_effect = post_side_effect

                results = self.agent.run_sync(self.scanner_output)
                
                # Check that unknown_pkg is skipped
                self.assertEqual(len(results), 2)
                
                django_res = next(r for r in results if r["name"] == "django")
                self.assertEqual(django_res["severity"], "HIGH")
                self.assertEqual(django_res["latest_version"], "5.0.6")
                self.assertEqual(len(django_res["cves"]), 0)
                
                req_res = next(r for r in results if r["name"] == "requests")
                self.assertEqual(req_res["severity"], "CRITICAL")
                self.assertEqual(req_res["latest_version"], "2.32.2")
                self.assertEqual(len(req_res["cves"]), 1)
                self.assertEqual(req_res["cves"][0]["id"], "CVE-2024-XXXX")

        sys.argv = [sys.argv[0]]
        print("Running Watchdog Agent Unit Tests...\\n")
        unittest.main()
