import pytest
import tempfile
import os
from pathlib import Path
from tools.lockfile_resolver import LockfileResolver

def test_resolve_pipfile_lock():
    with tempfile.TemporaryDirectory() as d:
        lockfile = Path(d) / "Pipfile.lock"
        lockfile.write_text("""
        {
            "default": {
                "requests": {
                    "version": "==2.31.0"
                }
            }
        }
        """)
        
        resolver = LockfileResolver()
        assert resolver.resolve("requests", d) == "2.31.0"
        assert resolver.resolve("django", d) is None

def test_resolve_poetry_lock():
    with tempfile.TemporaryDirectory() as d:
        lockfile = Path(d) / "poetry.lock"
        lockfile.write_text("""
        [[package]]
        name = "pydantic"
        version = "1.10.8"
        
        [[package]]
        name = "fastapi"
        version = "0.95.1"
        """)
        
        resolver = LockfileResolver()
        assert resolver.resolve("pydantic", d) == "1.10.8"
        assert resolver.resolve("httpx", d) is None

def test_no_lockfiles():
    with tempfile.TemporaryDirectory() as d:
        resolver = LockfileResolver()
        assert resolver.resolve("anything", d) is None

def test_resolve_package_lock_json_v2():
    with tempfile.TemporaryDirectory() as d:
        lockfile = Path(d) / "package-lock.json"
        lockfile.write_text("""
        {
            "packages": {
                "node_modules/react": {
                    "version": "18.2.0"
                }
            }
        }
        """)
        
        resolver = LockfileResolver()
        assert resolver.resolve("react", d) == "18.2.0"
