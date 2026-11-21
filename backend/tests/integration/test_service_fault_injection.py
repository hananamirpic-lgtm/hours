"""Hours keeps working when the ticket system is broken (task 14.2; T2, T3; Requirements 20.13 and 20.8).

Two faults, each with the service-ticket flag on:

* the `svc_` tables and the ticket-number sequence do not exist
  (`test_hours_unaffected_when_ticket_tables_are_dropped`). The application must still start, every
  ticket route must answer 503 `service_unavailable` (a database failure mapped by `ServiceRoute`, not a
  crash), and every Hours request must answer exactly what it answered before. Dropping the tables without
  `CASCADE` also proves that no Hours object depends on them.
* a ticket service raises `RuntimeError` (`test_forced_ticket_error_does_not_affect_hours`). The route answers
  500 for that one request, and Hours is unaffected, both on the very next requests and afterwards.

The corpus is a set of Hours requests: staff sign-in (wrong and right password), `/auth/me`, and one read per
Hours router. It runs against a scratch database upgraded to head (never the development database), through
the real application. Its answers are compared status for status and body for body, with the two token fields
of a sign-in answer masked because they are different on every call.

Skips without `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import User, UserRole
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.services import config_admin
from app.service_tickets.services import dashboard as dashboard_service
from auth_support import DEFAULT_PASSWORD

SIGN_IN_USERNAME = "corpus.signin"
CODE_SERVICE_UNAVAILABLE = "service_unavailable"

#: One read per Hours router, plus `/auth/me`. The audit reads are narrowed to the `clients` entity so that
#: the audit rows written by the sign-in requests (entity `users`) cannot make two passes differ.
HOURS_READS: tuple[str, ...] = (
    "/api/health",
    "/api/auth/me",
    "/api/users",
    "/api/employees",
    "/api/clients",
    "/api/sites",
    "/api/staffing-companies",
    "/api/scans/my-sites",
    "/api/scans/status",
    "/api/scans/history",
    "/api/me/photo",
    "/api/time-entries",
    "/api/periods",
    "/api/payroll",
    "/api/billing",
    "/api/reports/by-employee",
    "/api/reports/employee-daily",
    "/api/reports/by-site",
    "/api/reports/by-client",
    "/api/reports/profitability",
    "/api/reports/by-staffing-company",
    "/api/reports/dashboard",
    "/api/reports/missing-reports",
    "/api/notifications",
    "/api/audit/all?entity_type=clients",
    "/api/settings",
    "/api/search?q=a",
)

#: Columns the sign-in request moves on the user it signs in. They show in `/api/users`, so they are put
#: back between passes; everything else the corpus does is invisible to a read.
_SIGN_IN_COLUMNS = "last_login_at, failed_login_count, locked_until, updated_at"


# --------------------------------------------------------------------------------------- the world


@dataclass
class World:
    engine: Engine
    settings: Settings
    admin_headers: dict[str, str]
    technician_headers: dict[str, str]
    customer_headers: dict[str, str]
    client_id: uuid.UUID
    sign_in_state: dict[str, Any]

    def build_app(self):
        """The real application with the flag on, serving the scratch database."""
        app = create_app(self.settings.model_copy(update={"service_tickets_enabled": "true"}))
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, future=True)

        def request_session() -> Iterator[Session]:
            with factory() as per_request:
                yield per_request

        app.dependency_overrides[get_db] = request_session
        return app

    def restore_sign_in_user(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE users SET last_login_at = :last_login_at, "
                    "failed_login_count = :failed_login_count, "
                    "locked_until = :locked_until, updated_at = :updated_at WHERE username = :u"
                ),
                {**self.sign_in_state, "u": SIGN_IN_USERNAME},
            )


@pytest.fixture
def world(
    settings: Settings, empty_database: str, make_alembic_config: Callable[[str], Config]
) -> Iterator[World]:
    command.upgrade(make_alembic_config(empty_database), "head")
    engine = sa.create_engine(empty_database, poolclass=sa.pool.NullPool)
    try:
        password_hash = hash_password(DEFAULT_PASSWORD)
        with Session(engine) as session:
            admin = User(
                username="corpus.admin",
                password_hash=password_hash,
                role=UserRole.ADMIN,
                is_2fa_enabled=True,
                must_change_password=False,
            )
            technician = User(
                username="corpus.tech",
                password_hash=password_hash,
                role=UserRole.TECHNICIAN,
                must_change_password=False,
            )
            session.add_all(
                [
                    admin,
                    technician,
                    User(
                        username=SIGN_IN_USERNAME,
                        password_hash=password_hash,
                        role=UserRole.SITE_MANAGER,
                        must_change_password=False,
                    ),
                ]
            )
            session.commit()
            admin_token = create_access_token(
                user_id=admin.id, token_version=admin.token_version, role=admin.role.value
            )
            technician_token = create_access_token(
                user_id=technician.id, token_version=technician.token_version, role=technician.role.value
            )
        with engine.begin() as connection:
            client_id = connection.execute(
                sa.text("INSERT INTO clients (name) VALUES ('Kibbutz Rosh Tzurim') RETURNING id")
            ).scalar_one()
            customer_id = connection.execute(
                sa.text(
                    "INSERT INTO svc_customers (phone, full_name, full_name_key, password_hash, "
                    "must_change_password) VALUES ('0501234567', 'Owner', 'owner', :h, false) RETURNING id"
                ),
                {"h": hash_password("1234")},
            ).scalar_one()
            sign_in_state = dict(
                connection.execute(
                    sa.text(f"SELECT {_SIGN_IN_COLUMNS} FROM users WHERE username = :u"),
                    {"u": SIGN_IN_USERNAME},
                )
                .one()
                ._mapping
            )
        yield World(
            engine=engine,
            settings=settings,
            admin_headers={"Authorization": f"Bearer {admin_token}"},
            technician_headers={"Authorization": f"Bearer {technician_token}"},
            customer_headers={"Authorization": f"Bearer {mint_customer_token(customer_id, 1)}"},
            client_id=client_id,
            sign_in_state=sign_in_state,
        )
    finally:
        engine.dispose()


@contextmanager
def _running(world: World) -> Iterator[TestClient]:
    """Start the application (lifespan included) and yield a client that turns a crash into a 500."""
    app = world.build_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------------------- the corpus


Answer = tuple[int, Any]


def _mask(body: Any) -> Any:
    if isinstance(body, dict):
        return {
            key: ("<token>" if key in {"access_token", "refresh_token"} else _mask(value))
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [_mask(item) for item in body]
    return body


def _answer(response) -> Answer:  # noqa: ANN001
    kind = response.headers.get("content-type", "")
    body = response.json() if kind.startswith("application/json") else response.text
    return response.status_code, _mask(body)


def run_corpus(world: World, client: TestClient) -> dict[str, Answer]:
    """Every Hours request of the corpus, answered by `client`. Read-only except the two sign-ins."""
    answers: dict[str, Answer] = {}
    for path in HOURS_READS:
        answers[f"GET {path}"] = _answer(client.get(path, headers=world.admin_headers))
    answers[f"GET /api/audit?clients/{world.client_id}"] = _answer(
        client.get(
            f"/api/audit?entity_type=clients&entity_id={world.client_id}", headers=world.admin_headers
        )
    )
    answers["GET /api/users (technician)"] = _answer(
        client.get("/api/users", headers=world.technician_headers)
    )
    answers["GET /api/clients (customer token)"] = _answer(
        client.get("/api/clients", headers=world.customer_headers)
    )
    answers["POST /api/auth/login (wrong password)"] = _answer(
        client.post("/api/auth/login", json={"username": SIGN_IN_USERNAME, "password": "not-the-password"})
    )
    answers["POST /api/auth/login (right password)"] = _answer(
        client.post("/api/auth/login", json={"username": SIGN_IN_USERNAME, "password": DEFAULT_PASSWORD})
    )
    answers["POST /api/auth/login (unknown user)"] = _answer(
        client.post("/api/auth/login", json={"username": "nobody", "password": DEFAULT_PASSWORD})
    )
    # The two sign-ins above changed the signed-in user's own row; put it back for the next pass.
    world.restore_sign_in_user()
    return answers


def assert_same_answers(before: dict[str, Answer], after: dict[str, Answer]) -> None:
    assert after.keys() == before.keys()
    differing = {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    assert not differing, f"Hours answered differently: {differing}"


def assert_corpus_is_meaningful(answers: dict[str, Answer]) -> None:
    """A corpus of refusals proves nothing: most reads must have succeeded, and no request crashed."""
    assert not [key for key, (status, _) in answers.items() if status >= 500], answers
    assert answers["GET /api/health"][0] == 200
    assert answers["GET /api/auth/me"][0] == 200
    assert answers["GET /api/clients"][0] == 200
    assert answers["GET /api/users"][0] == 200
    assert answers["POST /api/auth/login (right password)"][0] == 200
    assert answers["POST /api/auth/login (wrong password)"][0] == 401
    ok = sum(1 for status, _ in answers.values() if status == 200)
    assert ok >= 15, {key: status for key, (status, _) in answers.items()}


# --------------------------------------------------------------------------------------- ticket routes


def _ticket_probes(world: World, client: TestClient) -> dict[str, Answer]:
    """One request per kind of ticket route that reaches the database."""
    return {
        "customer sign-in": _answer(
            client.post("/api/service/auth/sign-in", json={"phone": "0501234567", "password": "0000"})
        ),
        "customer form-config": _answer(
            client.get("/api/service/form-config", headers=world.customer_headers)
        ),
        "customer tickets": _answer(client.get("/api/service/tickets", headers=world.customer_headers)),
        "staff dashboard": _answer(client.get("/api/service/staff/dashboard", headers=world.admin_headers)),
        "staff tickets": _answer(client.get("/api/service/staff/tickets", headers=world.admin_headers)),
        "staff categories": _answer(client.get("/api/service/staff/categories", headers=world.admin_headers)),
        "staff customers": _answer(client.get("/api/service/staff/customers", headers=world.admin_headers)),
        "staff settings": _answer(client.get("/api/service/staff/settings", headers=world.admin_headers)),
    }


# --------------------------------------------------------------------------------------- the tests


def test_hours_unaffected_when_ticket_tables_are_dropped(world: World) -> None:
    # Pass 1: tables present, flag on. The ticket routes work, and Hours answers are the reference.
    with _running(world) as client:
        reference = run_corpus(world, client)
        working = _ticket_probes(world, client)
    assert_corpus_is_meaningful(reference)
    assert working["customer form-config"][0] == 200
    assert working["staff categories"][0] == 200
    assert working["customer sign-in"][0] == 401  # unknown password for a real customer, not a 503

    # Drop every ticket table and the sequence. No CASCADE: an Hours object that depended on one would
    # make this statement fail, which is itself a finding.
    with world.engine.begin() as connection:
        names = (
            connection.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = current_schema() AND tablename LIKE 'svc\\_%'"
                )
            )
            .scalars()
            .all()
        )
        assert len(names) == 7, names
        connection.execute(sa.text("DROP TABLE " + ", ".join(sorted(names))))
        connection.execute(sa.text("DROP SEQUENCE svc_ticket_number_seq"))
    with world.engine.connect() as connection:
        assert connection.execute(sa.text("SELECT to_regclass('svc_tickets')")).scalar_one() is None

    # Pass 2: a fresh process start with the flag on. Startup (lifespan) must succeed.
    with _running(world) as client:
        # The ticket routes fail with a clean 503 and the code the front end translates.
        broken = _ticket_probes(world, client)
        for name, (status, body) in broken.items():
            assert status == 503, (name, status, body)
            assert body == {"detail": {"error": {"code": CODE_SERVICE_UNAVAILABLE}}}, (name, body)

        # Feature discovery needs no database and still answers.
        features = client.get("/api/features")
        assert features.status_code == 200 and features.json()["service_tickets"] is True

        # Hours answers what it answered before, immediately after the failed ticket requests.
        assert_same_answers(reference, run_corpus(world, client))

        # And again after more ticket failures, so no failed connection or session was left behind.
        _ticket_probes(world, client)
        assert_same_answers(reference, run_corpus(world, client))


def test_forced_ticket_error_does_not_affect_hours(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("forced ticket failure")

    with _running(world) as client:
        reference = run_corpus(world, client)
        assert_corpus_is_meaningful(reference)
        healthy = _ticket_probes(world, client)
        assert healthy["customer form-config"][0] == 200
        assert healthy["staff dashboard"][0] == 200

        monkeypatch.setattr(config_admin, "get_form_config", explode)
        monkeypatch.setattr(dashboard_service, "get_dashboard", explode)

        for path, headers in (
            ("/api/service/form-config", world.customer_headers),
            ("/api/service/staff/dashboard", world.admin_headers),
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 500, (path, response.status_code)

        # The process survived: Hours answers exactly as before, while the fault is still in place.
        assert_same_answers(reference, run_corpus(world, client))
        # Unrelated ticket routes are not affected by the fault either.
        assert client.get("/api/service/tickets", headers=world.customer_headers).status_code == 200

        monkeypatch.undo()
        assert client.get("/api/service/form-config", headers=world.customer_headers).status_code == 200
        assert_same_answers(reference, run_corpus(world, client))
