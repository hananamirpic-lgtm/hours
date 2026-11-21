"""Access matrix and check order of the Ticket API (task 13.8, Property 8).

Requirements 1.2 to 1.12, 1.14, 2.5, 2.7, 6.1 and 14.5; design.md, "Request check order", "Staff
dependencies" and "Customer principal".

What is checked
---------------
* **The access matrix.** Every token state of `REFERENCE` is sent to every route of the endpoint table
  (`test_service_route_classification.ENDPOINT_TABLE`, imported, so a new route that lands there is
  picked up once it has a probe below), and the status and error code are compared with the reference
  matrix. The reference is written out by hand from the requirements: it is not computed from the
  dependencies. Every 401 and 403 body must be exactly `{"detail": {"error": {"code": ...}}}` with no
  ticket data in it, and no row of any `svc_` table, of `users` or of `change_logs` may differ before and
  after the request (and no ticket number may have been taken).
* **Check order (R1.14).** Failures that happen together return the first one of authentication, role,
  forced password change, validation, ownership. Tokens that fail at two stages at once (a deactivated
  site manager, a technician who owes a password change on a configuration route) are in the matrix; the
  request side (invalid body or query, someone else's or a missing ticket) is combined with every token
  state in `test_check_order`.
* **Administrator and operations administrator** receive identical responses and leave identical data.
* **Role change, deactivation and revocation** apply from the next request, with the older token.
* **R14.5.** A customer cannot set a staff status.

Adding the shared photo route (task 16.4)
-----------------------------------------
`PROBES` already holds the photo probe (`GET /tickets/{ticket_number}/photo`, class shared-photo). It is
left out while the endpoint table marks the route `pending`, and joins the matrix by itself when task 16.4
removes the marker. The `p` column of `REFERENCE` holds its expectations (staff of the three ticket roles
and a customer admitted, a customer who owes a password change refused). `ok=None` on its probe means
"any status other than 401 or 403", because the exact success status belongs to task 16.4: tighten it
there. The photo case of the ownership property (another customer's photo is a 404) is task 13.9's.

Findings
--------
Where the code differs from what the requirements say, the matrix still asserts the requirement and the
cell is marked `xfail(strict=True)` through `DEVIATIONS` (one entry per family, with the reason). A strict
xfail turns into a failure the day the behavior changes, which is the moment to delete the entry.

Harness
-------
One in-memory SQLite database is seeded once (`template`) and copied into a fresh database for every test
(`make_world`), so a test that succeeds in changing data cannot disturb the next one. Each world has a
small application that mounts the two routers through `mount.mount`, with the per-request session
override and a counting ticket-number allocator (SQLite has no sequence). Tokens are real. Requests use
sessions of their own, as in production, so every read the test makes is of committed data.
"""

from __future__ import annotations

import itertools
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from types import SimpleNamespace
from typing import Any, NamedTuple

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_service_route_classification import (
    CONFIG_STAFF,
    CUSTOMER,
    CUSTOMER_UNGATED,
    ENDPOINT_TABLE,
    SHARED_PHOTO,
    TICKET_STAFF,
    UNAUTHENTICATED,
)

from app.core.security import create_access_token, create_refresh_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.models.user import User, UserRole
from app.service_tickets.api import customer_router
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import decode_customer_token, mint_customer_token
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Category,
    Customer,
    FieldLabel,
    HistoryEntry,
    ServiceSettings,
    Ticket,
)
from app.service_tickets.mount import mount

SERVICE = "/api/service"
LONG_AGO = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)

# --------------------------------------------------------------------------- seeded identities


