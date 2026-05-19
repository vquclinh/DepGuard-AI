"""
10-Package Update Pipeline Integration Test

Creates 10 mini-projects (one per breaking-change scenario), runs the full
ASTScanner → PatchAgent pipeline with real LLM calls, verifies correctness,
and writes a detailed markdown report for any failures.

Pipeline tested: (pre-computed scout output) → ASTScanner.scan() → PatchAgent.run()

All LLM calls (system prompt, user prompt, response) to PatchAgent are captured
so that failures can be traced to the exact prompt/response stage.

Failure classification:
  [NATURAL]  Scout extraction issue — scout output lacks enough context; LLM
             cannot patch correctly. Not a system bug.
  [FLAW]     Context-passing issue — scout output is complete, but information
             was not forwarded to PatchAgent's prompt, causing incorrect output.

Run:
    cd /mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI
    python -m tests.integration_10_packages 2>&1 | tee /tmp/pipeline_10_results.txt

Reports:
    /tmp/pipeline_10_report.md     — full analysis (failures + root causes)
    /tmp/pipeline_10_summary.json  — machine-readable summary
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.patch import PatchAgent
from tools.ast_scanner import ASTScanner

RESET = "\033[0m"
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW= "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"

def color(text, c): return f"{c}{text}{RESET}"


# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class LLMCallLog:
    call_number: int
    system_prompt: str
    user_prompt: str
    response: str
    provider: str


@dataclass
class PipelineTestCase:
    id: str
    description: str
    project_files: dict[str, str]
    dep_file: str
    scout_output: dict
    # assertions: file_path → [(text, should_contain)]
    assertions: dict[str, list[tuple[str, bool]]]


@dataclass
class PipelineResult:
    case: PipelineTestCase
    passed: bool
    elapsed_s: float
    patch_report: dict
    assertion_results: dict[str, list[tuple[str, bool, bool]]]
    failure_reasons: list[str]
    project_dir: str
    ast_output: dict
    llm_calls: list[LLMCallLog] = field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────────────────

def init_git_repo(path: str) -> None:
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "test@depguard.ai"],
        ["git", "config", "user.name", "DepGuard Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ]:
        subprocess.run(cmd, cwd=path, capture_output=True, check=False)


def make_project(files: dict[str, str]) -> str:
    tmpdir = tempfile.mkdtemp(prefix="depguard_10pkg_")
    for rel_path, content in files.items():
        full = Path(tmpdir) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(textwrap.dedent(content))
    init_git_repo(tmpdir)
    return tmpdir


# ── 10 test cases ─────────────────────────────────────────────────────────────

PIPELINE_CASES: list[PipelineTestCase] = [

    # ── 1. pydantic: @validator → @field_validator ────────────────────────────
    PipelineTestCase(
        id="pydantic-rename",
        description="pydantic 1.10 → 2.x: @validator renamed to @field_validator",
        project_files={
            "requirements.txt": "pydantic==1.10.12\n",
            "models/user.py": """\
                from pydantic import BaseModel, validator

                class User(BaseModel):
                    username: str
                    age: int

                    @validator('age', always=True)
                    @classmethod
                    def check_age(cls, v):
                        if v < 18:
                            raise ValueError('Age must be >= 18')
                        return v

                class Product(BaseModel):
                    name: str
                    price: float

                    @validator('price')
                    @classmethod
                    def check_price(cls, v):
                        if v <= 0:
                            raise ValueError('Price must be positive')
                        return v
            """,
            "models/admin.py": """\
                from pydantic import BaseModel, validator

                class Admin(BaseModel):
                    email: str

                    @validator('email')
                    @classmethod
                    def validate_email(cls, v):
                        if '@' not in v:
                            raise ValueError('Invalid email')
                        return v.lower()
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "pydantic",
            "from_version": "1.10.12",
            "to_version": "2.13.4",
            "breaking_changes": [
                {
                    "type": "renamed",
                    "old_api": "pydantic.validator",
                    "new_api": "pydantic.field_validator",
                    "description": "@validator decorator has been deprecated in Pydantic V2 and should be "
                                   "replaced with @field_validator. The new decorator requires @classmethod. "
                                   "Note: always= argument removed; use Field(validate_default=True) instead. "
                                   "Note: pre= argument removed; use mode='before' instead.",
                    "parameters_changed": [
                        {"old_param": "always", "replacement": "Field(validate_default=True)"},
                        {"old_param": "pre", "replacement": "mode='before'"},
                    ],
                }
            ],
            "api_evidence": [
                {
                    "api": "pydantic.validator",
                    "change_type": "renamed",
                    "replacement": "pydantic.field_validator",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://docs.pydantic.dev/latest/migration/",
                                  "quote": "@validator has been deprecated, replaced with @field_validator"}],
                    "reason": "Migration guide explicitly documents @validator as deprecated in V2.",
                }
            ],
            "confidence_score": 0.95,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "models/user.py": [
                ("field_validator", True),
                ("always=True", False),
            ],
            "models/admin.py": [
                ("field_validator", True),
            ],
        },
    ),

    # ── 2. PyJWT: algorithms= required ───────────────────────────────────────
    PipelineTestCase(
        id="pyjwt-algorithms",
        description="PyJWT 1.7 → 2.x: algorithms= parameter required in jwt.decode()",
        project_files={
            "requirements.txt": "PyJWT==1.7.1\n",
            "auth/tokens.py": """\
                import jwt

                SECRET = 'my-secret-key'

                def verify_token(token: str) -> dict:
                    try:
                        payload = jwt.decode(token, SECRET)
                        return payload
                    except jwt.ExpiredSignatureError:
                        raise ValueError('Token expired')

                def verify_token_strict(token: str) -> dict:
                    return jwt.decode(token, SECRET, options={'verify_exp': True})

                def create_token(data: dict) -> str:
                    return jwt.encode(data, SECRET, algorithm='HS256')
            """,
            "utils/auth_helper.py": """\
                import jwt
                from typing import Optional

                def decode_jwt(token: str, secret: str, verify: bool = True) -> Optional[dict]:
                    try:
                        return jwt.decode(token, secret, verify=verify)
                    except jwt.InvalidTokenError:
                        return None
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "PyJWT",
            "from_version": "1.7.1",
            "to_version": "2.12.1",
            "breaking_changes": [
                {
                    "type": "changed_signature",
                    "old_api": "jwt.decode",
                    "new_api": "jwt.decode",
                    "description": "jwt.decode() now requires algorithms= parameter (list of allowed algorithms). "
                                   "The verify= boolean parameter was removed. Pass algorithms=['HS256'] "
                                   "(or appropriate algorithm) as third positional or keyword argument.",
                }
            ],
            "api_evidence": [
                {
                    "api": "jwt.decode",
                    "change_type": "changed_signature",
                    "replacement": "jwt.decode(token, key, algorithms=['HS256'])",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "release_notes",
                                  "url": "https://pyjwt.readthedocs.io/en/stable/changelog.html",
                                  "quote": "algorithms is now required. verify= parameter removed."}],
                    "reason": "CHANGELOG documents algorithms= as required from v2.0.0 onwards.",
                }
            ],
            "confidence_score": 0.90,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "auth/tokens.py": [("algorithms=", True)],
            "utils/auth_helper.py": [("algorithms=", True)],
        },
    ),

    # ── 3. numpy: type aliases removed ───────────────────────────────────────
    PipelineTestCase(
        id="numpy-type-aliases",
        description="numpy 1.x → 2.x: np.bool/np.int/np.float type aliases removed",
        project_files={
            "requirements.txt": "numpy==1.26.4\n",
            "ml/features.py": """\
                import numpy as np

                def preprocess(data):
                    mask = np.array([True, False, True], dtype=np.bool)
                    indices = np.array([0, 1, 2], dtype=np.int)
                    weights = np.array([0.1, 0.5, 0.4], dtype=np.float)
                    z = np.array([1+2j, 3+4j], dtype=np.complex)
                    return weights[mask]

                def get_zeros(n: int):
                    return np.zeros(n, dtype=np.float)
            """,
            "ml/model.py": """\
                import numpy as np

                class LinearModel:
                    def __init__(self):
                        self.weights: np.ndarray = np.array([], dtype=np.float)

                    def predict(self, X: np.ndarray) -> np.ndarray:
                        result = X @ self.weights
                        idx = result.astype(np.int)
                        return idx
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "numpy",
            "from_version": "1.26.4",
            "to_version": "2.2.5",
            "breaking_changes": [
                {"type": "removed", "old_api": "numpy.bool", "new_api": "bool",
                 "description": "np.bool type alias was deprecated since 1.20 and removed in 1.24/2.0. Use Python's built-in bool instead."},
                {"type": "removed", "old_api": "numpy.int", "new_api": "numpy.intp",
                 "description": "np.int type alias removed. Use np.intp or Python's built-in int."},
                {"type": "removed", "old_api": "numpy.float", "new_api": "numpy.float64",
                 "description": "np.float type alias removed. Use np.float64 for the 64-bit floating point type."},
                {"type": "removed", "old_api": "numpy.complex", "new_api": "numpy.complex128",
                 "description": "np.complex type alias removed. Use np.complex128."},
            ],
            "api_evidence": [
                {
                    "api": "numpy.bool",
                    "change_type": "removed",
                    "replacement": "bool",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "release_notes",
                                  "url": "https://numpy.org/doc/stable/release/2.0.0-notes.html",
                                  "quote": "np.bool, np.int, np.float, np.complex aliases removed"}],
                    "reason": "NumPy 2.0 release notes document removal of these type aliases.",
                }
            ],
            "confidence_score": 0.90,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "ml/features.py": [
                ("dtype=np.bool)", False),
                ("dtype=np.int)", False),
                ("dtype=np.float)", False),
            ],
            "ml/model.py": [
                ("dtype=np.float)", False),
                (".astype(np.int)", False),
            ],
        },
    ),

    # ── 4. marshmallow: dump/load return changed ──────────────────────────────
    PipelineTestCase(
        id="marshmallow-dump-return",
        description="marshmallow 2.x → 3.x: Schema.dump()/load() return dict, not (data, errors) tuple",
        project_files={
            "requirements.txt": "marshmallow==2.21.0\n",
            "api/serializers.py": """\
                from marshmallow import Schema, fields

                class UserSchema(Schema):
                    id = fields.Int()
                    name = fields.Str(required=True)
                    email = fields.Email()

                user_schema = UserSchema()

                def serialize_user(user_dict):
                    result, errors = user_schema.dump(user_dict)
                    if errors:
                        raise ValueError(f'Serialization errors: {errors}')
                    return result

                def deserialize_user(data):
                    user, errors = user_schema.load(data)
                    if errors:
                        raise ValueError(f'Validation errors: {errors}')
                    return user
            """,
            "api/views.py": """\
                from marshmallow import Schema, fields

                class ResponseSchema(Schema):
                    id = fields.Int()
                    name = fields.Str()

                resp_schema = ResponseSchema()

                def get_user_response(user):
                    data, errors = resp_schema.dump(user)
                    if errors:
                        return {'error': str(errors)}, 400
                    return data, 200
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "marshmallow",
            "from_version": "2.21.0",
            "to_version": "3.21.3",
            "breaking_changes": [
                {
                    "type": "changed_signature",
                    "old_api": "marshmallow.Schema.dump",
                    "new_api": "marshmallow.Schema.dump",
                    "description": "Schema.dump() now returns only the serialized data dict (not a (data, errors) tuple). "
                                   "Validation errors raise ValidationError exceptions instead.",
                },
                {
                    "type": "changed_signature",
                    "old_api": "marshmallow.Schema.load",
                    "new_api": "marshmallow.Schema.load",
                    "description": "Schema.load() now returns only the deserialized data (not a (data, errors) tuple). "
                                   "Raises ValidationError on invalid input.",
                },
            ],
            "api_evidence": [
                {
                    "api": "marshmallow.Schema.dump",
                    "change_type": "changed_signature",
                    "replacement": "result = schema.dump(obj)  # raises ValidationError on error",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://marshmallow.readthedocs.io/en/stable/upgrading.html",
                                  "quote": "dump() and load() no longer return a (data, errors) tuple."}],
                    "reason": "marshmallow 3.x migration guide explicitly documents this return type change.",
                }
            ],
            "confidence_score": 0.90,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "api/serializers.py": [
                ("result, errors = user_schema.dump", False),
                ("result, errors = user_schema.load", False),
            ],
            "api/views.py": [
                ("data, errors = resp_schema.dump", False),
            ],
        },
    ),

    # ── 5. Flask: @before_first_request removed ───────────────────────────────
    PipelineTestCase(
        id="flask-before-first-request",
        description="Flask 1.x → 3.x: @app.before_first_request removed",
        project_files={
            "requirements.txt": "Flask==1.1.4\n",
            "app/main.py": """\
                from flask import Flask, g, current_app

                app = Flask(__name__)

                @app.before_first_request
                def initialize_db():
                    g.db_ready = True
                    print('Database initialized on first request')

                @app.before_first_request
                def load_config():
                    app.config['LOADED'] = True

                @app.before_request
                def check_auth():
                    pass

                @app.route('/health')
                def health():
                    return {'status': 'ok'}

                if __name__ == '__main__':
                    app.run(debug=True)
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "Flask",
            "from_version": "1.1.4",
            "to_version": "3.1.1",
            "breaking_changes": [
                {
                    "type": "removed",
                    "old_api": "flask.Flask.before_first_request",
                    "new_api": "",
                    "description": "@app.before_first_request decorator was removed in Flask 2.3. "
                                   "Use app.with_appcontext() at startup or move initialization to a "
                                   "CLI command / factory function instead.",
                }
            ],
            "api_evidence": [
                {
                    "api": "flask.before_first_request",
                    "change_type": "removed",
                    "replacement": "",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "release_notes",
                                  "url": "https://flask.palletsprojects.com/en/3.0.x/changes/",
                                  "quote": "Remove before_first_request and the associated error."}],
                    "reason": "Flask 2.3 changelog documents removal of before_first_request.",
                }
            ],
            "confidence_score": 0.85,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "app/main.py": [
                ("@app.before_first_request", False),
            ],
        },
    ),

    # ── 6. Celery: CELERY_* config keys renamed ───────────────────────────────
    PipelineTestCase(
        id="celery-config-rename",
        description="Celery 4.x → 5.x: CELERY_* uppercase config keys renamed to lowercase",
        project_files={
            "requirements.txt": "celery==4.4.7\n",
            "tasks/celery_app.py": """\
                from celery import Celery

                app = Celery('myapp')

                app.conf.update(
                    CELERY_BROKER_URL='redis://localhost:6379/0',
                    CELERY_RESULT_BACKEND='redis://localhost:6379/1',
                    CELERY_TASK_SERIALIZER='json',
                    CELERY_RESULT_SERIALIZER='json',
                    CELERY_ACCEPT_CONTENT=['json'],
                    CELERY_TIMEZONE='UTC',
                    CELERY_ENABLE_UTC=True,
                    CELERYD_MAX_TASKS_PER_CHILD=100,
                )

                @app.task(bind=True)
                def add(self, x, y):
                    return x + y
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "celery",
            "from_version": "4.4.7",
            "to_version": "5.4.0",
            "breaking_changes": [
                {"type": "renamed", "old_api": "CELERY_BROKER_URL", "new_api": "broker_url",
                 "description": "CELERY_BROKER_URL → broker_url"},
                {"type": "renamed", "old_api": "CELERY_RESULT_BACKEND", "new_api": "result_backend",
                 "description": "CELERY_RESULT_BACKEND → result_backend"},
                {"type": "renamed", "old_api": "CELERY_TASK_SERIALIZER", "new_api": "task_serializer",
                 "description": "CELERY_TASK_SERIALIZER → task_serializer"},
                {"type": "renamed", "old_api": "CELERY_RESULT_SERIALIZER", "new_api": "result_serializer",
                 "description": "CELERY_RESULT_SERIALIZER → result_serializer"},
                {"type": "renamed", "old_api": "CELERY_ACCEPT_CONTENT", "new_api": "accept_content",
                 "description": "CELERY_ACCEPT_CONTENT → accept_content"},
                {"type": "renamed", "old_api": "CELERY_TIMEZONE", "new_api": "timezone",
                 "description": "CELERY_TIMEZONE → timezone"},
                {"type": "renamed", "old_api": "CELERY_ENABLE_UTC", "new_api": "enable_utc",
                 "description": "CELERY_ENABLE_UTC → enable_utc"},
                {"type": "renamed", "old_api": "CELERYD_MAX_TASKS_PER_CHILD", "new_api": "worker_max_tasks_per_child",
                 "description": "CELERYD_MAX_TASKS_PER_CHILD → worker_max_tasks_per_child"},
            ],
            "api_evidence": [
                {
                    "api": "CELERY_*",
                    "change_type": "renamed",
                    "replacement": "lowercase_key",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://docs.celeryq.dev/en/stable/userguide/configuration.html",
                                  "quote": "All CELERY_ prefixed uppercase keys removed. Use lowercase keys."}],
                    "reason": "Celery 4.0 migration guide documents removal of uppercase CELERY_* keys.",
                }
            ],
            "confidence_score": 0.90,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "tasks/celery_app.py": [
                ("CELERY_BROKER_URL", False),
                ("CELERY_RESULT_BACKEND", False),
                ("CELERY_TASK_SERIALIZER", False),
                ("broker_url", True),
                ("result_backend", True),
                ("task_serializer", True),
            ],
        },
    ),

    # ── 7. redis: StrictRedis removed ────────────────────────────────────────
    PipelineTestCase(
        id="redis-strict-redis",
        description="redis 3.x → 5.x: StrictRedis removed (merged into Redis class)",
        project_files={
            "requirements.txt": "redis==3.5.3\n",
            "cache/client.py": """\
                import redis

                client = redis.StrictRedis(
                    host='localhost',
                    port=6379,
                    db=0,
                    decode_responses=True
                )

                def get_value(key: str) -> str:
                    return client.get(key)

                def set_value(key: str, value: str, ttl: int = 3600) -> None:
                    client.setex(key, ttl, value)

                def batch_set(items: dict) -> None:
                    pipe = client.pipeline()
                    for key, value in items.items():
                        pipe.set(key, value)
                    pipe.execute()
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "redis",
            "from_version": "3.5.3",
            "to_version": "5.2.1",
            "breaking_changes": [
                {
                    "type": "removed",
                    "old_api": "redis.StrictRedis",
                    "new_api": "redis.Redis",
                    "description": "redis.StrictRedis has been removed. It was an alias for redis.Redis since v3.0. "
                                   "Use redis.Redis directly.",
                }
            ],
            "api_evidence": [
                {
                    "api": "redis.StrictRedis",
                    "change_type": "removed",
                    "replacement": "redis.Redis",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "release_notes",
                                  "url": "https://github.com/redis/redis-py/releases",
                                  "quote": "StrictRedis is removed. Use Redis class directly."}],
                    "reason": "redis-py changelog documents StrictRedis removal in v5.",
                }
            ],
            "confidence_score": 0.85,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "cache/client.py": [
                ("redis.StrictRedis(", False),
                ("redis.Redis(", True),
            ],
        },
    ),

    # ── 8. SQLAlchemy: engine.execute() removed ───────────────────────────────
    PipelineTestCase(
        id="sqlalchemy-engine-execute",
        description="SQLAlchemy 1.4 → 2.0: Engine.execute() removed; use engine.connect() + text()",
        project_files={
            "requirements.txt": "SQLAlchemy==1.4.46\n",
            "db/queries.py": """\
                from sqlalchemy import create_engine
                from sqlalchemy.engine import Engine

                engine: Engine = create_engine("sqlite:///mydb.db")

                def get_user(user_id: int) -> dict:
                    result = engine.execute(
                        "SELECT id, name, email FROM users WHERE id = :uid",
                        {"uid": user_id}
                    )
                    row = result.fetchone()
                    return dict(row) if row else {}

                def list_active_users() -> list:
                    result = engine.execute(
                        "SELECT id, name FROM users WHERE active = 1"
                    )
                    return [{"id": r[0], "name": r[1]} for r in result]

                def deactivate_user(user_id: int) -> None:
                    engine.execute(
                        "UPDATE users SET active = 0 WHERE id = :uid",
                        {"uid": user_id}
                    )
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "SQLAlchemy",
            "from_version": "1.4.46",
            "to_version": "2.0.36",
            "breaking_changes": [
                {
                    "type": "removed",
                    "old_api": "sqlalchemy.engine.Engine.execute",
                    "new_api": "sqlalchemy.engine.Connection.execute",
                    "description": "Engine.execute() is removed in SQLAlchemy 2.0. Use engine.connect() as a "
                                   "context manager and call conn.execute(text(sql)) instead. "
                                   "All SQL strings must be wrapped in text().",
                }
            ],
            "api_evidence": [
                {
                    "api": "sqlalchemy.engine.Engine.execute",
                    "change_type": "removed",
                    "replacement": (
                        "with engine.connect() as conn:\n"
                        "    result = conn.execute(text(\"SELECT ...\"))"
                    ),
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://docs.sqlalchemy.org/en/20/changelog/migration_20.html",
                                  "quote": "Engine.execute() and legacy execute() removed. "
                                           "Use text() for string SQL and engine.connect() context manager."}],
                    "reason": "SQLAlchemy 2.0 migration guide documents removal of legacy execute methods.",
                }
            ],
            "confidence_score": 0.92,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "db/queries.py": [
                ("engine.execute(", False),
                ("conn.execute(", True),
            ],
        },
    ),

    # ── 9. attrs: @attr.s → @attr.define ─────────────────────────────────────
    PipelineTestCase(
        id="attrs-define",
        description="attrs 21.x → 23.x: @attr.s deprecated, use @attr.define; attr.ib() → attrs.field()",
        project_files={
            "requirements.txt": "attrs==21.4.0\n",
            "config/settings.py": """\
                import attr

                @attr.s
                class ServerConfig:
                    host = attr.ib(default="localhost")
                    port = attr.ib(default=8080)
                    debug = attr.ib(default=False)

                @attr.s(auto_attribs=True)
                class DatabaseConfig:
                    url: str = attr.ib(default="sqlite:///db.sqlite3")
                    pool_size: int = attr.ib(default=5)
                    timeout: float = attr.ib(default=30.0)
            """,
            "models/point.py": """\
                import attr

                @attr.s(slots=True)
                class Point:
                    x = attr.ib()
                    y = attr.ib()

                    def distance(self):
                        return (self.x ** 2 + self.y ** 2) ** 0.5
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "attrs",
            "from_version": "21.4.0",
            "to_version": "23.2.0",
            "breaking_changes": [
                {
                    "type": "renamed",
                    "old_api": "attr.s",
                    "new_api": "attr.define",
                    "description": "@attr.s is deprecated. Use @attr.define instead. "
                                   "The new decorator is recommended for all new code and has improved "
                                   "defaults (slots=True by default, on_setattr hooks enabled).",
                },
                {
                    "type": "renamed",
                    "old_api": "attr.ib",
                    "new_api": "attr.field",
                    "description": "attr.ib() is deprecated. Use attr.field() instead for field declarations "
                                   "in @attr.define classes.",
                },
            ],
            "api_evidence": [
                {
                    "api": "attr.s",
                    "change_type": "renamed",
                    "replacement": "@attr.define",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://www.attrs.org/en/stable/api.html",
                                  "quote": "@attr.s is the classic API. The modern/recommended API uses "
                                           "@attr.define which has better defaults."}],
                    "reason": "attrs documentation recommends @attr.define as the modern replacement.",
                },
                {
                    "api": "attr.ib",
                    "change_type": "renamed",
                    "replacement": "attr.field(default=...)",
                    "confidence": "high",
                    "evidence": [{"source_index": 2, "source": "migration_guide",
                                  "url": "https://www.attrs.org/en/stable/api.html",
                                  "quote": "attr.ib() is an alias for attr.attrib() which is the classic API. "
                                           "Use attr.field() in @attr.define classes."}],
                    "reason": "attrs documentation documents attr.field() as the modern replacement for attr.ib().",
                },
            ],
            "confidence_score": 0.88,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "config/settings.py": [
                ("@attr.s", False),
                ("@attr.define", True),
            ],
            "models/point.py": [
                ("@attr.s(", False),
            ],
        },
    ),

    # ── 10. Pillow: Image.ANTIALIAS removed ───────────────────────────────────
    PipelineTestCase(
        id="pillow-antialias",
        description="Pillow 9.x → 10.x: Image.ANTIALIAS removed, use Image.Resampling.LANCZOS",
        project_files={
            "requirements.txt": "Pillow==9.0.1\n",
            "utils/image_utils.py": """\
                from PIL import Image

                def resize_image(path: str, width: int, height: int) -> Image.Image:
                    img = Image.open(path)
                    return img.resize((width, height), Image.ANTIALIAS)

                def make_thumbnail(path: str, max_size: int = 128) -> Image.Image:
                    img = Image.open(path)
                    img.thumbnail((max_size, max_size), Image.ANTIALIAS)
                    return img

                def scale_to_width(path: str, target_width: int) -> Image.Image:
                    img = Image.open(path)
                    ratio = target_width / img.width
                    new_height = int(img.height * ratio)
                    return img.resize((target_width, new_height), Image.ANTIALIAS)
            """,
            "utils/avatar.py": """\
                from PIL import Image

                AVATAR_SIZE = (256, 256)

                def process_avatar(input_path: str, output_path: str) -> None:
                    img = Image.open(input_path).convert('RGB')
                    img = img.resize(AVATAR_SIZE, Image.ANTIALIAS)
                    img.save(output_path, quality=95)
            """,
        },
        dep_file="requirements.txt",
        scout_output={
            "package": "Pillow",
            "from_version": "9.0.1",
            "to_version": "10.4.0",
            "breaking_changes": [
                {
                    "type": "removed",
                    "old_api": "PIL.Image.ANTIALIAS",
                    "new_api": "PIL.Image.Resampling.LANCZOS",
                    "description": "Image.ANTIALIAS was deprecated in Pillow 9.1.0 and removed in 10.0.0. "
                                   "Use Image.Resampling.LANCZOS instead.",
                }
            ],
            "api_evidence": [
                {
                    "api": "PIL.Image.ANTIALIAS",
                    "change_type": "removed",
                    "replacement": "Image.Resampling.LANCZOS",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "release_notes",
                                  "url": "https://pillow.readthedocs.io/en/stable/releasenotes/10.0.0.html",
                                  "quote": "Image.ANTIALIAS is removed. Use Image.Resampling.LANCZOS instead."}],
                    "reason": "Pillow 10.0.0 release notes document removal of ANTIALIAS constant.",
                }
            ],
            "confidence_score": 0.95,
            "evidence_confidence": "high",
            "api_semantics": [],
            "evidence_references": [],
            "references": [],
            "changelog_url": "",
        },
        assertions={
            "utils/image_utils.py": [
                ("Image.ANTIALIAS", False),
                ("Image.Resampling.LANCZOS", True),
            ],
            "utils/avatar.py": [
                ("Image.ANTIALIAS", False),
                ("Image.Resampling.LANCZOS", True),
            ],
        },
    ),
]


