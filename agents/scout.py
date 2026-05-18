import os
import re
import json
import html
import asyncio
import inspect
import logging
import argparse
import sys
from collections import defaultdict
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
    GENERIC_EVIDENCE_TERMS = {
        "add", "append", "build", "call", "close", "connect", "create", "delete",
        "execute", "find", "get", "init", "insert", "list", "load", "make", "model",
        "model_name", "name", "open", "parse", "post", "put", "read", "render",
        "request", "run", "save", "send", "set", "setup", "start", "stop", "update",
        "use", "write",
    }

    CHANGELOG_PATHS = [
        "CHANGELOG.md",
        "CHANGELOG.rst",
        "CHANGELOG.txt",
        "CHANGES.md",
        "CHANGES.rst",
        "CHANGES.txt",
        "NEWS.md",
        "NEWS.rst",
        "NEWS.txt",
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
        self.changelog_file_max_chars = self._env_int("SCOUT_CHANGELOG_FILE_MAX_CHARS", 30000)
        self.analysis_max_chars = self._env_int("SCOUT_ANALYSIS_MAX_CHARS", 30000)
        self.analysis_max_tokens = self._env_int("SCOUT_LLM_MAX_TOKENS", 4000)
        self.evidence_window_chars = self._env_int("SCOUT_EVIDENCE_WINDOW_CHARS", 900)
        self.evidence_chunk_chars = self._env_int("SCOUT_EVIDENCE_CHUNK_CHARS", 1800)
        self.evidence_max_chunks = self._env_int("SCOUT_EVIDENCE_MAX_CHUNKS", 14)
        self.docs_url_fetch_limit = self._env_int("SCOUT_DOCS_URL_FETCH_LIMIT", 10)
        self.github_tree_doc_limit = self._env_int("SCOUT_GITHUB_TREE_DOC_LIMIT", 12)
        self.old_api_doc_fetch_limit = self._env_int("SCOUT_OLD_API_DOC_FETCH_LIMIT", 18)
        self.old_api_doc_max_chars = self._env_int("SCOUT_OLD_API_DOC_MAX_CHARS", 6000)

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
            if parts and parts[0].lower() in {
                "about", "apps", "collections", "contact", "customer-stories",
                "enterprise", "events", "explore", "features", "login",
                "marketplace", "new", "notifications", "orgs", "pricing",
                "pulls", "search", "security", "settings", "sponsors",
                "topics", "trending",
            }:
                return ""
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
            response = await client.get(url, timeout=10.0, follow_redirects=True)
            if response.status_code != 200:
                return ""
            text = response.text or ""
            if "<html" in text[:200].lower():
                text = self._html_to_text(text)
            if self._looks_like_not_found_page(text, url):
                return ""
            return text[:max_chars]
        except Exception as exc:
            logger.debug("Error fetching text reference %s: %s", url, exc)
            return ""

    def _looks_like_not_found_page(self, text: str, url: str = "") -> bool:
        normalized = self._normalized_evidence_text(text)
        if not normalized:
            return False
        not_found_markers = [
            "page not found",
            "404 not found",
            "can't find the page",
            "could not find the page",
        ]
        if not any(marker in normalized for marker in not_found_markers):
            return False
        docs_markers = [
            "documentation",
            "contents index",
            "search terms",
            "current documentation",
        ]
        return any(marker in normalized for marker in docs_markers) or bool(url)

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
                content = self._version_focused_changelog_content(
                    response.text, path, current_version, latest_version
                )
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
            "doc/releasenotes/{version}.rst",
            "docs/releasenotes/{version}.rst",
            "doc/release-notes/{version}.rst",
            "docs/release-notes/{version}.rst",
            "doc/release_notes/{version}.rst",
            "docs/release_notes/{version}.rst",
            "doc/releasenotes/{version}.md",
            "docs/releasenotes/{version}.md",
            "doc/release-notes/{version}.md",
            "docs/release-notes/{version}.md",
            "doc/release_notes/{version}.md",
            "docs/release_notes/{version}.md",
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
        symbols = self._api_symbols(api_usages, None)
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

    def _api_doc_url_candidates(
        self,
        references: list[dict],
        package: str,
        ecosystem: str,
        current_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[tuple[str, str, str]]:
        version = self._clean_version(current_version)
        if not version:
            return []

        docs_bases = set()
        for reference in references:
            url = (reference.get("url") or "").strip()
            if self._is_docs_like_url(url):
                base = self._docs_base_url(url) or url.rstrip("/")
                if base:
                    docs_bases.add(base.rstrip("/"))

        candidates: list[tuple[str, str, str]] = []
        ecosystem_key = (ecosystem or "").lower()
        symbols = self._api_symbols(api_usages, None)

        if ecosystem_key == "cargo":
            for symbol in symbols:
                slug = self._slugify_symbol(symbol)
                candidates.extend([
                    (symbol, f"docs.rs {symbol} {version}", f"https://docs.rs/{package}/{version}/{slug}.html"),
                    (symbol, f"docs.rs {symbol} {version}", f"https://docs.rs/{package}/{version}/{symbol.replace('::', '/').replace('.', '/')}.html"),
                ])
        elif ecosystem_key == "go":
            for symbol in symbols:
                parts = [part for part in re.split(r"[.:/]+", symbol) if part]
                anchor = parts[-1] if parts else ""
                if anchor:
                    candidates.append((symbol, f"pkg.go.dev {symbol} {version}", f"https://pkg.go.dev/{package}@{version}#{anchor}"))
        elif ecosystem_key == "maven" and ":" in package:
            group, artifact = package.split(":", 1)
            for symbol in symbols:
                api_path = symbol.replace(".", "/")
                candidates.append((symbol, f"javadoc {symbol} {version}", f"https://javadoc.io/doc/{group}/{artifact}/{version}/{api_path}.html"))

        for base in sorted(docs_bases):
            for symbol in symbols:
                api = symbol.replace("/", ".").replace("::", ".")
                slug = self._slugify_symbol(symbol)
                api_path = api.replace(".", "/")
                for suffix in [
                    f"{version}/reference/api/{api}.html",
                    f"v{version}/reference/api/{api}.html",
                    f"version/{version}/reference/api/{api}.html",
                    f"{version}/reference/generated/{api}.html",
                    f"v{version}/reference/generated/{api}.html",
                    f"reference/api/{api}.html",
                    f"reference/generated/{api}.html",
                    f"{version}/api/{slug}/",
                    f"v{version}/api/{slug}/",
                    f"{version}/reference/{slug}/",
                    f"v{version}/reference/{slug}/",
                    f"{version}/{api_path}.html",
                    f"v{version}/{api_path}.html",
                    f"api/{slug}/",
                    f"reference/{slug}/",
                    f"{slug}/",
                ]:
                    candidates.append((symbol, f"Old docs for {symbol}", f"{base}/{suffix.lstrip('/')}"))

        seen = set()
        deduped = []
        for api, title, url in candidates:
            normalized = url.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append((api, title, normalized))
        return deduped[: self.old_api_doc_fetch_limit]

    async def _fetch_old_api_doc_references(
        self,
        client: httpx.AsyncClient,
        references: list[dict],
        package: str,
        ecosystem: str,
        current_version: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        fetched = []
        for api, title, url in self._api_doc_url_candidates(
            references,
            package,
            ecosystem,
            current_version,
            api_usages,
            api_contexts,
        ):
            content = await self._fetch_text_url(client, url, self.old_api_doc_max_chars)
            if not content.strip():
                continue
            fetched.append({
                "source": "docs",
                "title": title,
                "url": url,
                "content": content,
                "document_kind": "api_docs",
                "api": api,
                "version": self._clean_version(current_version),
                "role": "old_api_docs",
                "discovery": "old_api_semantics",
            })
        return fetched

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

    def _semantic_terms_from_text(self, text: str, limit: int = 20) -> list[str]:
        normalized = self._normalized_evidence_text(text)
        stopwords = {
            "about", "above", "after", "again", "against", "all", "also", "and", "any",
            "are", "because", "been", "being", "between", "both", "but", "can", "class",
            "code", "data", "def", "does", "each", "from", "function", "have", "import", "into",
            "its", "may", "method", "must", "not", "object", "only", "other", "over",
            "package", "parameter", "parameters", "return", "returns", "same", "self",
            "should", "that", "the", "their", "then", "this", "through", "type", "use",
            "used", "using", "value", "values", "when", "where", "which", "with", "without",
            "typing", "none", "true", "false",
        }
        tokens = [
            token
            for token in re.findall(r"[a-z][a-z0-9_]{2,}", normalized)
            if token not in stopwords and not token.isdigit()
        ]
        weighted: dict[str, int] = {}
        for token in tokens:
            weighted[token] = weighted.get(token, 0) + 1
        phrases: dict[str, int] = {}
        for size in (2, 3):
            for index in range(0, max(0, len(tokens) - size + 1)):
                phrase = " ".join(tokens[index:index + size])
                if len(phrase) > 8:
                    phrases[phrase] = phrases.get(phrase, 0) + size
        ranked = sorted(
            {**weighted, **phrases}.items(),
            key=lambda item: (item[1], len(item[0])),
            reverse=True,
        )
        return [term for term, _score in ranked[:limit]]

    def _extract_doc_section(self, text: str, heading_pattern: str, max_chars: int = 1200) -> str:
        lines = text.splitlines()
        start_index = -1
        heading_re = re.compile(heading_pattern, re.IGNORECASE)
        for index, line in enumerate(lines):
            if heading_re.search(line.strip()):
                start_index = index + 1
                break
        if start_index < 0:
            return ""
        collected = []
        for line in lines[start_index:]:
            stripped = line.strip()
            if collected and re.match(r"^(#{1,6}\s+|[A-Z][A-Za-z ]{2,}:?$)", stripped):
                break
            if re.match(r"^-{3,}$|^~{3,}$|^\^{3,}$", stripped):
                continue
            collected.append(line)
            if len("\n".join(collected)) >= max_chars:
                break
        return "\n".join(collected).strip()[:max_chars]

    def _purpose_from_old_doc(self, api: str, content: str) -> str:
        if not content.strip():
            return ""
        focused = self._semantic_focus_text(content, [api, *self._api_symbols([api])], window=1200)
        text = focused or content[:1800]
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not re.match(r"^[#=`~*\- ]+$", line.strip())
        ]
        joined = " ".join(lines)
        sentences = re.split(r"(?<=[.!?])\s+", joined)
        purpose = " ".join(sentence for sentence in sentences[:3] if sentence).strip()
        return purpose[:900]

    def _semantic_focus_text(self, content: str, terms: list[str], window: int = 900) -> str:
        lowered = content.lower()
        windows = []
        for term in terms:
            term_lower = (term or "").lower()
            if not term_lower:
                continue
            index = lowered.find(term_lower)
            if index >= 0:
                windows.append((max(0, index - window), min(len(content), index + len(term) + window)))
        if not windows:
            return ""
        start = min(item[0] for item in windows)
        end = max(item[1] for item in windows)
        return content[start:end].strip()

    def _api_semantics_from_references(
        self,
        api_usages: Optional[list[str]],
        old_doc_references: list[dict],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        semantics = []
        docs_by_api: dict[str, list[dict]] = {}
        for reference in old_doc_references:
            api = str(reference.get("api", "") or "").strip()
            if api:
                docs_by_api.setdefault(api, []).append(reference)

        for usage in api_usages or []:
            usage = (usage or "").strip()
            if not usage:
                continue
            docs = docs_by_api.get(usage, [])
            if not docs:
                docs = [
                    reference for reference in old_doc_references
                    if usage.lower() in self._normalized_evidence_text(
                        " ".join([
                            str(reference.get("title", "")),
                            str(reference.get("url", "")),
                            str(reference.get("content", ""))[:1000],
                        ])
                    )
                ]

            if docs:
                reference = docs[0]
                content = str(reference.get("content", "") or "")
                purpose = self._purpose_from_old_doc(usage, content)
                parameters = self._extract_doc_section(content, r"^(parameters?|arguments?|args?)\b")
                returns = self._extract_doc_section(content, r"^(returns?|return value|result)\b")
                behavior = "\n".join(filter(None, [purpose, parameters[:400], returns[:300]])).strip()
                terms = self._semantic_terms_from_text(behavior or purpose)
                semantics.append({
                    "api": usage,
                    "source": "old_version_docs",
                    "confidence": "high" if purpose else "medium",
                    "purpose": purpose,
                    "behavior": behavior,
                    "parameters": parameters,
                    "returns": returns,
                    "search_terms": terms,
                    "docs": [{
                        "title": reference.get("title", ""),
                        "url": reference.get("url", ""),
                    }],
                })
                continue

            fallback = self._api_semantics_from_code_context(usage, api_contexts)
            if fallback:
                semantics.append(fallback)

        return semantics

    def _api_semantics_from_code_context(
        self,
        api: str,
        api_contexts: Optional[list[dict]],
    ) -> dict | None:
        matched_contexts = []
        api_last = self._slugify_symbol(api)
        for context in api_contexts or []:
            if not isinstance(context, dict):
                continue
            context_api = str(context.get("api", "") or context.get("old_api", "") or "")
            text = "\n".join([
                str(context.get("code_snippet", "") or ""),
                str(context.get("context", "") or ""),
                str(context.get("matched_text", "") or ""),
            ])
            if context_api == api or api_last in self._normalized_evidence_text(text):
                matched_contexts.append(context)
        if not matched_contexts:
            return None
        snippets = "\n".join(
            str(context.get("context") or context.get("code_snippet") or "")
            for context in matched_contexts[:3]
        )
        meaningful_lines = [
            line.strip()
            for line in snippets.splitlines()
            if line.strip()
            and not line.strip().startswith(("import ", "from "))
            and not line.strip().startswith("#")
        ]
        meaningful_text = "\n".join(meaningful_lines)
        call_names = re.findall(r"\.([A-Za-z_]\w*)\s*\(", meaningful_text)
        keyword_args = re.findall(r"\b([A-Za-z_]\w*)\s*=", snippets)
        parts = [part for part in re.split(r"[.:/]+", api) if part]
        method = parts[-1] if parts else api_last
        owner = parts[-2] if len(parts) >= 2 else ""
        subclass_names = re.findall(r"\bclass\s+([A-Za-z_]\w*)\s*\([^)]*\b" + re.escape(method) + r"\b[^)]*\)", snippets)
        fields = [
            name
            for name in re.findall(r"^\s*([A-Za-z_]\w*)\s*:\s*[^=\n]+", meaningful_text, re.MULTILINE)
            if name not in {"self", "cls"}
        ]
        functions = re.findall(r"\bdef\s+([A-Za-z_]\w*)\s*\(", meaningful_text)
        purpose = " ".join(filter(None, [
            f"Project code uses {method}",
            f"on {owner}" if owner else "",
            f"through subclasses {', '.join(sorted(set(subclass_names)))}" if subclass_names else "",
            f"inside functions {', '.join(sorted(set(functions)))}" if functions else "",
            f"with fields {', '.join(sorted(set(fields)))}" if fields else "",
            f"with keyword arguments {', '.join(sorted(set(keyword_args)))}" if keyword_args else "",
        ])).strip()
        terms = self._semantic_terms_from_text(" ".join([
            purpose,
            meaningful_text,
            " ".join(call_names),
            " ".join(keyword_args),
            " ".join(fields),
        ]))
        return {
            "api": api,
            "source": "code_context",
            "confidence": "low",
            "purpose": purpose,
            "behavior": meaningful_text[:1000],
            "parameters": ", ".join(sorted(set(keyword_args))),
            "returns": "",
            "search_terms": terms,
            "docs": [],
        }

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
        if self._versions_from_text(path):
            score += 20

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
            
            # Always include the major version roots to catch X.0.0 breaking changes
            raw_versions.append(f"{low[0]}.0.0")
            if high[0] > low[0]:
                raw_versions.append(f"{high[0]}.0.0")

            if low[0] == high[0] and 0 <= high[1] - low[1] <= 12:
                for minor in range(low[1], high[1] + 1):
                    raw_versions.append(f"{low[0]}.{minor}.0")
            elif 0 < high[0] - low[0] <= 12:
                for major in range(low[0] + 1, high[0] + 1):
                    raw_versions.append(f"{major}.0.0")
                for minor in range(low[1] + 1, low[1] + 5):
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
                content = self._version_focused_changelog_content(
                    response.text or "", path, current_version, latest_version
                )
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

        # Broaden the lower bound to the base of the current major version
        # to catch breaking changes that happened at X.0.0
        low_bound = (low[0], 0, 0) if len(low) > 0 else low
        return low_bound <= version <= high

    def _version_focused_changelog_content(
        self,
        raw_content: str,
        path: str,
        current_version: str,
        latest_version: str,
    ) -> str:
        """
        Parse a changelog file and return only the sections that fall within the
        migration range [current_version, latest_version] (broadened to the base
        major of current).  Falls back to a top-N slice when no version headers
        are found so that non-standard files still produce useful output.
        """
        if not current_version or not latest_version or not raw_content:
            return raw_content[:self.changelog_file_max_chars]

        lines = raw_content.splitlines(keepends=True)
        is_rst = path.lower().endswith(".rst")

        md_heading = re.compile(
            r'^#{1,3}\s+(?:\[?v?|[Vv]ersion\s+)(\d+\.\d+(?:\.\d+)*)'
        )
        rst_underline = re.compile(r'^[-=~^#*+]{3,}$')

        section_starts: list[tuple[int, tuple[int, ...]]] = []  # (line_idx, version_tuple)

        for i, raw_line in enumerate(lines):
            stripped = raw_line.rstrip()
            # Markdown heading
            m = md_heading.match(stripped)
            if m:
                vt = self._version_tuple(m.group(1))
                if vt:
                    section_starts.append((i, vt))
                    continue
            # RST underline-style heading: current line has a version, next is all dashes/equals
            if is_rst and i + 1 < len(lines):
                next_stripped = lines[i + 1].rstrip()
                if (
                    next_stripped
                    and rst_underline.match(next_stripped)
                    and abs(len(next_stripped) - len(stripped)) <= 4
                    and len(stripped) >= 3
                ):
                    m2 = re.search(r'v?(\d+\.\d+(?:\.\d+)*)', stripped)
                    if m2:
                        vt = self._version_tuple(m2.group(1))
                        if vt:
                            section_starts.append((i, vt))

        if not section_starts:
            # No recognisable version headers — just truncate from the top
            return raw_content[:self.changelog_file_max_chars]

        section_starts.sort(key=lambda x: x[0])

        # Pair each section start with its end (= next section's start line)
        sections: list[tuple[tuple[int, ...], int, int]] = []
        for idx, (start, vt) in enumerate(section_starts):
            end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
            sections.append((vt, start, end))

        # Keep only sections whose version falls inside the migration window
        relevant = [
            (vt, s, e)
            for (vt, s, e) in sections
            if self._version_is_relevant(vt, current_version, latest_version)
        ]

        if not relevant:
            return raw_content[:self.changelog_file_max_chars]

        # For migration across major versions (e.g. 1.x → 2.x) the X.0.0 section contains
        # the most important breaking changes. Always include those FIRST so they are never
        # crowded out by a large volume of recent minor-release notes.
        major_boundary = [
            (vt, s, e) for (vt, s, e) in relevant
            if len(vt) >= 3 and vt[1] == 0 and vt[2] == 0
        ]
        other_sections = [
            (vt, s, e) for (vt, s, e) in relevant
            if not (len(vt) >= 3 and vt[1] == 0 and vt[2] == 0)
        ]
        major_boundary.sort(key=lambda x: x[0])           # oldest → newest (2.0.0 before 3.0.0)
        other_sections.sort(key=lambda x: x[0], reverse=True)  # newest-first for the rest
        ordered = major_boundary + other_sections

        max_chars = self.changelog_file_max_chars * 3
        parts: list[str] = []
        total = 0
        for vt, start, end in ordered:
            chunk = "".join(lines[start:end])
            if total + len(chunk) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    parts.append(chunk[:remaining])
                break
            parts.append(chunk)
            total += len(chunk)

        result = "".join(parts)
        return result if result.strip() else raw_content[:self.changelog_file_max_chars]

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
        current = self._version_tuple(self._clean_version(current_version))
        latest = self._version_tuple(self._clean_version(latest_version))
        
        if current and latest:
            low, high = sorted([current, latest])
            low_bound = (low[0], 0, 0) if len(low) > 0 else (0, 0, 0)
        else:
            low_bound = (0, 0, 0)
            
        release_notes = []
        max_pages = 3  # safety cap: fetch at most 300 releases
        try:
            for page in range(1, max_pages + 1):
                response = await client.get(
                    url, headers=headers,
                    params={"per_page": 100, "page": page},
                    timeout=15.0,
                )
                if response.status_code != 200:
                    break
                page_data = response.json()
                if not page_data:
                    break
                reached_lower_bound = False
                for release in page_data:
                    tag_name = release.get("tag_name", "")
                    clean_tag = self._clean_version(tag_name)
                    tag_tuple = self._version_tuple(clean_tag)
                    release_notes.append(f"## Version {tag_name}\n{release.get('body', '')}\n")
                    if tag_tuple and tag_tuple < low_bound:
                        reached_lower_bound = True
                        break
                if reached_lower_bound or len(page_data) < 100:
                    break
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

    def _format_api_semantics(self, api_semantics: list[dict], max_chars: int = 12000) -> str:
        if not api_semantics:
            return "No old-version API semantics were retrieved."
        sections = []
        for index, item in enumerate(api_semantics[:16], start=1):
            if not isinstance(item, dict):
                continue
            docs = []
            for doc in item.get("docs", [])[:2]:
                if isinstance(doc, dict):
                    docs.append(" | ".join(filter(None, [
                        str(doc.get("title", "") or ""),
                        str(doc.get("url", "") or ""),
                    ])))
            block = [
                f"[{index}] api: {item.get('api', '')}",
                f"source: {item.get('source', '')}",
                f"confidence: {item.get('confidence', '')}",
                f"purpose: {item.get('purpose', '')}",
                f"behavior: {str(item.get('behavior', '') or '')[:1200]}",
                f"parameters: {str(item.get('parameters', '') or '')[:700]}",
                f"returns: {str(item.get('returns', '') or '')[:500]}",
                f"semantic_search_terms: {', '.join(str(term) for term in (item.get('search_terms', []) or [])[:12])}",
            ]
            if docs:
                block.extend(["docs:", "\n".join(docs)])
            sections.append("\n".join(block))
            if len("\n\n".join(sections)) > max_chars:
                sections.append("... API semantics truncated ...")
                break
        return "\n\n".join(sections)[:max_chars]

    def _api_search_terms(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
        api_semantics: Optional[list[dict]] = None,
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
            for receiver, method in re.findall(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*\(", snippet):
                receiver_leaf = receiver.rsplit(".", 1)[-1]
                if len(receiver_leaf) > 2 and len(method) > 2:
                    terms.add(f"{receiver_leaf}.{method}")
            for method in re.findall(r"\.([A-Za-z_]\w*)\s*\(", snippet):
                if len(method) > 2:
                    terms.add(method)
        for semantic in api_semantics or []:
            if not isinstance(semantic, dict):
                continue
            for term in semantic.get("search_terms", []) or []:
                value = str(term or "").strip()
                if value:
                    terms.add(value)
            for key in ("purpose", "behavior", "parameters", "returns"):
                for term in self._semantic_terms_from_text(str(semantic.get(key, "") or "")):
                    terms.add(term)
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
        # Registry source takes precedence over any content-based classification
        if source in {"pypi", "npm", "crates.io", "maven"}:
            return "registry"
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
            "changed signature", "signature", "requires", "must", "now named",
            "renamed to", "has been renamed", "has moved", "moved to",
            "migrate", "review", "construction", "setup",
        ])

    def _normalized_evidence_text(self, text: str) -> str:
        value = (text or "").lower()
        value = re.sub(r":[a-z_]+:`~?([^`]+)`", r"\1", value)
        value = value.replace("``", "")
        value = value.replace("`", "")
        value = re.sub(r"\s+", " ", value)
        return value

    def _method_api_profiles(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        profiles = []
        for usage in api_usages or []:
            parts = [part for part in re.split(r"[.:/]+", usage or "") if part]
            if len(parts) < 2:
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
        for context in api_contexts or []:
            if not isinstance(context, dict):
                continue
            usage_owners = []
            for usage in api_usages or []:
                usage_parts = [part for part in re.split(r"[.:/]+", str(usage or "")) if part]
                if len(usage_parts) >= 2:
                    usage_owners.append((str(usage or ""), usage_parts[-1]))
            snippet = "\n".join([
                str(context.get("matched_text", "") or ""),
                str(context.get("code_snippet", "") or ""),
            ])
            for receiver, method in re.findall(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.([A-Za-z_]\w*)\s*\(", snippet):
                owner = receiver.rsplit(".", 1)[-1]
                if not owner or not method:
                    continue
                profiles.append({
                    "api": str(context.get("api", "") or ""),
                    "owner": owner,
                    "method": method,
                    "exact": f"{owner}.{method}",
                })
                context_api = str(context.get("api", "") or context.get("old_api", "") or "").strip()
                context_parts = [part for part in re.split(r"[.:/]+", context_api) if part]
                if len(context_parts) >= 2:
                    api_owner = context_parts[-1]
                    if api_owner and api_owner.lower() != method.lower():
                        profiles.append({
                            "api": context_api,
                            "owner": api_owner,
                            "method": method,
                            "exact": f"{api_owner}.{method}",
                        })
                elif not context_api:
                    for usage, api_owner in usage_owners:
                        if api_owner and api_owner.lower() != method.lower():
                            profiles.append({
                                "api": usage,
                                "owner": api_owner,
                                "method": method,
                                "exact": f"{api_owner}.{method}",
                            })
        return profiles

    def _api_centered_terms(
        self,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[str]:
        terms: set[str] = set()
        for usage in api_usages or []:
            normalized = str(usage or "").replace("::", ".").replace("/", ".").strip()
            parts = [part for part in normalized.split(".") if part]
            if len(parts) >= 2:
                terms.add(".".join(parts[-2:]))
            if len(parts) >= 3:
                terms.add(".".join(parts[-3:]))
                terms.add(normalized)
        for profile in self._method_api_profiles(api_usages, api_contexts):
            exact = str(profile.get("exact", "") or "").strip()
            owner = str(profile.get("owner", "") or "").strip()
            method = str(profile.get("method", "") or "").strip()
            if exact and owner and method and not self._is_weak_unqualified_term(method):
                terms.add(exact)
            elif exact and owner and method:
                terms.add(exact)
        return sorted(terms, key=lambda item: (-len(item), item))

    def _api_centered_reference_chunks(
        self,
        reference: dict,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> list[dict]:
        content = (reference.get("content") or "").strip()
        if not content:
            return []

        terms = self._api_centered_terms(api_usages, api_contexts)
        if not terms:
            return []

        chunks: list[dict] = []
        seen_windows: set[tuple[int, int]] = set()
        normalized_content = self._normalized_evidence_text(content)
        window_size = max(self.evidence_chunk_chars, 800)
        half_window = window_size // 2

        for term in terms:
            normalized_term = self._normalized_evidence_text(term)
            if not normalized_term:
                continue
            for match in re.finditer(rf"(?<![\w.]){re.escape(normalized_term)}(?![\w.])", normalized_content):
                # normalized offsets are close enough for windowing because normalization mostly strips markup.
                index = match.start()
                window_start = max(0, index - half_window)
                window_end = min(len(content), index + len(term) + half_window)
                paragraph_start = content.rfind("\n\n", 0, window_start)
                paragraph_end = content.find("\n\n", window_end)
                if paragraph_start != -1 and index - paragraph_start <= window_size:
                    window_start = paragraph_start + 2
                if paragraph_end != -1 and paragraph_end - index <= window_size:
                    window_end = paragraph_end
                adjusted_start, adjusted_end = self._extend_bounds_to_code_blocks(
                    content,
                    window_start,
                    window_end,
                )
                code_extended = (adjusted_start, adjusted_end) != (window_start, window_end)
                window_start, window_end = adjusted_start, adjusted_end
                key = (window_start, window_end)
                if key in seen_windows:
                    continue
                seen_windows.add(key)
                line_start = content[:window_start].count("\n") + 1
                line_end = line_start + content[window_start:window_end].count("\n")
                chunk_content = content[window_start:window_end].strip()
                if not code_extended:
                    chunk_content = chunk_content[: self.evidence_chunk_chars + 300]
                chunks.append({
                    **reference,
                    "content": chunk_content,
                    "line_start": line_start,
                    "line_end": max(line_start, line_end),
                    "document_kind": self._document_kind(reference),
                    "api_centered": True,
                })
        return chunks

    def _extend_bounds_to_code_blocks(self, content: str, start: int, end: int) -> tuple[int, int]:
        spans = self._document_code_block_spans(content)
        if not spans:
            return start, end

        adjusted_start = start
        adjusted_end = end
        changed = True
        while changed:
            changed = False
            for span_start, span_end in spans:
                if span_start < adjusted_end and span_end > adjusted_start:
                    next_start = min(adjusted_start, span_start)
                    next_end = max(adjusted_end, span_end)
                    if next_start != adjusted_start or next_end != adjusted_end:
                        adjusted_start, adjusted_end = next_start, next_end
                        changed = True
        return adjusted_start, adjusted_end

    def _document_code_block_spans(self, content: str) -> list[tuple[int, int]]:
        lines = content.splitlines(keepends=True)
        offsets = []
        offset = 0
        for line in lines:
            offsets.append(offset)
            offset += len(line)

        spans: list[tuple[int, int]] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            fence_match = re.match(r"(```|~~~)", stripped)
            if fence_match:
                fence = fence_match.group(1)
                start = offsets[index]
                index += 1
                end = offset
                while index < len(lines):
                    end = offsets[index] + len(lines[index])
                    if lines[index].lstrip().startswith(fence):
                        index += 1
                        break
                    index += 1
                spans.append((start, end))
                continue

            if re.match(r"\.\.\s+(?:code-block|code)::", stripped):
                start = offsets[index]
                index += 1
                end = offsets[index - 1] + len(lines[index - 1])
                saw_body = False
                while index < len(lines):
                    current = lines[index]
                    current_stripped = current.strip()
                    current_indent = len(current) - len(current.lstrip())
                    if not current_stripped:
                        end = offsets[index] + len(current)
                        index += 1
                        continue
                    if current_indent > indent:
                        saw_body = True
                        end = offsets[index] + len(current)
                        index += 1
                        continue
                    if saw_body:
                        break
                    break
                spans.append((start, end))
                continue

            index += 1
        return spans

    def _method_evidence_score(
        self,
        text: str,
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
    ) -> tuple[int, list[str]]:
        normalized = self._normalized_evidence_text(text)
        score = 0
        matches = []
        seen_profiles = set()
        for profile in self._method_api_profiles(api_usages, api_contexts):
            profile_key = (profile["owner"].lower(), profile["method"].lower(), profile["exact"].lower())
            if profile_key in seen_profiles:
                continue
            seen_profiles.add(profile_key)
            owner = profile["owner"].lower()
            method = profile["method"].lower()
            exact = profile["exact"].lower()
            if exact in normalized:
                score += 90 + self._migration_replacement_boost(normalized, exact)
                matches.append(profile["exact"])
                continue

            owner_positions = [match.start() for match in re.finditer(re.escape(owner), normalized)]
            method_positions = [match.start() for match in re.finditer(re.escape(method), normalized)]
            if not owner_positions or not method_positions:
                continue
            min_distance = min(abs(owner_pos - method_pos) for owner_pos in owner_positions for method_pos in method_positions)
            if min_distance <= 120:
                # Double the proximity score when migration language appears near the method name —
                # e.g. "@validator has been deprecated" without the full "pydantic.validator" string
                prox_score = 70 if self._method_has_migration_context(normalized, method) else 35
                score += prox_score
                matches.append(f"{profile['owner']}~{profile['method']}")
        return score, matches

    def _method_has_migration_context(self, normalized: str, method: str) -> bool:
        """True when the method name appears within 200 chars of migration-signalling language."""
        migration_signals = [
            "deprecated", "removed", "replaced", "no longer", "legacy",
            "instead", "migrate", "migration", "breaking",
        ]
        method_lower = method.lower()
        for m in re.finditer(re.escape(method_lower), normalized):
            window = normalized[max(0, m.start() - 200): m.end() + 200]
            if any(sig in window for sig in migration_signals):
                return True
        return False

    def _migration_replacement_boost(self, normalized_text: str, exact_api: str) -> int:
        exact = self._normalized_evidence_text(exact_api)
        if not exact or exact not in normalized_text:
            return 0
        boost = 0
        if re.search(rf"\buse\b[^.\n]{{0,180}}\bnot\b[^.\n]{{0,80}}{re.escape(exact)}", normalized_text):
            boost += 70
        if re.search(rf"\bnot\b[^.\n]{{0,80}}{re.escape(exact)}[^.\n]{{0,180}}\buse\b", normalized_text):
            boost += 70
        if re.search(rf"{re.escape(exact)}[^.\n]{{0,160}}\b(?:removed|deprecated|legacy|no longer)\b", normalized_text):
            boost += 35
        if re.search(rf"\b(?:removed|deprecated|legacy|no longer)\b[^.\n]{{0,160}}{re.escape(exact)}", normalized_text):
            boost += 35
        return boost

    def _semantic_evidence_score(self, text: str, api_semantics: Optional[list[dict]]) -> tuple[int, list[str]]:
        normalized = self._normalized_evidence_text(text)
        score = 0
        matches = []
        for semantic in api_semantics or []:
            if not isinstance(semantic, dict):
                continue
            seen_for_api = set()
            for term in semantic.get("search_terms", []) or []:
                term = str(term or "").strip()
                if not term:
                    continue
                term_lower = self._normalized_evidence_text(term)
                if not term_lower or term_lower in seen_for_api:
                    continue
                seen_for_api.add(term_lower)
                if len(term_lower) > 4 and term_lower in normalized:
                    score += 18 if " " in term_lower else 7
                    matches.append(term)
                    continue
                fuzzy_score = self._fuzzy_semantic_term_score(term_lower, normalized)
                if fuzzy_score:
                    score += fuzzy_score
                    matches.append(term)
        return score, matches

    def _fuzzy_semantic_term_score(self, term: str, normalized_text: str) -> int:
        if " " not in term or not normalized_text:
            return 0
        term_tokens = [
            self._semantic_token_stem(token)
            for token in re.findall(r"[a-z][a-z0-9_]{3,}", term)
            if token not in {
                "from", "into", "with", "this", "that", "other", "return",
                "returns", "parameter", "parameters", "function", "method",
            }
        ]
        term_tokens = [token for token in term_tokens if token]
        if len(term_tokens) < 2:
            return 0
        text_tokens = {
            self._semantic_token_stem(token)
            for token in re.findall(r"[a-z][a-z0-9_]{3,}", normalized_text)
        }
        overlap = sum(1 for token in set(term_tokens) if token in text_tokens)
        if overlap >= 3:
            return 12
        if overlap >= 2 and len(set(term_tokens)) <= 4:
            return 9
        return 0

    def _semantic_token_stem(self, token: str) -> str:
        value = (token or "").lower()
        for suffix in ("ing", "ed", "es", "s"):
            if len(value) > len(suffix) + 3 and value.endswith(suffix):
                return value[: -len(suffix)]
        return value

    def _is_weak_unqualified_term(self, term: str) -> bool:
        normalized = self._normalized_evidence_text(term)
        if not normalized:
            return True
        if any(separator in normalized for separator in [".", "::", "/", "~"]):
            return False
        if " " in normalized:
            return len(normalized) < 8
        return (
            len(normalized) <= 5
            or normalized in self.GENERIC_EVIDENCE_TERMS
            or normalized.endswith("_name")
            or normalized.endswith("_id")
            or normalized.endswith("_key")
        )

    def _is_strong_evidence_term(self, term: str, root_terms: set[str]) -> bool:
        normalized = self._normalized_evidence_text(term)
        if not normalized or normalized in root_terms or normalized == "breaking-change-section":
            return False
        return not self._is_weak_unqualified_term(normalized)

    def _chunk_passes_evidence_anchor(
        self,
        chunk: dict,
        matched_terms: list[str],
        strong_terms: list[str],
        method_score: int,
        semantic_score: int,
    ) -> bool:
        content = str(chunk.get("content", "") or "")
        has_migration = self._has_migration_language(content)
        if not has_migration:
            return False
        if method_score >= 35:
            return True
        if semantic_score >= 18:
            return has_migration
        if any(any(separator in term for separator in [".", "::", "/", "~"]) for term in strong_terms):
            return has_migration
        if chunk.get("api_centered") and strong_terms:
            return has_migration
        if strong_terms:
            return has_migration
        if not matched_terms:
            return False
        return False

    def _split_reference_chunks(self, reference: dict) -> list[dict]:
        content = (reference.get("content") or "").strip()
        if not content:
            return []

        chunks: list[dict] = []
        current: list[dict] = []

        def flush() -> None:
            nonlocal current
            if not current:
                return
            start = current[0]["start"]
            end = current[-1]["end"]
            adjusted_start, adjusted_end = self._extend_bounds_to_code_blocks(content, start, end)
            code_extended = (adjusted_start, adjusted_end) != (start, end)
            text = content[adjusted_start:adjusted_end].strip()
            if text:
                if not code_extended:
                    text = text[: self.evidence_chunk_chars + 300]
                line_start = content[:adjusted_start].count("\n") + 1
                line_end = line_start + content[adjusted_start:adjusted_end].count("\n")
                chunks.append({
                    **reference,
                    "content": text,
                    "line_start": line_start,
                    "line_end": max(line_start, line_end),
                    "document_kind": self._document_kind(reference),
                })
            current = []

        for paragraph in self._reference_paragraphs(content):
            pending_len = (
                sum(len(item["text"]) for item in current)
                + max(0, len(current)) * 2
                + len(paragraph["text"])
            )
            if current and pending_len > self.evidence_chunk_chars:
                flush()
            if len(paragraph["text"]) > self.evidence_chunk_chars:
                flush()
                start = paragraph["start"]
                end = paragraph["end"]
                adjusted_start, adjusted_end = self._extend_bounds_to_code_blocks(content, start, end)
                if (adjusted_start, adjusted_end) != (start, end):
                    text = content[adjusted_start:adjusted_end].strip()
                    line_start = content[:adjusted_start].count("\n") + 1
                    line_end = line_start + content[adjusted_start:adjusted_end].count("\n")
                    chunks.append({
                        **reference,
                        "content": text,
                        "line_start": line_start,
                        "line_end": max(line_start, line_end),
                        "document_kind": self._document_kind(reference),
                    })
                else:
                    paragraph_text = paragraph["text"]
                    for offset in range(0, len(paragraph_text), self.evidence_chunk_chars):
                        part_start = start + offset
                        part_end = min(end, start + offset + self.evidence_chunk_chars)
                        text = content[part_start:part_end].strip()
                        if not text:
                            continue
                        line_start = content[:part_start].count("\n") + 1
                        line_end = line_start + content[part_start:part_end].count("\n")
                        chunks.append({
                            **reference,
                            "content": text,
                            "line_start": line_start,
                            "line_end": max(line_start, line_end),
                            "document_kind": self._document_kind(reference),
                        })
                continue
            current.append(paragraph)

        flush()
        return chunks

    def _reference_paragraphs(self, content: str) -> list[dict]:
        lines = content.splitlines(keepends=True)
        paragraphs: list[dict] = []
        start_line = 0
        start_offset = 0
        end_line = 0
        end_offset = 0
        in_paragraph = False
        offset = 0

        for line_number, line in enumerate(lines, start=1):
            line_start = offset
            line_end = offset + len(line)
            offset = line_end
            if line.strip():
                if not in_paragraph:
                    start_line = line_number
                    start_offset = line_start
                    in_paragraph = True
                end_line = line_number
                end_offset = line_end
                continue

            if in_paragraph:
                text = content[start_offset:end_offset].strip()
                if text:
                    paragraphs.append({
                        "text": text,
                        "start": start_offset,
                        "end": end_offset,
                        "line_start": start_line,
                        "line_end": end_line,
                    })
                in_paragraph = False

        if in_paragraph:
            text = content[start_offset:end_offset].strip()
            if text:
                paragraphs.append({
                    "text": text,
                    "start": start_offset,
                    "end": end_offset,
                    "line_start": start_line,
                    "line_end": end_line,
                })
        return paragraphs

    def _score_evidence_chunk(
        self,
        chunk: dict,
        terms: list[str],
        api_usages: Optional[list[str]] = None,
        api_contexts: Optional[list[dict]] = None,
        api_semantics: Optional[list[dict]] = None,
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
        method_score, method_matches = self._method_evidence_score(text, api_usages, api_contexts)
        score += method_score
        matched_terms.extend(method_matches)
        semantic_score, semantic_matches = self._semantic_evidence_score(text, api_semantics)
        score += semantic_score
        matched_terms.extend(semantic_matches)

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
        api_semantics: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        terms = self._api_search_terms(api_usages, api_contexts, api_semantics)
        if not terms:
            return []
        root_terms = self._root_package_terms(api_usages)

        ranked: list[tuple[int, int, dict]] = []
        for reference_index, reference in enumerate(self._dedupe_references(references), start=1):
            if reference.get("role") == "old_api_docs":
                continue
            reference_locator = " ".join([
                str(reference.get("title", "")),
                str(reference.get("url", "")),
            ])
            if not self._path_versions_are_relevant(reference_locator, current_version, latest_version):
                continue
            chunks = self._api_centered_reference_chunks(reference, api_usages, api_contexts)
            chunks.extend(self._split_reference_chunks(reference))
            for chunk_index, chunk in enumerate(chunks, start=1):
                score, matched_terms = self._score_evidence_chunk(
                    chunk,
                    terms,
                    api_usages,
                    api_contexts,
                    api_semantics,
                    current_version,
                    latest_version,
                )
                strong_terms = [
                    term
                    for term in matched_terms
                    if self._is_strong_evidence_term(term, root_terms)
                ]
                semantic_score, semantic_matches = self._semantic_evidence_score(
                    str(chunk.get("content", "")),
                    api_semantics,
                )
                method_score, method_matches = self._method_evidence_score(
                    str(chunk.get("content", "")),
                    api_usages,
                    api_contexts,
                )
                if chunk.get("document_kind") == "registry":
                    if method_score < 80 and semantic_score < 36:
                        continue
                if not self._chunk_passes_evidence_anchor(
                    chunk,
                    matched_terms,
                    strong_terms,
                    method_score,
                    semantic_score,
                ):
                    continue
                if score < 10 or not matched_terms:
                    continue
                confidence = "high" if method_score >= 80 else "medium" if semantic_score >= 18 or method_score >= 35 or strong_terms else "low"
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
                    "semantic_score": semantic_score,
                    "evidence_confidence": confidence,
                    "reference_index": reference_index,
                    "chunk_index": chunk_index,
                }))

        # Migration/release docs are the most valuable; reference API docs are secondary.
        # Limit how many chunks any single source URL can contribute so that a single
        # reference doc (e.g. validators.md showing the new API) cannot crowd out more
        # specific migration guides or changelogs.
        kind_caps = {
            "migration_guide": 6,
            "release_notes":   5,
            "api_docs":        2,
            "reference":       2,
            "registry":        2,
        }
        default_cap = 3

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        evidence = []
        seen: set = set()
        source_counts: dict[str, int] = {}
        for _score, _ref_order, chunk in ranked:
            key = (chunk["url"], chunk["content"][:160])
            if key in seen:
                continue
            url = chunk.get("url", "")
            kind = chunk.get("document_kind", "reference")
            cap = kind_caps.get(kind, default_cap)
            if source_counts.get(url, 0) >= cap:
                continue
            seen.add(key)
            source_counts[url] = source_counts.get(url, 0) + 1
            evidence.append(chunk)
            if len(evidence) >= self.evidence_max_chunks:
                break
        return evidence

    def _focused_reference_snippets(
        self,
        references: list[dict],
        api_usages: Optional[list[str]],
        api_contexts: Optional[list[dict]] = None,
        api_semantics: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        ranked_chunks = self._ranked_evidence_chunks(
            references,
            api_usages,
            api_contexts,
            api_semantics,
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
                    "semantic_score": chunk.get("semantic_score", 0),
                    "evidence_confidence": chunk.get("evidence_confidence", "medium"),
                    "line_start": chunk.get("line_start"),
                    "line_end": chunk.get("line_end"),
                }
                for chunk in ranked_chunks
            ]

        fallback = self._fallback_breaking_change_evidence(references, current_version, latest_version)
        if fallback and not api_contexts:
            return fallback

        terms = self._api_search_terms(api_usages, api_contexts, api_semantics)
        if not terms:
            return []
        root_terms = self._root_package_terms(api_usages)

        focused = []
        for reference in self._dedupe_references(references):
            if reference.get("role") == "old_api_docs":
                continue
            reference_locator = " ".join([
                str(reference.get("title", "")),
                str(reference.get("url", "")),
            ])
            if not self._path_versions_are_relevant(reference_locator, current_version, latest_version):
                continue
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
                if self._is_strong_evidence_term(term, root_terms)
            ]
            if not strong_terms or not self._has_migration_language(content):
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
                    "evidence_confidence": "low",
                })

        return focused

    def _fallback_breaking_change_evidence(
        self,
        references: list[dict],
        current_version: str = "",
        latest_version: str = "",
    ) -> list[dict]:
        fallback: list[tuple[int, dict]] = []
        for reference in self._dedupe_references(references):
            if reference.get("role") == "old_api_docs":
                continue
            reference_locator = " ".join([
                str(reference.get("title", "")),
                str(reference.get("url", "")),
            ])
            if not self._path_versions_are_relevant(reference_locator, current_version, latest_version):
                continue
            kind = self._document_kind(reference)
            if kind not in {"release_notes", "migration_guide"}:
                continue
            for chunk in self._split_reference_chunks(reference):
                content = str(chunk.get("content", "") or "")
                lowered = content.lower()
                if not self._has_migration_language(content):
                    continue
                score = 0
                if any(term in lowered for term in ["breaking", "backwards incompatible", "migration", "upgrade"]):
                    score += 20
                if any(term in lowered for term in ["removed", "deprecated", "renamed", "replacement", "no longer"]):
                    score += 10
                heading = content.splitlines()[0].lower() if content.splitlines() else ""
                if any(term in heading for term in ["breaking", "migration", "upgrade", "removed", "deprecated"]):
                    score += 15
                if score <= 0:
                    continue
                fallback.append((score, {
                    "source": chunk.get("source", ""),
                    "title": f"{chunk.get('title') or chunk.get('source')} (low-confidence breaking-change fallback)",
                    "url": chunk.get("url", ""),
                    "content": content,
                    "document_kind": kind,
                    "matched_terms": ["breaking-change-section"],
                    "strong_terms": [],
                    "score": score,
                    "semantic_score": 0,
                    "evidence_confidence": "low",
                    "line_start": chunk.get("line_start"),
                    "line_end": chunk.get("line_end"),
                }))
        fallback.sort(key=lambda item: item[0], reverse=True)
        return [item for _score, item in fallback[: min(4, self.evidence_max_chunks)]]

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
        old_api_doc_references = await self._fetch_old_api_doc_references(
            client,
            references,
            package,
            ecosystem,
            current_version,
            api_usages,
            api_contexts,
        )
        references.extend(old_api_doc_references)

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
            changelog_refs = await self._fetch_github_changelog_files(
                client,
                owner,
                repo,
                default_branch,
                current_version,
                latest_version,
                api_usages,
                api_contexts,
            )
            references.extend(changelog_refs)
            # If nothing found on "main" (e.g. repo uses "master"), retry with "master"
            if not changelog_refs and default_branch == "main":
                master_refs = await self._fetch_github_changelog_files(
                    client,
                    owner,
                    repo,
                    "master",
                    current_version,
                    latest_version,
                    api_usages,
                    api_contexts,
                )
                references.extend(master_refs)

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
        elif parts and parts[0] in {"docs", "doc", "documentation", "api", "reference"}:
            base_parts.append(parts[0])
        elif len(parts) == 1:
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
        api_semantics: Optional[list[dict]] = None,
        current_version: str = "",
        latest_version: str = "",
    ) -> str:
        focused = self._focused_reference_snippets(
            references,
            api_usages,
            api_contexts,
            api_semantics,
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
                    f"evidence_confidence: {reference.get('evidence_confidence', '')}",
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
        old_doc_references = [
            reference for reference in references
            if reference.get("role") == "old_api_docs"
        ]
        api_semantics = self._api_semantics_from_references(
            api_usages,
            old_doc_references,
            api_contexts,
        )
        result["api_semantics"] = api_semantics
        evidence_references = self._focused_reference_snippets(
            references,
            api_usages,
            api_contexts,
            api_semantics,
            from_v,
            to_v,
        )
        result["evidence_references"] = evidence_references
        result["evidence_confidence"] = (
            "high"
            if any(item.get("evidence_confidence") == "high" for item in evidence_references)
            else "medium"
            if any(item.get("evidence_confidence") == "medium" for item in evidence_references)
            else "low"
            if evidence_references
            else "none"
        )
        changelog_text = self._references_changelog_text(
            references,
            api_usages,
            api_contexts,
            api_semantics,
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
                api_semantics,
                api_contexts,
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
        api_semantics: Optional[list[dict]] = None,
        api_contexts: Optional[list[dict]] = None,
    ) -> dict:
        system_prompt = (
            "You are a senior dependency management AI for Python, Rust, Go, JavaScript, TypeScript, Java, "
            "and other ecosystems. Extract ONLY documented migration facts relevant to code usage. "
            "Use release notes, changelogs, migration guides, registry metadata, and docs references. "
            "Format strictly as JSON."
        )
        
        if api_usages:
            usages_str = ", ".join(api_usages)
            usage_constraint = f"\nCRITICAL: You MUST ONLY extract breaking changes that affect these specific APIs used in the project: [{usages_str}]. Ignore all other breaking changes."
        else:
            usage_constraint = "\nOnly extract breaking changes that are explicitly and directly documented in the provided evidence chunks. Do not infer, speculate about, or add undocumented API changes."

        prompt = f"""
            Analyze DepGuard's compact evidence dossier for '{package}' from version {from_v} to {to_v} and extract ONLY breaking changes relevant to the package's ecosystem and API usage:{usage_constraint}
            The dossier is already retrieved and ranked. Do not use outside knowledge to add APIs, replacements, or version facts.
            Prefer old_api values that match the exact APIs used by the project when possible.
            Fill new_api only when an evidence chunk documents a replacement, renamed API, changed signature, or clear migration pattern.
            If a referenced change needs review but no replacement is documented, leave new_api empty and explain why.
            Do not report an API as removed, renamed, or changed unless an evidence chunk directly names that API, its matched code alias, or a documented old/new API pair for it.
            Do not convert broad compatibility notes into removals for ordinary still-valid APIs.

            IMPORTANT — keyword argument and parameter changes:
            When an evidence chunk documents that a keyword argument or parameter was removed, renamed, or deprecated, you MUST:
            1. Include the kwarg migration detail in the description of the related breaking_changes entry. Format: "Note: <old_kwarg> argument removed; use <replacement> instead."
            2. Set api_evidence.replacement to a COMPLETE code example of the new call — NEVER include deprecated/removed kwargs in the replacement example.

            Return a JSON object with this exact structure:
            {{
            "breaking_changes": [
                {{
                "type": "removed|renamed|changed_signature",
                "old_api": "fully qualified or clear name",
                "new_api": "new name or workaround if applicable",
                "description": "migration description — MUST include any keyword arguments that were removed/renamed and their replacements (format: 'Note: <kwarg> argument removed; use <replacement> instead.')"
                }}
            ],
            "api_evidence": [
                {{
                "api": "API affected",
                "change_type": "removed|renamed|changed_signature|behavior_change|migration_review",
                "replacement": "COMPLETE code example of the new call with correct kwargs — NEVER include deprecated/removed kwargs in this example",
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

            Old API semantic map gathered from old-version docs or code context:
            {self._format_api_semantics(api_semantics or [])}

            Evidence dossier:
            {changelog[:self.analysis_max_chars]}
            """
        try:
            response = await self.router.complete(system_prompt, prompt, max_tokens=self.analysis_max_tokens, task_type="changelog")
            data = self._extract_json_object(response.content)
            if data:
                data = self._filter_llm_migrations_by_evidence(
                    data,
                    api_usages,
                    references or [],
                    api_contexts,
                )
                data["llm_provider"] = response.provider
                return data
        except Exception as e:
            logger.error(f"Error calling LLM: {e}")
        return {"breaking_changes": [], "confidence_score": 0.0, "llm_provider": "none"}

    def _filter_llm_migrations_by_evidence(
        self,
        data: dict,
        api_usages: Optional[list[str]],
        references: list[dict],
        api_contexts: Optional[list[dict]] = None,
    ) -> dict:
        if not isinstance(data, dict):
            return {"breaking_changes": [], "api_evidence": [], "confidence_score": 0.0}

        allowed_apis = {str(api or "").strip() for api in api_usages or [] if str(api or "").strip()}
        api_evidence = [
            item for item in data.get("api_evidence", []) or []
            if isinstance(item, dict)
        ]
        evidence_by_api: dict[str, list[dict]] = defaultdict(list)
        for item in api_evidence:
            api = str(item.get("api", "") or "").strip()
            if api:
                evidence_by_api[api].append(item)

        kept_changes = []
        kept_evidence = []
        for change in data.get("breaking_changes", []) or []:
            if not isinstance(change, dict):
                continue
            old_api = str(change.get("old_api", "") or "").strip()
            if not old_api:
                continue
            change_type = str(change.get("type", "") or "").strip()
            new_api = str(change.get("new_api", "") or "").strip()
            if change_type == "renamed" and (
                not new_api
                or self._normalized_api_name(old_api) == self._normalized_api_name(new_api)
            ):
                logger.debug("Dropping LLM migration for %s: no-op rename", old_api)
                continue
            if allowed_apis and not any(
                old_api == allowed_api or old_api.startswith(f"{allowed_api}.")
                for allowed_api in allowed_apis
            ):
                continue

            related_evidence = [
                item
                for api, items in evidence_by_api.items()
                if old_api == api or old_api.startswith(f"{api}.") or api.startswith(f"{old_api}.")
                for item in items
            ]
            if api_evidence and not related_evidence:
                logger.debug("Dropping LLM migration for %s: no api_evidence item", old_api)
                continue

            valid_related_evidence = [
                item for item in related_evidence
                if self._evidence_supports_api_change(
                    old_api,
                    self._api_evidence_item_text(item),
                    api_contexts,
                    change.get("type", ""),
                )
            ]
            generated_evidence = None
            support_text = "\n".join(
                self._api_evidence_item_text(item)
                for item in valid_related_evidence
            )
            if not support_text:
                generated_evidence = self._find_supporting_reference_evidence(
                    old_api,
                    change,
                    references,
                    api_contexts,
                )
                if generated_evidence:
                    support_text = self._api_evidence_item_text(generated_evidence)

            if not generated_evidence and not self._evidence_supports_api_change(
                old_api,
                support_text,
                api_contexts,
                change.get("type", ""),
            ):
                logger.debug("Dropping LLM migration for %s: no direct evidence support", old_api)
                continue

            kept_changes.append(change)
            kept_evidence.extend(valid_related_evidence)
            if generated_evidence:
                kept_evidence.append(generated_evidence)

        data["breaking_changes"] = kept_changes
        if api_evidence:
            seen = set()
            deduped_evidence = []
            for item in kept_evidence:
                key = json.dumps(item, sort_keys=True, default=str)
                if key in seen:
                    continue
                seen.add(key)
                deduped_evidence.append(item)
            data["api_evidence"] = deduped_evidence
        if not kept_changes:
            data["confidence_score"] = 0.0
        return data

    def _api_evidence_item_text(self, item: dict) -> str:
        # Validate against quoted/source evidence only; LLM-authored api/reason fields
        # can repeat the requested API even when the cited quote is unrelated.
        parts = []
        for evidence in item.get("evidence", []) or []:
            if isinstance(evidence, dict):
                parts.extend([
                    str(evidence.get("quote", "") or ""),
                    str(evidence.get("url", "") or ""),
                    str(evidence.get("source", "") or ""),
                ])
        return "\n".join(part for part in parts if part)

    def _find_supporting_reference_evidence(
        self,
        old_api: str,
        change: dict,
        references: list[dict],
        api_contexts: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        variants = sorted(
            self._api_evidence_variants(old_api, api_contexts),
            key=len,
            reverse=True,
        )
        replacement_variants = self._replacement_evidence_variants(str(change.get("new_api", "") or ""))
        for index, reference in enumerate(references, start=1):
            if not isinstance(reference, dict):
                continue
            raw_text = "\n".join([
                str(reference.get("title", "") or ""),
                str(reference.get("url", "") or ""),
                str(reference.get("content", "") or ""),
            ])
            normalized = self._normalized_evidence_text(raw_text)
            if not normalized or not self._has_migration_language(normalized):
                continue
            direct_support = any(
                self._contains_api_variant(normalized, variant)
                for variant in variants
            )
            semantic_support = (
                replacement_variants
                and any(self._contains_api_variant(normalized, variant) for variant in replacement_variants)
                and self._reference_has_semantic_match(reference)
            )
            if not direct_support and not semantic_support:
                continue
            if direct_support and not self._evidence_supports_api_change(
                old_api,
                raw_text,
                api_contexts,
                str(change.get("type", "") or ""),
            ):
                continue
            quote = self._extract_reference_quote(
                raw_text,
                variants if direct_support else replacement_variants,
            )
            return {
                "api": old_api,
                "change_type": str(change.get("type", "") or "migration_review"),
                "replacement": str(change.get("new_api", "") or ""),
                "confidence": str(reference.get("evidence_confidence", "") or "medium"),
                "evidence": [{
                    "source_index": index,
                    "source": self._document_kind(reference),
                    "url": str(reference.get("url", "") or ""),
                    "quote": quote,
                }],
                "reason": f"Retrieved evidence directly mentions {old_api} with migration language.",
            }
        return None

    def _replacement_evidence_variants(self, new_api: str) -> list[str]:
        normalized_api = new_api.replace("::", ".").replace("/", ".").strip()
        if not normalized_api:
            return []
        parts = [part for part in normalized_api.split(".") if part]
        variants = {normalized_api}
        if len(parts) >= 2:
            variants.add(".".join(parts[-2:]))
            variants.add(parts[-1])
            variants.add(f"{parts[-1]}()")
        return sorted(variants, key=len, reverse=True)

    def _reference_has_semantic_match(self, reference: dict) -> bool:
        try:
            if float(reference.get("semantic_score", 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        matched_terms = [
            str(term or "").strip()
            for term in reference.get("matched_terms", []) or []
        ]
        generic_terms = {"project", "package", "breaking-change-section"}
        return any(
            len(term) >= 4 and term.lower() not in generic_terms
            for term in matched_terms
        )

    def _extract_reference_quote(self, text: str, variants: list[str], max_chars: int = 280) -> str:
        clean_text = re.sub(r"\s+", " ", text or "").strip()
        normalized = self._normalized_evidence_text(clean_text)
        if not clean_text or not normalized:
            return ""
        best_pos = -1
        for variant in variants:
            normalized_variant = self._normalized_evidence_text(variant)
            if not normalized_variant:
                continue
            match = re.search(rf"(?<![\w.]){re.escape(normalized_variant)}(?![\w.])", normalized)
            if match:
                best_pos = match.start()
                break
        if best_pos < 0:
            return clean_text[:max_chars]
        start = max(0, best_pos - max_chars // 2)
        end = min(len(clean_text), start + max_chars)
        snippet = clean_text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(clean_text):
            snippet += "..."
        return snippet

    def _evidence_supports_api_change(
        self,
        old_api: str,
        evidence_text: str,
        api_contexts: Optional[list[dict]] = None,
        change_type: str = "",
    ) -> bool:
        normalized = self._normalized_evidence_text(evidence_text)
        if not normalized or not self._has_migration_language(normalized):
            return False
        if change_type == "removed" and not any(
            term in normalized
            for term in ["removed", "remove", "no longer", "deprecated", "deprecation", "expired"]
        ):
            return False
        variants = self._api_evidence_variants(old_api, api_contexts)
        return any(self._contains_api_variant(normalized, variant) for variant in variants)

    def _api_evidence_variants(
        self,
        old_api: str,
        api_contexts: Optional[list[dict]] = None,
    ) -> set[str]:
        variants = {old_api}
        normalized_api = old_api.replace("::", ".").replace("/", ".")
        parts = [part for part in normalized_api.split(".") if part]
        if len(parts) >= 2:
            variants.add(".".join(parts[-2:]))
            leaf = parts[-1]
            ambiguous_leaves = {
                "validator",
                "serializer",
                "model",
                "field",
                "fields",
                "schema",
                "config",
                "base",
                "basemodel",
                "manager",
                "client",
                "create",
                "open",
                "append",
            }
            if len(leaf) >= 8 and leaf.lower() not in ambiguous_leaves:
                variants.add(leaf)
            if leaf.isidentifier():
                variants.add(f"{leaf}(")
                variants.add(f"@{leaf}")
        if len(parts) >= 3:
            variants.add(f"{parts[-1]}()")
        for context in api_contexts or []:
            if not isinstance(context, dict):
                continue
            context_api = str(context.get("api", "") or context.get("old_api", "") or "").strip()
            if context_api != old_api:
                continue
            for key in ("matched_text", "code_snippet"):
                value = str(context.get(key, "") or "").strip()
                if not value:
                    continue
                for match in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value):
                    variants.add(match.rstrip("("))
        return {variant for variant in variants if variant}

    def _normalized_api_name(self, api_name: str) -> str:
        value = (api_name or "").strip()
        value = value.replace("::", ".").replace("/", ".")
        value = re.sub(r"\(\)$", "", value)
        value = value.strip(".")
        return value.lower()

    def _contains_api_variant(self, normalized_text: str, variant: str) -> bool:
        normalized_variant = self._normalized_evidence_text(variant)
        if not normalized_variant:
            return False
        pattern = rf"(?<![\w.]){re.escape(normalized_variant)}(?![\w.])"
        return re.search(pattern, normalized_text) is not None

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
            "api_semantics": [],
            "evidence_confidence": "none",
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