def _uid(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


@dataclass(frozen=True)
class StaffSpec:
    id: uuid.UUID
    role: UserRole
    active: bool = True
    db_version: int = 1  # the version stored on the row; tokens are minted with version 1
    must_change: bool = False
    two_fa: bool = True


#: Every staff user of the seeded database; the key is the username and the token-state name.
STAFF: dict[str, StaffSpec] = {
    "admin": StaffSpec(_uid(0x101), UserRole.ADMIN),
    "operations_admin": StaffSpec(_uid(0x102), UserRole.OPERATIONS_ADMIN),
    "technician": StaffSpec(_uid(0x103), UserRole.TECHNICIAN),
    "site_manager": StaffSpec(_uid(0x104), UserRole.SITE_MANAGER),
    "accounting": StaffSpec(_uid(0x105), UserRole.ACCOUNTING),
    "employee": StaffSpec(_uid(0x106), UserRole.EMPLOYEE),
    "deactivated_technician": StaffSpec(_uid(0x107), UserRole.TECHNICIAN, active=False),
    "revoked_staff": StaffSpec(_uid(0x108), UserRole.TECHNICIAN, db_version=2),
    "deactivated_site_manager": StaffSpec(_uid(0x109), UserRole.SITE_MANAGER, active=False),
    "technician_must_change": StaffSpec(_uid(0x10A), UserRole.TECHNICIAN, must_change=True),
    "operations_admin_must_change": StaffSpec(_uid(0x10B), UserRole.OPERATIONS_ADMIN, must_change=True),
    "admin_must_change": StaffSpec(_uid(0x10C), UserRole.ADMIN, must_change=True),
    "admin_2fa_owed": StaffSpec(_uid(0x10D), UserRole.ADMIN, two_fa=False),
    "site_manager_must_change": StaffSpec(_uid(0x10E), UserRole.SITE_MANAGER, must_change=True),
    # Not a token state: the user whose role and activity the next-request tests change.
    "mover": StaffSpec(_uid(0x1FF), UserRole.TECHNICIAN),
}


@dataclass(frozen=True)
class CustomerSpec:
    id: uuid.UUID
    phone: str
    name: str
    active: bool = True
    db_version: int = 1
    must_change: bool = False


CUSTOMERS: dict[str, CustomerSpec] = {
    "owner": CustomerSpec(_uid(0x201), "0505556677", "Avi Cohen"),
    "inactive": CustomerSpec(_uid(0x202), "0505550002", "Inactive Ilan", active=False),
    "revoked": CustomerSpec(_uid(0x203), "0505550003", "Revoked Rina", db_version=2),
    "must_change": CustomerSpec(_uid(0x204), "0505550004", "Fresh Fanny", must_change=True),
    "inactive_must_change": CustomerSpec(
        _uid(0x205), "0505550005", "Both Boaz", active=False, must_change=True
    ),
    # The customer the staff routes act on, and who owns the "someone else's" ticket 10001.
    "target": CustomerSpec(_uid(0x206), "0501234567", "Dana Levi"),
}
OWNER = CUSTOMERS["owner"]
TARGET = CUSTOMERS["target"]
CUSTOMER_PASSWORD = "4321"

CAT_A, CAT_B, CAT_C = _uid(0x301), _uid(0x302), _uid(0x303)  # A used by both tickets, B spare, C inactive
TICKET_OWN, TICKET_OTHER = 10000, 10001  # the owner's ticket and the target customer's
TICKET_OWN_ID, TICKET_OTHER_ID = _uid(0x401), _uid(0x402)
ENTRY_ID = _uid(0x501)
MISSING_UUID = _uid(0x9999)
MISSING_TICKET = 99999

#: Text that is ticket or customer data. None of it may appear in a 401 or 403 body.
SENTINELS = (
    "leaking tap",
    "Haifa",
    "Avi Cohen",
    "Dana Levi",
    "0505556677",
    "0501234567",
    "10000",
    "10001",
    "Electric",
    "Plumbing",
    "Moshe",
)


@cache
def _customer_password_hash() -> str:
    return hash_password(CUSTOMER_PASSWORD)


# --------------------------------------------------------------------------- token states


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(days=2)


def _staff_token(key: str, *, expired: bool = False, role_claim: str | None = None) -> str:
    spec = STAFF[key]
    return create_access_token(
        user_id=spec.id,
        token_version=1,
        role=role_claim or spec.role.value,
        now=_past() if expired else None,
    )


def _customer_token(key: str, *, expired: bool = False) -> str:
    return mint_customer_token(CUSTOMERS[key].id, 1, now=_past() if expired else None)


def _forged(token: str) -> str:
    """The same claims signed with another key: a well-formed token whose signature is wrong."""
    claims = jwt.decode(token, options={"verify_signature": False})
    return jwt.encode(claims, "not-the-key-" * 4, algorithm="HS256")


#: Token state -> the bearer token it presents (`None`: no Authorization header).
def _token_for(state: str) -> str | None:
    fixed: dict[str, Callable[[], str | None]] = {
        "absent": lambda: None,
        "expired_customer": lambda: _customer_token("owner", expired=True),
        "expired_staff": lambda: _staff_token("admin", expired=True),
        "bad_signature_customer": lambda: _forged(_customer_token("owner")),
        "bad_signature_staff": lambda: _forged(_staff_token("admin")),
        "staff_refresh_token": lambda: create_refresh_token(user_id=STAFF["admin"].id, token_version=1)[0],
        "revoked_customer": lambda: _customer_token("revoked"),
        "inactive_customer": lambda: _customer_token("inactive"),
        "inactive_and_must_change_customer": lambda: _customer_token("inactive_must_change"),
        "customer": lambda: _customer_token("owner"),
        "customer_must_change": lambda: _customer_token("must_change"),
    }
    if state in fixed:
        return fixed[state]()
    return _staff_token(state)


def _bearer(token: str | None) -> dict[str, str] | None:
    return None if token is None else {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- the reference matrix
#
# Written by hand from the requirements, one row per token state, one column per endpoint class:
#   s: ticket staff   c: configuration staff   k: customer   u: customer, ungated   p: shared photo
# `None` means admitted. The classes are: R1.2 (staff endpoints take an active admin, operations admin or
# technician), R1.3 (customer endpoints take an active customer), R1.4 (photo: owner or ticket staff),
# R1.5 (other Hours roles: 403), R1.6 (customer token on a staff endpoint: 403), R1.7 (staff token on a
# customer endpoint: 403), R1.8 (no token, expired, bad signature, session ended, inactive customer: 401),
# R1.10 and R1.11 (a technician on a configuration endpoint: 403), R2.5 (deactivated technician: the 401
# `invalid_token` of Hours), R6.1 (a customer who owes a password change: 403 except on the ungated
# routes). Staff who owe a password change or an administrator who owes 2FA are the design's gates
# ("Staff dependencies"): a non-administrator owing a password change and an administrator owing
# enrolment are refused after the role check.

Outcome = tuple[int, str] | None

NOAUTH: Outcome = (401, "not_authenticated")
INVALID: Outcome = (401, "invalid_token")
CUSTOMER_AS_STAFF: Outcome = (403, "customer_token_not_accepted")
STAFF_AS_CUSTOMER: Outcome = (403, "staff_token_not_accepted")
ROLE: Outcome = (403, "insufficient_role")
PASSWORD: Outcome = (403, "password_change_required")
TOTP: Outcome = (403, "totp_enrolment_required")
ADMIT: Outcome = None


class Row(NamedTuple):
    s: Outcome
    c: Outcome
    k: Outcome
    u: Outcome
    p: Outcome

    def of(self, klass: str) -> Outcome:
        return {
            TICKET_STAFF: self.s,
            CONFIG_STAFF: self.c,
            CUSTOMER: self.k,
            CUSTOMER_UNGATED: self.u,
            SHARED_PHOTO: self.p,
        }[klass]


def _all(outcome: Outcome) -> Row:
    return Row(outcome, outcome, outcome, outcome, outcome)


_STAFF_ONLY_ROLES = Row(s=ROLE, c=ROLE, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=ROLE)
_CONFIG_OR_TICKET = Row(s=ADMIT, c=ADMIT, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=ADMIT)

REFERENCE: dict[str, Row] = {
    # --- nothing usable: authentication fails first, whatever the endpoint
    "absent": _all(NOAUTH),
    "expired_customer": _all(INVALID),
    "expired_staff": _all(INVALID),
    "bad_signature_customer": _all(INVALID),
    "bad_signature_staff": _all(INVALID),
    "staff_refresh_token": _all(INVALID),
    # --- a customer token whose session ended, or whose customer is inactive: 401 on every endpoint
    # (R1.8; authentication is checked before the kind of token, R1.14).
    "revoked_customer": _all(INVALID),
    "inactive_customer": _all(INVALID),
    "inactive_and_must_change_customer": _all(INVALID),
    # --- a valid customer token
    "customer": Row(s=CUSTOMER_AS_STAFF, c=CUSTOMER_AS_STAFF, k=ADMIT, u=ADMIT, p=ADMIT),
    "customer_must_change": Row(s=CUSTOMER_AS_STAFF, c=CUSTOMER_AS_STAFF, k=PASSWORD, u=ADMIT, p=PASSWORD),
    # --- a valid staff token of each of the six roles
    "admin": _CONFIG_OR_TICKET,
    "operations_admin": _CONFIG_OR_TICKET,
    "technician": Row(s=ADMIT, c=ROLE, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=ADMIT),
    "site_manager": _STAFF_ONLY_ROLES,
    "accounting": _STAFF_ONLY_ROLES,
    "employee": _STAFF_ONLY_ROLES,
    # --- a deactivated or revoked staff user: the 401 of Hours (R2.5), on every endpoint
    "deactivated_technician": _all(INVALID),
    "revoked_staff": _all(INVALID),
    "deactivated_site_manager": _all(INVALID),
    # --- forced password change and 2FA enrolment, after the role check (R1.14)
    "technician_must_change": Row(s=PASSWORD, c=ROLE, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=PASSWORD),
    "operations_admin_must_change": Row(
        s=PASSWORD, c=PASSWORD, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=PASSWORD
    ),
    "admin_must_change": _CONFIG_OR_TICKET,  # an administrator is exempt from the forced change
    "admin_2fa_owed": Row(s=TOTP, c=TOTP, k=STAFF_AS_CUSTOMER, u=STAFF_AS_CUSTOMER, p=TOTP),
    "site_manager_must_change": _STAFF_ONLY_ROLES,
}

#: The six Hours roles and what each may do on the ticket staff and configuration routes (R1.2, R1.5,
#: R1.9, R1.10). Used by the role-change test.
ROLE_ACCESS: dict[UserRole, tuple[bool, bool]] = {
    UserRole.ADMIN: (True, True),
    UserRole.OPERATIONS_ADMIN: (True, True),
    UserRole.TECHNICIAN: (True, False),
    UserRole.SITE_MANAGER: (False, False),
    UserRole.ACCOUNTING: (False, False),
    UserRole.EMPLOYEE: (False, False),
}

# --------------------------------------------------------------------------- findings

_D1 = (
    "FINDING D1 (R1.8, R1.14): a customer token whose session ended or whose customer is inactive "
    "is presented to a staff endpoint. R1.8 says 401 and authentication is checked first, but "
    "`staff_principal` answers 403 `customer_token_not_accepted` because `is_valid_customer_token` "
    "checks only the signature, never the customer row."
)
_D2 = (
    "FINDING D2 (R2.5, R1.14; R1.7 conflicts): a deactivated or revoked staff user's token is "
    "presented to a customer endpoint. R2.5 says every request of a deactivated technician is "
    "rejected with Hours' 401 `invalid_token`, but `_resolve_customer` answers 403 "
    "`staff_token_not_accepted` because it checks only the token signature and reads no `users` row."
)
_D3 = (
    "FINDING D3 (R1.14, R1.11): a technician who owes a password change calls a configuration "
    "endpoint. The role check comes before the forced password change, so the answer must be 403 "
    "`insufficient_role`; `config_staff_principal` runs after `staff_principal` and answers 403 "
    "`password_change_required`."
)

_D4 = (
    "FINDING D4 (R1.14): a request body that is not parseable JSON is a validation failure, which R1.14 "
    "puts after authentication, role and forced password change. FastAPI reads the body before it runs "
    "any dependency, so such a request gets 422 `invalid_request` even from a caller with no token or the "
    "wrong role. Nothing is read or changed; only the status and code differ from the required order."
)

#: (token state, endpoint class) -> why the required behavior is not what the code does today.
DEVIATIONS: dict[tuple[str, str], str] = {}
for _state in ("revoked_customer", "inactive_customer", "inactive_and_must_change_customer"):
    for _klass in (TICKET_STAFF, CONFIG_STAFF):
        DEVIATIONS[(_state, _klass)] = _D1
for _state in ("deactivated_technician", "revoked_staff", "deactivated_site_manager"):
    for _klass in (CUSTOMER, CUSTOMER_UNGATED):
        DEVIATIONS[(_state, _klass)] = _D2
DEVIATIONS[("technician_must_change", CONFIG_STAFF)] = _D3


def _marks(state: str, klass: str) -> list[pytest.MarkDecorator]:
    reason = DEVIATIONS.get((state, klass))
    return [pytest.mark.xfail(strict=True, reason=reason)] if reason else []


# --------------------------------------------------------------------------- the probes


@dataclass(frozen=True)
class Probe:
    """One request per route of the endpoint table, with a body that succeeds for an admitted caller."""

    method: str
    path: str  # as in ENDPOINT_TABLE, relative to `/api/service`
    klass: str
    ok: int | None  # status when admitted; None: any status other than 401 and 403
    json: Any = None
    form: dict[str, str] | None = None
    values: dict[str, Any] = field(default_factory=dict)  # path parameter overrides

    @property
    def url(self) -> str:
        defaults = {
            "ticket_number": TICKET_OWN,
            "entry_id": ENTRY_ID,
            "customer_id": TARGET.id,
            "category_id": CAT_B,
        }
        return self.path.format(**{**defaults, **self.values})

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


GOOD_FORM = {
    "city": "Haifa",
    "apartment": "12",
    "phone": "050-555-6677",
    "category_id": str(CAT_A),
    "description": "The kitchen tap leaks.",
    "urgent": "false",
}
_PLAIN = {"name_he": "שונה", "name_en": "Renamed"}
_STATUS_OK = {
    "expected_status": "received",
    "target_status": "in_progress",
    "assigned_technician_name": "Moshe",
}

PROBES: tuple[Probe, ...] = (
    # --- customer, gated
    Probe("GET", "/form-config", CUSTOMER, 200),
    Probe("POST", "/tickets", CUSTOMER, 201, form=GOOD_FORM),
    Probe("GET", "/tickets", CUSTOMER, 200),
    Probe("GET", "/tickets/{ticket_number}", CUSTOMER, 200),
    Probe("POST", "/tickets/{ticket_number}/cancel", CUSTOMER, 200, json={"expected_status": "received"}),
    # --- customer, ungated
    Probe("POST", "/auth/sign-out", CUSTOMER_UNGATED, 204),
    Probe("GET", "/auth/me", CUSTOMER_UNGATED, 200),
    Probe(
        "POST",
        "/auth/change-password",
        CUSTOMER_UNGATED,
        200,
        json={"current_password": CUSTOMER_PASSWORD, "new_password": "5566"},
    ),
    # --- shared photo: left out of the matrix until the endpoint table drops its `pending` marker
    Probe("GET", "/tickets/{ticket_number}/photo", SHARED_PHOTO, None),
    # --- ticket staff
    Probe("GET", "/staff/dashboard", TICKET_STAFF, 200),
    Probe("GET", "/staff/tickets", TICKET_STAFF, 200),
    Probe("GET", "/staff/tickets/{ticket_number}", TICKET_STAFF, 200),
    Probe(
        "POST",
        "/staff/tickets/{ticket_number}/status",
        TICKET_STAFF,
        200,
        json=_STATUS_OK,
    ),
    Probe(
        "PUT",
        "/staff/tickets/{ticket_number}/assigned-technician",
        TICKET_STAFF,
        200,
        json={"assigned_technician_name": "Moshe"},
    ),
    Probe(
        "POST",
        "/staff/tickets/{ticket_number}/history",
        TICKET_STAFF,
        201,
        json={"entry_text": "Checked the tap.", "technician_name": "Moshe"},
    ),
    Probe(
        "PUT",
        "/staff/tickets/{ticket_number}/history/{entry_id}",
        TICKET_STAFF,
        200,
        json={"entry_text": "Checked the tap again.", "technician_name": "Moshe"},
    ),
    Probe("GET", "/staff/customers", TICKET_STAFF, 200),
    Probe(
        "POST",
        "/staff/customers",
        TICKET_STAFF,
        201,
        json={"phone": "052-111-2233", "full_name": "Noa Katz", "city": "Haifa", "apartment": "3"},
    ),
    Probe(
        "PUT",
        "/staff/customers/{customer_id}",
        TICKET_STAFF,
        200,
        json={"phone": "0501234567", "full_name": "Dana Levi", "city": "Haifa", "apartment": "4"},
    ),
    Probe("POST", "/staff/customers/{customer_id}/deactivate", TICKET_STAFF, 200),
    Probe("POST", "/staff/customers/{customer_id}/reactivate", TICKET_STAFF, 200),
    Probe("POST", "/staff/customers/{customer_id}/reset-password", TICKET_STAFF, 204),
    Probe("GET", "/staff/reports", TICKET_STAFF, 200),
    Probe("GET", "/staff/categories", TICKET_STAFF, 200),
    # --- configuration staff
    Probe("POST", "/staff/categories", CONFIG_STAFF, 201, json={"name_he": "קירות", "name_en": "Walls"}),
    Probe("PUT", "/staff/categories/{category_id}", CONFIG_STAFF, 200, json=_PLAIN),
    Probe("POST", "/staff/categories/{category_id}/deactivate", CONFIG_STAFF, 200),
    Probe(
        "POST", "/staff/categories/{category_id}/reactivate", CONFIG_STAFF, 200, values={"category_id": CAT_C}
    ),
    Probe("DELETE", "/staff/categories/{category_id}", CONFIG_STAFF, 200, values={"category_id": CAT_C}),
    Probe("GET", "/staff/settings", CONFIG_STAFF, 200),
    Probe(
        "PUT",
        "/staff/settings",
        CONFIG_STAFF,
        200,
        json={"description_required": True, "photo_upload_enabled": True},
    ),
    Probe("PUT", "/staff/labels", CONFIG_STAFF, 200, json={"labels": {"city": {"he": "עיר", "en": "City"}}}),
)

_PARAMETER = re.compile(r"\{[^}]*\}")


def _key(method: str, path: str) -> tuple[str, str]:
    return method, _PARAMETER.sub("{}", path)


_TABLE = {_key(entry.method, entry.path): entry for entry in ENDPOINT_TABLE}


def _active(probe: Probe) -> bool:
    entry = _TABLE.get(_key(probe.method, probe.path))
    return entry is not None and entry.pending is None


ACTIVE_PROBES = tuple(probe for probe in PROBES if _active(probe))
STAFF_PROBES = tuple(p for p in ACTIVE_PROBES if p.klass in (TICKET_STAFF, CONFIG_STAFF))


def _probe_id(probe: Probe) -> str:
    return probe.label


# --------------------------------------------------------------------------- database and worlds


def _new_engine() -> Engine:
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )


def _seed(session: Session) -> None:
    for key, spec in STAFF.items():
        session.add(
            User(
                id=spec.id,
                username=key,
                password_hash="not-a-hash",
                role=spec.role,
                is_active=spec.active,
                token_version=spec.db_version,
                must_change_password=spec.must_change,
                is_2fa_enabled=spec.two_fa,
            )
        )
    for key, spec in CUSTOMERS.items():
        session.add(
            Customer(
                id=spec.id,
                phone=spec.phone,
                full_name=spec.name,
                full_name_key=spec.name.casefold(),
                city="Haifa",
                apartment="12" if key == "owner" else "4",
                password_hash=_customer_password_hash(),
                must_change_password=spec.must_change,
                is_active=spec.active,
                token_version=spec.db_version,
            )
        )
    for seq, (cat_id, name, active) in enumerate(
        ((CAT_A, "Electric", True), (CAT_B, "Plumbing", True), (CAT_C, "Locks", False)), start=1
    ):
        session.add(
            Category(
                id=cat_id,
                seq=seq,
                name_he=f"{name}-he",
                name_en=name,
                name_he_key=f"{name}-he".casefold(),
                name_en_key=name.casefold(),
                is_active=active,
            )
        )
    session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
    session.add_all(
        FieldLabel(field_key=key, label_he=f"{key}-he", label_en=f"{key}-en") for key in FIELD_LABEL_KEYS
    )
    tickets = ((TICKET_OWN_ID, TICKET_OWN, OWNER), (TICKET_OTHER_ID, TICKET_OTHER, TARGET))
    for ticket_id, number, owner in tickets:
        session.add(
            Ticket(
                id=ticket_id,
                ticket_number=number,
                customer_id=owner.id,
                category_id=CAT_A,
                city="Haifa",
                city_key="haifa",
                apartment="12",
                phone=owner.phone,
                description="leaking tap",
                status="received",
                assigned_technician_name="",
                created_at=LONG_AGO,
                created_date=LONG_AGO.date(),
                updated_at=LONG_AGO,
            )
        )
    session.add(
        HistoryEntry(
            id=ENTRY_ID,
            ticket_id=TICKET_OWN_ID,
            entry_text="Looked at it.",
            technician_name="Moshe",
            created_at=LONG_AGO,
        )
    )
    session.commit()


@pytest.fixture(scope="module")
def template() -> Iterator[Engine]:
    engine = _new_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as session:
        _seed(session)
        # Stand-in for `svc_ticket_number_seq`, which the dashboard reads for configuration staff.
        session.execute(text("CREATE TABLE svc_ticket_number_seq (last_value INTEGER, is_called BOOLEAN)"))
        session.execute(text("INSERT INTO svc_ticket_number_seq VALUES (10001, 1)"))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `Category.seq` of categories the tests create, as PostgreSQL's identity column does."""
    counters: dict[Engine, Iterator[int]] = {}  # one count per database, so two worlds number alike

    def assign(_mapper: Any, connection: Any, target: Category) -> None:
        if target.seq is None:
            target.seq = next(counters.setdefault(connection.engine, itertools.count(1000)))

    event.listen(Category, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Category, "before_insert", assign)


class Numbers:
    """The ticket-number allocator of the tests: counts the numbers it hands out."""

    def __init__(self) -> None:
        self.next = 20000
        self.calls = 0

    def __call__(self, _session: Session) -> int:
        self.calls += 1
        self.next += 1
        return self.next - 1


def _enabled_config() -> ServiceConfig:
    names = (
        "service_customer_token_ttl_seconds service_signin_rate_max service_signin_rate_window_seconds "
        "service_submit_rate_max service_submit_rate_window_seconds service_request_rate_max "
        "service_request_rate_window_seconds service_signin_lock_attempts service_signin_lock_seconds "
        "service_client_ip_header service_trusted_proxy_cidrs service_accessibility_contact"
    ).split()
    values = dict.fromkeys(names, "") | {"service_tickets_enabled": "true"}
    return ServiceConfig.from_settings(SimpleNamespace(**values))  # type: ignore[arg-type]


#: Tables whose rows must not change on a 401 or 403: every ticket table, the staff users the
#: principals read, and the Hours audit log.
_WATCHED = tuple(
    table.name
    for table in Base.metadata.sorted_tables
    if table.name.startswith("svc_") or table.name in ("users", "change_logs")
)


class World:
    """A seeded database copy, the application on top of it, and a client."""

    def __init__(self, template_engine: Engine) -> None:
        self.engine = _new_engine()
        source, target = template_engine.raw_connection(), self.engine.raw_connection()
        try:
            source.driver_connection.backup(target.driver_connection)  # type: ignore[union-attr]
        finally:
            source.close()
            target.close()
        self.factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, future=True)
        self.numbers = Numbers()
        config = _enabled_config()
        app = FastAPI()
        mount(app, config, "/api")
        app.dependency_overrides[get_db] = self._request_session
        app.dependency_overrides[customer_router.get_ticket_allocator] = lambda: self.numbers
        app.dependency_overrides[customer_router.get_service_config] = lambda: config
        self.client = TestClient(app, raise_server_exceptions=False)

    def _request_session(self) -> Iterator[Session]:
        with self.factory() as per_request:
            yield per_request

    def close(self) -> None:
        self.client.close()
        self.engine.dispose()

    # --- requests

    def headers(self, state: str) -> dict[str, str] | None:
        return _bearer(_token_for(state))

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        *,
        json: Any = None,
        form: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if form is not None:
            kwargs["files"] = [(name, (None, value)) for name, value in form.items()]
        if content is not None:
            kwargs["content"] = content
            headers = {**(headers or {}), "Content-Type": "application/json"}
        return self.client.request(method, SERVICE + path, headers=headers, **kwargs)

    def send(self, probe: Probe, headers: dict[str, str] | None) -> Any:
        return self.request(probe.method, probe.url, headers, json=probe.json, form=probe.form)

    # --- stored data

    def snapshot(self) -> dict[str, list[str]]:
        """Every row of every watched table, as sorted text."""
        with self.factory() as session:
            return {
                name: sorted(repr(tuple(row)) for row in session.execute(text(f"SELECT * FROM {name}")).all())
                for name in _WATCHED
            }

    def update_user(self, key: str, **values: Any) -> None:
        with self.factory() as session:
            session.execute(update(User).where(User.id == STAFF[key].id).values(**values))
            session.commit()

    def ticket_status(self, number: int) -> str:
        with self.factory() as session:
            return session.execute(select(Ticket.status).where(Ticket.ticket_number == number)).scalar_one()