# ── evaluator ─────────────────────────────────────────────────────────────────

def evaluate_pipeline(
    case: PipelineTestCase,
    project_dir: str,
    patch_report: dict,
    ast_output: dict,
    llm_calls: list[LLMCallLog],
    elapsed: float,
) -> PipelineResult:
    failures: list[str] = []
    assertion_results: dict = {}

    overall_status = patch_report.get("overall_status", "unknown")
    if overall_status not in ("success",):
        failures.append(f"PatchAgent status={overall_status} (expected success)")

    for rel_path, checks in case.assertions.items():
        full_path = Path(project_dir) / rel_path
        if not full_path.exists():
            failures.append(f"File not found after patching: {rel_path}")
            continue
        content = full_path.read_text()
        file_results = []
        for text, should_contain in checks:
            actual_contains = text in content
            ok = actual_contains == should_contain
            if not ok:
                direction = "contain" if should_contain else "NOT contain"
                failures.append(f"{rel_path}: expected to {direction} '{text}'")
            file_results.append((text, should_contain, actual_contains))
        assertion_results[rel_path] = file_results

    passed = len(failures) == 0
    return PipelineResult(
        case=case,
        passed=passed,
        elapsed_s=elapsed,
        patch_report=patch_report,
        assertion_results=assertion_results,
        failure_reasons=failures,
        project_dir=project_dir,
        ast_output=ast_output,
        llm_calls=llm_calls,
    )


