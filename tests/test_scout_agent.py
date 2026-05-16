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
                "api_evidence": [{
                    "api": "pandas.DataFrame.append",
                    "change_type": "removed",
                    "replacement": "pandas.concat",
                    "confidence": "high",
                    "evidence": [{
                        "source_index": 1,
                        "source": "release_notes",
                        "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v2.0.0.rst",
                        "quote": "DataFrame.append and Series.append were removed. Use concat instead.",
                    }],
                    "reason": "The evidence explicitly names the removed method and replacement.",
                }],
                "confidence_score": 0.94,
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class FakeScipyChangelogRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        assert "scipy.integrate.simps" in user_prompt
        assert "doc/source/release/1.14.0-notes.rst" in user_prompt
        assert "simpson" in user_prompt
        assert "SciPy (pronounced" not in user_prompt
        return LLMResponse(
            content=json.dumps({
                "breaking_changes": [{
                    "type": "removed",
                    "old_api": "scipy.integrate.simps",
                    "new_api": "scipy.integrate.simpson",
                    "description": "simps was removed; use simpson instead.",
                }],
                "api_evidence": [{
                    "api": "scipy.integrate.simps",
                    "change_type": "removed",
                    "replacement": "scipy.integrate.simpson",
                    "confidence": "high",
                    "evidence": [{
                        "source_index": 1,
                        "source": "release_notes",
                        "url": "https://github.com/scipy/scipy/blob/main/doc/source/release/1.14.0-notes.rst",
                        "quote": "simps has been removed in favour of simpson.",
                    }],
                    "reason": "The release notes explicitly document the removed function and replacement.",
                }],
                "confidence_score": 0.95,
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class FakePydanticMigrationRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        assert "pydantic.BaseModel" in user_prompt
        assert "migration/" in user_prompt or "docs/migration.md" in user_prompt
        assert "model_dump" in user_prompt
        assert "Pydantic Logfire" not in user_prompt
        return LLMResponse(
            content=json.dumps({
                "breaking_changes": [{
                    "type": "renamed",
                    "old_api": "pydantic.BaseModel.dict",
                    "new_api": "pydantic.BaseModel.model_dump",
                    "description": "BaseModel.dict was renamed to model_dump in Pydantic v2.",
                }],
                "api_evidence": [{
                    "api": "pydantic.BaseModel",
                    "change_type": "behavior_change",
                    "replacement": "",
                    "confidence": "high",
                    "evidence": [{
                        "source_index": 1,
                        "source": "migration_guide",
                        "url": "https://docs.pydantic.dev/latest/migration/",
                        "quote": "BaseModel methods have been renamed; dict() is now model_dump().",
                    }],
                    "reason": "The migration guide documents BaseModel method renames that affect subclasses.",
                }],
                "confidence_score": 0.93,
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


class FakeScipyDocsClient:
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
                "links": [{"url": "https://github.com/scipy/scipy"}],
            })
        if "api.github.com/repos/scipy/scipy/releases" in url:
            return FakeResponse(404, {})
        if "api.github.com/repos/scipy/scipy" in url:
            return FakeResponse(200, {
                "html_url": "https://github.com/scipy/scipy",
                "description": "SciPy library main repository",
                "default_branch": "main",
            })
        if "pypi.org" in url:
            return FakeResponse(200, {
                "info": {
                    "summary": "Fundamental algorithms for scientific computing in Python",
                    "description": "SciPy (pronounced Sigh Pie) is an open-source package for science.",
                    "package_url": "https://pypi.org/project/scipy/",
                    "project_urls": {
                        "Documentation": "https://docs.scipy.org/doc/scipy/",
                        "Source": "https://github.com/scipy/scipy",
                    },
                },
            })
        if "raw.githubusercontent.com/scipy/scipy/main/doc/source/release.rst" in url:
            return FakeResponse(200, text="\n".join([
                ".. toctree::",
                "   :maxdepth: 1",
                "",
                "   release/1.17.0-notes",
                "   release/1.14.0-notes",
            ]))
        if "raw.githubusercontent.com/scipy/scipy/main/doc/source/release/1.14.0-notes.rst" in url:
            return FakeResponse(
                200,
                text="scipy.integrate.simps, trapz, and cumtrapz have been removed in favour of simpson, trapezoid, and cumulative_trapezoid.",
            )
        return FakeResponse(404, {})


class FakePydanticDocsClient:
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
                "links": [{"url": "https://github.com/pydantic/pydantic"}],
            })
        if "api.github.com/repos/pydantic/pydantic/releases" in url:
            return FakeResponse(404, {})
        if "api.github.com/repos/pydantic/pydantic/git/trees/main" in url:
            return FakeResponse(200, {
                "tree": [
                    {"type": "blob", "path": "docs/migration.md"},
                    {"type": "blob", "path": "docs/concepts/models.md"},
                    {"type": "blob", "path": "README.md"},
                ],
            })
        if "api.github.com/repos/pydantic/pydantic" in url:
            return FakeResponse(200, {
                "html_url": "https://github.com/pydantic/pydantic",
                "description": "Data validation using Python type hints",
                "default_branch": "main",
            })
        if "pypi.org" in url:
            return FakeResponse(200, {
                "info": {
                    "summary": "Data validation using Python type hints",
                    "description": "Pydantic Logfire\n\n## Pydantic V1.10 vs. V2\nPydantic V2 is a ground-up rewrite with breaking changes.",
                    "package_url": "https://pypi.org/project/pydantic/",
                    "project_urls": {
                        "Documentation": "https://docs.pydantic.dev/latest/",
                        "Source": "https://github.com/pydantic/pydantic",
                    },
                },
            })
        if "raw.githubusercontent.com/pydantic/pydantic/main/docs/migration.md" in url:
            return FakeResponse(
                200,
                text="## Changes to pydantic.BaseModel\nBaseModel methods have been renamed. The dict() method is now named model_dump(). The json() method is now named model_dump_json().",
            )
        if "docs.pydantic.dev/latest/migration" in url:
            return FakeResponse(
                200,
                text="<html><body><main><h1>Migration Guide</h1><h2>Changes to pydantic.BaseModel</h2><p>BaseModel methods have been renamed. The dict() method is now named model_dump().</p></main></body></html>",
            )
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
    assert result["api_evidence"][0]["replacement"] == "pandas.concat"


@pytest.mark.asyncio
async def test_scout_follows_scipy_release_notes_and_filters_registry_noise(monkeypatch: pytest.MonkeyPatch):
    client = FakeScipyDocsClient()
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: client)

    scout = ScoutAgent()
    scout.router = FakeScipyChangelogRouter()

    result = await scout.run(
        {
            "name": "scipy",
            "current_version": "1.14.0",
            "latest_version": "1.17.1",
            "ecosystem": "pip",
        },
        ["scipy", "scipy.integrate", "scipy.integrate.simps"],
    )

    assert any(
        reference["title"] == "doc/source/release/1.14.0-notes.rst"
        for reference in result["references"]
    )
    assert result["evidence_references"]
    assert "simps" in result["evidence_references"][0]["content"]
    assert all(reference.get("source") != "pypi" for reference in result["evidence_references"])
    assert result["breaking_changes"] == [{
        "type": "removed",
        "old_api": "scipy.integrate.simps",
        "new_api": "scipy.integrate.simpson",
        "description": "simps was removed; use simpson instead.",
    }]
    assert result["api_evidence"][0]["replacement"] == "scipy.integrate.simpson"


@pytest.mark.asyncio
async def test_scout_finds_pydantic_v2_migration_docs_from_docs_url_and_repo_tree(monkeypatch: pytest.MonkeyPatch):
    client = FakePydanticDocsClient()
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: client)

    scout = ScoutAgent()
    scout.router = FakePydanticMigrationRouter()

    result = await scout.run(
        {
            "name": "pydantic",
            "current_version": "1.10.12",
            "latest_version": "2.13.4",
            "ecosystem": "pip",
        },
        ["pydantic", "pydantic.BaseModel"],
    )

    assert any(
        reference["url"] == "https://docs.pydantic.dev/latest/migration"
        for reference in result["references"]
    )
    assert any(
        reference["title"] == "docs/migration.md"
        for reference in result["references"]
    )
    assert result["evidence_references"]
    assert "BaseModel methods have been renamed" in result["evidence_references"][0]["content"]
    assert all(reference.get("source") != "pypi" for reference in result["evidence_references"])
    assert result["breaking_changes"][0]["new_api"] == "pydantic.BaseModel.model_dump"