@pytest.fixture
def make_world(template: Engine) -> Iterator[Callable[[], World]]:
    made: list[World] = []

    def _make() -> World:
        world = World(template)
        made.append(world)
        return world

    try:
        yield _make
    finally:
        for world in made:
            world.close()


@pytest.fixture
def world(make_world: Callable[[], World]) -> World:
    return make_world()


# --------------------------------------------------------------------------- reading responses


def _body(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _code(response: Any) -> str | None:
    body = _body(response)
    try:
        return body["detail"]["error"]["code"]
    except (KeyError, TypeError):
        return None


def _outcome(response: Any) -> tuple[int, str | None]:
    return response.status_code, _code(response)


def _assert_no_ticket_data(response: Any) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in response.text, f"{sentinel!r} leaked into {response.text}"


def _assert_rejected(response: Any, expected: tuple[int, str]) -> None:
    assert _outcome(response) == expected
    assert _body(response) == {"detail": {"error": {"code": expected[1]}}}
    _assert_no_ticket_data(response)


def _assert_admitted(response: Any, probe: Probe) -> None:
    status = response.status_code
    if probe.ok is None:
        assert status not in (401, 403), _outcome(response)
    else:
        assert status == probe.ok, (_outcome(response), response.text[:300])
    assert _code(response) not in {code for _, code in (NOAUTH, INVALID, CUSTOMER_AS_STAFF, STAFF_AS_CUSTOMER,
                                                          ROLE, PASSWORD, TOTP)}


# =========================================================================== guards of the matrix


def test_the_reference_has_a_row_for_every_state_and_every_class():
    assert set(REFERENCE) >= {"absent", "customer", "admin", "technician"}
    # The six Hours roles each have a valid token state.
    assert {STAFF[state].role for state in REFERENCE if state in STAFF} == set(UserRole)
    for row in REFERENCE.values():
        for klass in (TICKET_STAFF, CONFIG_STAFF, CUSTOMER, CUSTOMER_UNGATED, SHARED_PHOTO):
            row.of(klass)
    # Every state the reference names can be turned into a token.
    for state in REFERENCE:
        assert _token_for(state) is None or isinstance(_token_for(state), str)


def test_every_listed_route_has_exactly_one_probe_of_the_right_class():
    """A new route in ENDPOINT_TABLE without a probe here, or with another class, fails this test."""
    probe_keys = [_key(p.method, p.path) for p in PROBES]
    assert len(probe_keys) == len(set(probe_keys)), "two probes for one route"
    for entry in ENDPOINT_TABLE:
        if entry.klass == UNAUTHENTICATED:
            continue
        probe = next((p for p in PROBES if _key(p.method, p.path) == _key(entry.method, entry.path)), None)
        assert probe is not None, f"{entry.method} {entry.path} has no probe in test_service_access_matrix.py"
        assert probe.klass == entry.klass, (entry.method, entry.path, probe.klass, entry.klass)
    for probe in PROBES:
        assert _key(probe.method, probe.path) in _TABLE, f"{probe.label} is not in ENDPOINT_TABLE"


def test_the_matrix_covers_every_class_that_exists():
    seen = {probe.klass for probe in ACTIVE_PROBES}
    # The photo class joins when its endpoint-table marker goes (task 16.4).
    assert seen >= {CUSTOMER, CUSTOMER_UNGATED, TICKET_STAFF, CONFIG_STAFF}
    assert (SHARED_PHOTO in seen) == any(
        e.klass == SHARED_PHOTO and e.pending is None for e in ENDPOINT_TABLE
    )
    assert len(ACTIVE_PROBES) >= 30


def test_the_seeded_world_admits_a_valid_customer_and_a_valid_administrator(world: World):
    """The seeded data and the harness work, so a refusal in the matrix is the matrix's doing."""
    assert world.request("GET", "/form-config", world.headers("customer")).status_code == 200
    assert world.request("GET", "/staff/dashboard", world.headers("operations_admin")).status_code == 200


# =========================================================================== the matrix

MATRIX_CELLS = [
    pytest.param(state, probe, id=f"{state}|{probe.label}", marks=_marks(state, probe.klass))
    for state in REFERENCE
    for probe in ACTIVE_PROBES
]


@pytest.mark.parametrize(("state", "probe"), MATRIX_CELLS)
def test_access_matrix(world: World, state: str, probe: Probe) -> None:
    """R1.2 to R1.11: status and code per token state and class; 401 and 403 show and change nothing."""
    expected = REFERENCE[state].of(probe.klass)
    before = world.snapshot()

    response = world.send(probe, world.headers(state))

    if expected is None:
        _assert_admitted(response, probe)
        return
    _assert_rejected(response, expected)
    assert world.snapshot() == before
    assert world.numbers.calls == 0, "a refused request took a ticket number"


# =========================================================================== check order (R1.14)


class Req(NamedTuple):
    method: str
    path: str
    json: Any = None
    form: dict[str, str] | None = None


@dataclass(frozen=True)
class OrderRoute:
    """A route with validation and/or ownership steps, and the request that fails each of them.

    `v`, `o` and `vo` are the requests whose only failures are validation, ownership, and both. The
    expected answers are the 422 and 404 of the route; `vo` must give the validation answer.
    """

    name: str
    klass: str
    v_code: str | None = None
    o_code: str | None = None
    v: Req | None = None
    o: Req | None = None
    vo: Req | None = None

    def variants(self) -> list[tuple[str, Req, tuple[int, str]]]:
        found: list[tuple[str, Req, tuple[int, str]]] = []
        if self.v is not None and self.v_code is not None:
            found.append(("validation", self.v, (422, self.v_code)))
        if self.o is not None and self.o_code is not None:
            found.append(("ownership", self.o, (404, self.o_code)))
        if self.vo is not None and self.v_code is not None:
            found.append(("validation+ownership", self.vo, (422, self.v_code)))  # validation wins
        return found


_CANCEL_OK = {"expected_status": "received"}
_CANCEL_BAD = {"expected_status": "bogus"}
_STATUS_BAD = {"expected_status": "bogus", "target_status": "closed"}
_CUSTOMER_OK = {"phone": "0501234567", "full_name": "Dana Levi"}
_CUSTOMER_BAD = {"phone": "12", "full_name": "Dana Levi"}

ORDER_ROUTES: tuple[OrderRoute, ...] = (
    OrderRoute(
        "customer cancel",
        CUSTOMER,
        v_code="invalid_expected_status",
        o_code="ticket_not_found",
        v=Req("POST", f"/tickets/{TICKET_OWN}/cancel", _CANCEL_BAD),
        o=Req("POST", f"/tickets/{TICKET_OTHER}/cancel", _CANCEL_OK),
        vo=Req("POST", f"/tickets/{TICKET_OTHER}/cancel", _CANCEL_BAD),
    ),
    OrderRoute(
        "customer ticket list",
        CUSTOMER,
        v_code="invalid_page",
        v=Req("GET", "/tickets?page=abc"),
    ),
    OrderRoute(
        "customer ticket detail",
        CUSTOMER,
        o_code="ticket_not_found",
        o=Req("GET", f"/tickets/{MISSING_TICKET}"),
    ),
    OrderRoute(
        "customer submit",
        CUSTOMER,
        v_code="invalid_category",
        v=Req("POST", "/tickets", form={**GOOD_FORM, "category_id": "not-a-uuid"}),
    ),
    OrderRoute(
        "staff status",
        TICKET_STAFF,
        v_code="invalid_expected_status",
        o_code="ticket_not_found",
        v=Req("POST", f"/staff/tickets/{TICKET_OWN}/status", _STATUS_BAD),
        o=Req("POST", f"/staff/tickets/{MISSING_TICKET}/status", _STATUS_OK),
        vo=Req("POST", f"/staff/tickets/{MISSING_TICKET}/status", _STATUS_BAD),
    ),
    OrderRoute(
        "staff ticket list",
        TICKET_STAFF,
        v_code="invalid_page",
        v=Req("GET", "/staff/tickets?page=abc"),
    ),
    OrderRoute(
        "staff ticket detail",
        TICKET_STAFF,
        o_code="ticket_not_found",
        o=Req("GET", f"/staff/tickets/{MISSING_TICKET}"),
    ),
    OrderRoute(
        "staff customer update",
        TICKET_STAFF,
        v_code="invalid_phone",
        o_code="customer_not_found",
        v=Req("PUT", f"/staff/customers/{TARGET.id}", _CUSTOMER_BAD),
        o=Req("PUT", f"/staff/customers/{MISSING_UUID}", _CUSTOMER_OK),
        vo=Req("PUT", f"/staff/customers/{MISSING_UUID}", _CUSTOMER_BAD),
    ),
    OrderRoute(
        "config category rename",
        CONFIG_STAFF,
        v_code="invalid_name_en",
        o_code="category_not_found",
        v=Req("PUT", f"/staff/categories/{CAT_B}", {"name_he": "שונה"}),
        o=Req("PUT", f"/staff/categories/{MISSING_UUID}", _PLAIN),
        vo=Req("PUT", f"/staff/categories/{MISSING_UUID}", {"name_he": "שונה"}),
    ),
)

#: Token states combined with every request variant. Each stage of R1.14 is represented, and so are
#: the tokens that fail at two stages at once.
ORDER_STATES = (
    "absent",
    "expired_customer",
    "revoked_customer",
    "inactive_and_must_change_customer",
    "customer",
    "customer_must_change",
    "admin",
    "operations_admin",
    "technician",
    "site_manager",
    "deactivated_technician",
    "deactivated_site_manager",
    "technician_must_change",
    "site_manager_must_change",
    "operations_admin_must_change",
    "admin_2fa_owed",
)

ORDER_CELLS = [
    pytest.param(
        state,
        route,
        variant,
        id=f"{state}|{route.name}|{variant[0]}",
        marks=_marks(state, route.klass),
    )
    for state in ORDER_STATES
    for route in ORDER_ROUTES
    for variant in route.variants()
]


@pytest.mark.parametrize(("state", "route", "variant"), ORDER_CELLS)
def test_check_order(world: World, state: str, route: OrderRoute, variant: tuple[str, Req, tuple[int, str]]):
    """R1.14: authentication, role, forced password change, validation, ownership: the first failure wins.

    A rejected caller gets the matrix answer whatever is wrong with the request; an admitted caller gets
    the validation answer when the request is invalid, even if the ticket is also missing, and the
    ownership answer only when the request is valid.
    """
    _, request, request_failure = variant
    gate = REFERENCE[state].of(route.klass)
    expected = gate if gate is not None else request_failure
    before = world.snapshot()

    response = world.request(
        request.method, request.path, world.headers(state), json=request.json, form=request.form
    )

    assert _outcome(response) == expected
    if gate is not None:
        _assert_rejected(response, gate)
        assert world.snapshot() == before
        assert world.numbers.calls == 0
    else:
        assert world.snapshot() == before, "a failed request changed data"


@pytest.mark.parametrize("route_name", ["customer cancel", "customer submit", "config category rename"])
def test_every_order_route_is_valid_for_an_admitted_caller(world: World, route_name: str):
    """The 'valid' twin of each request is accepted, so the variants above fail only for their reason."""
    valid = {
        "customer cancel": ("customer", "POST", f"/tickets/{TICKET_OWN}/cancel", _CANCEL_OK, None, 200),
        "customer submit": ("customer", "POST", "/tickets", None, GOOD_FORM, 201),
        "config category rename": ("admin", "PUT", f"/staff/categories/{CAT_B}", _PLAIN, None, 200),
    }
    state, method, path, body, form, status = valid[route_name]

    assert world.request(method, path, world.headers(state), json=body, form=form).status_code == status


MALFORMED = b'{"expected_status": '


def _malformed_marks(state: str, klass: str) -> list[pytest.MarkDecorator]:
    """Finding D4 applies where a gate should have answered first."""
    marks = _marks(state, klass)
    if REFERENCE[state].of(klass) is not None:
        marks.append(pytest.mark.xfail(strict=True, reason=_D4))
    return marks


MALFORMED_CASES = [
    # (state, method, path, class) of a request whose JSON body cannot be parsed at all.
    pytest.param(
        state,
        method,
        path,
        klass,
        id=f"{state}|{method} {path}",
        marks=_malformed_marks(state, klass),
    )
    for state in (
        "absent",
        "customer",
        "technician",
        "site_manager",
        "technician_must_change",
        "operations_admin",
    )
    for method, path, klass in (
        ("POST", f"/staff/tickets/{TICKET_OWN}/status", TICKET_STAFF),
        ("PUT", f"/staff/categories/{CAT_B}", CONFIG_STAFF),
        ("POST", f"/tickets/{TICKET_OWN}/cancel", CUSTOMER),
    )
]


@pytest.mark.parametrize(("state", "method", "path", "klass"), MALFORMED_CASES)
def test_an_unparseable_body_is_checked_after_authentication_role_and_password(
    world: World, state: str, method: str, path: str, klass: str
):
    """R1.14: a body that is not JSON is a validation failure, so a failed gate must answer first."""
    gate = REFERENCE[state].of(klass)
    before = world.snapshot()

    response = world.request(method, path, world.headers(state), content=MALFORMED)

    if gate is None:
        assert response.status_code == 422
    else:
        _assert_rejected(response, gate)
    assert world.snapshot() == before


# =========================================================================== admin = operations admin (R1.12)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_BCRYPT = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")


def _masked(text_value: str) -> str:
    """Generated ids, clock times and salted hashes differ between two runs by nature; the rest must not."""
    return _BCRYPT.sub("<hash>", _TIME.sub("<time>", _UUID.sub("<id>", text_value)))


def _masked_snapshot(world: World) -> dict[str, list[str]]:
    return {name: sorted(_masked(row) for row in rows) for name, rows in world.snapshot().items()}


EQUALITY_REQUESTS: list[Any] = [
    pytest.param(probe.method, probe.url, probe.json, probe.form, id=probe.label) for probe in STAFF_PROBES
] + [
    pytest.param(
        req.method, req.path, req.json, req.form, id=f"{route.name} {kind}"
    )
    for route in ORDER_ROUTES
    if route.klass in (TICKET_STAFF, CONFIG_STAFF)
    for kind, req, _ in route.variants()
]


@pytest.mark.parametrize(("method", "path", "body", "form"), EQUALITY_REQUESTS)
def test_admin_and_operations_admin_get_identical_responses(
    make_world: Callable[[], World], method: str, path: str, body: Any, form: Any
):
    """R1.12: same request, same stored data: same status, same body, same resulting data."""
    admin_world, ops_world = make_world(), make_world()

    as_admin = admin_world.request(method, path, admin_world.headers("admin"), json=body, form=form)
    as_ops = ops_world.request(method, path, ops_world.headers("operations_admin"), json=body, form=form)

    assert as_admin.status_code == as_ops.status_code
    assert _masked(as_admin.text) == _masked(as_ops.text)
    assert as_admin.status_code not in (401, 403)
    assert _masked_snapshot(admin_world) == _masked_snapshot(ops_world)


# ======================================================================== next-request effects (R2.5, R2.7)

_DASHBOARD = Probe("GET", "/staff/dashboard", TICKET_STAFF, 200)
_SETTINGS = Probe("GET", "/staff/settings", CONFIG_STAFF, 200)


def _expect_role(world: World, token: str, role: UserRole) -> None:
    can_staff, can_config = ROLE_ACCESS[role]
    for probe, allowed in ((_DASHBOARD, can_staff), (_SETTINGS, can_config)):
        response = world.send(probe, _bearer(token))
        if allowed:
            assert response.status_code == 200, (role, probe.label, _outcome(response))
        else:
            assert _outcome(response) == (403, "insufficient_role"), (role, probe.label)
            _assert_no_ticket_data(response)


@pytest.mark.parametrize(
    ("before", "after"),
    [(a, b) for a in UserRole for b in UserRole if a is not b],
    ids=lambda role: role.value,
)
def test_a_role_change_applies_on_the_next_request_with_the_older_token(
    world: World, before: UserRole, after: UserRole
):
    """R2.7: the role is read from the row on every request, never from the token's claim."""
    world.update_user("mover", role=before)
    token = _staff_token("mover", role_claim=before.value)  # minted while the user had the old role
    _expect_role(world, token, before)

    world.update_user("mover", role=after)

    _expect_role(world, token, after)  # the very next request, same token, new role


@pytest.mark.parametrize("role", [UserRole.TECHNICIAN, UserRole.OPERATIONS_ADMIN, UserRole.ADMIN])
@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"is_active": False, "token_version": 2},  # what the Hours Users page does
        {"token_version": 2},  # a password change or sign-out elsewhere
    ],
    ids=["deactivated", "deactivated-and-revoked", "revoked"],
)
def test_deactivation_applies_on_the_next_request_with_the_older_token(
    world: World, role: UserRole, change: dict
):
    """R2.5: every later request of a deactivated or revoked staff user is the 401 Hours gives."""
    world.update_user("mover", role=role)
    token = _staff_token("mover")
    assert world.send(_DASHBOARD, _bearer(token)).status_code == 200
    world.update_user("mover", **change)
    before = world.snapshot()

    for probe in STAFF_PROBES:
        response = world.send(probe, _bearer(token))
        _assert_rejected(response, (401, "invalid_token"))
        assert world.snapshot() == before, probe.label


