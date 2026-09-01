"""Security hardening verification (Requirement 20).

These are the checks a reviewer of Requirement 20 would want proved against behaviour and against the
tree, rather than trusted from a comment:

* the hardening response headers are on every response, and CORS is an allowlist not a wildcard
  (20.9, 20.10);
* the auth and scan endpoints are rate limited (20.4);
* no secret is committed to the repository (20.6);
* every sensitive employee/user column is declared with an encrypting column type (20.2);
* no location data is collected, stored or transmitted anywhere in the source tree (20.10).

The repository-tree checks walk the filesystem rather than shelling out to git, so they hold whether
or not the checkout is a git working tree, and they skip the generated and vendored directories that
are never committed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- repository tree helpers

#: The backend package root is two levels up from this test file: tests/ -> backend/.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
#: The monorepo root holds backend/, frontend/, infra/. It is the backend's parent.
_REPO_ROOT = _BACKEND_ROOT.parent

#: Directories that are generated, vendored or local-only and never part of the committed source.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".vite",
    "htmlcov",
    "hours_api.egg-info",
    ".docker-data",
}

#: File suffixes worth scanning as text. Binary assets and lockfiles are skipped.
_TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".html",
    ".css",
    ".conf",
    ".sh",
    ".env",
    ".ini",
    ".cfg",
    ".txt",
}


def _tracked_text_files() -> Iterator[Path]:
    """Every committed-looking text file under the repo root, skipping generated trees.

    A `.env` file is git-ignored and must never be scanned as if it were source; it is excluded by
    name so a developer's real local secrets do not fail their own test run.
    """
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_REPO_ROOT).parts):
            continue
        if path.name in {".env", ".env.local"}:
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.name != ".env.example":
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------- response headers and CORS


def test_every_response_carries_the_hardening_headers(client):
    """Requirement 20.9. Read off a real response rather than off the middleware's constants."""
    response = client.get("/api/health")
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert "geolocation=()" in headers["permissions-policy"]


def test_permissions_policy_denies_geolocation(client):
    """Requirement 20.10, at the header level: the server refuses the location capability outright."""
    response = client.get("/api/health")
    assert "geolocation=()" in response.headers["permissions-policy"]


def test_hsts_is_absent_when_disabled(client):
    """The default (no TLS in front) must not pin a browser to HTTPS."""
    response = client.get("/api/health")
    assert "strict-transport-security" not in response.headers


def test_hsts_is_present_when_enabled(settings, monkeypatch):
    """Behind TLS, HSTS is emitted with the configured max-age (Requirement 20.1)."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("HSTS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(get_settings())) as tls_client:
            response = tls_client.get("/api/health")
        header = response.headers["strict-transport-security"]
        assert header.startswith("max-age=")
        assert "includeSubDomains" in header
    finally:
        get_settings.cache_clear()


def test_cors_reflects_a_known_origin_only(client, settings):
    """Requirement 20.9: cross-origin access is limited to the configured front-end origins."""
    known = settings.cors_origins[0]
    allowed = client.get("/api/health", headers={"Origin": known})
    assert allowed.headers["access-control-allow-origin"] == known

    refused = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    # An unknown origin is not echoed back, so the browser blocks the cross-origin read.
    assert refused.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_is_not_a_wildcard(settings):
    """A wildcard origin with credentials is the mistake this asserts against."""
    assert "*" not in settings.cors_origins


# --------------------------------------------------------------------------- rate limiting


class _FakeRedis:
    """A minimal in-memory stand-in for the rate limiter's Redis, counting per key.

    Only the two commands the limiter uses — `incr` and `expire` — are implemented, through a pipeline
    that mirrors `redis-py`'s `pipeline().incr().expire().execute()` shape. Enough to prove the
    limiter counts and refuses; the real Redis is exercised by the health probe.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def pipeline(self):  # noqa: ANN201 - mirrors redis-py's loose typing
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, store: _FakeRedis) -> None:
        self._store = store
        self._key: str | None = None

    def incr(self, key: str):  # noqa: ANN201
        self._key = key
        self._store.counts[key] = self._store.counts.get(key, 0) + 1
        return self

    def expire(self, key: str, seconds: int, nx: bool = False):  # noqa: ANN201, ARG002
        return self

    def execute(self):  # noqa: ANN201
        assert self._key is not None
        return [self._store.counts[self._key], True]


