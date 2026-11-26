"""The body guard on `PUT /staff/branding` (task 30.5; R28.2).

The guard has one limit per route: 12 MiB for the two ticket submissions (unchanged) and 2 MiB for the
branding form. Same two levels as `test_service_body_limit.py`: the middleware alone with a scripted body,
and the real application.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_service_body_limit import (
    CHUNK,
    LIMIT,
    MIB,
    PATH,
    PREFIX,
    STAFF_PATH,
    TOO_LARGE_BODY,
    _call,
    _Counting,
    _guard,
    _reader,
)

from app.db.session import get_db
from app.main import create_app
from app.service_tickets.api.body_limit import MAX_BODY_BYTES, MAX_BRANDING_BODY_BYTES, BodyLimitMiddleware

BRANDING_LIMIT = 2 * MIB
BRANDING_PATH = f"{PREFIX}/service/staff/branding"


def test_the_limits_are_twelve_and_two_mebibytes() -> None:
    assert MAX_BRANDING_BODY_BYTES == BRANDING_LIMIT == 2_097_152
    assert MAX_BODY_BYTES == LIMIT == 12 * MIB  # the ticket paths keep their limit


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_exactly_two_mebibytes_pass_and_one_more_byte_fails(declared: bool) -> None:
    def declare(total: int) -> bytes | None:
        return str(total).encode() if declared else None

    counted = _Counting(_reader)
    passed = _call(
        _guard(counted), "PUT", BRANDING_PATH, total=BRANDING_LIMIT, declared=declare(BRANDING_LIMIT)
    )
    assert passed.status == 200 and passed.json() == {"bytes": BRANDING_LIMIT}
    assert counted.calls == 1

    refused = _Counting(_reader)
    over = _call(
        _guard(refused), "PUT", BRANDING_PATH, total=BRANDING_LIMIT + 1, declared=declare(BRANDING_LIMIT + 1)
    )
    assert over.status == 413 and over.json() == TOO_LARGE_BODY
    if declared:
        assert refused.calls == 0 and over.receive_calls == 0  # refused before one byte was read
    else:
        assert over.delivered_bytes <= BRANDING_LIMIT + CHUNK  # the stream was cut


def test_the_stream_is_cut_even_when_the_declared_length_is_a_lie() -> None:
    outcome = _call(_guard(), "PUT", BRANDING_PATH, total=BRANDING_LIMIT + 1, declared=b"100")

    assert outcome.status == 413 and outcome.json() == TOO_LARGE_BODY


def test_a_body_that_passes_the_ticket_limit_does_not_pass_the_branding_limit() -> None:
    size = 3 * MIB  # fine for a submission, too big for the branding form

    assert _call(_guard(), "POST", PATH, total=size, declared=str(size).encode()).status == 200
    assert _call(_guard(), "POST", STAFF_PATH, total=size, declared=str(size).encode()).status == 200
    assert _call(_guard(), "PUT", BRANDING_PATH, total=size, declared=str(size).encode()).status == 413


def test_the_ticket_paths_are_still_limited_at_twelve_mebibytes() -> None:
    for path in (PATH, STAFF_PATH):
        assert _call(_guard(), "POST", path, total=LIMIT, declared=str(LIMIT).encode()).status == 200
        assert _call(_guard(), "POST", path, total=LIMIT + 1, declared=str(LIMIT + 1).encode()).status == 413


def test_the_limits_can_be_set_per_route() -> None:
    guard = BodyLimitMiddleware(_reader, api_prefix=PREFIX, limit=1000, branding_limit=500)

    assert _call(guard, "POST", PATH, total=1000).status == 200
    assert _call(guard, "POST", PATH, total=1001).status == 413
    assert _call(guard, "PUT", BRANDING_PATH, total=500).status == 200
    assert _call(guard, "PUT", BRANDING_PATH, total=501).status == 413


def test_a_custom_prefix_moves_the_branding_path() -> None:
    guard = BodyLimitMiddleware(_reader, api_prefix="/v2/")
    declared = str(BRANDING_LIMIT + 1).encode()

    assert (
        _call(guard, "PUT", "/v2/service/staff/branding", total=BRANDING_LIMIT + 1, declared=declared).status
        == 413
    )
    assert _call(guard, "PUT", BRANDING_PATH, total=BRANDING_LIMIT + 1, declared=declared).status == 200


OTHER_ROUTES = [
    ("GET", BRANDING_PATH),  # another method on the same path
    ("POST", BRANDING_PATH),
    ("PATCH", BRANDING_PATH),
    ("DELETE", BRANDING_PATH),
    ("PUT", f"{BRANDING_PATH}/"),  # exact match only
    ("PUT", f"{BRANDING_PATH}/x"),
    ("PUT", f"{PREFIX}/service/staff/branding-extra"),
    ("PUT", f"{PREFIX}/service/branding"),
    ("PUT", f"{PREFIX}/service/staff/settings"),
    ("PUT", f"{PREFIX}/service/staff/labels"),
    ("PUT", f"{PREFIX}/staff/branding"),
    ("PUT", "/other/api/service/staff/branding"),
    ("GET", f"{PREFIX}/service/branding/logo"),
]


@pytest.mark.parametrize(("method", "path"), OTHER_ROUTES)
def test_other_paths_and_methods_are_not_limited_by_the_branding_rule(method: str, path: str) -> None:
    total = LIMIT + MIB
    for declared in (str(total).encode(), None):
        counted = _Counting(_reader)
        outcome = _call(_guard(counted), method, path, total=total, declared=declared)

        assert outcome.status == 200, (method, path, declared)
        assert outcome.json() == {"bytes": total}
        assert counted.calls == 1


# --------------------------------------------------------------------------- the real application


@pytest.fixture
def client(settings) -> TestClient:
    app = create_app(settings.model_copy(update={"service_tickets_enabled": "true", "api_prefix": PREFIX}))
    app.dependency_overrides[get_db] = lambda: None  # the guard must answer without a database
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


@pytest.mark.parametrize("size", [BRANDING_LIMIT + 1, 3 * MIB, LIMIT])
def test_an_oversized_branding_body_gets_the_coded_413_without_a_token(client: TestClient, size: int) -> None:
    response = client.put(
        BRANDING_PATH, content=b"x" * size, headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code == 413
    assert response.json() == TOO_LARGE_BODY
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"  # the security headers still apply


def test_a_branding_body_of_exactly_the_limit_reaches_authentication(client: TestClient) -> None:
    response = client.put(
        BRANDING_PATH, content=b"x" * BRANDING_LIMIT, headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "not_authenticated"


def test_the_ticket_paths_of_the_real_app_keep_their_limit(client: TestClient) -> None:
    body = b"x" * (3 * MIB)
    for path in (PATH, STAFF_PATH):
        response = client.post(path, content=body, headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 401, path  # not a 413: three mebibytes is fine there


def test_the_branding_read_is_not_limited(client: TestClient) -> None:
    response = client.request(
        "GET", BRANDING_PATH, content=b"x" * (3 * MIB), headers={"Content-Type": "application/octet-stream"}
    )

    assert response.status_code == 401
