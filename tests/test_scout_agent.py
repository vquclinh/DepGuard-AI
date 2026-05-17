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


class FakeSemanticRenameRouter:
    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000, task_type: str = "general"):
        assert "Old API semantic map" in user_prompt
        assert "Append rows from another table" in user_prompt
        assert "Row concatenation helpers were removed" in user_prompt
        return LLMResponse(
            content=json.dumps({
                "breaking_changes": [{
                    "type": "renamed",
                    "old_api": "tabularlib.Table.append",
                    "new_api": "tabularlib.merge_tables",
                    "description": "Table row append behavior moved to merge_tables.",
                }],
                "api_evidence": [{
                    "api": "tabularlib.Table.append",
                    "change_type": "renamed",
                    "replacement": "tabularlib.merge_tables",
                    "confidence": "medium",
                    "evidence": [{
                        "source_index": 1,
                        "source": "release_notes",
                        "url": "https://github.com/acme/tabularlib/blob/main/CHANGELOG.md",
                        "quote": "Row concatenation helpers were removed. Use merge_tables.",
                    }],
                    "reason": "Old docs describe row append behavior and release notes describe that behavior moving to merge_tables.",
                }],
                "confidence_score": 0.82,
            }),
            provider="fake",
            model="test",
            latency_ms=1,
            fallback_used=False,
        )


class FakeSemanticRenameClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *args, **kwargs):
        self.requests.append(url)
        if "api.deps.dev" in url:
            return FakeResponse(200, {"links": [{"url": "https://github.com/acme/tabularlib"}]})
        if "pypi.org" in url:
            return FakeResponse(200, {
                "info": {
                    "summary": "Tables for examples.",
                    "package_url": "https://pypi.org/project/tabularlib/",
                    "project_urls": {
                        "Documentation": "https://docs.example.test/tabularlib/",
                        "Source": "https://github.com/acme/tabularlib",
                    },
                }
            })
        if "docs.example.test/tabularlib/1.0.0/reference/api/tabularlib.Table.append.html" in url:
            return FakeResponse(
                200,
                text="<html><body><h1>Table.append</h1><p>Append rows from another table to the end of this table and return a new table.</p><h2>Parameters</h2><p>other: table-like rows to add. ignore_index: reset row labels.</p><h2>Returns</h2><p>A new combined table.</p></body></html>",
            )
        if "api.github.com/repos/acme/tabularlib/releases" in url:
            return FakeResponse(404, {})
        if "api.github.com/repos/acme/tabularlib" in url:
            return FakeResponse(200, {
                "html_url": "https://github.com/acme/tabularlib",
                "description": "Table utilities",
                "default_branch": "main",
            })
        if "raw.githubusercontent.com/acme/tabularlib/main/CHANGELOG.md" in url:
            return FakeResponse(
                200,
                text="## 2.0.0 Breaking changes\nRow concatenation helpers were removed. Use merge_tables for combining rows into a new table.",
            )
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


@pytest.mark.asyncio
async def test_scout_uses_old_api_docs_semantics_to_find_renamed_behavior(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scout_module.httpx, "AsyncClient", lambda: FakeSemanticRenameClient())

    scout = ScoutAgent()
    scout.router = FakeSemanticRenameRouter()

    result = await scout.run(
        {
            "name": "tabularlib",
            "current_version": "1.0.0",
            "latest_version": "2.0.0",
            "ecosystem": "pip",
        },
        ["tabularlib.Table.append"],
        [{"api": "tabularlib.Table.append", "code_snippet": "result = table.append(other, ignore_index=True)"}],
    )

    assert result["api_semantics"]
    assert result["api_semantics"][0]["source"] == "old_version_docs"
    assert "Append rows from another table" in result["api_semantics"][0]["purpose"]
    assert result["evidence_references"]
    assert "Row concatenation helpers were removed" in result["evidence_references"][0]["content"]
    assert result["evidence_references"][0]["semantic_score"] > 0
    assert result["breaking_changes"][0]["new_api"] == "tabularlib.merge_tables"