@pytest.fixture
def fake_redis(monkeypatch):
    """Point the rate limiter at an in-memory counter and clear the cached client."""
    from app.core import rate_limit
    from app.core import redis as redis_module

    fake = _FakeRedis()
    redis_module.get_redis_client.cache_clear()
    monkeypatch.setattr(rate_limit, "get_redis_client", lambda: fake)
    return fake


def test_login_is_rate_limited(api_client, settings, fake_redis):
    """Requirement 20.4: repeated sign-in attempts from one client hit a ceiling and get 429.

    The credentials are wrong on purpose — the limiter sits in front of authentication, so the point
    is that the *number* of attempts is capped regardless of whether each would have failed anyway.
    """
    limit = settings.auth_rate_limit_max_requests
    last_status = None
    for _ in range(limit + 2):
        response = api_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
        last_status = response.status_code
    assert last_status == 429
    body = response.json()
    assert body["detail"]["error"]["code"] == "rate_limited"
    assert "retry-after" in {k.lower() for k in response.headers}


def test_scan_endpoints_are_rate_limited(api_client, make_user, fake_redis, settings):
    """Requirement 20.4: the scan group is limited too, not only auth.

    Exercised through the status read, which shares the router-level limiter. A minted employee token
    keys the limiter; the request need not reach the endpoint body for the ceiling to apply, because
    the router-level limiter runs before the path operation's own dependencies.
    """
    from app.core.security import create_access_token
    from app.models.user import UserRole

    user = make_user(role=UserRole.EMPLOYEE)
    token = create_access_token(user_id=user.id, token_version=user.token_version, role=user.role.value)
    headers = {"Authorization": f"Bearer {token}"}

    limit = settings.scan_rate_limit_max_requests
    last = None
    for _ in range(limit + 2):
        last = api_client.get("/api/scans/status", headers=headers)
    assert last.status_code == 429
    assert last.json()["detail"]["error"]["code"] == "rate_limited"


def test_rate_limiter_fails_open_when_redis_is_down(api_client, monkeypatch):
    """A cache outage must not take sign-in down (Requirement 20.4 operational note).

    With the limiter unable to reach Redis, a request is allowed through to authentication rather than
    refused with a 429 — so the response is the ordinary 401, never a 429.
    """
    import redis as redis_lib

    from app.core import rate_limit

    def _boom():
        raise redis_lib.ConnectionError("down")

    monkeypatch.setattr(rate_limit, "get_redis_client", _boom)
    response = api_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- secrets in the repository


#: Patterns that betray a real credential committed to the tree. Deliberately narrow so a placeholder
#: or a variable name does not trip them; the point is a *value* that looks like a live secret.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    (
        "AWS secret access key assignment",
        re.compile(r"aws_secret_access_key\s*[=:]\s*['\"][A-Za-z0-9/+]{40}['\"]"),
    ),
    # A JWT-shaped triple of base64url segments, long enough not to match a short example.
    ("hard-coded JWT", re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
)


def test_no_secret_is_committed_to_the_repository():
    """Requirement 20.6. Scan the source tree for anything shaped like a live credential."""
    offenders: list[str] = []
    for path in _tracked_text_files():
        content = _read(path)
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {label}")
    assert not offenders, "possible secrets committed:\n" + "\n".join(offenders)


def test_the_env_file_is_gitignored():
    """The one file that legitimately holds secrets locally must be excluded from version control."""
    gitignore = _REPO_ROOT / ".gitignore"
    assert gitignore.exists()
    ignored = _read(gitignore).splitlines()
    assert ".env" in ignored


