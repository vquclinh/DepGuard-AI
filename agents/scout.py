import os
import re
import json
import asyncio
import logging
import argparse
import sys
from typing import Dict, List, Any
from urllib.parse import quote, urlparse
from dotenv import load_dotenv

try:
    import httpx
except ImportError:
    print("httpx is required. Install with: pip install httpx")
    sys.exit(1)

try:
    from tools.llm_router import LLMRouter
except ImportError:
    print("llm_router is required. Ensure you are running from the project root.")
    sys.exit(1)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --------------------------------- Scout Agent ------------------------------------
class ScoutAgent:
    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.router = LLMRouter() # Abstraction layer: OpenAI, Claude, Gemini, Qwen, local models

    def _map_deps_dev_ecosystem(self, ecosystem: str) -> str:
        mapping = {
            "pip": "PYPI",
            "npm": "NPM",
            "go": "GO",
            "cargo": "CARGO",
            "maven": "MAVEN"
        }
        return mapping.get(ecosystem, "")

    # Get Github Repo (call Deps Dev API)
    async def _get_github_repo(self, client: httpx.AsyncClient, name: str, ecosystem: str) -> str:
        system = self._map_deps_dev_ecosystem(ecosystem)
        if not system: return ""
        encoded_name = quote(name, safe='')
        url = f"https://api.deps.dev/v3/systems/{system}/packages/{encoded_name}"
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                for link in data.get("links", []):
                    if "github.com" in link.get("url", ""):
                        return link.get("url", "")
        except Exception as e:
            logger.debug(f"Error fetching repo for {name}: {e}")
        return ""

    # Parse the repo
    def _parse_github_owner_repo(self, repo_url: str) -> tuple[str, str]:
        parsed = urlparse(repo_url)
        path = parsed.path.strip("/")
        if path.endswith(".git"): path = path[:-4]
        parts = path.split("/")
        if len(parts) >= 2: return parts[0], parts[1]
        return "", ""

    # Get Release History, Release Notes, Changelog Text
    async def _fetch_release_notes(self, client: httpx.AsyncClient, owner: str, repo: str, current_version: str, latest_version: str) -> str:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token: headers["Authorization"] = f"token {self.github_token}"
        def clean_version(v): return re.sub(r'^[^\d]+', '', v)
        cv = clean_version(current_version)
        release_notes = []
        try:
            response = await client.get(url, headers=headers, params={"per_page": 100}, timeout=15.0)
            if response.status_code == 200:
                for release in response.json():
                    tag_name = release.get("tag_name", "")
                    clean_tag = clean_version(tag_name)
                    release_notes.append(f"## Version {tag_name}\n{release.get('body', '')}\n")
                    if clean_tag == cv or tag_name == current_version: break
        except Exception as e:
            logger.debug(f"Error fetching releases for {owner}/{repo}: {e}")
        return "\n".join(release_notes)

    # Send Changelog for LLM
    async def _analyze_changelog_with_llm(self, package: str, from_v: str, to_v: str, changelog: str) -> dict:
        system_prompt = "You are a senior dependency management AI. Extract ONLY breaking changes from changelogs relevant to code usage. Format strictly as JSON."
        prompt = f"""
            Analyze the changelog for '{package}' from version {from_v} to {to_v} and extract ONLY breaking changes relevant to Python/the ecosystem API usage:
            Return a JSON object with this exact structure:
            {{
            "breaking_changes": [
                {{
                "type": "removed|renamed|changed_signature",
                "old_api": "fully qualified or clear name",
                "new_api": "new name or workaround if applicable",
                "description": "short description"
                }}
            ],
            "confidence_score": 0.9
            }}
            Changelog:
            {changelog[:8000]}
            """
        try:
            response = await self.router.complete(system_prompt, prompt, max_tokens=1000, task_type="changelog")
            text = response.content
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                data["llm_provider"] = response.provider
                return data
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
        return {"breaking_changes": [], "confidence_score": 0.0, "llm_provider": "none"}

    async def run(self, package_info: dict) -> dict:
        name = package_info.get("name")
        current_version = package_info.get("current_version")
        latest_version = package_info.get("latest_version")
        ecosystem = package_info.get("ecosystem")

        if not isinstance(name, str):
            raise ValueError("Invalid package name")

        if not isinstance(current_version, str):
            raise ValueError("Invalid current_version")

        if not isinstance(latest_version, str):
            raise ValueError("Invalid latest_version")

        if not isinstance(ecosystem, str):
            raise ValueError("Invalid ecosystem")

        result = {
            "package": name, "from_version": current_version, "to_version": latest_version,
            "breaking_changes": [], "confidence_score": 0.0, "changelog_url": ""
        }
        async with httpx.AsyncClient() as client:
            repo_url = await self._get_github_repo(client, name, ecosystem)
            if not repo_url: return result

            owner, repo = self._parse_github_owner_repo(repo_url)
            if not owner or not repo: return result
            
            result["changelog_url"] = f"https://github.com/{owner}/{repo}/releases"

            changelog_text = await self._fetch_release_notes(client, owner, repo, current_version, latest_version)
            if not changelog_text: return result
            
            llm_result = await self._analyze_changelog_with_llm(name, current_version, latest_version, changelog_text)
            result["breaking_changes"] = llm_result.get("breaking_changes", [])
            result["confidence_score"] = llm_result.get("confidence_score", 0.0)
            result["llm_provider"] = llm_result.get("llm_provider", "none")
        return result

    def run_sync(self, package_info: dict) -> dict:
        return asyncio.run(self.run(package_info))