def test_scout_normalizes_common_repository_urls():
    scout = ScoutAgent()

    assert scout._normalize_repo_url("git+https://github.com/pandas-dev/pandas.git") == "https://github.com/pandas-dev/pandas"
    assert scout._normalize_repo_url("git@github.com:ramsayleung/rspotify.git") == "https://github.com/ramsayleung/rspotify"
    assert scout._normalize_repo_url("github:expressjs/express") == "https://github.com/expressjs/express"
    assert scout._normalize_repo_url("https://github.com/owner/repo/tree/main") == "https://github.com/owner/repo"
    assert scout._normalize_repo_url("https://github.com/sponsors/someone") == ""


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


def test_scout_considers_intermediate_major_release_note_paths():
    scout = ScoutAgent()

    versions = scout._candidate_version_strings("9.5.0", "12.2.0")
    candidates = scout._common_versioned_doc_candidates("9.5.0", "12.2.0")

    assert "10.0.0" in versions
    assert "11.0.0" in versions
    assert "12.0.0" in versions
    assert "docs/releasenotes/10.0.0.rst" in candidates
    assert scout._score_doc_path(
        "docs/releasenotes/10.0.0.rst",
        ["PIL.Image.OLD_CONSTANT"],
        None,
        "9.5.0",
        "12.2.0",
    ) > scout._score_doc_path(
        "docs/reference/Image.rst",
        ["PIL.Image.OLD_CONSTANT"],
        None,
        "9.5.0",
        "12.2.0",
    )


def test_scout_considers_old_major_minor_releases_before_next_major():
    scout = ScoutAgent()

    versions = scout._candidate_version_strings("1.23.0", "2.4.5")
    candidates = scout._common_versioned_doc_candidates("1.23.0", "2.4.5")

    assert "1.24.0" in versions
    assert "1.25.0" in versions
    assert "1.26.0" in versions
    assert "2.0.0" in versions
    assert "doc/source/release/1.24.0-notes.rst" in candidates


def test_scout_filters_llm_changes_without_direct_api_evidence():
    scout = ScoutAgent()
    data = {
        "breaking_changes": [
            {
                "type": "removed",
                "old_api": "numpy.float",
                "new_api": "numpy.float64",
                "description": "np.float was removed.",
            },
            {
                "type": "removed",
                "old_api": "numpy.array",
                "new_api": "",
                "description": "array behavior may have changed.",
            },
            {
                "type": "removed",
                "old_api": "numpy.bool",
                "new_api": "numpy.bool_",
                "description": "np.bool was removed.",
            },
        ],
        "api_evidence": [
            {
                "api": "numpy.float",
                "confidence": "high",
                "evidence": [{"quote": "``np.float`` was a deprecated alias and has been removed. Use ``float`` or ``np.float64``."}],
            },
            {
                "api": "numpy.array",
                "confidence": "medium",
                "evidence": [{"quote": "The semantics of the copy keyword in np.array changed."}],
            },
            {
                "api": "numpy.bool",
                "confidence": "high",
                "evidence": [{"quote": "Alias ``np.float_`` has been removed. Use ``np.float64`` instead."}],
            },
        ],
        "confidence_score": 0.9,
    }

    filtered = scout._filter_llm_migrations_by_evidence(
        data,
        ["numpy.float", "numpy.array", "numpy.bool"],
        [],
        [{"api": "numpy.float", "matched_text": "np.float"}, {"api": "numpy.bool", "matched_text": "np.bool"}],
    )

    assert [change["old_api"] for change in filtered["breaking_changes"]] == ["numpy.float"]
    assert "numpy.array" not in {item["api"] for item in filtered["api_evidence"]}
    assert "numpy.bool" not in {item["api"] for item in filtered["api_evidence"]}