def test_the_example_env_carries_no_real_secret():
    """`.env.example` is committed; it must contain placeholders, not values."""
    example = _REPO_ROOT / ".env.example"
    assert example.exists()
    content = _read(example)
    for _label, pattern in _SECRET_PATTERNS:
        assert pattern.search(content) is None


# --------------------------------------------------------------------------- encryption at rest


def test_every_sensitive_column_uses_an_encrypting_type():
    """Requirement 20.2, read off the mapped columns.

    The passport number, phone, address, date of birth and emergency-contact phone on the employee,
    and the TOTP secrets on the user, are the sensitive fields the design names. Each must be mapped
    with an encrypting column type — not plain `Text` — so a service that assigns plaintext stores
    ciphertext. Checked against the mapper so a new plaintext mapping of one of these names fails.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.db.types import DeterministicHash, _EncryptedType
    from app.models.employee import Employee
    from app.models.user import User

    encrypting = (_EncryptedType, DeterministicHash)

    expected = {
        Employee: {
            "passport_number",
            "passport_number_hash",
            "phone",
            "address",
            "date_of_birth",
            "emergency_contact_phone",
        },
        User: {"totp_secret", "totp_pending_secret"},
    }

    for model, attribute_names in expected.items():
        mapper = sa_inspect(model)
        for name in attribute_names:
            # `column_attrs[name]` is the mapped attribute; its single column carries the type the
            # value is stored with, whatever the physical column name is.
            column = mapper.column_attrs[name].columns[0]
            assert isinstance(column.type, encrypting), (
                f"{model.__name__}.{name} is {type(column.type).__name__}, not an encrypting type"
            )


# --------------------------------------------------------------------------- no location data


#: Patterns that betray location being *acquired* — not the words appearing in prose. The codebase
#: documents the prohibition in several places (a `Permissions-Policy: geolocation=()` that *denies*
#: the capability, a scanner comment stating it never touches `navigator.geolocation`, schema
#: docstrings and tests asserting a `latitude` field is *rejected*), and none of that is a use. What
#: would actually collect a position is a *call* to the browser geolocation API, or a coordinate field
#: being *declared as a real model/schema attribute*. Those are what these patterns catch.
_LOCATION_CALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A call to the browser geolocation API — the only way a web client obtains a position.
    re.compile(r"geolocation\s*\.\s*(?:getCurrentPosition|watchPosition)\s*\("),
    re.compile(r"navigator\.geolocation\.(?:getCurrentPosition|watchPosition)"),
    # A coordinate declared as a SQLAlchemy column or a Pydantic field — i.e. a place the schema would
    # store or accept one. `mapped_column`/`Field`/`Column` on the same line as the name.
    re.compile(r"\b(?:latitude|longitude)\b[^\n]*(?:mapped_column|Column\(|Field\()"),
    re.compile(r"\b(?:latitude|longitude)\s*:\s*(?:float|Decimal|Mapped)"),
)


def test_no_location_is_acquired_or_stored_anywhere_in_the_source_tree():
    """Requirement 20.10. Nothing collects, stores or transmits location.

    The patterns match a browser geolocation *call* or a coordinate declared as a stored/accepted
    field — the two shapes that would actually handle location. They deliberately do not match the
    word appearing in a comment that forbids it, a header that denies the capability, or a test that
    asserts a coordinate is rejected, all of which are the codebase enforcing this requirement rather
    than breaking it.
    """
    offenders: list[str] = []
    for path in _tracked_text_files():
        # This file necessarily contains the patterns it searches for; scanning it would match its
        # own source. Every other file in the tree is scanned.
        if path.resolve() == Path(__file__).resolve():
            continue
        content = _read(path)
        for pattern in _LOCATION_CALL_PATTERNS:
            match = pattern.search(content)
            if match:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {match.group(0)!r}")
    assert not offenders, "possible location handling found:\n" + "\n".join(offenders)
