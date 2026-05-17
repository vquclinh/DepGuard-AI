"""
End-to-end Scout Agent integration tests.

Each test case defines:
  - package_info : what to upgrade
  - api_usages   : APIs found in the project code
  - api_contexts : code snippets using those APIs
  - expected     : keywords / API names that MUST appear in the LLM output
  - forbidden    : strings that must NOT appear (false positives)

Run with:
    cd /mnt/vquclinh/PROJECT-CMAKE/DEPGUARD-AI/DepGuard-AI
    python -m tests.integration_scout_e2e 2>&1 | tee /tmp/scout_e2e_results.txt
"""

import asyncio
import json
import os
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.scout import ScoutAgent

# ─────────────────────────── helpers ────────────────────────────────────────

RESET = "\033[0m"
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW= "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"

def color(text, c): return f"{c}{text}{RESET}"


@dataclass
class TestCase:
    id: str
    description: str
    package_info: dict
    api_usages: list[str]
    api_contexts: list[dict]
    # At least ONE expected keyword must appear in breaking_changes descriptions
    expected_change_keywords: list[str]
    # At least ONE expected old_api must appear (exact or substring match)
    expected_old_apis: list[str]
    # These should NOT appear as breaking changes (false-positive guard)
    forbidden_old_apis: list[str] = field(default_factory=list)
    # Minimum acceptable confidence_score
    min_confidence: float = 0.4
    # Whether we expect any breaking changes at all
    expect_breaking: bool = True


@dataclass
class TestResult:
    case: TestCase
    passed: bool
    score: float
    breaking_changes: list[dict]
    api_evidence: list[dict]
    confidence_score: float
    evidence_confidence: str
    elapsed_s: float
    failure_reasons: list[str]
    raw_output: dict


# ─────────────────────────── test cases ─────────────────────────────────────