def test_scout_resolves_nested_release_index_paths_relative_to_index_file():
    scout = ScoutAgent()

    candidates = scout._linked_changelog_candidates(
        "doc/source/release.rst",
        "\n".join([
            ".. toctree::",
            "   :maxdepth: 1",
            "",
            "   release/1.17.0-notes",
            "   release/1.14.0-notes",
        ]),
        "1.14.0",
        "1.17.1",
    )

    assert "doc/source/release/1.14.0-notes.rst" in candidates
    assert "doc/source/release/1.17.0-notes.rst" in candidates


def test_scout_builds_compact_ranked_evidence_chunks():
    scout = ScoutAgent()
    scout.evidence_chunk_chars = 700
    scout.evidence_max_chunks = 3
    references = [{
        "source": "github",
        "title": "doc/source/whatsnew/v2.0.0.rst",
        "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v2.0.0.rst",
        "content": "\n\n".join([
            "Pandas Logo\n" + ("general project metadata\n" * 80),
            "Removal notice\nDataFrame.append and Series.append were removed. Use concat instead.",
            "Other unrelated release notes\n" + ("DataFrame.sum regression details\n" * 80),
        ]),
    }]

    evidence = scout._focused_reference_snippets(
        references,
        ["pandas.DataFrame.append"],
        current_version="2.0.0",
        latest_version="3.0.3",
    )

    assert evidence
    assert "DataFrame.append and Series.append were removed" in evidence[0]["content"]
    assert "Pandas Logo" not in evidence[0]["content"]
    assert len(evidence[0]["content"]) < 1000