# ── runner ────────────────────────────────────────────────────────────────────

async def run_case(case: PipelineTestCase) -> PipelineResult:
    project_dir = make_project(case.project_files)
    start = time.monotonic()
    patch_report: dict = {}
    ast_output: dict = {}
    llm_calls: list[LLMCallLog] = []

    try:
        scout_output = case.scout_output
        breaking_changes = scout_output.get("breaking_changes", [])
        print(f"    [Scout]  {len(breaking_changes)} breaking change(s): "
              f"{[bc['old_api'] for bc in breaking_changes[:4]]}", flush=True)

        if not breaking_changes:
            patch_report = {"overall_status": "no_breaking_changes", "files_patched": []}
            return evaluate_pipeline(case, project_dir, patch_report, ast_output, llm_calls,
                                     time.monotonic() - start)

        # ── AST scan ─────────────────────────────────────────────────────────
        scanner = ASTScanner()
        ast_output = scanner.scan(project_dir, breaking_changes)
        total_matches = ast_output.get("total_matches", 0)
        files_affected = ast_output.get("total_files_affected", 0)
        print(f"    [AST]    {files_affected} file(s) affected, {total_matches} match(es)", flush=True)

        if total_matches == 0:
            files_scanned = ast_output.get("total_files_scanned", 0)
            print(f"    [AST]    WARNING: 0 matches found; {files_scanned} files scanned", flush=True)

        # ── Patch agent with LLM call capture ────────────────────────────────
        dep_path = str(Path(project_dir) / case.dep_file)
        agent = PatchAgent(project_root=project_dir)

        _original_complete = agent.router.complete  # bound method

        async def _capturing_complete(system_prompt, user_prompt, *args, **kwargs):
            result = await _original_complete(system_prompt, user_prompt, *args, **kwargs)
            llm_calls.append(LLMCallLog(
                call_number=len(llm_calls) + 1,
                system_prompt=str(system_prompt),
                user_prompt=str(user_prompt),
                response=str(result.content),
                provider=str(result.provider),
            ))
            return result

        agent.router.complete = _capturing_complete

        patch_report = await agent.run(scout_output, ast_output, dep_path)
        print(f"    [Patch]  status={patch_report.get('overall_status')} "
              f"provider={patch_report.get('llm_provider')} "
              f"llm_calls={len(llm_calls)}", flush=True)

    except Exception as exc:
        import traceback
        patch_report = {"overall_status": "exception", "files_patched": [], "error": str(exc)}
        elapsed = time.monotonic() - start
        result = evaluate_pipeline(case, project_dir, patch_report, ast_output, llm_calls, elapsed)
        result.failure_reasons.insert(0, f"Exception: {exc}")
        result.passed = False
        print(f"    [ERROR]  {exc}", flush=True)
        traceback.print_exc()
        return result

    elapsed = time.monotonic() - start
    return evaluate_pipeline(case, project_dir, patch_report, ast_output, llm_calls, elapsed)