# =========================================================================== customer sessions end (R1.8)


def _customer_headers(spec: CustomerSpec, version: int = 1) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_customer_token(spec.id, version)}"}


@pytest.mark.parametrize("action", ["deactivate", "reset-password"])
def test_a_customer_token_stops_working_when_staff_deactivate_or_reset(world: World, action: str):
    """R3.7, R3.9, R1.8: from the next request, with the older token, 401 and no ticket data."""
    token = _customer_headers(TARGET)
    assert world.request("GET", "/tickets", token).status_code == 200

    staff = world.request("POST", f"/staff/customers/{TARGET.id}/{action}", world.headers("operations_admin"))
    assert staff.status_code in (200, 204)

    for path in ("/tickets", "/auth/me", f"/tickets/{TICKET_OTHER}"):
        _assert_rejected(world.request("GET", path, token), (401, "invalid_token"))
    if action == "deactivate":
        # Reactivation does not revive tokens issued before the deactivation.
        reactivate = world.request(
            "POST", f"/staff/customers/{TARGET.id}/reactivate", world.headers("admin")
        )
        assert reactivate.status_code == 200
        _assert_rejected(world.request("GET", "/tickets", token), (401, "invalid_token"))


def test_change_password_and_sign_out_end_earlier_customer_tokens(world: World):
    """R6.3, R1.8: the changing token itself is refused afterwards; the new one works and is gate-free."""
    old = _customer_headers(CUSTOMERS["must_change"])
    _assert_rejected(world.request("GET", "/tickets", old), (403, "password_change_required"))

    changed = world.request(
        "POST",
        "/auth/change-password",
        old,
        json={"current_password": CUSTOMER_PASSWORD, "new_password": "5566"},
    )
    assert changed.status_code == 200
    new = {"Authorization": f"Bearer {changed.json()['access_token']}"}

    _assert_rejected(world.request("GET", "/auth/me", old), (401, "invalid_token"))
    assert world.request("GET", "/tickets", new).status_code == 200  # the gate is cleared (R6.1)

    assert world.request("POST", "/auth/sign-out", new).status_code == 204
    _assert_rejected(world.request("GET", "/tickets", new), (401, "invalid_token"))