def test_scout_filters_versioned_docs_outside_migration_window():
    scout = ScoutAgent()

    assert scout._score_doc_path(
        "doc/source/whatsnew/v0.11.0.rst",
        ["pandas.DataFrame.append"],
        None,
        "2.0.0",
        "3.0.3",
    ) == 0
    assert scout._score_doc_path(
        "doc/source/whatsnew/v2.0.0.rst",
        ["pandas.DataFrame.append"],
        None,
        "2.0.0",
        "3.0.3",
    ) > 0


def test_scout_prioritizes_exact_method_evidence_over_generic_dataframe_chunks():
    scout = ScoutAgent()
    scout.evidence_chunk_chars = 900
    references = [
        {
            "source": "github",
            "title": "doc/source/whatsnew/v3.0.0.rst",
            "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v3.0.0.rst",
            "content": "pd.DataFrame examples now support pd.col expressions for DataFrame.assign.",
        },
        {
            "source": "github",
            "title": "doc/source/whatsnew/v2.0.0.rst",
            "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v2.0.0.rst",
            "content": "Removed deprecated :meth:`DataFrame.append` and :meth:`Series.append`; use :func:`concat` instead.",
        },
        {
            "source": "github",
            "title": "doc/source/whatsnew/v0.11.0.rst",
            "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v0.11.0.rst",
            "content": "pd.DataFrame examples with HDFStore append and table append behavior.",
        },
    ]

    evidence = scout._focused_reference_snippets(
        references,
        ["pandas", "pandas.DataFrame", "pandas.DataFrame.append"],
        current_version="2.0.0",
        latest_version="3.0.3",
    )

    assert evidence
    assert evidence[0]["url"].endswith("v2.0.0.rst")
    assert "DataFrame.append" in evidence[0]["content"]
    assert all("v0.11.0" not in reference["url"] for reference in evidence)


def test_scout_uses_code_context_terms_for_retrieval():
    scout = ScoutAgent()
    references = [{
        "source": "github",
        "title": "doc/source/whatsnew/v2.0.0.rst",
        "url": "https://github.com/pandas-dev/pandas/blob/main/doc/source/whatsnew/v2.0.0.rst",
        "content": "DataFrame.append and Series.append were removed. Use concat instead.",
    }]

    evidence = scout._focused_reference_snippets(
        references,
        ["pandas.DataFrame"],
        api_contexts=[{"code_snippet": "result = df1.append(df2, ignore_index=True)"}],
    )

    assert evidence
    assert "append" in evidence[0]["matched_terms"]