def print_result(result: PipelineResult) -> None:
    status = color("PASS", GREEN) if result.passed else color("FAIL", RED)
    print(f"\n{'─'*70}")
    print(f"  [{status}] {result.case.id}  ({result.elapsed_s:.1f}s)")
    print(f"  {result.case.description}")
    print(f"  patch_status={result.patch_report.get('overall_status', '?')}  "
          f"llm_provider={result.patch_report.get('llm_provider', '?')}")

    for rel_path, checks in result.assertion_results.items():
        for text, expected, actual in checks:
            ok = actual == expected
            icon = color("✓", GREEN) if ok else color("✗", RED)
            direction = "present" if expected else "absent"
            print(f"  {icon} {rel_path}: '{text}' → {direction} ({'OK' if ok else 'WRONG'})")

    # Show first 30 lines of patched files
    for rel_path in result.case.assertions:
        full_path = Path(result.project_dir) / rel_path
        if full_path.exists():
            lines = full_path.read_text().splitlines()
            preview = "\n".join(f"      {l}" for l in lines[:30])
            print(f"\n  Patched {rel_path}:\n{preview}")
            if len(lines) > 30:
                print(f"      ... ({len(lines)} lines total)")

    if result.failure_reasons:
        print(f"\n  {color('Failures:', RED)}")
        for r in result.failure_reasons:
            print(f"    ✗ {r}")


