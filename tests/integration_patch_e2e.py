"""
End-to-end Patch Agent integration tests.

Pipeline under test:
    ASTScanner.scan()  →  PatchAgent.run()

Scout output is pre-computed from real Scout runs so we don't burn API budget
on the Scout step for every patch test. One FULL pipeline test (Scout→AST→Patch)
is included as case "pydantic-full-pipeline".

Run:
    cd /mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI
    python -m tests.integration_patch_e2e 2>&1 | tee /tmp/patch_e2e_results.txt

Filter specific cases:
    python -m tests.integration_patch_e2e pydantic-rename pyjwt-algorithms
"""

import asyncio
import json
import os
import re
import shutil
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


# ─────────────────── helpers ─────────────────────────────────────────────────

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
    """Create a temp git repo with the given files. Returns temp dir path."""
    tmpdir = tempfile.mkdtemp(prefix="depguard_patch_test_")
    for rel_path, content in files.items():
        full = Path(tmpdir) / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(textwrap.dedent(content))
    init_git_repo(tmpdir)
    return tmpdir


# ─────────────────── test case data ──────────────────────────────────────────

@dataclass
class PatchTestCase:
    id: str
    description: str
    # Project files: relative_path → content
    project_files: dict[str, str]
    # requirements.txt relative path
    dep_file: str
    # Pre-computed scout output (what ScoutAgent would return)
    scout_output: dict
    # Checks: function(patched_content: str) -> bool
    # Keys are file paths (relative to project root)
    assertions: dict[str, list[tuple[str, bool]]]
    # Extra: run full Scout pipeline instead of using pre-computed output
    full_pipeline: bool = False
    full_pipeline_package_info: Optional[dict] = None
    full_pipeline_api_usages: Optional[list] = None
    full_pipeline_api_contexts: Optional[list] = None


