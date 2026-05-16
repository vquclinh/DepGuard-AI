import os
import re
import json
import html
import asyncio
import inspect
import logging
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional
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
    CHANGELOG_PATHS = [
        "CHANGELOG.md",
        "CHANGELOG.rst",
        "CHANGELOG.txt",
        "CHANGES.md",
        "CHANGES.rst",
        "HISTORY.md",
        "HISTORY.rst",
        "RELEASES.md",
        "RELEASE_NOTES.md",
        "MIGRATION.md",
        "MIGRATING.md",
        "UPGRADE.md",
        "UPGRADING.md",
        "docs/CHANGELOG.md",
        "docs/changelog.md",
        "docs/changes.md",
        "docs/release-notes.md",
        "docs/release_notes.md",
        "docs/migration.md",
        "docs/migrating.md",
        "docs/upgrade.md",
        "docs/upgrading.md",
        "docs/source/whatsnew/index.rst",
        "doc/source/whatsnew/index.rst",
        "docs/source/release.rst",
        "doc/source/release.rst",
        "docs/source/release/index.rst",
        "doc/source/release/index.rst",
        "docs/releases/index.md",
        "doc/releases/index.md",
        "docs/migration/index.md",
        "docs/migration/index.rst",
        "docs/upgrading/index.md",
        "docs/upgrade/index.md",
        "docs/api/index.md",
        "docs/reference/index.md",
    ]

    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.router = LLMRouter() # Abstraction layer: OpenAI, Claude, Gemini, Qwen, local models
        self.reference_max_chars = self._env_int("SCOUT_REFERENCE_MAX_CHARS", 16000)
        self.changelog_file_max_chars = self._env_int("SCOUT_CHANGELOG_FILE_MAX_CHARS", 12000)
        self.analysis_max_chars = self._env_int("SCOUT_ANALYSIS_MAX_CHARS", 30000)
        self.analysis_max_tokens = self._env_int("SCOUT_LLM_MAX_TOKENS", 4000)
        self.evidence_window_chars = self._env_int("SCOUT_EVIDENCE_WINDOW_CHARS", 900)
        self.evidence_chunk_chars = self._env_int("SCOUT_EVIDENCE_CHUNK_CHARS", 1800)
        self.evidence_max_chunks = self._env_int("SCOUT_EVIDENCE_MAX_CHUNKS", 14)
        self.docs_url_fetch_limit = self._env_int("SCOUT_DOCS_URL_FETCH_LIMIT", 10)
        self.github_tree_doc_limit = self._env_int("SCOUT_GITHUB_TREE_DOC_LIMIT", 12)

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default

    def _github_headers(self) -> dict:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"
        return headers

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
                    normalized = self._normalize_repo_url(link.get("url", ""))
                    if "github.com" in normalized:
                        return normalized
        except Exception as e:
            logger.debug(f"Error fetching repo for {name}: {e}")
        return ""

    def _clean_version(self, version: str) -> str:
        cleaned = (version or "").strip()
        cleaned = re.sub(r"^[=~^<>!\s]+", "", cleaned)
        cleaned = cleaned.split(",", 1)[0].strip()
        return cleaned.lstrip("v")

    def _normalize_repo_url(self, url: str) -> str:
        value = (url or "").strip()
        if not value:
            return ""

        value = re.sub(r"^(git\+)", "", value)
        value = value.replace("git://github.com/", "https://github.com/")

        github_short = re.match(r"^github:([^/]+)/([^/#?]+)", value)
        if github_short:
            return f"https://github.com/{github_short.group(1)}/{github_short.group(2).removesuffix('.git')}"

        git_ssh = re.match(r"^git@github\.com:([^/]+)/([^/#?]+)", value)
        if git_ssh:
            return f"https://github.com/{git_ssh.group(1)}/{git_ssh.group(2).removesuffix('.git')}"

        if value.startswith("github.com/"):
            value = f"https://{value}"

        parsed = urlparse(value)
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")

        if host in {"github.com", "www.github.com"} or host.endswith("@github.com"):
            parts = path.split("/")
            if len(parts) >= 2:
                repo = parts[1].removesuffix(".git")
                return f"https://github.com/{parts[0]}/{repo}"

        return value

    async def _fetch_registry_references(
        self,
        client: httpx.AsyncClient,
        name: str,
        ecosystem: str,
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        ecosystem_key = (ecosystem or "").lower()
        if ecosystem_key in {"pip", "pypi"}:
            return await self._fetch_pypi_references(client, name, latest_version)
        if ecosystem_key == "npm":
            return await self._fetch_npm_references(client, name, latest_version)
        if ecosystem_key == "cargo":
            return await self._fetch_crates_references(client, name, latest_version)
        if ecosystem_key == "go":
            version = self._clean_version(latest_version)
            references = [{
                "source": "pkg.go.dev",
                "title": f"{name} documentation",
                "url": f"https://pkg.go.dev/{name}",
                "content": "",
            }]
            if version:
                references.append({
                    "source": "pkg.go.dev",
                    "title": f"{name} {version} documentation",
                    "url": f"https://pkg.go.dev/{name}@{version}",
                    "content": "",
                })
            return references
        if ecosystem_key == "maven":
            version = self._clean_version(latest_version)
            group_artifact = name.replace(":", "/")
            references = [{
                "source": "maven",
                "title": f"{name} artifact metadata",
                "url": f"https://mvnrepository.com/artifact/{group_artifact}",
                "content": "",
            }]
            parts = name.split(":", 1)
            if len(parts) == 2 and version:
                references.append({
                    "source": "javadoc.io",
                    "title": f"{name} {version} API docs",
                    "url": f"https://javadoc.io/doc/{parts[0]}/{parts[1]}/{version}/",
                    "content": "",
                })
            return references
        return []

    async def _fetch_pypi_references(self, client: httpx.AsyncClient, name: str, latest_version: str = "") -> list[dict]:
        try:
            response = await client.get(f"https://pypi.org/pypi/{quote(name, safe='')}/json", timeout=10.0)
            if response.status_code != 200:
                return []
            data = response.json()
            info = data.get("info", {}) or {}
            version = self._clean_version(latest_version or info.get("version", ""))
            references = [{
                "source": "pypi",
                "title": f"{name} PyPI metadata",
                "url": info.get("package_url") or f"https://pypi.org/project/{name}/",
                "content": "\n".join(filter(None, [
                    f"Summary: {info.get('summary', '')}",
                    f"Requires Python: {info.get('requires_python', '')}",
                    (info.get("description") or "")[:2000],
                ])),
            }]
            if version:
                references.append({
                    "source": "pypi",
                    "title": f"{name} {version} PyPI release",
                    "url": f"https://pypi.org/project/{name}/{version}/",
                    "content": "",
                })
            for title, url in (info.get("project_urls") or {}).items():
                references.append({
                    "source": "pypi",
                    "title": title,
                    "url": url,
                    "content": "",
                })
            for field in ["home_page", "docs_url", "download_url"]:
                if info.get(field):
                    references.append({
                        "source": "pypi",
                        "title": field.replace("_", " ").title(),
                        "url": info[field],
                        "content": "",
                    })
            return self._dedupe_references(references)
        except Exception as exc:
            logger.debug("Error fetching PyPI references for %s: %s", name, exc)
            return []

    async def _fetch_npm_references(self, client: httpx.AsyncClient, name: str, latest_version: str = "") -> list[dict]:
        try:
            response = await client.get(f"https://registry.npmjs.org/{quote(name, safe='@/')}", timeout=10.0)
            if response.status_code != 200:
                return []
            data = response.json()
            version = self._clean_version(latest_version)
            if not version:
                version = ((data.get("dist-tags") or {}).get("latest") or "").strip()
            version_data = (data.get("versions") or {}).get(version, {}) if version else {}
            references = [{
                "source": "npm",
                "title": f"{name} npm metadata",
                "url": f"https://www.npmjs.com/package/{name}",
                "content": "\n".join(filter(None, [
                    data.get("description", ""),
                    (data.get("readme") or "")[:2000],
                ])),
            }]
            if version:
                references.append({
                    "source": "npm",
                    "title": f"{name} {version} npm release",
                    "url": f"https://www.npmjs.com/package/{name}/v/{version}",
                    "content": (version_data.get("description") or "")[:1000],
                })
            for title, url in [
                ("homepage", data.get("homepage")),
                ("repository", self._repo_url_from_npm(data.get("repository"))),
                ("bugs", (data.get("bugs") or {}).get("url") if isinstance(data.get("bugs"), dict) else None),
            ]:
                if url:
                    references.append({"source": "npm", "title": title, "url": self._normalize_repo_url(url), "content": ""})
            if version:
                for path in ["CHANGELOG.md", "CHANGELOG", "CHANGES.md", "MIGRATION.md", "UPGRADE.md"]:
                    url = f"https://unpkg.com/{quote(name, safe='@/')}@{version}/{path}"
                    references.append({
                        "source": "unpkg",
                        "title": f"{path} at {version}",
                        "url": url,
                        "content": await self._fetch_text_url(client, url, 4000),
                    })
            return self._dedupe_references(references)
        except Exception as exc:
            logger.debug("Error fetching npm references for %s: %s", name, exc)
            return []

    async def _fetch_text_url(self, client: httpx.AsyncClient, url: str, max_chars: int) -> str:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code != 200:
                return ""
            text = response.text or ""
            if "<html" in text[:200].lower():
                text = self._html_to_text(text)
            return text[:max_chars]
        except Exception as exc:
            logger.debug("Error fetching text reference %s: %s", url, exc)
            return ""

    def _html_to_text(self, text: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|section|article|li|h[1-6]|tr)>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _repo_url_from_npm(self, repository) -> str:
        if isinstance(repository, str):
            return self._normalize_repo_url(repository)
        if isinstance(repository, dict):
            return self._normalize_repo_url(repository.get("url", ""))
        return ""

    async def _fetch_crates_references(self, client: httpx.AsyncClient, name: str, latest_version: str = "") -> list[dict]:
        try:
            response = await client.get(f"https://crates.io/api/v1/crates/{quote(name, safe='')}", timeout=10.0)
            if response.status_code != 200:
                return []
            crate = (response.json().get("crate") or {})
            version = self._clean_version(latest_version or crate.get("newest_version", "") or crate.get("max_version", ""))
            references = [{
                "source": "crates.io",
                "title": f"{name} crate metadata",
                "url": f"https://crates.io/crates/{name}",
                "content": crate.get("description", ""),
            }]
            if version:
                references.extend([
                    {
                        "source": "crates.io",
                        "title": f"{name} {version} crate release",
                        "url": f"https://crates.io/crates/{name}/{version}",
                        "content": "",
                    },
                    {
                        "source": "docs.rs",
                        "title": f"{name} {version} Rust API docs",
                        "url": f"https://docs.rs/{name}/{version}/",
                        "content": "",
                    },
                    {
                        "source": "docs.rs",
                        "title": f"{name} {version} crate source",
                        "url": f"https://docs.rs/crate/{name}/{version}/source/",
                        "content": "",
                    },
                    {
                        "source": "docs.rs",
                        "title": f"{name} latest Rust API docs",
                        "url": f"https://docs.rs/{name}/latest/",
                        "content": "",
                    },
                ])
            for title, url in [
                ("documentation", crate.get("documentation")),
                ("homepage", crate.get("homepage")),
                ("repository", crate.get("repository")),
            ]:
                if url:
                    references.append({"source": "crates.io", "title": title, "url": self._normalize_repo_url(url), "content": ""})
            return self._dedupe_references(references)
        except Exception as exc:
            logger.debug("Error fetching crates.io references for %s: %s", name, exc)
            return []

    async def _fetch_github_repo_metadata(self, client: httpx.AsyncClient, owner: str, repo: str) -> dict:
        try:
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=self._github_headers(),
                timeout=10.0,
            )
            if response.status_code == 200:
                return response.json()
        except Exception as exc:
            logger.debug("Error fetching GitHub metadata for %s/%s: %s", owner, repo, exc)
        return {}

    async def _fetch_github_changelog_files(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        default_branch: str,
        current_version: str = "",
        latest_version: str = "",
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        references = []
        branch = default_branch or "main"
        paths = self._dedupe_paths([
            *self.CHANGELOG_PATHS,
            *self._common_versioned_doc_candidates(current_version, latest_version),
            *self._api_doc_path_candidates(api_usages, api_contexts),
        ])
        for path in paths:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            try:
                response = await client.get(raw_url, timeout=10.0)
                if response.status_code != 200:
                    continue
                content = response.text[:self.changelog_file_max_chars]
                if not content.strip():
                    continue
                references.append({
                    "source": "github",
                    "title": path,
                    "url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
                    "content": content,
                })
                references.extend(await self._fetch_linked_changelog_pages(
                    client,
                    owner,
                    repo,
                    branch,
                    path,
                    content,
                    current_version,
                    latest_version,
                ))
            except Exception as exc:
                logger.debug("Error fetching changelog file %s: %s", raw_url, exc)
        references.extend(await self._fetch_github_tree_docs(
            client,
            owner,
            repo,
            branch,
            current_version,
            latest_version,
            api_usages,
            api_contexts,
            {reference.get("title", "") for reference in references},
        ))
        return references

    def _dedupe_paths(self, paths: list[str]) -> list[str]:
        seen = set()
        deduped = []
        for path in paths:
            normalized = path.strip().lstrip("/")
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _common_versioned_doc_candidates(self, current_version: str, latest_version: str) -> list[str]:
        versions = self._candidate_version_strings(current_version, latest_version)
        templates = [
            "doc/source/release/{version}-notes.rst",
            "docs/source/release/{version}-notes.rst",
            "doc/source/release/{version}.rst",
            "docs/source/release/{version}.rst",
            "doc/release/{version}-notes.rst",
            "docs/release/{version}-notes.rst",
            "docs/releases/{version}.md",
            "release/{version}.md",
            "releases/{version}.md",
        ]
        return [template.format(version=version) for version in versions for template in templates]

    def _api_doc_path_candidates(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[str]:
        symbols = self._api_symbols(api_usages, api_contexts)
        templates = [
            "docs/api/{slug}.md",
            "docs/api/{slug}.rst",
            "docs/reference/{slug}.md",
            "docs/reference/{slug}.rst",
            "docs/concepts/{slug}.md",
            "docs/concepts/{slug_plural}.md",
            "docs/usage/{slug}.md",
            "doc/source/reference/generated/{api}.rst",
            "docs/source/reference/generated/{api}.rst",
        ]
        candidates = []
        for symbol in symbols[:16]:
            slug = self._slugify_symbol(symbol)
            api = symbol.replace("/", ".").replace("::", ".")
            for template in templates:
                candidates.append(template.format(
                    slug=slug,
                    slug_plural=f"{slug}s",
                    api=api,
                ))
        return self._dedupe_paths(candidates)

    def _api_symbols(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[str]:
        symbols = []
        for usage in api_usages or []:
            usage = (usage or "").strip()
            if not usage:
                continue
            symbols.append(usage)
            parts = [part for part in re.split(r"[.:/]+", usage) if part]
            if len(parts) >= 2:
                symbols.append(parts[-1])
                symbols.append(".".join(parts[-2:]))
        for context in api_contexts or []:
            if not isinstance(context, dict):
                continue
            for key in ("api", "matched_text", "old_api"):
                value = str(context.get(key, "") or "").strip()
                if value:
                    symbols.append(value.strip("("))
            for method in re.findall(r"\.([A-Za-z_]\w*)\s*\(", str(context.get("code_snippet", "") or "")):
                symbols.append(method)

        seen = set()
        deduped = []
        for symbol in symbols:
            normalized = symbol.strip().strip(".")
            if not normalized or normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            deduped.append(normalized)
        return deduped

    def _slugify_symbol(self, symbol: str) -> str:
        parts = [part for part in re.split(r"[.:/()]+", symbol) if part]
        value = parts[-1] if parts else symbol
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
        return value or "api"

    async def _fetch_github_tree_docs(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        branch: str,
        current_version: str,
        latest_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]],
        already_fetched_paths: set[str],
    ) -> list[dict]:
        try:
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
                headers=self._github_headers(),
                params={"recursive": "1"},
                timeout=15.0,
            )
            if response.status_code != 200:
                return []
            tree = response.json().get("tree", []) or []
        except Exception as exc:
            logger.debug("Error fetching GitHub tree for %s/%s: %s", owner, repo, exc)
            return []

        scored_paths = []
        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if path in already_fetched_paths:
                continue
            score = self._score_doc_path(path, api_usages, api_contexts, current_version, latest_version)
            if score <= 0:
                continue
            scored_paths.append((score, path))

        scored_paths.sort(key=lambda item: item[0], reverse=True)
        references = []
        for score, path in scored_paths[:self.github_tree_doc_limit]:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            content = await self._fetch_text_url(client, raw_url, self.changelog_file_max_chars)
            if not content.strip():
                continue
            references.append({
                "source": "github",
                "title": path,
                "url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
                "content": content,
                "discovery": "github_tree",
                "path_score": score,
            })
        return references

    def _score_doc_path(
        self,
        path: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]],
        current_version: str,
        latest_version: str,
    ) -> int:
        lowered = path.lower()
        if not lowered.endswith((".md", ".rst", ".txt")):
            return 0
        if not any(part in lowered for part in ["doc", "docs", "change", "release", "migration", "upgrade", "whatsnew", "api", "reference"]):
            return 0
        if not self._path_versions_are_relevant(path, current_version, latest_version):
            return 0

        score = 0
        if any(term in lowered for term in ["migration", "migrating", "upgrade", "upgrading"]):
            score += 35
        if any(term in lowered for term in ["changelog", "changes", "history", "release", "whatsnew", "what"]):
            score += 25
        if any(term in lowered for term in ["api", "reference", "concept", "usage"]):
            score += 10
        if lowered.startswith(("docs/", "doc/")):
            score += 5

        for version in self._candidate_version_strings(current_version, latest_version):
            if version and version in lowered:
                score += 18

        for symbol in self._api_symbols(api_usages, api_contexts):
            slug = self._slugify_symbol(symbol)
            compact = re.sub(r"[^a-z0-9]+", "", symbol.lower())
            path_compact = re.sub(r"[^a-z0-9]+", "", lowered)
            if slug and slug in lowered:
                score += 22
            elif compact and compact in path_compact:
                score += 16

        return score

    def _path_versions_are_relevant(self, path: str, current_version: str, latest_version: str) -> bool:
        versions = self._versions_from_text(path)
        if not versions:
            return True
        return any(self._version_is_relevant(version, current_version, latest_version) for version in versions)

    def _versions_from_text(self, text: str) -> list[tuple[int, ...]]:
        versions = []
        for match in re.finditer(r"(?<!\d)v?(\d+\.\d+(?:\.\d+){0,2})(?!\d)", text or "", re.IGNORECASE):
            versions.append(self._version_tuple(match.group(1)))
        return versions

    def _candidate_version_strings(self, current_version: str, latest_version: str) -> list[str]:
        current = self._version_tuple(self._clean_version(current_version))
        latest = self._version_tuple(self._clean_version(latest_version))
        raw_versions = [
            self._clean_version(current_version),
            self._clean_version(latest_version),
        ]

        if len(current) >= 2 and len(latest) >= 2:
            low, high = sorted([current, latest])
            if low[0] == high[0] and 0 <= high[1] - low[1] <= 12:
                for minor in range(low[1], high[1] + 1):
                    raw_versions.append(f"{low[0]}.{minor}.0")

        versions = []
        seen = set()
        for version in raw_versions:
            version = (version or "").strip().lstrip("v")
            if not version or version in seen:
                continue
            seen.add(version)
            versions.append(version)
        return versions[:16]

    async def _fetch_linked_changelog_pages(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        branch: str,
        index_path: str,
        index_content: str,
        current_version: str,
        latest_version: str,
    ) -> list[dict]:
        candidates = self._linked_changelog_candidates(index_path, index_content, current_version, latest_version)
        references = []
        for path in candidates[:8]:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            try:
                response = await client.get(raw_url, timeout=10.0)
                if response.status_code != 200:
                    continue
                content = (response.text or "")[:self.changelog_file_max_chars]
                if not content.strip():
                    continue
                references.append({
                    "source": "github",
                    "title": path,
                    "url": f"https://github.com/{owner}/{repo}/blob/{branch}/{path}",
                    "content": content,
                })
            except Exception as exc:
                logger.debug("Error fetching linked changelog page %s: %s", raw_url, exc)
        return references

    def _linked_changelog_candidates(
        self,
        index_path: str,
        index_content: str,
        current_version: str,
        latest_version: str,
    ) -> list[str]:
        base_dir = Path(index_path).parent.as_posix()
        if base_dir == ".":
            base_dir = ""
        version_refs: list[tuple[str, tuple[int, ...]]] = []

        for line in index_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("..", "#", "*", "-")):
                continue
            if "://" in stripped:
                continue
            path_part = stripped.split()[0].strip().strip("`<>")
            if not re.search(r"v?\d+\.\d+", path_part):
                continue
            path_part = path_part.removesuffix(".rst").removesuffix(".md")
            version_match = re.search(r"v?(\d+(?:\.\d+){1,3})", path_part)
            if not version_match:
                continue
            version_tuple = self._version_tuple(version_match.group(1))
            if not self._version_is_relevant(version_tuple, current_version, latest_version):
                continue
            extension = ".rst" if index_path.endswith(".rst") else ".md"
            if base_dir and not path_part.startswith(f"{base_dir}/"):
                candidate = f"{base_dir}/{path_part}{extension}"
            elif "/" in path_part:
                candidate = f"{path_part}{extension}"
            elif not base_dir:
                candidate = f"{path_part}{extension}"
            else:
                candidate = f"{base_dir}/{path_part}{extension}"
            version_refs.append((candidate, version_tuple))

        version_refs.sort(key=lambda item: item[1], reverse=True)
        seen = set()
        candidates = []
        for path, _version in version_refs:
            if path in seen:
                continue
            seen.add(path)
            candidates.append(path)
        return candidates

    def _version_tuple(self, version: str) -> tuple[int, ...]:
        parts = re.findall(r"\d+", version or "")
        return tuple(int(part) for part in parts[:4])

    def _version_is_relevant(self, version: tuple[int, ...], current_version: str, latest_version: str) -> bool:
        if not version:
            return False
        current = self._version_tuple(self._clean_version(current_version))
        latest = self._version_tuple(self._clean_version(latest_version))
        if not current or not latest:
            return True
        low, high = sorted([current, latest])
        return low <= version <= high

    # Parse the repo
    def _parse_github_owner_repo(self, repo_url: str) -> tuple[str, str]:
        repo_url = self._normalize_repo_url(repo_url)
        parsed = urlparse(repo_url)
        path = parsed.path.strip("/")
        if path.endswith(".git"): path = path[:-4]
        parts = path.split("/")
        if len(parts) >= 2: return parts[0], parts[1]
        return "", ""

    def _github_owner_repo_from_references(self, references: list[dict]) -> tuple[str, str, str]:
        for reference in references:
            url = self._normalize_repo_url(reference.get("url", ""))
            if "github.com" not in url:
                continue
            owner, repo = self._parse_github_owner_repo(url)
            if owner and repo:
                return owner, repo, url
        return "", "", ""

    # Get Release History, Release Notes, Changelog Text
    async def _fetch_release_notes(self, client: httpx.AsyncClient, owner: str, repo: str, current_version: str, latest_version: str) -> str:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        headers = self._github_headers()
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

    def _dedupe_references(self, references: list[dict]) -> list[dict]:
        seen = set()
        deduped = []
        for reference in references:
            key = (reference.get("source", ""), reference.get("title", ""), reference.get("url", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(reference)
        return deduped

    def _format_references_for_llm(self, references: list[dict], max_chars: int | None = None) -> str:
        if not references:
            return "No external references were retrieved. Use exact project API usages and package/version context."
        max_chars = max_chars or self.reference_max_chars
        sections = []
        for index, reference in enumerate(self._dedupe_references(references), start=1):
            content = (reference.get("content") or "").strip()
            block = [
                f"[{index}] {reference.get('title') or reference.get('source')}",
                f"source: {reference.get('source', '')}",
                f"url: {reference.get('url', '')}",
            ]
            if reference.get("document_kind"):
                block.append(f"document_kind: {reference.get('document_kind')}")
            if reference.get("matched_terms"):
                block.append(f"matched_terms: {', '.join(reference.get('matched_terms', []))}")
            if content:
                block.append("excerpt:")
                block.append(content[:3000])
            sections.append("\n".join(block))
            if len("\n\n".join(sections)) > max_chars:
                sections.append("... references truncated ...")
                break
        return "\n\n".join(sections)[:max_chars]

    def _api_search_terms(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[str]:
        terms: set[str] = set()
        for usage in api_usages or []:
            if not usage:
                continue
            terms.add(usage)
            normalized = usage.replace("::", ".").replace("/", ".")
            parts = [part for part in normalized.split(".") if part]
            if len(parts) >= 2:
                terms.add(".".join(parts[-2:]))
                if len(parts[-1]) > 3:
                    terms.add(parts[-1])
            if len(parts) >= 3:
                terms.add(".".join(parts[-3:]))
        for context in api_contexts or []:
            if not isinstance(context, dict):
                continue
            for key in ("api", "old_api", "matched_text"):
                value = str(context.get(key, "") or "").strip()
                if value:
                    terms.add(value)
            snippet = str(context.get("code_snippet", "") or "")
            for method in re.findall(r"\.([A-Za-z_]\w*)\s*\(", snippet):
                if len(method) > 2:
                    terms.add(method)
        return sorted(terms, key=lambda item: (-len(item), item))

    def _root_package_terms(self, api_usages: Optional[list[str]]) -> set[str]:
        roots = set()
        for usage in api_usages or []:
            parts = [part for part in re.split(r"[.:/]+", usage or "") if part]
            if parts:
                roots.add(parts[0].lower())
        return roots

    def _document_kind(self, reference: dict) -> str:
        title = (reference.get("title") or "").lower()
        url = (reference.get("url") or "").lower()
        source = (reference.get("source") or "").lower()
        text = " ".join([title, url, source])
        if any(token in text for token in ["migration", "migrating", "upgrade", "upgrading"]):
            return "migration_guide"
        if any(token in text for token in ["changelog", "changes", "history", "release", "whatsnew", "what's new"]):
            return "release_notes"
        if any(token in text for token in ["docs.rs", "pkg.go.dev", "javadoc", "api docs", "documentation"]):
            return "api_docs"
        if any(token in text for token in ["pypi", "npm", "crates.io", "maven"]):
            return "registry"
        return "reference"

    def _has_migration_language(self, text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in [
            "removed", "remove", "deprecated", "deprecation", "renamed",
            "replaced", "replacement", "instead", "use ", "no longer",
            "breaking", "backwards incompatible", "migration", "upgrade",
            "changed signature", "signature", "requires", "must",
        ])

    def _normalized_evidence_text(self, text: str) -> str:
        value = (text or "").lower()
        value = re.sub(r":[a-z_]+:`~?([^`]+)`", r"\1", value)
        value = value.replace("``", "")
        value = value.replace("`", "")
        value = re.sub(r"\s+", " ", value)
        return value

    def _method_api_profiles(self, api_usages: Optional[list[str]]) -> list[dict]:
        profiles = []
        for usage in api_usages or []:
            parts = [part for part in re.split(r"[.:/]+", usage or "") if part]
            if len(parts) < 3:
                continue
            owner = parts[-2]
            method = parts[-1]
            if not owner or not method:
                continue
            profiles.append({
                "api": usage,
                "owner": owner,
                "method": method,
                "exact": f"{owner}.{method}",
            })
        return profiles

    def _method_evidence_score(self, text: str, api_usages: Optional[list[str]]) -> tuple[int, list[str]]:
        normalized = self._normalized_evidence_text(text)
        score = 0
        matches = []
        for profile in self._method_api_profiles(api_usages):
            owner = profile["owner"].lower()
            method = profile["method"].lower()
            exact = profile["exact"].lower()
            if exact in normalized:
                score += 90
                matches.append(profile["exact"])
                continue

            owner_positions = [match.start() for match in re.finditer(re.escape(owner), normalized)]
            method_positions = [match.start() for match in re.finditer(re.escape(method), normalized)]
            if not owner_positions or not method_positions:
                continue
            min_distance = min(abs(owner_pos - method_pos) for owner_pos in owner_positions for method_pos in method_positions)
            if min_distance <= 120:
                score += 35
                matches.append(f"{profile['owner']}~{profile['method']}")
        return score, matches

    def _split_reference_chunks(self, reference: dict) -> list[dict]:
        content = (reference.get("content") or "").strip()
        if not content:
            return []

        paragraphs = re.split(r"\n\s*\n", content)
        chunks: list[dict] = []
        current: list[str] = []
        current_start = 1
        line_cursor = 1

        def flush(end_line: int) -> None:
            nonlocal current, current_start
            text = "\n\n".join(part.strip() for part in current if part.strip()).strip()
            if text:
                chunks.append({
                    **reference,
                    "content": text[: self.evidence_chunk_chars + 300],
                    "line_start": current_start,
                    "line_end": max(current_start, end_line),
                    "document_kind": self._document_kind(reference),
                })
            current = []
            current_start = line_cursor

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                line_cursor += 1
                continue
            paragraph_lines = paragraph.count("\n") + 1
            pending = "\n\n".join(current + [paragraph])
            if current and len(pending) > self.evidence_chunk_chars:
                flush(line_cursor - 1)
            if not current:
                current_start = line_cursor
            if len(paragraph) > self.evidence_chunk_chars:
                for start in range(0, len(paragraph), self.evidence_chunk_chars):
                    current = [paragraph[start:start + self.evidence_chunk_chars]]
                    flush(line_cursor + paragraph_lines - 1)
            else:
                current.append(paragraph)
            line_cursor += paragraph_lines + 1

        if current:
            flush(line_cursor - 1)
        return chunks

    def _score_evidence_chunk(
        self,
        chunk: dict,
        terms: list[str],
        api_usages: Optional[list[str]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> tuple[int, list[str]]:
        text = " ".join([
            str(chunk.get("title", "")),
            str(chunk.get("url", "")),
            str(chunk.get("content", "")),
        ])
        lowered = text.lower()
        matched_terms = []
        score = 0

        for term in terms:
            term_lower = term.lower()
            if not term_lower:
                continue
            if term_lower in lowered:
                matched_terms.append(term)
                score += 10 + min(8, len(term_lower) // 5)

        change_terms = [
            "removed", "remove", "deprecated", "deprecation", "renamed",
            "replaced", "replacement", "instead", "use ", "no longer",
            "breaking", "backwards incompatible", "migration", "upgrade",
            "changed signature", "signature", "requires", "must",
        ]
        score += sum(3 for term in change_terms if term in lowered)
        method_score, method_matches = self._method_evidence_score(text, api_usages)
        score += method_score
        matched_terms.extend(method_matches)

        for version in [self._clean_version(current_version), self._clean_version(latest_version)]:
            if version and version.lower() in lowered:
                score += 2

        kind = chunk.get("document_kind")
        if kind == "migration_guide":
            score += 8
        elif kind == "release_notes":
            score += 6
        elif kind == "api_docs":
            score += 3

        return score, matched_terms

    def _ranked_evidence_chunks(
        self,
        references: list[dict],
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        terms = self._api_search_terms(api_usages, api_contexts)
        if not terms:
            return []
        root_terms = self._root_package_terms(api_usages)

        ranked: list[tuple[int, int, dict]] = []
        for reference_index, reference in enumerate(self._dedupe_references(references), start=1):
            reference_locator = " ".join([
                str(reference.get("title", "")),
                str(reference.get("url", "")),
            ])
            if not self._path_versions_are_relevant(reference_locator, current_version, latest_version):
                continue
            for chunk_index, chunk in enumerate(self._split_reference_chunks(reference), start=1):
                score, matched_terms = self._score_evidence_chunk(
                    chunk,
                    terms,
                    api_usages,
                    current_version,
                    latest_version,
                )
                strong_terms = [
                    term
                    for term in matched_terms
                    if term.lower() not in root_terms and len(term.strip()) > 2
                ]
                if not strong_terms:
                    if chunk.get("document_kind") == "registry":
                        continue
                    if not self._has_migration_language(str(chunk.get("content", ""))):
                        continue
                if score < 10 or not matched_terms:
                    continue
                ranked.append((score, -reference_index, {
                    "source": chunk.get("source", ""),
                    "title": chunk.get("title") or chunk.get("source") or "Reference",
                    "url": chunk.get("url", ""),
                    "content": chunk.get("content", ""),
                    "document_kind": chunk.get("document_kind", "reference"),
                    "line_start": chunk.get("line_start"),
                    "line_end": chunk.get("line_end"),
                    "matched_terms": sorted(set(matched_terms), key=lambda item: (-len(item), item))[:8],
                    "strong_terms": sorted(set(strong_terms), key=lambda item: (-len(item), item))[:8],
                    "score": score,
                    "reference_index": reference_index,
                    "chunk_index": chunk_index,
                }))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        evidence = []
        seen = set()
        for _score, _ref_order, chunk in ranked:
            key = (chunk["url"], chunk["content"][:160])
            if key in seen:
                continue
            seen.add(key)
            evidence.append(chunk)
            if len(evidence) >= self.evidence_max_chunks:
                break
        return evidence

    def _focused_reference_snippets(
        self,
        references: list[dict],
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        ranked_chunks = self._ranked_evidence_chunks(
            references,
            api_usages,
            api_contexts,
            current_version,
            latest_version,
        )
        if ranked_chunks:
            return [
                {
                    "source": chunk.get("source", ""),
                    "title": f"{chunk.get('title') or chunk.get('source')} (evidence chunk)",
                    "url": chunk.get("url", ""),
                    "content": chunk.get("content", ""),
                    "document_kind": chunk.get("document_kind", "reference"),
                    "matched_terms": chunk.get("matched_terms", []),
                    "strong_terms": chunk.get("strong_terms", []),
                    "score": chunk.get("score", 0),
                    "line_start": chunk.get("line_start"),
                    "line_end": chunk.get("line_end"),
                }
                for chunk in ranked_chunks
            ]

        terms = self._api_search_terms(api_usages, api_contexts)
        if not terms:
            return []
        root_terms = self._root_package_terms(api_usages)

        focused = []
        for reference in self._dedupe_references(references):
            content = (reference.get("content") or "").strip()
            if not content:
                continue

            windows = []
            lowered = content.lower()
            matched_terms = set()
            for term in terms:
                term_lower = term.lower()
                start = 0
                while True:
                    index = lowered.find(term_lower, start)
                    if index < 0:
                        break
                    matched_terms.add(term)
                    window_start = max(0, index - self.evidence_window_chars)
                    window_end = min(len(content), index + len(term) + self.evidence_window_chars)
                    windows.append((window_start, window_end))
                    start = index + len(term_lower)

            if not windows:
                continue
            strong_terms = [
                term
                for term in matched_terms
                if term.lower() not in root_terms and len(term.strip()) > 2
            ]
            if not strong_terms:
                if self._document_kind(reference) == "registry":
                    continue
                if not self._has_migration_language(content):
                    continue

            merged = []
            for start, end in sorted(windows):
                if not merged or start > merged[-1][1] + 80:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)

            snippets = []
            for start, end in merged[:4]:
                prefix = "... " if start > 0 else ""
                suffix = " ..." if end < len(content) else ""
                snippets.append(f"{prefix}{content[start:end].strip()}{suffix}")

            if snippets:
                focused.append({
                    **reference,
                    "title": f"{reference.get('title') or reference.get('source')} (focused evidence)",
                    "content": "\n\n---\n\n".join(snippets),
                    "document_kind": self._document_kind(reference),
                    "matched_terms": sorted(matched_terms, key=lambda item: (-len(item), item))[:8],
                    "strong_terms": sorted(strong_terms, key=lambda item: (-len(item), item))[:8],
                })

        return focused

    async def _collect_references(
        self,
        client: httpx.AsyncClient,
        package: str,
        ecosystem: str,
        repo_url: str,
        current_version: str = "",
        latest_version: str = "",
        owner: str = "",
        repo: str = "",
        release_notes: str = "",
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        references = []
        repo_url = self._normalize_repo_url(repo_url)
        references.extend(await self._fetch_registry_references(
            client,
            package,
            ecosystem,
            current_version,
            latest_version,
        ))

        if repo_url:
            references.append({
                "source": "deps.dev",
                "title": "Repository",
                "url": repo_url,
                "content": "",
            })

        if not owner or not repo:
            owner, repo, derived_repo_url = self._github_owner_repo_from_references(references)
            if derived_repo_url and not repo_url:
                repo_url = derived_repo_url

        if owner and repo:
            metadata = await self._fetch_github_repo_metadata(client, owner, repo)
            default_branch = "main"
            if metadata:
                default_branch = metadata.get("default_branch", "main")
                references.append({
                    "source": "github",
                    "title": "Repository metadata",
                    "url": metadata.get("html_url", repo_url),
                    "content": "\n".join(filter(None, [
                        metadata.get("description", ""),
                        f"default_branch: {default_branch}",
                    ])),
                })
            references.extend(await self._fetch_github_changelog_files(
                client,
                owner,
                repo,
                default_branch,
                current_version,
                latest_version,
                api_usages,
                api_contexts,
            ))

        if release_notes.strip():
            references.insert(0, {
                "source": "github",
                "title": "GitHub release notes",
                "url": repo_url,
                "content": release_notes,
            })

        references.extend(await self._fetch_docs_url_references(
            client,
            references,
            package,
            current_version,
            latest_version,
            api_usages,
            api_contexts,
        ))
        return self._dedupe_references(references)

    async def _fetch_docs_url_references(
        self,
        client: httpx.AsyncClient,
        references: list[dict],
        package: str,
        current_version: str,
        latest_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]],
    ) -> list[dict]:
        candidates = self._docs_url_candidates(
            references,
            package,
            current_version,
            latest_version,
            api_usages,
            api_contexts,
        )
        fetched = []
        for title, url in candidates[:self.docs_url_fetch_limit]:
            content = await self._fetch_text_url(client, url, self.changelog_file_max_chars)
            if not content.strip():
                continue
            fetched.append({
                "source": "docs",
                "title": title,
                "url": url,
                "content": content,
                "discovery": "docs_url",
            })
        return fetched

    def _docs_url_candidates(
        self,
        references: list[dict],
        package: str,
        current_version: str,
        latest_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]],
    ) -> list[tuple[str, str]]:
        urls = set()
        for reference in references:
            url = (reference.get("url") or "").strip()
            if self._is_docs_like_url(url):
                urls.add(url)
            content = reference.get("content") or ""
            for found in re.findall(r"https?://[^\s)>\"]+", content):
                found = found.rstrip(".,;")
                if self._is_docs_like_url(found):
                    urls.add(found)

        candidates: list[tuple[str, str]] = []
        for url in sorted(urls):
            base = self._docs_base_url(url)
            if not base:
                continue
            candidates.append(("Documentation page", url))
            for suffix in self._docs_suffix_candidates(package, current_version, latest_version, api_usages, api_contexts):
                candidates.append((suffix.strip("/") or "Documentation", f"{base}/{suffix.lstrip('/')}"))

        seen = set()
        deduped = []
        for title, url in candidates:
            normalized = url.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append((title, normalized))
        return deduped

    def _is_docs_like_url(self, url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        text = f"{host}{path}"
        if "github.com" in host:
            return False
        return any(token in text for token in [
            "docs", "documentation", "readthedocs", "gitbook", "docusaurus",
            "mkdocs", "api", "reference", "migration", "upgrade", "changelog",
        ])

    def _docs_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        path = parsed.path.strip("/")
        parts = [part for part in path.split("/") if part]
        base_parts = []
        if parts and parts[0] in {"latest", "stable", "dev", "main"}:
            base_parts.append(parts[0])
        elif len(parts) >= 2 and re.match(r"v?\d+(?:\.\d+)*", parts[0]):
            base_parts.append(parts[0])
        return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(base_parts)}".rstrip("/")

    def _docs_suffix_candidates(
        self,
        package: str,
        current_version: str,
        latest_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]],
    ) -> list[str]:
        suffixes = [
            "llms.txt",
            "llms-full.txt",
            "migration/",
            "migrations/",
            "upgrade/",
            "upgrading/",
            "changelog/",
            "release-notes/",
            "releases/",
            "api/",
            "reference/",
        ]
        for version in self._candidate_version_strings(current_version, latest_version):
            suffixes.extend([
                f"{version}/",
                f"v{version}/",
                f"{version}/migration/",
                f"v{version}/migration/",
            ])
        for symbol in self._api_symbols(api_usages, api_contexts):
            slug = self._slugify_symbol(symbol)
            suffixes.extend([
                f"api/{slug}/",
                f"reference/{slug}/",
                f"concepts/{slug}/",
                f"usage/{slug}/",
            ])
        return self._dedupe_paths(suffixes)

    def _references_changelog_text(
        self,
        references: list[dict],
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> str:
        focused = self._focused_reference_snippets(
            references,
            api_usages,
            api_contexts,
            current_version,
            latest_version,
        )

        sections = []
        for index, reference in enumerate(focused, start=1):
            content = (reference.get("content") or "").strip()
            if not content:
                continue
            sections.append(
                "\n".join([
                    f"## Evidence chunk {index}: {reference.get('title')}",
                    f"source: {reference.get('source', '')}",
                    f"url: {reference.get('url', '')}",
                    f"document_kind: {reference.get('document_kind', '')}",
                    f"matched_terms: {', '.join(reference.get('matched_terms', []))}",
                    f"strong_terms: {', '.join(reference.get('strong_terms', []))}",
                    content,
                ])
            )
        return "\n\n".join(sections)

    async def _analyze_references(
        self,
        result: dict,
        package: str,
        from_v: str,
        to_v: str,
        api_usages: Optional[list[str]],
        references: list[dict],
        api_contexts: Optional[list[dict]] = None,
    ) -> dict:
        evidence_references = self._focused_reference_snippets(
            references,
            api_usages,
            api_contexts,
            from_v,
            to_v,
        )
        result["evidence_references"] = evidence_references
        changelog_text = self._references_changelog_text(
            references,
            api_usages,
            api_contexts,
            from_v,
            to_v,
        )
        if evidence_references and changelog_text.strip():
            llm_result = await self._analyze_changelog_with_llm(
                package,
                from_v,
                to_v,
                changelog_text,
                api_usages,
                evidence_references or references,
            )
            result["breaking_changes"] = llm_result.get("breaking_changes", [])
            result["api_evidence"] = llm_result.get("api_evidence", [])
            result["confidence_score"] = llm_result.get("confidence_score", 0.0)
            result["llm_provider"] = llm_result.get("llm_provider", "none")
            if result["breaking_changes"]:
                return result

        return result

    # Send Changelog for LLM
    async def _analyze_changelog_with_llm(
        self,
        package: str,
        from_v: str,
        to_v: str,
        changelog: str,
        api_usages: Optional[list[str]] = None,
        references: Optional[list[dict]] = None,
    ) -> dict:
        system_prompt = (
            "You are a senior dependency management AI for Python, Rust, Go, JavaScript, TypeScript, Java, "
            "and other ecosystems. Extract ONLY documented migration facts relevant to code usage. "
            "Use release notes, changelogs, migration guides, registry metadata, and docs references. "
            "Format strictly as JSON."
        )
        
        usage_constraint = ""
        if api_usages:
            usages_str = ", ".join(api_usages)
            usage_constraint = f"\nCRITICAL: You MUST ONLY extract breaking changes that affect these specific APIs used in the project: [{usages_str}]. Ignore all other breaking changes."

        prompt = f"""
            Analyze DepGuard's compact evidence dossier for '{package}' from version {from_v} to {to_v} and extract ONLY breaking changes relevant to the package's ecosystem and API usage:{usage_constraint}
            The dossier is already retrieved and ranked. Do not use outside knowledge to add APIs, replacements, or version facts.
            Prefer old_api values that match the exact APIs used by the project when possible.
            Fill new_api only when an evidence chunk documents a replacement, renamed API, changed signature, or clear migration pattern.
            If a referenced change needs review but no replacement is documented, leave new_api empty and explain why.
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
            "api_evidence": [
                {{
                "api": "API affected",
                "change_type": "removed|renamed|changed_signature|behavior_change|migration_review",
                "replacement": "replacement/workaround or empty string",
                "confidence": "high|medium|low",
                "evidence": [
                    {{
                    "source_index": 1,
                    "source": "release_notes|migration_guide|api_docs|registry|reference",
                    "url": "source URL",
                    "quote": "short supporting quote from the evidence dossier"
                    }}
                ],
                "reason": "one sentence explaining why this evidence applies"
                }}
            ],
            "confidence_score": 0.9
            }}
            Compact evidence chunks gathered by DepGuard:
            {self._format_references_for_llm(references or [])}

            Evidence dossier:
            {changelog[:self.analysis_max_chars]}
            """
        try:
            response = await self.router.complete(system_prompt, prompt, max_tokens=self.analysis_max_tokens, task_type="changelog")
            data = self._extract_json_object(response.content)
            if data:
                data["llm_provider"] = response.provider
                return data
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
        return {"breaking_changes": [], "confidence_score": 0.0, "llm_provider": "none"}

    def _extract_json_object(self, text: str) -> dict | None:
        decoder = json.JSONDecoder()
        stripped = (text or "").strip()
        for index, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                data, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    async def _close_router(self) -> None:
        close = getattr(self.router, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def run(
        self,
        package_info: dict,
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
    ) -> dict:
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
            "package": name,
            "from_version": current_version,
            "to_version": latest_version,
            "breaking_changes": [],
            "confidence_score": 0.0,
            "changelog_url": "",
            "references": [],
            "evidence_references": [],
            "api_evidence": [],
        }
        try:
            async with httpx.AsyncClient() as client:
                repo_url = await self._get_github_repo(client, name, ecosystem)
                if not repo_url:
                    references = await self._collect_references(
                        client,
                        name,
                        ecosystem,
                        repo_url="",
                        current_version=current_version,
                        latest_version=latest_version,
                        api_usages=api_usages,
                        api_contexts=api_contexts,
                    )
                    result["references"] = references
                    return await self._analyze_references(
                        result,
                        name,
                        current_version,
                        latest_version,
                        api_usages,
                        references,
                        api_contexts,
                    )

                owner, repo = self._parse_github_owner_repo(repo_url)
                if not owner or not repo:
                    references = await self._collect_references(
                        client,
                        name,
                        ecosystem,
                        repo_url=repo_url,
                        current_version=current_version,
                        latest_version=latest_version,
                        api_usages=api_usages,
                        api_contexts=api_contexts,
                    )
                    result["references"] = references
                    return await self._analyze_references(
                        result,
                        name,
                        current_version,
                        latest_version,
                        api_usages,
                        references,
                        api_contexts,
                    )
                
                result["changelog_url"] = f"https://github.com/{owner}/{repo}/releases"

                changelog_text = await self._fetch_release_notes(client, owner, repo, current_version, latest_version)
                references = await self._collect_references(
                    client,
                    name,
                    ecosystem,
                    repo_url=repo_url,
                    current_version=current_version,
                    latest_version=latest_version,
                    owner=owner,
                    repo=repo,
                    release_notes=changelog_text,
                    api_usages=api_usages,
                    api_contexts=api_contexts,
                )
                result["references"] = references
                return await self._analyze_references(
                    result,
                    name,
                    current_version,
                    latest_version,
                    api_usages,
                    references,
                    api_contexts,
                )
        finally:
            await self._close_router()
        return result

    def run_sync(
        self,
        package_info: dict,
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
    ) -> dict:
        return asyncio.run(self.run(package_info, api_usages, api_contexts))