def test_scout_filters_noop_and_broad_pydantic_validator_claims():
    scout = ScoutAgent()
    data = {
        "breaking_changes": [
            {
                "type": "renamed",
                "old_api": "pydantic.BaseModel",
                "new_api": "pydantic.BaseModel",
                "description": "BaseModel methods changed, but the class remains available.",
            },
            {
                "type": "removed",
                "old_api": "pydantic.validator",
                "new_api": "",
                "description": "The validator decorator was replaced by field_validator.",
            },
        ],
        "api_evidence": [
            {
                "api": "pydantic.BaseModel",
                "confidence": "high",
                "evidence": [{
                    "quote": "Various method names have been changed; BaseModel methods now use model_* names.",
                }],
            },
            {
                "api": "pydantic.validator",
                "confidence": "medium",
                "evidence": [{
                    "quote": "This release fixes supported after model validator function signatures.",
                }],
            },
        ],
        "confidence_score": 0.9,
    }

    filtered = scout._filter_llm_migrations_by_evidence(
        data,
        ["pydantic.BaseModel", "pydantic.validator"],
        [],
        [{"api": "pydantic.validator", "matched_text": "validator", "code_snippet": "@validator('age', always=True)"}],
    )

    assert filtered["breaking_changes"] == []
    assert filtered["api_evidence"] == []
    assert filtered["confidence_score"] == 0.0


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


def test_scout_extends_markdown_code_block_chunks_to_closing_fence():
    scout = ScoutAgent()
    scout.evidence_chunk_chars = 220
    references = [{
        "source": "github",
        "title": "migration/v1.md",
        "url": "https://github.com/openai/openai-python/blob/main/migration/v1.md",
        "content": "\n".join([
            "openai.ChatCompletion.create was removed. Use the client chat completions API instead.",
            "",
            "```python",
            "from openai import OpenAI",
            "client = OpenAI(api_key='dummy-key')",
            "completion = client.chat.completions.create(",
            "    model='gpt-5.2',",
            "    messages=[{'role': 'user', 'content': 'hello'}],",
            ")",
            "print(completion.choices[0].message.content)",
            "```",
            "",
            "Other migration notes.",
        ]),
    }]

    evidence = scout._focused_reference_snippets(
        references,
        ["openai.ChatCompletion.create"],
        current_version="0.28.0",
        latest_version="1.0.0",
    )

    assert evidence
    assert "completion = client.chat.completions.create(" in evidence[0]["content"]
    assert "completion.choices[0].message.content" in evidence[0]["content"]
    assert "```" in evidence[0]["content"]


def test_scout_extends_rst_code_block_chunks_to_complete_example():
    scout = ScoutAgent()
    scout.evidence_chunk_chars = 220
    references = [{
        "source": "github",
        "title": "migration/v1.rst",
        "url": "https://github.com/openai/openai-python/blob/main/migration/v1.rst",
        "content": "\n".join([
            "openai.ChatCompletion.create was removed. Use the client chat completions API instead.",
            "",
            ".. code-block:: python",
            "",
            "    from openai import OpenAI",
            "    client = OpenAI(api_key='dummy-key')",
            "    completion = client.chat.completions.create(",
            "        model='gpt-5.2',",
            "        messages=[{'role': 'user', 'content': 'hello'}],",
            "    )",
            "    print(completion.choices[0].message.content)",
            "",
            "Other migration notes.",
        ]),
    }]

    evidence = scout._focused_reference_snippets(
        references,
        ["openai.ChatCompletion.create"],
        current_version="0.28.0",
        latest_version="1.0.0",
    )

    assert evidence
    assert "completion = client.chat.completions.create(" in evidence[0]["content"]
    assert "completion.choices[0].message.content" in evidence[0]["content"]


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


def test_scout_prioritizes_exact_constant_attribute_evidence():
    scout = ScoutAgent()
    references = [
        {
            "source": "github",
            "title": "docs/releasenotes/2.0.0.rst",
            "url": "https://github.com/acme/examplelib/blob/main/docs/releasenotes/2.0.0.rst",
            "content": "Backwards incompatible changes\n\n``Image.OLD_CONSTANT`` has been removed. Use ``Image.New.OLD_CONSTANT`` instead.",
        },
        {
            "source": "github",
            "title": "docs/releasenotes/2.1.0.rst",
            "url": "https://github.com/acme/examplelib/blob/main/docs/releasenotes/2.1.0.rst",
            "content": "Image resizing performance improvements and assorted API additions.",
        },
    ]

    evidence = scout._focused_reference_snippets(
        references,
        ["importroot.Image.OLD_CONSTANT"],
        current_version="1.0.0",
        latest_version="2.1.0",
    )

    assert evidence
    assert evidence[0]["url"].endswith("2.0.0.rst")
    assert evidence[0]["evidence_confidence"] == "high"
    assert "Image.OLD_CONSTANT" in evidence[0]["content"]


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