PATCH_CASES: list[PatchTestCase] = [

    # ── 1. pydantic @validator → @field_validator ─────────────────────────────
    PatchTestCase(
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
                    "description": "@validator decorator has been deprecated in Pydantic V2 and should be replaced with @field_validator. The new decorator requires @classmethod and uses mode= instead of always=.",
                }
            ],
            "api_evidence": [
                {
                    "api": "pydantic.validator",
                    "change_type": "renamed",
                    "replacement": "pydantic.field_validator",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://github.com/pydantic/pydantic/blob/main/docs/migration.md",
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
                ("field_validator", True),    # import updated
                ("from pydantic import", True), # import still there
                # @validator should be gone or at least @field_validator present
            ],
            "models/admin.py": [
                ("field_validator", True),
            ],
        },
    ),

    # ── 2. PyJWT: algorithms= now required in decode() ────────────────────────
    PatchTestCase(
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
                    "description": "jwt.decode() now requires algorithms= parameter (list of allowed algorithms). The verify= boolean parameter was removed. Pass algorithms=['HS256'] (or appropriate algorithm) as third positional or keyword argument.",
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
            "auth/tokens.py": [
                ("algorithms=", True),
            ],
            "utils/auth_helper.py": [
                ("algorithms=", True),
            ],
        },
    ),

    # ── 3. numpy type alias removal ───────────────────────────────────────────
    PatchTestCase(
        id="numpy-type-aliases",
        description="numpy 1.x → 2.x: np.bool/np.int/np.float type aliases removed",
        project_files={
            "requirements.txt": "numpy==1.26.4\n",
            "ml/features.py": """\
                import numpy as np

                def preprocess(data):
                    # np.bool alias removed in numpy 1.24, hard error in 2.0
                    mask = np.array([True, False, True], dtype=np.bool)
                    # np.int alias removed
                    indices = np.array([0, 1, 2], dtype=np.int)
                    # np.float alias removed
                    weights = np.array([0.1, 0.5, 0.4], dtype=np.float)
                    # np.complex alias removed
                    z = np.array([1+2j, 3+4j], dtype=np.complex)
                    return weights[mask]

                def get_zeros(n: int):
                    # np.float still used as dtype
                    return np.zeros(n, dtype=np.float)
            """,
            "ml/model.py": """\
                import numpy as np

                class LinearModel:
                    def __init__(self):
                        self.weights: np.ndarray = np.array([], dtype=np.float)

                    def predict(self, X: np.ndarray) -> np.ndarray:
                        result = X @ self.weights
                        # cast to int indices
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
                {
                    "type": "removed",
                    "old_api": "numpy.bool",
                    "new_api": "bool",
                    "description": "np.bool type alias was deprecated since 1.20 and removed in 1.24/2.0. Use Python's built-in bool instead.",
                },
                {
                    "type": "removed",
                    "old_api": "numpy.int",
                    "new_api": "numpy.intp",
                    "description": "np.int type alias removed. Use np.intp or Python's built-in int for general integer types.",
                },
                {
                    "type": "removed",
                    "old_api": "numpy.float",
                    "new_api": "numpy.float64",
                    "description": "np.float type alias removed. Use np.float64 for the 64-bit floating point type.",
                },
                {
                    "type": "removed",
                    "old_api": "numpy.complex",
                    "new_api": "numpy.complex128",
                    "description": "np.complex type alias removed. Use np.complex128 for the complex type.",
                },
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
                    "reason": "NumPy 2.0 release notes explicitly document removal of these type aliases.",
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
                # check the exact alias form (dtype=np.bool) - not np.bool64 etc.
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

    # ── 4. marshmallow v2 → v3: dump() returns dict not tuple ─────────────────
    PatchTestCase(
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

                class OrderSchema(Schema):
                    id = fields.Int()
                    user_id = fields.Int()
                    total = fields.Float()

                user_schema = UserSchema()
                order_schema = OrderSchema()

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

                def serialize_order(order):
                    result, errors = order_schema.dump(order)
                    return result
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
                    "description": "Schema.dump() now returns only the serialized data dict (not a (data, errors) tuple). Validation errors raise ValidationError exceptions instead. Same applies to Schema.load().",
                },
                {
                    "type": "changed_signature",
                    "old_api": "marshmallow.Schema.load",
                    "new_api": "marshmallow.Schema.load",
                    "description": "Schema.load() now returns only the deserialized data (not a (data, errors) tuple). Raises ValidationError on invalid input.",
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
                                  "quote": "dump() and load() no longer return a (data, errors) tuple; they return the data directly and raise ValidationError on failure."}],
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
                ("result, errors = order_schema.dump", False),
            ],
            "api/views.py": [
                ("data, errors = resp_schema.dump", False),
            ],
        },
    ),

    # ── 5. Flask: @before_first_request removed ────────────────────────────────
    PatchTestCase(
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
                    "description": "@app.before_first_request decorator was removed in Flask 2.3. Use app.with_appcontext() at startup or move initialization to a cli command / factory function instead.",
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
                                  "quote": "Remove before_first_request and the associated error. Use a with_appcontext callback or similar alternative."}],
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
                # decorator call form should be gone; comment mentions are acceptable
                ("@app.before_first_request", False),
            ],
        },
    ),

    # ── 6. Celery: CELERY_* config keys renamed ────────────────────────────────
    PatchTestCase(
        id="celery-config-rename",
        description="Celery 4.x → 5.x: CELERY_* uppercase config keys removed",
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
                    CELERY_TASK_TRACK_STARTED=True,
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
                {
                    "type": "renamed",
                    "old_api": "CELERY_BROKER_URL",
                    "new_api": "broker_url",
                    "description": "Celery 4.0+ uses lowercase configuration keys. CELERY_BROKER_URL → broker_url",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_RESULT_BACKEND",
                    "new_api": "result_backend",
                    "description": "CELERY_RESULT_BACKEND → result_backend",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_TASK_SERIALIZER",
                    "new_api": "task_serializer",
                    "description": "CELERY_TASK_SERIALIZER → task_serializer",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_RESULT_SERIALIZER",
                    "new_api": "result_serializer",
                    "description": "CELERY_RESULT_SERIALIZER → result_serializer",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_ACCEPT_CONTENT",
                    "new_api": "accept_content",
                    "description": "CELERY_ACCEPT_CONTENT → accept_content",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_TIMEZONE",
                    "new_api": "timezone",
                    "description": "CELERY_TIMEZONE → timezone",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERY_ENABLE_UTC",
                    "new_api": "enable_utc",
                    "description": "CELERY_ENABLE_UTC → enable_utc",
                },
                {
                    "type": "renamed",
                    "old_api": "CELERYD_MAX_TASKS_PER_CHILD",
                    "new_api": "worker_max_tasks_per_child",
                    "description": "CELERYD_MAX_TASKS_PER_CHILD → worker_max_tasks_per_child",
                },
            ],
            "api_evidence": [
                {
                    "api": "CELERY_*",
                    "change_type": "renamed",
                    "replacement": "lowercase_key",
                    "confidence": "high",
                    "evidence": [{"source_index": 1, "source": "migration_guide",
                                  "url": "https://docs.celeryq.dev/en/stable/userguide/configuration.html",
                                  "quote": "Configuration key naming convention changed. All CELERY_ prefixed uppercase keys are removed. Use lowercase keys."}],
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

    # ── 7. redis: StrictRedis removed, pipeline() must use context manager ────
    PatchTestCase(
        id="redis-strict-redis",
        description="redis 3.x → 5.x: StrictRedis removed (merged into Redis class)",
        project_files={
            "requirements.txt": "redis==3.5.3\n",
            "cache/client.py": """\
                import redis

                # Old: StrictRedis as the strict API
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
                    "description": "redis.StrictRedis has been removed. It was an alias for redis.Redis since v3.0. Use redis.Redis directly.",
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
                # Check the API call form, not the word in comments
                ("redis.StrictRedis(", False),
                ("redis.Redis(", True),
            ],
        },
    ),

    # ── 8. FULL PIPELINE: Scout → AST → Patch for pydantic ───────────────────
    PatchTestCase(
        id="pydantic-full-pipeline",
        description="FULL PIPELINE (Scout+AST+Patch): pydantic 1.10 → 2.x",
        project_files={
            "requirements.txt": "pydantic==1.10.12\n",
            "models/user.py": """\
                from pydantic import BaseModel, validator

                class UserProfile(BaseModel):
                    username: str
                    age: int
                    email: str

                    @validator('age', always=True)
                    @classmethod
                    def validate_age(cls, v):
                        if v < 0 or v > 150:
                            raise ValueError('Invalid age')
                        return v

                    @validator('email')
                    @classmethod
                    def validate_email(cls, v):
                        if '@' not in v:
                            raise ValueError('Email must contain @')
                        return v.strip().lower()
            """,
        },
        dep_file="requirements.txt",
        scout_output={},  # unused: full_pipeline=True fetches this from Scout
        full_pipeline=True,
        full_pipeline_package_info={
            "name": "pydantic",
            "current_version": "1.10.12",
            "latest_version": "2.13.4",
            "ecosystem": "pypi",
        },
        full_pipeline_api_usages=["pydantic.BaseModel", "pydantic.validator"],
        full_pipeline_api_contexts=[
            {
                "api": "pydantic.validator",
                "file": "models/user.py",
                "line": 9,
                "code_snippet": textwrap.dedent("""\
                    from pydantic import BaseModel, validator

                    class UserProfile(BaseModel):
                        @validator('age', always=True)
                        @classmethod
                        def validate_age(cls, v):
                            if v < 0 or v > 150:
                                raise ValueError('Invalid age')
                            return v
                """),
            }
        ],
        assertions={
            "models/user.py": [
                ("field_validator", True),
                ("always=True", False),  # always= kwarg must be removed in pydantic v2
            ],
        },
    ),
]