# ── markdown report ───────────────────────────────────────────────────────────

def _md_fence(content: str, lang: str = "", max_chars: int = 8000) -> str:
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... [truncated after {max_chars} chars]"
    return f"```{lang}\n{content}\n```"


def _get_patched_content(result: PipelineResult) -> dict[str, str]:
    """Read the final patched file contents."""
    patched = {}
    for rel_path in result.case.assertions:
        full_path = Path(result.project_dir) / rel_path
        if full_path.exists():
            patched[rel_path] = full_path.read_text()
    return patched


def classify_failure(result: PipelineResult) -> tuple[str, str]:
    """
    Returns (tag, explanation) where tag is 'NATURAL' or 'FLAW'.

    NATURAL: Scout extraction was the limiting factor — e.g., AST found 0 matches,
             or scout output lacked enough detail; LLM couldn't patch.
    FLAW:    Scout output is complete, but context was not forwarded into
             the Patch prompt, so LLM lacked necessary information.
    """
    # No matches found by AST → LLM had nothing to patch
    ast_total = result.ast_output.get("total_matches", 0)
    if ast_total == 0:
        return (
            "NATURAL",
            "The ASTScanner found **0 matches** for the breaking change API patterns in the project files. "
            "This means the Patch Agent never received any target code blocks to edit. "
            "Root cause: the old_api identifiers in the scout output did not match the code patterns "
            "in the project (likely a mismatch between the fully-qualified scout API name and the actual "
            "variable/call form used in the source code). This is a scout extraction / API naming issue, "
            "not a context-passing bug in the pipeline."
        )

    # patch_report says no_change when there were matches → LLM decided not to patch
    patch_status = result.patch_report.get("overall_status", "unknown")
    if patch_status == "no_change":
        return (
            "NATURAL",
            "The ASTScanner found matches, but the Patch Agent's LLM returned `no_change` — it decided "
            "no edit was needed. This typically happens when the LLM finds the evidence ambiguous or "
            "the existing code already compatible. This is a scout evidence quality / LLM reasoning issue."
        )

    # Check if any LLM call prompt is missing critical scout fields
    if result.llm_calls:
        first_prompt = result.llm_calls[0].user_prompt
        scout_bc = result.case.scout_output.get("breaking_changes", [])
        scout_ev = result.case.scout_output.get("api_evidence", [])

        # Check if evidence is present in the patch prompt
        evidence_in_prompt = any(
            ev.get("replacement", "") and ev["replacement"][:30] in first_prompt
            for ev in scout_ev
        )
        bc_in_prompt = any(
            bc.get("old_api", "") in first_prompt
            for bc in scout_bc
        )

        if not bc_in_prompt:
            return (
                "FLAW",
                "The scout output contains breaking change `old_api` values, but they do not appear in "
                "the Patch Agent's first LLM prompt. This is a **context-passing issue**: the scout's "
                "extracted information is not being forwarded to the LLM."
            )

        if scout_ev and not evidence_in_prompt:
            return (
                "FLAW",
                "The scout output contains `api_evidence` with replacement examples, but the evidence "
                "replacement text does not appear in the Patch Agent's first LLM prompt. "
                "The LLM may be missing the documented migration pattern, causing incorrect output."
            )

    # Default: likely LLM reasoning / evidence quality issue
    return (
        "NATURAL",
        "The scout output and AST matches appear complete and are present in the Patch Agent's prompt. "
        "The patch is incorrect due to LLM reasoning limitations or ambiguity in the evidence — "
        "this is a natural failure related to scout evidence quality or LLM capability, not a system flaw."
    )