def test_scout_prioritizes_receiver_method_migration_evidence():
    scout = ScoutAgent()
    references = [
        {
            "source": "github",
            "title": "doc/build/changelog/migration_14.rst",
            "url": "https://github.com/sqlalchemy/sqlalchemy/blob/main/doc/build/changelog/migration_14.rst",
            "content": "\n".join([
                "ORM Session.execute() uses future style Result sets in all cases.",
                "As noted elsewhere, Result and Row objects now feature named tuple behavior",
                "when used with an Engine that includes create_engine.future set to True.",
            ]),
        },
        {
            "source": "github",
            "title": "doc/build/changelog/migration_20.rst",
            "url": "https://github.com/sqlalchemy/sqlalchemy/blob/main/doc/build/changelog/migration_20.rst",
            "content": "\n".join([
                "engine = create_engine(\"sqlite://\")",
                "engine.execute(\"CREATE TABLE foo (id integer)\")",
                "The above program uses several legacy patterns.",
                "With the above guidance, migrate to use 2.0 styles:",
                "with engine.connect() as connection:",
                "    # use connection.execute(), not engine.execute()",
                "    result = connection.execute(text(\"select id from foo\"))",
            ]),
        },
    ]

    evidence = scout._focused_reference_snippets(
        references,
        ["sqlalchemy.create_engine.execute"],
        api_contexts=[{
            "api": "sqlalchemy.create_engine.execute",
            "matched_text": "self.engine.execute(",
            "code_snippet": "self.engine = create_engine(db_url)\nresult = self.engine.execute(\"SELECT * FROM users\")",
        }],
        current_version="1.4.46",
        latest_version="2.0.49",
    )

    assert evidence
    assert evidence[0]["url"].endswith("migration_20.rst")
    assert "use connection.execute(), not engine.execute()" in evidence[0]["content"]
    assert "engine.execute" in evidence[0]["matched_terms"]
    assert evidence[0]["evidence_confidence"] == "high"


