import json

import pytest

from agents import scout as scout_module
from agents.scout import ScoutAgent
from tools.llm_router import LLMResponse


class FakeNoCallRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        raise AssertionError("Scout should not ask the LLM to classify deprecated APIs without retrieved changelog text")


class FakeCargoChangelogRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        assert "rspotify" in user_prompt
        assert "CHANGELOG.md" in user_prompt
        assert "AuthCodePkceSpotify" in user_prompt
        return LLMResponse(
            content=json.dumps({
                "breaking_changes": [{
                    "type": "changed_signature",
                    "old_api": "rspotify.AuthCodePkceSpotify",
                    "new_api": "rspotify.AuthCodePkceSpotify::with_config",
                    "description": "Auth client construction should be reviewed against the latest crate docs.",
                }],
                "confidence_score": 0.91,
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class FakePandasChangelogRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        assert "pandas.DataFrame.append" in user_prompt
        assert "doc/source/whatsnew/v2.0.0.rst" in user_prompt
        assert "concat" in user_prompt
        assert "Pandas Logo" not in user_prompt
        assert "Powerful Python Data Analysis Toolkit" not in user_prompt
        return LLMResponse(
            content=json.dumps({
                "breaking_changes": [{
                    "type": "removed",
                    "old_api": "pandas.DataFrame.append",
                    "new_api": "pandas.concat",
                    "description": "DataFrame.append was removed; use pandas.concat instead.",
                }],
                "confidence_score": 0.94,
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class FakeNoRepoClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *args, **kwargs):
        self.requests.append(url)
        if "api.deps.dev" in url:
            return FakeResponse(404, {})
        if "pypi.org" in url:
            return FakeResponse(200, {
                "info": {
                    "summary": "Powerful data structures for data analysis.",
                    "package_url": "https://pypi.org/project/pandas/",
                    "project_urls": {
                        "Migration Guide": "https://pandas.pydata.org/docs/whatsnew/",
                    },
                }
            })
        return FakeResponse(404, {})


class FakePandasDocsClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *args, **kwargs):
        self.requests.append(url)
        if "api.deps.dev" in url:
            return FakeResponse(200, {
                "links": [{"url": "https://github.com/pandas-dev/pandas"}],
            })
        if "api.github.com/repos/pandas-dev/pandas/releases" in url:
            return FakeResponse(404, {})
        if "api.github.com/repos/pandas-dev/pandas" in url:
            return FakeResponse(200, {
                "html_url": "https://github.com/pandas-dev/pandas",
                "description": "Powerful data structures for data analysis.",
                "default_branch": "main",
            })
        if "raw.githubusercontent.com/pandas-dev/pandas/main/doc/source/whatsnew/index.rst" in url:
            return FakeResponse(200, text="\n".join([
                ".. toctree::",
                "   :maxdepth: 1",
                "",
                "   v3.0.0",
                "   v2.2.0",
                "   v2.0.0",
            ]))
        if "raw.githubusercontent.com/pandas-dev/pandas/main/doc/source/whatsnew/v2.0.0.rst" in url:
            return FakeResponse(200, text="DataFrame.append and Series.append were removed. Use concat instead.")
        return FakeResponse(404, {})


class FakeCargoClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *args, **kwargs):
        self.requests.append(url)
        if "api.deps.dev" in url:
            return FakeResponse(404, {})
        if "crates.io/api/v1/crates/rspotify" in url:
            return FakeResponse(200, {
                "crate": {
                    "description": "Spotify API wrapper.",
                    "newest_version": "0.16.1",
                    "documentation": "https://docs.rs/rspotify",
                    "repository": "git+https://github.com/ramsayleung/rspotify.git",
                }
            })
        if "api.github.com/repos/ramsayleung/rspotify" in url:
            return FakeResponse(200, {
                "html_url": "https://github.com/ramsayleung/rspotify",
                "description": "Spotify API wrapper",
                "default_branch": "master",
            })
        if "raw.githubusercontent.com/ramsayleung/rspotify/master/CHANGELOG.md" in url:
            return FakeResponse(200, text="## 0.16.1\nReview AuthCodePkceSpotify construction and OAuth setup.")
        return FakeResponse(404, {})


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_scout_does_not_guess_api_migrations_when_changelog_text_is_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: FakeNoRepoClient())

    scout = ScoutAgent()
    scout.router = FakeNoCallRouter()

    result = await scout.run(
        {
            "name": "pandas",
            "current_version": "2.0.0",
            "latest_version": "3.0.3",
            "ecosystem": "pip",
        },
        ["pandas.DataFrame", "pandas.DataFrame.append"],
    )

    assert result["references"]
    assert any(reference["title"] == "Migration Guide" for reference in result["references"])
    assert result["breaking_changes"] == []
    assert result["confidence_score"] == 0.0


@pytest.mark.asyncio
async def test_scout_collects_crates_docs_and_github_changelog(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: FakeCargoClient())

    scout = ScoutAgent()
    scout.router = FakeCargoChangelogRouter()

    result = await scout.run(
        {
            "name": "rspotify",
            "current_version": "0.10.0",
            "latest_version": "0.16.1",
            "ecosystem": "cargo",
        },
        ["rspotify.AuthCodePkceSpotify", "rspotify.OAuth"],
    )

    urls = [reference["url"] for reference in result["references"]]
    assert "https://docs.rs/rspotify/0.16.1/" in urls
    assert "https://docs.rs/crate/rspotify/0.16.1/source/" in urls
    assert "https://github.com/ramsayleung/rspotify" in urls
    assert any(reference["title"] == "CHANGELOG.md" for reference in result["references"])
    assert result["breaking_changes"] == [{
        "type": "changed_signature",
        "old_api": "rspotify.AuthCodePkceSpotify",
        "new_api": "rspotify.AuthCodePkceSpotify::with_config",
        "description": "Auth client construction should be reviewed against the latest crate docs.",
    }]


def test_scout_normalizes_common_repository_urls():
    scout = ScoutAgent()

    assert scout._normalize_repo_url("git+https://github.com/pandas-dev/pandas.git") == "https://github.com/pandas-dev/pandas"
    assert scout._normalize_repo_url("git@github.com:ramsayleung/rspotify.git") == "https://github.com/ramsayleung/rspotify"
    assert scout._normalize_repo_url("github:expressjs/express") == "https://github.com/expressjs/express"
    assert scout._normalize_repo_url("https://github.com/owner/repo/tree/main") == "https://github.com/owner/repo"


@pytest.mark.asyncio
async def test_scout_follows_pandas_whatsnew_index_to_version_pages(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: FakePandasDocsClient())

    scout = ScoutAgent()
    scout.router = FakePandasChangelogRouter()

    result = await scout.run(
        {
            "name": "pandas",
            "current_version": "2.0.0",
            "latest_version": "3.0.3",
            "ecosystem": "pip",
        },
        ["pandas.DataFrame", "pandas.DataFrame.append"],
    )

    assert any(
        reference["title"] == "doc/source/whatsnew/v2.0.0.rst"
        for reference in result["references"]
    )
    assert result["evidence_references"]
    assert any(
        "DataFrame.append and Series.append were removed" in reference["content"]
        for reference in result["evidence_references"]
    )
    assert result["breaking_changes"] == [{
        "type": "removed",
        "old_api": "pandas.DataFrame.append",
        "new_api": "pandas.concat",
        "description": "DataFrame.append was removed; use pandas.concat instead.",
    }]
