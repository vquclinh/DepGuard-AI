import os
import re
import json
import asyncio
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
    ]

    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.router = LLMRouter() # Abstraction layer: OpenAI, Claude, Gemini, Qwen, local models
        self.reference_max_chars = self._env_int("SCOUT_REFERENCE_MAX_CHARS", 16000)
        self.changelog_file_max_chars = self._env_int("SCOUT_CHANGELOG_FILE_MAX_CHARS", 12000)
        self.analysis_max_chars = self._env_int("SCOUT_ANALYSIS_MAX_CHARS", 30000)
        self.analysis_max_tokens = self._env_int("SCOUT_LLM_MAX_TOKENS", 4000)
        self.evidence_window_chars = self._env_int("SCOUT_EVIDENCE_WINDOW_CHARS", 900)

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
                return ""
            return text[:max_chars]
        except Exception as exc:
            logger.debug("Error fetching text reference %s: %s", url, exc)
            return ""

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
    ) -> list[dict]:
        references = []
        branch = default_branch or "main"
        for path in self.CHANGELOG_PATHS:
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
        return references

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
            path_part = stripped.split()[0].strip()
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
            if "/" in path_part:
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
            if content:
                block.append("excerpt:")
                block.append(content[:3000])
            sections.append("\n".join(block))
            if len("\n\n".join(sections)) > max_chars:
                sections.append("... references truncated ...")
                break
        return "\n\n".join(sections)[:max_chars]

    def _api_search_terms(self, api_usages: Optional[list[str]]) -> list[str]:
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
        return sorted(terms, key=lambda item: (-len(item), item))

    def _focused_reference_snippets(
        self,
        references: list[dict],
        api_usages: Optional[list[str]],
    ) -> list[dict]:
        terms = self._api_search_terms(api_usages)
        if not terms:
            return []

        focused = []
        for reference in self._dedupe_references(references):
            content = (reference.get("content") or "").strip()
            if not content:
                continue

            windows = []
            lowered = content.lower()
            for term in terms:
                term_lower = term.lower()
                start = 0
                while True:
                    index = lowered.find(term_lower, start)
                    if index < 0:
                        break
                    window_start = max(0, index - self.evidence_window_chars)
                    window_end = min(len(content), index + len(term) + self.evidence_window_chars)
                    windows.append((window_start, window_end))
                    start = index + len(term_lower)

            if not windows:
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
            ))

        if release_notes.strip():
            references.insert(0, {
                "source": "github",
                "title": "GitHub release notes",
                "url": repo_url,
                "content": release_notes,
            })

        return self._dedupe_references(references)

    def _references_changelog_text(self, references: list[dict], api_usages: Optional[list[str]] = None) -> str:
        focused = self._focused_reference_snippets(references, api_usages)
        if api_usages:
            references = focused

        sections = []
        for reference in references:
            title = (reference.get("title") or "").lower()
            source = (reference.get("source") or "").lower()
            content = (reference.get("content") or "").strip()
            if not content:
                continue
            if source == "github" or any(token in title for token in ["changelog", "change", "history", "release", "migration", "upgrade"]):
                sections.append(f"## {reference.get('title')}\n{content}")
        return "\n\n".join(sections)

    async def _analyze_references(
        self,
        result: dict,
        package: str,
        from_v: str,
        to_v: str,
        api_usages: Optional[list[str]],
        references: list[dict],
    ) -> dict:
        evidence_references = self._focused_reference_snippets(references, api_usages)
        result["evidence_references"] = evidence_references
        changelog_text = self._references_changelog_text(references, api_usages)
        if changelog_text.strip():
            llm_result = await self._analyze_changelog_with_llm(
                package,
                from_v,
                to_v,
                changelog_text,
                api_usages,
                evidence_references or references,
            )
            result["breaking_changes"] = llm_result.get("breaking_changes", [])
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
            Analyze the changelog for '{package}' from version {from_v} to {to_v} and extract ONLY breaking changes relevant to the package's ecosystem and API usage:{usage_constraint}
            Prefer old_api values that match the exact APIs used by the project when possible.
            Fill new_api only when the references document a replacement, renamed API, changed signature, or clear migration pattern.
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
            "confidence_score": 0.9
            }}
            References gathered by DepGuard:
            {self._format_references_for_llm(references or [])}

            Changelog:
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

    async def run(self, package_info: dict, api_usages: Optional[list[str]] = None) -> dict:
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
        }
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
                )
                result["references"] = references
                return await self._analyze_references(
                    result,
                    name,
                    current_version,
                    latest_version,
                    api_usages,
                    references,
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
                )
                result["references"] = references
                return await self._analyze_references(
                    result,
                    name,
                    current_version,
                    latest_version,
                    api_usages,
                    references,
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
            )
            result["references"] = references
            if not changelog_text:
                return await self._analyze_references(
                    result,
                    name,
                    current_version,
                    latest_version,
                    api_usages,
                    references,
                )
            
            llm_result = await self._analyze_changelog_with_llm(
                name,
                current_version,
                latest_version,
                changelog_text,
                api_usages,
                references,
            )
            result["breaking_changes"] = llm_result.get("breaking_changes", [])
            result["confidence_score"] = llm_result.get("confidence_score", 0.0)
            result["llm_provider"] = llm_result.get("llm_provider", "none")
        return result

    def run_sync(self, package_info: dict, api_usages: Optional[list[str]] = None) -> dict:
        return asyncio.run(self.run(package_info, api_usages))