# =========================================================================== sign-in ignores tokens (R1.3)


@pytest.mark.parametrize("state", list(REFERENCE))
def test_customer_sign_in_ignores_any_token_sent_with_it(world: World, state: str):
    """R1.3: sign-in accepts unauthenticated callers and ignores a token, whatever its state."""
    response = world.request(
        "POST",
        "/auth/sign-in",
        world.headers(state),
        json={"phone": OWNER.phone, "password": CUSTOMER_PASSWORD},
    )

    assert response.status_code == 200, _outcome(response)
    assert decode_customer_token(response.json()["access_token"]).customer_id == OWNER.id


# =========================================================================== R14.5


@pytest.mark.parametrize("target", ["received", "in_progress", "closed"])
def test_a_customer_cannot_set_a_staff_status(world: World, target: str):
    """R14.5: a customer asking for received, in progress or closed gets 403 and the ticket stays."""
    before = world.snapshot()

    response = world.request(
        "POST",
        f"/staff/tickets/{TICKET_OWN}/status",
        world.headers("customer"),
        json={"expected_status": "received", "target_status": target},
    )

    _assert_rejected(response, (403, "customer_token_not_accepted"))
    assert world.snapshot() == before
    assert world.ticket_status(TICKET_OWN) == "received"


def test_a_customer_can_only_ask_to_cancel_and_cannot_name_another_target(world: World):
    """R14.5: the customer route takes no target. An attempt to send one is refused and changes nothing."""
    before = world.snapshot()

    named = world.request(
        "POST",
        f"/tickets/{TICKET_OWN}/cancel",
        world.headers("customer"),
        json={"expected_status": "received", "target_status": "closed"},
    )

    assert _outcome(named) == (422, "unknown_field")
    assert world.snapshot() == before

    cancelled = world.request(
        "POST", f"/tickets/{TICKET_OWN}/cancel", world.headers("customer"), json=_CANCEL_OK
    )
    assert cancelled.status_code == 200
    assert world.ticket_status(TICKET_OWN) == "cancelled"