@pytest.mark.parametrize(
    "case",
    [
        {
            "name": "openai_create_ignores_server_endpoint_create_noise",
            "api_usages": ["openai.ChatCompletion.create"],
            "api_contexts": [{"api": "openai.ChatCompletion.create", "code_snippet": "openai.ChatCompletion.create(model='gpt', messages=[])"}],
            "references": [
                ("docs/server-api.md", "LangSmith server API endpoints can create runs, projects, cron jobs, and datasets."),
                ("migration/v1.md", "The openai.ChatCompletion.create API was removed. Use client.chat.completions.create instead."),
            ],
            "expected": "ChatCompletion.create API was removed",
            "forbidden": "cron jobs",
        },
        {
            "name": "pillow_open_ignores_generic_open_docs",
            "api_usages": ["PIL.Image.open"],
            "api_contexts": [{"api": "PIL.Image.open", "code_snippet": "img = Image.open(path)"}],
            "references": [
                ("CONTRIBUTING.md", "Open a pull request after you create a test branch."),
                ("releasenotes/current.rst", "Image.open no longer accepts legacy mode strings. Use the documented mode enum instead."),
            ],
            "expected": "Image.open no longer accepts",
            "forbidden": "pull request",
        },
        {
            "name": "pandas_append_ignores_storage_append_noise",
            "api_usages": ["pandas.DataFrame.append"],
            "api_contexts": [{"api": "pandas.DataFrame.append", "code_snippet": "result = base_df.append(new_df, ignore_index=True)"}],
            "references": [
                ("io.rst", "HDFStore append table examples and append-to-file setup notes."),
                ("whatsnew/v2.0.0.rst", "Removed deprecated DataFrame.append and Series.append; use concat instead."),
            ],
            "expected": "DataFrame.append",
            "forbidden": "HDFStore",
        },
        {
            "name": "langchain_openai_model_name_ignores_langsmith_model_name_noise",
            "api_usages": ["langchain_openai.ChatOpenAI"],
            "api_contexts": [{"api": "langchain_openai.ChatOpenAI", "code_snippet": "llm = ChatOpenAI(model_name='gpt-4')"}],
            "references": [
                ("langsmith/server.md", "The model_name field can be used in LangSmith trace metadata and cron job filters."),
                ("langchain_openai/migration.md", "ChatOpenAI model_name was renamed to model. Use model instead of model_name."),
            ],
            "expected": "ChatOpenAI model_name",
            "forbidden": "trace metadata",
        },
        {
            "name": "langchain_openai_does_not_select_native_openai_migration",
            "api_usages": ["langchain_openai.ChatOpenAI"],
            "api_contexts": [{"api": "langchain_openai.ChatOpenAI", "code_snippet": "llm = ChatOpenAI(model_name='gpt-4')"}],
            "references": [
                ("openai/migration.md", "openai.ChatCompletion.create was removed. Use client.chat.completions.create instead."),
                ("langchain_openai/migration.md", "ChatOpenAI model_name was renamed to model. Use model instead of model_name."),
            ],
            "expected": "ChatOpenAI model_name",
            "forbidden": "ChatCompletion.create",
        },
        {
            "name": "native_openai_does_not_select_langchain_openai_migration",
            "api_usages": ["openai.ChatCompletion.create"],
            "api_contexts": [{"api": "openai.ChatCompletion.create", "code_snippet": "openai.ChatCompletion.create(model='gpt', messages=[])"}],
            "references": [
                ("langchain_openai/migration.md", "ChatOpenAI model_name was renamed to model. Use model instead of model_name."),
                ("openai/migration.md", "openai.ChatCompletion.create was removed. Use client.chat.completions.create instead."),
            ],
            "expected": "ChatCompletion.create",
            "forbidden": "ChatOpenAI",
        },
        {
            "name": "langsmith_create_endpoint_noise_only_returns_empty",
            "api_usages": ["openai.ChatCompletion.create"],
            "api_contexts": [{"api": "openai.ChatCompletion.create", "code_snippet": "openai.ChatCompletion.create(model='gpt', messages=[])"}],
            "references": [
                ("langsmith/server.md", "API endpoints create runs, create projects, create cron jobs, and create datasets."),
                ("docs/setup.md", "Create a virtual environment and create an API key before running examples."),
            ],
            "expected_empty": True,
        },
        {
            "name": "contributing_guides_with_generic_create_are_ignored",
            "api_usages": ["example.Client.create"],
            "api_contexts": [{"api": "example.Client.create", "code_snippet": "client.create(payload)"}],
            "references": [
                ("CONTRIBUTING.md", "Create a branch, create a pull request, and update the changelog."),
                ("docs/release.md", "Maintainers create releases after tests pass."),
            ],
            "expected_empty": True,
        },
        {
            "name": "buried_exact_api_window_beats_front_matter",
            "api_usages": ["pandas.DataFrame.append"],
            "api_contexts": [{"api": "pandas.DataFrame.append", "code_snippet": "result = base_df.append(new_df)"}],
            "references": [
                ("whatsnew/v2.0.0.rst", "\n\n".join([
                    "Project front matter\n" + ("DataFrame examples and setup notes. " * 60),
                    "Removed deprecated DataFrame.append and Series.append; use concat instead.",
                ])),
            ],
            "expected": "DataFrame.append",
            "forbidden": "front matter",
        },
        {
            "name": "keyword_rename_requires_owner_anchor",
            "api_usages": ["urllib3.util.retry.Retry"],
            "api_contexts": [{"api": "urllib3.util.retry.Retry", "code_snippet": "Retry(total=3, method_whitelist={'GET'})"}],
            "references": [
                ("generic.md", "The method whitelist in an unrelated server can be configured by name."),
                ("urllib3-2.0.rst", "Retry method_whitelist was renamed to allowed_methods. Use allowed_methods instead of method_whitelist."),
            ],
            "expected": "Retry method_whitelist",
            "forbidden": "unrelated server",
        },
        {
            "name": "private_underscore_suggestion_noise_is_not_evidence_without_old_api",
            "api_usages": ["pandas.DataFrame.append"],
            "api_contexts": [{"api": "pandas.DataFrame.append", "code_snippet": "base_df.append(new_df)"}],
            "references": [
                ("internal.md", "Internal developers may use _append while creating benchmarks."),
                ("whatsnew/v2.0.0.rst", "Removed deprecated DataFrame.append and Series.append; use concat instead."),
            ],
            "expected": "DataFrame.append",
            "forbidden": "_append",
        },
        {
            "name": "setup_language_with_package_root_but_no_api_anchor_is_ignored",
            "api_usages": ["langchain_openai.ChatOpenAI"],
            "api_contexts": [{"api": "langchain_openai.ChatOpenAI", "code_snippet": "ChatOpenAI(model_name='gpt-4')"}],
            "references": [
                ("setup.md", "Install langchain-openai, create an API key, and open the dashboard."),
            ],
            "expected_empty": True,
        },
    ],
    ids=lambda case: case["name"],
)
def test_scout_hardened_retrieval_noise_matrix(case):
    scout = ScoutAgent()
    scout.evidence_chunk_chars = 700
    scout.evidence_max_chunks = 2
    references = [
        {
            "source": "github",
            "title": title,
            "url": f"https://github.com/acme/project/blob/main/{title}",
            "content": content,
        }
        for title, content in case["references"]
    ]

    evidence = scout._focused_reference_snippets(
        references,
        case["api_usages"],
        api_contexts=case["api_contexts"],
        current_version="1.0.0",
        latest_version="2.0.0",
    )

    if case.get("expected_empty"):
        assert evidence == []
        return

    assert evidence
    assert case["expected"] in evidence[0]["content"]
    assert case["forbidden"] not in evidence[0]["content"]
    assert evidence[0]["evidence_confidence"] in {"high", "medium"}