TEST_CASES: list[TestCase] = [

    # ── 1. pydantic v1 → v2 ──────────────────────────────────────────────────
    TestCase(
        id="pydantic-v1-v2",
        description="pydantic 1.10.12 → 2.13.4: @validator + BaseModel migration",
        package_info={
            "name": "pydantic",
            "current_version": "1.10.12",
            "latest_version": "2.13.4",
            "ecosystem": "pypi",
        },
        api_usages=["pydantic.BaseModel", "pydantic.validator"],
        api_contexts=[
            {
                "api": "pydantic.BaseModel",
                "file": "models/user.py",
                "line": 5,
                "code_snippet": textwrap.dedent("""\
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
                """),
            },
        ],
        expected_change_keywords=["deprecated", "field_validator", "validator"],
        expected_old_apis=["pydantic.validator", "validator", "@validator"],
        min_confidence=0.5,
    ),

    # ── 2. PyJWT v1 → v2 ─────────────────────────────────────────────────────
    TestCase(
        id="pyjwt-v1-v2",
        description="PyJWT 1.7.1 → 2.12.1: verify=True removed, algorithms required",
        package_info={
            "name": "PyJWT",
            "current_version": "1.7.1",
            "latest_version": "2.12.1",
            "ecosystem": "pypi",
        },
        api_usages=["jwt.decode", "jwt.encode"],
        api_contexts=[
            {
                "api": "jwt.decode",
                "file": "auth/token.py",
                "line": 12,
                "code_snippet": textwrap.dedent("""\
                    import jwt

                    SECRET_KEY = 'my-secret'

                    def verify_token(token: str) -> dict:
                        return jwt.decode(token, SECRET_KEY, options={'verify_exp': True})

                    def create_token(payload: dict) -> str:
                        return jwt.encode(payload, SECRET_KEY, algorithm='HS256')
                """),
            },
        ],
        expected_change_keywords=["algorithms", "removed", "verify", "required"],
        expected_old_apis=["jwt.decode", "decode"],
        min_confidence=0.4,
    ),

    # ── 3. SQLAlchemy 1.4 → 2.0 ──────────────────────────────────────────────
    TestCase(
        id="sqlalchemy-1-2",
        description="SQLAlchemy 1.4.52 → 2.0.36: engine.execute removed, session patterns changed",
        package_info={
            "name": "sqlalchemy",
            "current_version": "1.4.52",
            "latest_version": "2.0.36",
            "ecosystem": "pypi",
        },
        api_usages=["sqlalchemy.create_engine", "sqlalchemy.Session", "sqlalchemy.Column"],
        api_contexts=[
            {
                "api": "sqlalchemy.create_engine",
                "file": "db/session.py",
                "line": 3,
                "code_snippet": textwrap.dedent("""\
                    from sqlalchemy import create_engine, Column, Integer, String
                    from sqlalchemy.ext.declarative import declarative_base
                    from sqlalchemy.orm import sessionmaker

                    engine = create_engine('sqlite:///app.db')
                    Base = declarative_base()
                    Session = sessionmaker(bind=engine)

                    # Legacy: engine.execute() usage
                    result = engine.execute('SELECT * FROM users')

                    # Legacy: session.execute with string
                    with Session() as session:
                        rows = session.execute('SELECT id, name FROM users')
                        for row in rows:
                            print(row['id'], row['name'])
                """),
            },
        ],
        expected_change_keywords=["removed", "execute", "text", "deprecated"],
        expected_old_apis=["engine.execute", "declarative_base"],
        min_confidence=0.4,
    ),

    # ── 4. Flask 1.x → 3.x ───────────────────────────────────────────────────
    TestCase(
        id="flask-1-3",
        description="Flask 1.1.4 → 3.1.1: before_first_request removed, app context changes",
        package_info={
            "name": "Flask",
            "current_version": "1.1.4",
            "latest_version": "3.1.1",
            "ecosystem": "pypi",
        },
        api_usages=["flask.Flask", "flask.request", "flask.before_first_request"],
        api_contexts=[
            {
                "api": "flask.Flask",
                "file": "app/main.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    from flask import Flask, request, jsonify, g

                    app = Flask(__name__)

                    @app.before_first_request
                    def setup():
                        print('App starting up...')

                    @app.route('/user/<int:user_id>', methods=['GET'])
                    def get_user(user_id):
                        return jsonify({'id': user_id})

                    if __name__ == '__main__':
                        app.run(debug=True, use_reloader=True)
                """),
            },
        ],
        expected_change_keywords=["removed", "before_first_request", "deprecated"],
        expected_old_apis=["before_first_request"],
        min_confidence=0.35,
    ),

    # ── 5. aiohttp 2.x → 3.x ─────────────────────────────────────────────────
    TestCase(
        id="aiohttp-2-3",
        description="aiohttp 2.3.10 → 3.11.12: ClientSession API redesign",
        package_info={
            "name": "aiohttp",
            "current_version": "2.3.10",
            "latest_version": "3.11.12",
            "ecosystem": "pypi",
        },
        api_usages=["aiohttp.ClientSession", "aiohttp.get"],
        api_contexts=[
            {
                "api": "aiohttp.ClientSession",
                "file": "services/http_client.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import aiohttp
                    import asyncio

                    async def fetch_data(url: str) -> dict:
                        # aiohttp v2 style: no async with required
                        session = aiohttp.ClientSession()
                        async with session.get(url) as response:
                            data = await response.json()
                        await session.close()
                        return data

                    async def main():
                        # deprecated top-level function
                        async with aiohttp.get('http://example.com') as resp:
                            print(await resp.text())
                """),
            },
        ],
        expected_change_keywords=["removed", "deprecated", "ClientSession", "async"],
        expected_old_apis=["aiohttp.get", "ClientSession"],
        min_confidence=0.35,
    ),

    # ── 6. numpy 1.x → 2.x ───────────────────────────────────────────────────
    TestCase(
        id="numpy-1-2",
        description="numpy 1.26.4 → 2.2.5: np.bool/np.int/np.float type aliases removed",
        package_info={
            "name": "numpy",
            "current_version": "1.26.4",
            "latest_version": "2.2.5",
            "ecosystem": "pypi",
        },
        api_usages=["numpy.bool", "numpy.int", "numpy.float", "numpy.array"],
        api_contexts=[
            {
                "api": "numpy.bool",
                "file": "ml/features.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import numpy as np

                    def normalize(data):
                        # np.bool was an alias for Python's bool, removed in 1.24
                        mask: np.bool = data > 0
                        # np.int alias removed
                        indices = np.array([1, 2, 3], dtype=np.int)
                        # np.float alias removed
                        values = np.array([1.0, 2.0], dtype=np.float)
                        # np.complex removed
                        z = np.array([1+2j], dtype=np.complex)
                        return values[mask]
                """),
            },
        ],
        expected_change_keywords=["removed", "bool", "int", "float", "alias"],
        expected_old_apis=["np.bool", "numpy.bool", "np.int", "np.float"],
        min_confidence=0.35,
    ),

    # ── 7. click 7.x → 8.x ───────────────────────────────────────────────────
    TestCase(
        id="click-7-8",
        description="click 7.1.2 → 8.1.8: invoke() signature change, autocompletion renamed",
        package_info={
            "name": "click",
            "current_version": "7.1.2",
            "latest_version": "8.1.8",
            "ecosystem": "pypi",
        },
        api_usages=["click.command", "click.option", "click.pass_context"],
        api_contexts=[
            {
                "api": "click.command",
                "file": "cli/main.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import click

                    @click.command()
                    @click.option('--name', default='World')
                    @click.pass_context
                    def greet(ctx, name):
                        click.echo(f'Hello {name}!')
                        ctx.invoke(other_command, arg=name)

                    @click.command()
                    @click.argument('arg')
                    def other_command(arg):
                        click.echo(arg)

                    # Testing commands
                    from click.testing import CliRunner
                    runner = CliRunner()
                    result = runner.invoke(greet, ['--name', 'Alice'])
                """),
            },
        ],
        expected_change_keywords=["autocompletion", "completion", "removed", "changed", "invoke"],
        expected_old_apis=["click.command", "click.option", "invoke"],
        min_confidence=0.3,
        # click has fewer breaking changes, may have low confidence
    ),

    # ── 8. marshmallow 2.x → 3.x ─────────────────────────────────────────────
    TestCase(
        id="marshmallow-2-3",
        description="marshmallow 2.21.0 → 3.21.3: Schema.dump() returns dict (not tuple), strict=True default",
        package_info={
            "name": "marshmallow",
            "current_version": "2.21.0",
            "latest_version": "3.21.3",
            "ecosystem": "pypi",
        },
        api_usages=["marshmallow.Schema", "marshmallow.fields"],
        api_contexts=[
            {
                "api": "marshmallow.Schema",
                "file": "serializers/user.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    from marshmallow import Schema, fields

                    class UserSchema(Schema):
                        id = fields.Int()
                        name = fields.Str(required=True)
                        email = fields.Email()

                        class Meta:
                            strict = True

                    schema = UserSchema()
                    user_data = {'id': 1, 'name': 'Alice', 'email': 'alice@example.com'}

                    # marshmallow v2: dump() returns (data, errors) tuple
                    result, errors = schema.dump(user_data)
                    # marshmallow v2: load() returns (data, errors) tuple
                    loaded, errors = schema.load({'name': 'Bob', 'email': 'bob@example.com'})
                """),
            },
        ],
        expected_change_keywords=["dump", "load", "strict", "removed", "changed"],
        expected_old_apis=["Schema.dump", "dump", "load", "strict"],
        min_confidence=0.35,
    ),

    # ── 9. celery 4.x → 5.x ──────────────────────────────────────────────────
    TestCase(
        id="celery-4-5",
        description="celery 4.4.7 → 5.4.0: CELERY_* config keys removed, Python 2 dropped",
        package_info={
            "name": "celery",
            "current_version": "4.4.7",
            "latest_version": "5.4.0",
            "ecosystem": "pypi",
        },
        api_usages=["celery.Celery", "celery.task"],
        api_contexts=[
            {
                "api": "celery.Celery",
                "file": "tasks/worker.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    from celery import Celery, task

                    app = Celery('myapp', broker='redis://localhost:6379/0')

                    app.conf.update(
                        CELERY_TASK_SERIALIZER='json',
                        CELERY_RESULT_BACKEND='redis://localhost:6379/1',
                        CELERY_ACCEPT_CONTENT=['json'],
                        CELERY_TIMEZONE='UTC',
                        CELERY_ENABLE_UTC=True,
                    )

                    @app.task
                    def add(x, y):
                        return x + y

                    # Old-style task decorator
                    @task
                    def multiply(x, y):
                        return x * y
                """),
            },
        ],
        expected_change_keywords=["CELERY_", "removed", "renamed", "config", "deprecated"],
        expected_old_apis=["CELERY_TASK_SERIALIZER", "celery.task", "task"],
        min_confidence=0.35,
    ),

    # ── 10. redis (Python) 3.x → 5.x ─────────────────────────────────────────
    TestCase(
        id="redis-py-3-5",
        description="redis 3.5.3 → 5.2.1: from_url auth changes, pipeline() ctx manager required",
        package_info={
            "name": "redis",
            "current_version": "3.5.3",
            "latest_version": "5.2.1",
            "ecosystem": "pypi",
        },
        api_usages=["redis.Redis", "redis.StrictRedis", "redis.ConnectionPool"],
        api_contexts=[
            {
                "api": "redis.Redis",
                "file": "cache/client.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import redis

                    # redis v3 style
                    client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

                    # Connection pool
                    pool = redis.ConnectionPool(host='localhost', port=6379, db=0)
                    r = redis.Redis(connection_pool=pool)

                    # Pipeline usage
                    pipe = r.pipeline()
                    pipe.set('key1', 'value1')
                    pipe.set('key2', 'value2')
                    results = pipe.execute()

                    # from_url
                    r2 = redis.Redis.from_url('redis://user:password@localhost:6379/0')
                """),
            },
        ],
        expected_change_keywords=["StrictRedis", "removed", "deprecated", "pipeline", "changed"],
        expected_old_apis=["redis.StrictRedis", "StrictRedis"],
        min_confidence=0.3,
    ),

    # ── 11. axios 0.x → 1.x (npm) ────────────────────────────────────────────
    TestCase(
        id="axios-0-1",
        description="axios 0.27.2 → 1.7.9: config structure, error response changes",
        package_info={
            "name": "axios",
            "current_version": "0.27.2",
            "latest_version": "1.7.9",
            "ecosystem": "npm",
        },
        api_usages=["axios.get", "axios.post", "axios.create"],
        api_contexts=[
            {
                "api": "axios.get",
                "file": "src/api/client.js",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    const axios = require('axios');

                    const client = axios.create({
                        baseURL: 'https://api.example.com',
                        timeout: 5000,
                    });

                    // Request interceptor
                    client.interceptors.request.use(config => {
                        config.headers.common['Authorization'] = `Bearer ${getToken()}`;
                        return config;
                    });

                    async function getUser(id) {
                        try {
                            const response = await client.get(`/users/${id}`);
                            return response.data;
                        } catch (err) {
                            console.error(err.response.data);
                        }
                    }

                    async function createUser(data) {
                        const { data: user } = await axios.post('/users', data, {
                            headers: { 'Content-Type': 'application/json' }
                        });
                        return user;
                    }
                """),
            },
        ],
        expected_change_keywords=["changed", "headers", "config", "deprecated", "removed"],
        expected_old_apis=["axios.get", "axios.post", "headers.common"],
        min_confidence=0.25,
        # axios 0→1 has relatively minor breaking changes
    ),

    # ── 12. lodash 3.x → 4.x (npm) ───────────────────────────────────────────
    TestCase(
        id="lodash-3-4",
        description="lodash 3.10.1 → 4.17.21: _.pluck removed, _.flatten changed, category splits",
        package_info={
            "name": "lodash",
            "current_version": "3.10.1",
            "latest_version": "4.17.21",
            "ecosystem": "npm",
        },
        api_usages=["lodash.pluck", "lodash.flatten", "lodash.first", "lodash.rest"],
        api_contexts=[
            {
                "api": "lodash.pluck",
                "file": "src/utils/collections.js",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    const _ = require('lodash');

                    const users = [
                        { name: 'Alice', age: 25 },
                        { name: 'Bob', age: 30 },
                    ];

                    // v3: _.pluck (removed in v4, use _.map)
                    const names = _.pluck(users, 'name');

                    // v3: _.flatten with depth (changed in v4)
                    const nested = [[1, [2]], [3]];
                    const flat = _.flatten(nested, true);  // v3: second arg = shallow

                    // v3: _.first / _.rest (renamed in v4 to _.head / _.tail)
                    const head = _.first(names);
                    const tail = _.rest(names);

                    // v3: _.contains (renamed to _.includes)
                    const has = _.contains(names, 'Alice');

                    // v3: _.support removed entirely
                    console.log(_.support);
                """),
            },
        ],
        expected_change_keywords=["removed", "pluck", "renamed", "flatten", "contains"],
        expected_old_apis=["_.pluck", "pluck", "_.flatten", "_.contains", "_.rest"],
        min_confidence=0.35,
    ),

    # ── 13. httpx 0.x → 0.27 (minor — expect NO major breaking changes) ──────
    TestCase(
        id="httpx-stable-no-breaking",
        description="httpx 0.23.0 → 0.27.2: minor release — expect no big breaking changes",
        package_info={
            "name": "httpx",
            "current_version": "0.23.0",
            "latest_version": "0.27.2",
            "ecosystem": "pypi",
        },
        api_usages=["httpx.AsyncClient", "httpx.get"],
        api_contexts=[
            {
                "api": "httpx.AsyncClient",
                "file": "services/http.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import httpx

                    async def fetch(url: str) -> dict:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(url)
                            response.raise_for_status()
                            return response.json()
                """),
            },
        ],
        expected_change_keywords=[],
        expected_old_apis=[],
        min_confidence=0.0,
        expect_breaking=False,  # httpx 0.23→0.27 is minor; no major breaking expected
    ),

    # ── 14. cryptography 3.x → 42.x ──────────────────────────────────────────
    TestCase(
        id="cryptography-3-42",
        description="cryptography 3.4.8 → 42.0.8: backend parameter removed, deprecated APIs",
        package_info={
            "name": "cryptography",
            "current_version": "3.4.8",
            "latest_version": "42.0.8",
            "ecosystem": "pypi",
        },
        api_usages=["cryptography.fernet.Fernet", "cryptography.hazmat.primitives"],
        api_contexts=[
            {
                "api": "cryptography.fernet.Fernet",
                "file": "crypto/encrypt.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    from cryptography.fernet import Fernet
                    from cryptography.hazmat.primitives.asymmetric import rsa
                    from cryptography.hazmat.backends import default_backend

                    # Generate key
                    key = Fernet.generate_key()
                    cipher = Fernet(key)

                    # RSA key generation - old style with backend parameter
                    private_key = rsa.generate_private_key(
                        public_exponent=65537,
                        key_size=2048,
                        backend=default_backend()  # deprecated - no longer needed
                    )
                    public_key = private_key.public_key()
                """),
            },
        ],
        expected_change_keywords=["backend", "removed", "deprecated", "default_backend"],
        expected_old_apis=["default_backend", "backend"],
        min_confidence=0.3,
    ),

    # ── 15. requests 2.x (stable — very minor changes) ───────────────────────
    TestCase(
        id="requests-stable",
        description="requests 2.28.0 → 2.32.3: minor patch — no breaking API changes expected",
        package_info={
            "name": "requests",
            "current_version": "2.28.0",
            "latest_version": "2.32.3",
            "ecosystem": "pypi",
        },
        api_usages=["requests.get", "requests.Session"],
        api_contexts=[
            {
                "api": "requests.get",
                "file": "utils/http.py",
                "line": 1,
                "code_snippet": textwrap.dedent("""\
                    import requests

                    def get_data(url: str, token: str) -> dict:
                        session = requests.Session()
                        session.headers.update({'Authorization': f'Bearer {token}'})
                        response = session.get(url, timeout=10)
                        response.raise_for_status()
                        return response.json()
                """),
            },
        ],
        expected_change_keywords=[],
        expected_old_apis=[],
        min_confidence=0.0,
        expect_breaking=False,
    ),
]


# ─────────────────────────── evaluator ──────────────────────────────────────

def evaluate_result(case: TestCase, raw: dict, elapsed: float) -> TestResult:
    breaking = raw.get("breaking_changes", [])
    api_evidence = raw.get("api_evidence", [])
    confidence_score = float(raw.get("confidence_score", 0.0))
    evidence_confidence = raw.get("evidence_confidence", "none")
    failures: list[str] = []

    if case.expect_breaking:
        if not breaking:
            failures.append("No breaking changes found (expected some)")

        # At least one expected keyword must appear in any breaking change description
        if case.expected_change_keywords:
            combined_text = " ".join(
                (bc.get("description", "") + " " + bc.get("old_api", "") + " " + bc.get("new_api", "")).lower()
                for bc in breaking
            )
            combined_text += " ".join(
                (ev.get("api", "") + " " + ev.get("replacement", "")).lower()
                for ev in api_evidence
            )
            found_keyword = any(kw.lower() in combined_text for kw in case.expected_change_keywords)
            if not found_keyword:
                failures.append(
                    f"None of the expected keywords found: {case.expected_change_keywords}"
                )

        # At least one expected old_api must appear
        if case.expected_old_apis:
            all_old_apis = [bc.get("old_api", "").lower() for bc in breaking]
            all_old_apis += [ev.get("api", "").lower() for ev in api_evidence]
            found_api = any(
                any(expected.lower() in api for api in all_old_apis)
                for expected in case.expected_old_apis
            )
            if not found_api:
                failures.append(
                    f"None of the expected old_apis found: {case.expected_old_apis}"
                )

    else:
        # Expect NO breaking changes (or very low confidence)
        if breaking and confidence_score > 0.5:
            failures.append(
                f"Unexpected high-confidence breaking changes reported for stable upgrade "
                f"(confidence={confidence_score:.2f}, {len(breaking)} changes)"
            )

    # Forbidden false positives
    for forbidden in case.forbidden_old_apis:
        for bc in breaking:
            if forbidden.lower() in bc.get("old_api", "").lower():
                failures.append(f"False positive: '{forbidden}' incorrectly reported as breaking")

    # Minimum confidence check (only for breaking cases)
    if case.expect_breaking and confidence_score < case.min_confidence:
        failures.append(
            f"confidence_score={confidence_score:.2f} below minimum {case.min_confidence}"
        )

    score = round(confidence_score, 3)
    passed = len(failures) == 0
    return TestResult(
        case=case,
        passed=passed,
        score=score,
        breaking_changes=breaking,
        api_evidence=api_evidence,
        confidence_score=confidence_score,
        evidence_confidence=evidence_confidence,
        elapsed_s=elapsed,
        failure_reasons=failures,
        raw_output=raw,
    )


# ─────────────────────────── runner ─────────────────────────────────────────

async def run_case(case: TestCase) -> TestResult:
    agent = ScoutAgent()
    start = time.monotonic()
    try:
        raw = await agent.run(
            case.package_info,
            api_usages=case.api_usages,
            api_contexts=case.api_contexts,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        raw = {
            "breaking_changes": [],
            "api_evidence": [],
            "confidence_score": 0.0,
            "evidence_confidence": "error",
            "error": str(exc),
        }
        result = evaluate_result(case, raw, elapsed)
        result.failure_reasons.insert(0, f"Exception: {exc}")
        return result
    elapsed = time.monotonic() - start
    return evaluate_result(case, raw, elapsed)


def print_result(result: TestResult) -> None:
    status = color("PASS", GREEN) if result.passed else color("FAIL", RED)
    print(f"\n{'─'*70}")
    print(f"  [{status}] {result.case.id}  ({result.elapsed_s:.1f}s)")
    print(f"  {result.case.description}")
    print(f"  confidence_score={result.confidence_score:.3f}  evidence_confidence={result.evidence_confidence}")

    if result.breaking_changes:
        print(f"  Breaking changes ({len(result.breaking_changes)}):")
        for bc in result.breaking_changes[:5]:
            print(f"    • [{bc.get('type','?')}] {bc.get('old_api','?')} → {bc.get('new_api','?')}")
            print(f"      {bc.get('description','')[:120]}")
    else:
        print("  Breaking changes: none")

    if result.api_evidence:
        print(f"  API evidence ({len(result.api_evidence)}):")
        for ev in result.api_evidence[:4]:
            print(f"    • {ev.get('api','?')} [{ev.get('confidence','?')}] "
                  f"change_type={ev.get('change_type','?')}")

    if result.failure_reasons:
        print(f"  {color('Failure reasons:', RED)}")
        for reason in result.failure_reasons:
            print(f"    ✗ {reason}")


async def main() -> None:
    print(color(f"\n{'═'*70}", BOLD))
    print(color("  DepGuard Scout Agent — End-to-End Integration Tests", BOLD))
    print(color(f"  {len(TEST_CASES)} test cases | provider: Qwen via OpenRouter", CYAN))
    print(color(f"{'═'*70}", BOLD))

    # Allow filtering via CLI args
    filter_ids = set(sys.argv[1:])
    cases = [c for c in TEST_CASES if not filter_ids or c.id in filter_ids]

    results: list[TestResult] = []
    for i, case in enumerate(cases, 1):
        print(f"\n{color(f'[{i}/{len(cases)}]', CYAN)} Running: {case.id} …", flush=True)
        result = await run_case(case)
        results.append(result)
        print_result(result)

        # Save raw output for inspection
        out_path = Path(f"/tmp/scout_e2e_{case.id}.json")
        out_path.write_text(json.dumps(result.raw_output, indent=2, ensure_ascii=False))
        print(f"  Raw output → {out_path}")

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed_s for r in results)

    print(f"\n{'═'*70}")
    print(color(f"  SUMMARY: {passed}/{len(results)} passed  ({failed} failed)  total={total_time:.0f}s", BOLD))
    print(f"{'═'*70}")
    for r in results:
        icon = color("✓", GREEN) if r.passed else color("✗", RED)
        print(f"  {icon}  {r.case.id:<35}  score={r.score:.3f}  {r.evidence_confidence:<8}  {r.elapsed_s:.1f}s")
    print(f"{'═'*70}\n")

    # Write summary JSON
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "total_time_s": round(total_time, 1),
        "results": [
            {
                "id": r.case.id,
                "passed": r.passed,
                "score": r.score,
                "evidence_confidence": r.evidence_confidence,
                "elapsed_s": round(r.elapsed_s, 1),
                "breaking_changes": len(r.breaking_changes),
                "failures": r.failure_reasons,
            }
            for r in results
        ],
    }
    summary_path = Path("/tmp/scout_e2e_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary JSON → {summary_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