# ─────────────────── evaluator ───────────────────────────────────────────────

@dataclass
class PatchResult:
    case: PatchTestCase
    passed: bool
    elapsed_s: float
    patch_report: dict
    assertion_results: dict[str, list[tuple[str, bool, bool]]]  # file → [(text, expected, actual)]
    failure_reasons: list[str]
    project_dir: str


def evaluate_patch(
    case: PatchTestCase,
    project_dir: str,
    patch_report: dict,
    elapsed: float,
) -> PatchResult:
    failures: list[str] = []
    assertion_results: dict = {}

    overall_status = patch_report.get("overall_status", "unknown")
    if overall_status not in ("success",):
        failures.append(f"PatchAgent status={overall_status} (expected success)")

    files_patched = {fp["file"]: fp["status"] for fp in patch_report.get("files_patched", [])}

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
    return PatchResult(
        case=case,
        passed=passed,
        elapsed_s=elapsed,
        patch_report=patch_report,
        assertion_results=assertion_results,
        failure_reasons=failures,
        project_dir=project_dir,
    )


# ─────────────────── runner ──────────────────────────────────────────────────

async def run_full_pipeline(case: PatchTestCase, project_dir: str) -> dict:
    """Run real ScoutAgent for the full-pipeline case, then return scout_output."""
    from agents.scout import ScoutAgent
    agent = ScoutAgent()
    print(f"    [Scout] Fetching migration analysis from real ScoutAgent ...", flush=True)
    scout_output = await agent.run(
        case.full_pipeline_package_info,
        api_usages=case.full_pipeline_api_usages,
        api_contexts=case.full_pipeline_api_contexts,
    )
    return scout_output