def test_scout_discards_html_not_found_pages():
    scout = ScoutAgent()

    assert scout._looks_like_not_found_page(
        "Page Not Found! Can't find the page you're looking for. SQLAlchemy Documentation Search terms: Contents | Index",
        "https://docs.example.invalid/missing",
    )
    assert not scout._looks_like_not_found_page(
        "A migration guide explains that legacy APIs are not found by old names after the rename.",
        "",
    )


def test_scout_falls_back_to_code_context_semantics_when_old_docs_missing():
    scout = ScoutAgent()

    semantics = scout._api_semantics_from_references(
        ["examplepkg.Widget.combine"],
        [],
        [{
            "api": "examplepkg.Widget.combine",
            "code_snippet": "result = widget.combine(other, ignore_index=True)",
            "context": "result = widget.combine(other, ignore_index=True)",
        }],
    )

    assert semantics
    assert semantics[0]["source"] == "code_context"
    assert semantics[0]["confidence"] == "low"
    assert "ignore_index" in semantics[0]["parameters"]
    assert "ignore_index" in semantics[0]["search_terms"]


def test_scout_uses_low_confidence_breaking_section_when_semantic_search_misses():
    scout = ScoutAgent()
    references = [{
        "source": "github",
        "title": "CHANGELOG.md",
        "url": "https://github.com/acme/example/blob/main/CHANGELOG.md",
        "content": "## 2.0.0 Breaking changes\nRemoved several legacy helpers. See the migration guide before upgrading.",
    }]

    evidence = scout._focused_reference_snippets(
        references,
        ["examplepkg.Widget.combine"],
        api_semantics=[{
            "api": "examplepkg.Widget.combine",
            "search_terms": ["combine widgets", "widget aggregation"],
        }],
        current_version="1.0.0",
        latest_version="2.0.0",
    )

    assert evidence
    assert evidence[0]["evidence_confidence"] == "low"
    assert evidence[0]["matched_terms"] == ["breaking-change-section"]