def build_markdown_report(results: list[PipelineResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed_s for r in results)

    lines = []
    lines.append("# DepGuard AI — 10-Package Update Pipeline Analysis Report\n")
    lines.append(f"**Date:** 2026-05-19  ")
    lines.append(f"**Pipeline:** pre-computed scout output → ASTScanner → PatchAgent  ")
    lines.append(f"**Total cases:** {len(results)}  |  **Passed:** {passed}  |  "
                 f"**Failed:** {failed}  |  **Total time:** {total_time:.0f}s\n")

    # ── Summary table ─────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append("| # | Case ID | Description | Result | AST Matches | LLM Calls | Time |")
    lines.append("|---|---------|-------------|--------|-------------|-----------|------|")
    for i, r in enumerate(results, 1):
        status = "✅ PASS" if r.passed else "❌ FAIL"
        ast_m = r.ast_output.get("total_matches", 0)
        lines.append(
            f"| {i} | `{r.case.id}` | {r.case.description[:55]}… | {status} | "
            f"{ast_m} | {len(r.llm_calls)} | {r.elapsed_s:.1f}s |"
        )
    lines.append("")

    if failed == 0:
        lines.append("**All 10 cases passed.** No issues found.\n")
        return "\n".join(lines)

    # ── Failure details ───────────────────────────────────────────────────────
    lines.append("---\n")
    lines.append("## Failure Details\n")
    lines.append(
        "> For each failure the log shows: (1) Scout output — what was extracted, "
        "(2) AST scan — what was found, (3) each Patch LLM call — system prompt, user prompt, "
        "LLM response, (4) final patched file content, (5) root-cause classification.\n"
    )

    failure_index = 0
    for result in results:
        if result.passed:
            continue
        failure_index += 1
        case = result.case

        lines.append(f"---\n")
        lines.append(f"### Failure {failure_index}: `{case.id}`\n")
        lines.append(f"**Description:** {case.description}\n")
        lines.append(f"**Assertion failures:**")
        for reason in result.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

        # ── Stage 1: Scout output ─────────────────────────────────────────────
        lines.append("#### Stage 1 — Scout Output (pre-computed)\n")
        lines.append(
            "> This is the pre-computed scout output that replaces a real Scout run. "
            "It represents what the Scout agent would have sent to the Patch pipeline after "
            "fetching changelogs and calling the LLM.\n"
        )
        lines.append("**Breaking changes extracted by Scout:**")
        lines.append(_md_fence(
            json.dumps(case.scout_output.get("breaking_changes", []), indent=2),
            "json"
        ))
        lines.append("")
        lines.append("**API evidence extracted by Scout:**")
        lines.append(_md_fence(
            json.dumps(case.scout_output.get("api_evidence", []), indent=2),
            "json"
        ))
        lines.append("")

        # ── Stage 2: AST scan ─────────────────────────────────────────────────
        lines.append("#### Stage 2 — ASTScanner Output\n")
        ast_total = result.ast_output.get("total_matches", 0)
        ast_files = result.ast_output.get("total_files_affected", 0)
        ast_scanned = result.ast_output.get("total_files_scanned", 0)
        lines.append(
            f"Files scanned: **{ast_scanned}** | Files affected: **{ast_files}** | "
            f"Total matches: **{ast_total}**\n"
        )
        matches_by_file = result.ast_output.get("matches_by_file", {})
        if matches_by_file:
            compact = {
                fp: [{"line": m.get("line"), "old_api": m.get("old_api"),
                      "type": m.get("type"), "matched_text": m.get("matched_text")}
                     for m in ms]
                for fp, ms in matches_by_file.items()
            }
            lines.append(_md_fence(json.dumps(compact, indent=2), "json"))
        else:
            lines.append("**No matches found by ASTScanner.**")
        lines.append("")

        # ── Stage 3: Patch LLM calls ──────────────────────────────────────────
        lines.append("#### Stage 3 — Patch Agent LLM Calls\n")
        if not result.llm_calls:
            lines.append("*No LLM calls were made (AST found 0 matches or Patch Agent skipped).*\n")
        else:
            for call in result.llm_calls:
                lines.append(f"##### LLM Call #{call.call_number} — Provider: `{call.provider}`\n")

                lines.append("**System Prompt (first 3000 chars):**")
                lines.append(_md_fence(call.system_prompt[:3000], max_chars=3000))
                lines.append("")

                lines.append("**User Prompt (first 6000 chars):**")
                lines.append(_md_fence(call.user_prompt[:6000], max_chars=6000))
                lines.append("")

                lines.append("**LLM Response (first 4000 chars):**")
                lines.append(_md_fence(call.response[:4000], max_chars=4000))
                lines.append("")

        # ── Stage 4: Final patched file content ───────────────────────────────
        lines.append("#### Stage 4 — Final Patched File Content\n")
        patched = _get_patched_content(result)
        for rel_path in case.assertions:
            lines.append(f"**`{rel_path}`:**")
            content = patched.get(rel_path, "(file not found)")
            lines.append(_md_fence(content, "python", max_chars=4000))
            lines.append("")

        # ── Stage 5: Root cause ───────────────────────────────────────────────
        lines.append("#### Stage 5 — Root Cause Analysis\n")
        tag, explanation = classify_failure(result)
        tag_label = "🔵 NATURAL FAILURE (Scout/Evidence Issue)" if tag == "NATURAL" \
            else "🔴 SYSTEM FLAW (Context-Passing Issue)"
        lines.append(f"**Classification: {tag_label}**\n")
        lines.append(explanation)
        lines.append("")

        if tag == "FLAW":
            lines.append(
                "> **⚠ Action required:** The scout correctly extracted migration information, "
                "but this information was not passed into the Patch Agent's prompt. "
                "The pipeline needs modification to ensure the full scout context reaches the LLM.\n"
            )
        else:
            lines.append(
                "> **ℹ No system fix needed:** This is a natural limitation of the current "
                "scout evidence quality or LLM capability. The pipeline is working as designed.\n"
            )

    # ── Global conclusions ────────────────────────────────────────────────────
    lines.append("---\n")
    lines.append("## Conclusions\n")

    natural_failures = []
    system_flaws = []
    for r in results:
        if r.passed:
            continue
        tag, explanation = classify_failure(r)
        if tag == "NATURAL":
            natural_failures.append((r.case.id, explanation))
        else:
            system_flaws.append((r.case.id, explanation))

    if system_flaws:
        lines.append("### System Flaws Found (Context-Passing Issues)\n")
        for case_id, explanation in system_flaws:
            lines.append(f"#### `{case_id}`")
            lines.append(explanation)
            lines.append("")
    else:
        lines.append("### System Flaws\n")
        lines.append("No context-passing flaws were detected across the 10 cases.\n")

    if natural_failures:
        lines.append("### Natural Failures (Scout/Evidence Quality Issues)\n")
        for case_id, explanation in natural_failures:
            lines.append(f"#### `{case_id}`")
            lines.append(explanation)
            lines.append("")
    else:
        lines.append("### Natural Failures\n")
        lines.append("No natural failures detected.\n")

    lines.append("---\n")
    lines.append("*Report generated by `tests/integration_10_packages.py`*\n")

    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(color(f"\n{'═'*70}", BOLD))
    print(color("  DepGuard — 10-Package Update Pipeline Integration Test", BOLD))
    print(color(f"  {len(PIPELINE_CASES)} cases | ASTScanner → PatchAgent pipeline", CYAN))
    print(color(f"{'═'*70}", BOLD))

    filter_ids = set(sys.argv[1:])
    cases = [c for c in PIPELINE_CASES if not filter_ids or c.id in filter_ids]

    results: list[PipelineResult] = []

    for i, case in enumerate(cases, 1):
        print(f"\n{color(f'[{i}/{len(cases)}]', CYAN)} {case.id}", flush=True)
        print(f"  {case.description}", flush=True)

        result = await run_case(case)
        results.append(result)
        print_result(result)

        # Save individual patch report
        out_path = Path(f"/tmp/pipeline_10_{case.id}.json")
        out_path.write_text(json.dumps(result.patch_report, indent=2, ensure_ascii=False))
        print(f"\n  Patch report → {out_path}")
        print(f"  Project dir  → {result.project_dir}")

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed_s for r in results)

    print(f"\n{'═'*70}")
    print(color(f"  SUMMARY: {passed}/{len(results)} passed  ({failed} failed)  "
                f"total={total_time:.0f}s", BOLD))
    print(f"{'═'*70}")
    for r in results:
        icon = color("✓", GREEN) if r.passed else color("✗", RED)
        ast_m = r.ast_output.get("total_matches", 0)
        print(f"  {icon}  {r.case.id:<35}  "
              f"status={r.patch_report.get('overall_status','?'):<12}  "
              f"ast={ast_m:<4}  {r.elapsed_s:.1f}s")
    print(f"{'═'*70}\n")

    # ── Write reports ──────────────────────────────────────────────────────────
    report_md = build_markdown_report(results)
    md_path = Path("/tmp/pipeline_10_report.md")
    md_path.write_text(report_md, encoding="utf-8")
    print(f"Markdown report → {md_path}")

    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "total_time_s": round(total_time, 1),
        "results": [
            {
                "id": r.case.id,
                "passed": r.passed,
                "patch_status": r.patch_report.get("overall_status"),
                "llm_provider": r.patch_report.get("llm_provider"),
                "ast_matches": r.ast_output.get("total_matches", 0),
                "llm_calls": len(r.llm_calls),
                "elapsed_s": round(r.elapsed_s, 1),
                "failures": r.failure_reasons,
            }
            for r in results
        ],
    }
    summary_path = Path("/tmp/pipeline_10_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary JSON    → {summary_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