async def run_patch_case(case: PatchTestCase) -> PatchResult:
    project_dir = make_project(case.project_files)
    start = time.monotonic()
    patch_report = {}
    try:
        # Step 1: get scout output
        if case.full_pipeline:
            scout_output = await run_full_pipeline(case, project_dir)
        else:
            scout_output = case.scout_output

        breaking_changes = scout_output.get("breaking_changes", [])
        print(f"    [Scout] {len(breaking_changes)} breaking change(s): "
              f"{[bc['old_api'] for bc in breaking_changes[:3]]}", flush=True)

        if not breaking_changes:
            patch_report = {"overall_status": "no_breaking_changes", "files_patched": []}
            return evaluate_patch(case, project_dir, patch_report, time.monotonic() - start)

        # Step 2: ASTScanner
        scanner = ASTScanner()
        ast_output = scanner.scan(project_dir, breaking_changes)
        total_matches = ast_output.get("total_matches", 0)
        files_affected = ast_output.get("total_files_affected", 0)
        print(f"    [AST]   {files_affected} file(s) affected, {total_matches} match(es)", flush=True)

        if total_matches == 0:
            print(f"    [AST]   WARNING: No matches found. Checking if scanner sees the files ...", flush=True)
            files_scanned = ast_output.get("total_files_scanned", 0)
            print(f"    [AST]   Files scanned: {files_scanned}", flush=True)

        # Step 3: PatchAgent
        dep_path = str(Path(project_dir) / case.dep_file)
        agent = PatchAgent()
        patch_report = await agent.run(scout_output, ast_output, dep_path)
        print(f"    [Patch] status={patch_report.get('overall_status')} "
              f"provider={patch_report.get('llm_provider')}", flush=True)

    except Exception as exc:
        import traceback
        patch_report = {"overall_status": "exception", "files_patched": [], "error": str(exc)}
        elapsed = time.monotonic() - start
        result = evaluate_patch(case, project_dir, patch_report, elapsed)
        result.failure_reasons.insert(0, f"Exception: {exc}")
        result.passed = False
        print(f"    [ERROR] {exc}", flush=True)
        traceback.print_exc()
        return result

    elapsed = time.monotonic() - start
    return evaluate_patch(case, project_dir, patch_report, elapsed)


def print_result(result: PatchResult) -> None:
    status = color("PASS", GREEN) if result.passed else color("FAIL", RED)
    print(f"\n{'─'*70}")
    print(f"  [{status}] {result.case.id}  ({result.elapsed_s:.1f}s)")
    print(f"  {result.case.description}")

    overall = result.patch_report.get("overall_status", "?")
    provider = result.patch_report.get("llm_provider", "?")
    print(f"  patch_status={overall}  llm_provider={provider}")

    files_patched = result.patch_report.get("files_patched", [])
    for fp in files_patched:
        icon = "✓" if fp["status"] == "success" else "✗"
        rel = Path(fp["file"]).name
        print(f"  {icon} {rel}: {fp['status']}")

    # Show assertion results
    for rel_path, checks in result.assertion_results.items():
        for text, expected, actual in checks:
            ok = actual == expected
            icon = color("✓", GREEN) if ok else color("✗", RED)
            direction = "contains" if expected else "absent"
            print(f"  {icon} {rel_path}: '{text}' → {direction} ({'OK' if ok else 'WRONG'})")

    # Show patched file contents (brief)
    for rel_path in result.case.assertions:
        full_path = Path(result.project_dir) / rel_path
        if full_path.exists():
            content = full_path.read_text()
            preview = "\n".join(
                f"      {line}" for line in content.splitlines()[:30]
            )
            print(f"\n  Patched {rel_path}:\n{preview}")
            if len(content.splitlines()) > 30:
                print(f"      ... ({len(content.splitlines())} lines total)")

    if result.failure_reasons:
        print(f"\n  {color('Failures:', RED)}")
        for r in result.failure_reasons:
            print(f"    ✗ {r}")


async def main() -> None:
    print(color(f"\n{'═'*70}", BOLD))
    print(color("  DepGuard Patch Agent — End-to-End Integration Tests", BOLD))
    print(color(f"  {len(PATCH_CASES)} patch cases | provider: Qwen via OpenRouter", CYAN))
    print(color(f"{'═'*70}", BOLD))

    filter_ids = set(sys.argv[1:])
    cases = [c for c in PATCH_CASES if not filter_ids or c.id in filter_ids]

    results: list[PatchResult] = []
    project_dirs: list[str] = []

    for i, case in enumerate(cases, 1):
        full_tag = color(" [FULL PIPELINE]", YELLOW) if case.full_pipeline else ""
        print(f"\n{color(f'[{i}/{len(cases)}]', CYAN)} {case.id}{full_tag}", flush=True)
        print(f"  {case.description}", flush=True)

        result = await run_patch_case(case)
        results.append(result)
        project_dirs.append(result.project_dir)
        print_result(result)

        # Save patch report
        out_path = Path(f"/tmp/patch_e2e_{case.id}.json")
        out_path.write_text(json.dumps(result.patch_report, indent=2, ensure_ascii=False))
        print(f"\n  Patch report → {out_path}")
        print(f"  Project dir  → {result.project_dir}")

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed_s for r in results)

    print(f"\n{'═'*70}")
    print(color(f"  SUMMARY: {passed}/{len(results)} passed  ({failed} failed)  total={total_time:.0f}s", BOLD))
    print(f"{'═'*70}")
    for r in results:
        icon = color("✓", GREEN) if r.passed else color("✗", RED)
        print(f"  {icon}  {r.case.id:<35}  {r.patch_report.get('overall_status','?'):<15}  {r.elapsed_s:.1f}s")
    print(f"{'═'*70}\n")

    # Summary JSON
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
                "files_patched": len(r.patch_report.get("files_patched", [])),
                "elapsed_s": round(r.elapsed_s, 1),
                "failures": r.failure_reasons,
            }
            for r in results
        ],
    }
    summary_path = Path("/tmp/patch_e2e_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary → {summary_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
