"""The ticket system touches no Hours data (task 14.1; Property 28; T4 and T5).

Requirements 3.11, 16.11, 20.3, 20.4, 20.11 and 20.12. Two required tests call every route of the ticket
routers, successes and failures, customer side and staff side, through one fixtures table:

* `test_ticket_api_audit_log_count_unchanged` counts `change_logs` rows (and their ids) before and after
  the run and finds them equal (R20.4, R16.11).
* `test_ticket_api_sql_isolation` records every statement the run sends to the database with a
  `before_cursor_execute` listener and asserts, per request:
    - every write goes to an `svc_` table (R20.3, R3.11);
    - every read of a non-`svc_` table is the `users` row of the calling staff member, selected by primary
      key, once, and without `password_hash` or a TOTP column (R20.3, design "Staff identity");
    - a request with no valid staff token reads no non-`svc_` table at all;
    - no response (body or header) carries a value read from an existing table, other than a staff
      username copied into `assigned_technician_name` or a history entry's `technician_name` (R20.12);
    - the contents of every non-`svc_` table are identical after the run;
    - every object key the photo pipeline touches starts with `service-tickets/` (R20.11), see
      `object_store_keys`.

The fixtures table is keyed by (method, path template). A route of the ticket routers that has no
entry fails both tests, and so does an entry for a route that does not exist. The routes come from the
routers of `mount._ticket_routers()` (so a router added there is covered) and are compared with the
`ENDPOINT_TABLE` of `test_service_route_classification.py`, which also gives each route's principal
class: for every route the table adds the refusals of that class (no token, the other kind of token,
a role that may not use it) without any per-route code.

The harness is the one of `test_service_api_tickets_customer.py` and `test_service_api_staff_tickets.py`:
one small application with the routers of `mount`, `get_db` overridden to give each request its own
session on the in-memory test database, real tokens, an allocator override (SQLite has no sequence) and
the `Category.seq` numbering listener. SQLite foreign keys are switched on so that deleting a category a
ticket uses takes its deactivation path.

The photo route (task 16.4) is covered by the same table: its entry in `ENDPOINT_TABLE` has no `pending`
marker, `_explicit_fixtures` holds the delivery route and a multipart submission with a photo, and the
app of this file has an in-memory `FakePhotoStore` injected through `get_photo_store`. The store records
every key it is asked to put, get or delete (it has no list operation, as the real one has none), and
`object_store_keys` returns that record for the run.
"""

from __future__ import annotations

import itertools
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_service_api_photo import FakePhotoStore, jpeg_bytes, png_bytes
from test_service_route_classification import (
    CONFIG_STAFF,
    CUSTOMER,
    CUSTOMER_UNGATED,
    ENDPOINT_TABLE,
    SHARED_PHOTO,
    TICKET_STAFF,
)

from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.models.change_log import ChangeLog
from app.models.user import User, UserRole
from app.service_tickets import mount as ticket_mount
from app.service_tickets.api import customer_router
from app.service_tickets.config import ServiceConfig
from app.service_tickets.customer_token import mint_customer_token
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Branding,
    Category,
    Customer,
    FieldLabel,
    HistoryEntry,
    Locality,
    Part,
    ServiceSettings,
    Ticket,
    TicketPart,
)
from app.service_tickets.photo_store import get_photo_store, new_logo_key
from app.service_tickets.services.numbering import TicketNumbersExhausted

API = "/api/service"
PASSWORD = "4321"
PHOTO_PREFIX = "service-tickets/"

#: The only response fields in which a staff username may appear (R14.6, R16.4, R20.12, R26.4).
COPY_KEYS = frozenset({"assigned_technician_name", "technician_name", "opened_by_username"})

Key = tuple[str, str]  # (METHOD, path template relative to API with every parameter written `{}`)

_PARAM = re.compile(r"\{[^}]*\}")


def _key(method: str, template: str) -> Key:
    return (method.upper(), _PARAM.sub("{}", template))


# --------------------------------------------------------------------------------- harness


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `seq` of categories, settlements and parts on insert, as PostgreSQL's identity does."""
    counter = itertools.count(1)

    def assign(_mapper: Any, _connection: Any, target: Any) -> None:
        if target.seq is None:
            target.seq = next(counter)

    models = (Category, Locality, Part)
    for model in models:
        event.listen(model, "before_insert", assign)
    try:
        yield
    finally:
        for model in models:
            event.remove(model, "before_insert", assign)


@pytest.fixture
def foreign_keys_on(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.fixture
def sequence(session: Session) -> None:
    """Stand-in for `svc_ticket_number_seq`, so the dashboard's remaining-numbers read runs."""
    session.execute(text("CREATE TABLE svc_ticket_number_seq (last_value INTEGER, is_called BOOLEAN)"))
    session.execute(text("INSERT INTO svc_ticket_number_seq VALUES (99501, 1)"))  # 498 numbers left
    session.commit()


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


class Numbers:
    """The ticket-number allocator of the tests. `exhausted_once` makes the next call fail, once."""

    def __init__(self, first: int = 40000) -> None:
        self.next = first
        self.exhausted_once = False

    def __call__(self, _session: Session) -> int:
        if self.exhausted_once:
            self.exhausted_once = False
            raise TicketNumbersExhausted
        number = self.next
        self.next += 1
        return number


def _config() -> ServiceConfig:
    names = (
        "service_tickets_enabled service_customer_token_ttl_seconds service_signin_rate_max "
        "service_signin_rate_window_seconds service_submit_rate_max service_submit_rate_window_seconds "
        "service_request_rate_max service_request_rate_window_seconds service_signin_lock_attempts "
        "service_signin_lock_seconds service_client_ip_header service_trusted_proxy_cidrs "
        "service_accessibility_contact"
    ).split()
    return ServiceConfig.from_settings(SimpleNamespace(**dict.fromkeys(names, "")))  # type: ignore[arg-type]


def _build_app(session: Session, numbers: Numbers, photos: FakePhotoStore) -> FastAPI:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True)

    def request_session() -> Iterator[Session]:
        with factory() as per_request:
            yield per_request

    app = FastAPI()
    for router in ticket_mount._ticket_routers():
        app.include_router(router, prefix=API)
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[customer_router.get_ticket_allocator] = lambda: numbers
    app.dependency_overrides[customer_router.get_service_config] = _config
    app.dependency_overrides[get_photo_store] = lambda: photos
    return app


# --------------------------------------------------------------------------------- the world


class World:
    """Committed seed data that the fixtures table refers to by name, and the factories it calls."""

    def __init__(
        self,
        session: Session,
        make_user: Callable[..., Any],
        numbers: Numbers,
        hashed: str,
        photos: FakePhotoStore,
    ) -> None:
        self.session = session
        self.numbers = numbers
        self.photos = photos
        self._hash = hashed
        self._phones = itertools.count(1)
        self._ticket_numbers = itertools.count(20000)

        def staff(label: str, role: UserRole, **extra: Any) -> User:
            return make_user(
                role=role,
                username=f"hours-{label}-{uuid.uuid4().hex[:12]}",
                is_2fa_enabled=True,
                must_change_password=False,
                **extra,
            )

        # Hours users. `bystander` never acts: its username must never appear in any response.
        self.admin = staff("admin", UserRole.ADMIN)
        self.ops = staff("ops", UserRole.OPERATIONS_ADMIN)
        self.tech = staff("tech", UserRole.TECHNICIAN)
        self.bystander = staff("bystander", UserRole.TECHNICIAN)
        self.accounting = staff("accounting", UserRole.ACCOUNTING)
        self.gone = staff("gone", UserRole.TECHNICIAN, is_active=False)
        self.users = [self.admin, self.ops, self.tech, self.bystander, self.accounting, self.gone]

        # Ticket configuration, as the migration seeds it.
        session.add_all(
            FieldLabel(field_key=key, label_he=f"{key}-he", label_en=f"{key}-en") for key in FIELD_LABEL_KEYS
        )
        session.add(ServiceSettings(id=1, description_required=True, photo_upload_enabled=True))
        session.commit()
        self.cat_a = self.category("Plumbing")
        self.cat_b = self.category("Electrical")
        self.cat_c = self.category("General")
        self.cat_spare = self.category("Spare")
        self.cat_used = self.category("Used")
        self.cat_off = self.category("Dormant", active=False)

        # Customers.
        self.cust = self.customer("Avi Cohen")
        self.cust2 = self.customer("Dana Levi")
        self.signer = self.customer("Sign In")
        self.leaver = self.customer("Leaver")
        self.changer = self.customer("Changer", must_change=True)
        self.owing = self.customer("Owing", must_change=True)
        self.inactive = self.customer("Inactive", active=False)
        self.editable = self.customer("Editable")
        self.victim = self.customer("Victim")
        self.sleeper = self.customer("Sleeper", active=False)
        self.resettable = self.customer("Resettable")

        self.ticket("received", category=self.cat_used)  # makes `cat_used` a category a ticket uses

    # --- factories

    def category(self, name: str, *, active: bool = True) -> Category:
        row = Category(
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=active,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def named(self, model: type[Locality] | type[Part], name: str, *, active: bool = True) -> Any:
        """A settlement or a part. Created by the call that needs it, not seeded: while a settlement is
        active the ticket form changes, and the other routes' fixtures must see the form as it is."""
        row = model(
            name_he=f"{name}-he",
            name_en=name,
            name_he_key=f"{name}-he".casefold(),
            name_en_key=name.casefold(),
            is_active=active,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def used_named(self, model: type[Locality] | type[Part], name: str) -> Any:
        """A settlement a ticket uses, or a part a resolved ticket uses."""
        row = self.named(model, name)
        ticket = self.ticket("closed" if model is Part else "received")
        if model is Part:
            self.session.add(TicketPart(ticket_id=ticket.id, part_id=row.id))
        else:
            ticket.locality_id = row.id
        self.session.commit()
        return row

    def customer(self, name: str, *, must_change: bool = False, active: bool = True) -> Customer:
        row = Customer(
            phone=f"05055500{next(self._phones):02d}",
            full_name=name,
            full_name_key=name.casefold(),
            city="Haifa",
            apartment="12",
            password_hash=self._hash,
            must_change_password=must_change,
            is_active=active,
            token_version=1,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def ticket(
        self,
        status: str = "received",
        *,
        owner: Customer | None = None,
        category: Category | None = None,
        name: str = "",
        photo_key: str | None = None,
    ) -> Ticket:
        created = datetime(2025, 3, 1, 8, 0, tzinfo=UTC)
        row = Ticket(
            ticket_number=next(self._ticket_numbers),
            customer_id=(owner or self.cust).id,
            category_id=(category or self.cat_a).id,
            city="Haifa",
            city_key="haifa",
            apartment="12",
            phone="0505556677",
            description="The tap leaks.",
            photo_key=photo_key,
            urgent=False,
            status=status,
            assigned_technician_name=name,
            created_at=created,
            created_date=created.date(),
            updated_at=created,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def photo_ticket(self, owner: Customer | None = None, *, stored: bool = True) -> Ticket:
        """A ticket with a photo key. The object is in the fake store unless `stored` is false.

        `seed` puts the object in without recording a call, so the record of the run holds only keys
        the ticket routes themselves touched.
        """
        key = f"{PHOTO_PREFIX}{uuid.uuid4().hex}.jpg"
        if stored:
            self.photos.seed(key, jpeg_bytes())
        return self.ticket(owner=owner, photo_key=key)

    def set_logo(self, *, present: bool = True, stored: bool = True) -> None:
        """Make the company logo what a call needs: stored, a row key whose object is gone, or none."""
        key = new_logo_key() if present else None
        if key is not None and stored:
            self.photos.seed(key, png_bytes((40, 20)))
        row = self.session.get(Branding, 1, populate_existing=True)
        if row is None:
            row = Branding(id=1)
            self.session.add(row)
        row.logo_key = key
        self.session.commit()

    def entry(self, ticket: Ticket) -> HistoryEntry:
        row = HistoryEntry(
            ticket_id=ticket.id,
            entry_text="first note",
            technician_name="Moshe Tech",
            created_at=datetime(2025, 3, 1, 9, 0, tzinfo=UTC),
        )
        self.session.add(row)
        self.session.commit()
        return row

    # --- tokens

    def token(self, user: User) -> dict[str, str]:
        token = create_access_token(user_id=user.id, token_version=user.token_version, role=user.role.value)
        return {"Authorization": f"Bearer {token}"}

    def ctoken(self, customer: Customer, version: int | None = None) -> dict[str, str]:
        """A token of the customer's current token version (read now), or of `version`."""
        if version is None:
            self.session.rollback()
            version = self.session.execute(
                select(Customer.token_version).where(Customer.id == customer.id)
            ).scalar_one()
        return {"Authorization": f"Bearer {mint_customer_token(customer.id, version)}"}


@pytest.fixture
def numbers() -> Numbers:
    return Numbers()


@pytest.fixture
def photos() -> FakePhotoStore:
    return FakePhotoStore()


@pytest.fixture
def world(
    session: Session,
    make_user,
    numbers: Numbers,
    password_hash: str,
    photos: FakePhotoStore,
    sequence,
    foreign_keys_on,
) -> World:
    return World(session, make_user, numbers, password_hash, photos)


@pytest.fixture
def client(session: Session, numbers: Numbers, photos: FakePhotoStore, world: World) -> Iterator[TestClient]:
    with TestClient(_build_app(session, numbers, photos), raise_server_exceptions=False) as test_client:
        yield test_client


# --------------------------------------------------------------------------------- statement recorder


@dataclass(frozen=True)
class Statement:
    sql: str
    parameters: Any


class Recorder:
    """Collects every statement sent to the engine while a `window` is open."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._sink: list[Statement] | None = None
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        sink = self._sink
        if sink is not None:
            sink.append(Statement(statement, parameters))

    @contextmanager
    def window(self) -> Iterator[list[Statement]]:
        sink: list[Statement] = []
        self._sink = sink
        try:
            yield sink
        finally:
            self._sink = None

    def close(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)


@pytest.fixture
def recorder(sqlite_engine: Engine) -> Iterator[Recorder]:
    made = Recorder(sqlite_engine)
    try:
        yield made
    finally:
        made.close()


# --------------------------------------------------------------------------------- statement judge


@cache
def hours_tables() -> frozenset[str]:
    """Every table that is not a ticket table, from the mapped metadata (all models are imported)."""
    return frozenset(name.lower() for name in Base.metadata.tables if not name.startswith("svc_"))


_NAME = r'(?:"?\w+"?\.)?"?(\w+)"?'
_STRING = re.compile(r"'(?:[^']|'')*'")
_NEXTVAL = re.compile(r"\bnextval\(\s*'(?:\w+\.)?(\w+)'", re.IGNORECASE)
_WRITE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+{_NAME}",
        rf"\bREPLACE\s+INTO\s+{_NAME}",
        rf"(?<!FOR )(?<!DO )\bUPDATE\s+(?:OR\s+\w+\s+)?{_NAME}",
        rf"\bDELETE\s+FROM\s+{_NAME}",
        rf"\b(?:CREATE|DROP|ALTER|TRUNCATE)\s+(?:TEMP\w*\s+)?(?:TABLE|INDEX|SEQUENCE|VIEW)\s+"
        rf"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?{_NAME}",
    )
)
_DELETE_FROM = re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)
_SOURCE = re.compile(r"\b(?:FROM|JOIN)\s+", re.IGNORECASE)
_TABLE_REF = re.compile(rf'\s*{_NAME}(?:\s+AS\s+"?\w+"?)?', re.IGNORECASE)
_COMMA = re.compile(r"\s*,")
_CONTROL = re.compile(r"^\s*(?:SAVEPOINT|RELEASE|ROLLBACK|BEGIN|COMMIT)\b", re.IGNORECASE)
_VERB = re.compile(r"^\s*(\w+)")
_VERBS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"})
_BIND = r"(?:\?|%\(\w+\)s|:\w+|\$\d+)"
_USERS_BY_PK = re.compile(rf"\bWHERE\s+users\.id\s*=\s*{_BIND}\s*$", re.IGNORECASE)
_SECRET_COLUMNS = ("password_hash", "totp")


@dataclass(frozen=True)
class Shape:
    verb: str
    reads: frozenset[str]
    writes: frozenset[str]
    mentioned: frozenset[str]  # non-ticket tables named anywhere in the statement


def _table_list(sql: str, position: int) -> set[str]:
    names: set[str] = set()
    while True:
        found = _TABLE_REF.match(sql, position)
        if found is None:  # a subquery, which the scan reaches on its own
            return names
        names.add(found.group(1).lower())
        comma = _COMMA.match(sql, found.end())
        if comma is None:
            return names
        position = comma.end()


def inspect_sql(sql: str) -> Shape:
    writes = {match.group(1).lower() for match in _NEXTVAL.finditer(sql)}
    bare = _STRING.sub("''", sql)
    for pattern in _WRITE_PATTERNS:
        writes |= {m.group(1).lower() for m in pattern.finditer(bare) if m.group(1).upper() != "SET"}
    scan = _DELETE_FROM.sub("DELETE", bare)
    reads: set[str] = set()
    for source in _SOURCE.finditer(scan):
        reads |= _table_list(scan, source.end())
    verb = _VERB.match(sql)
    return Shape(
        verb=verb.group(1).upper() if verb else "",
        reads=frozenset(reads),
        writes=frozenset(writes),
        mentioned=frozenset(set(re.findall(r"\w+", bare.lower())) & hours_tables()),
    )


def _short(sql: str) -> str:
    flat = " ".join(sql.split())
    return flat if len(flat) <= 160 else flat[:157] + "..."


def judge(statement: Statement) -> tuple[list[str], bool]:
    """(violations of the isolation rules, whether this is the one allowed read of a `users` row)."""
    sql = statement.sql
    if _CONTROL.match(sql):
        return [], False
    shape = inspect_sql(sql)
    problems: list[str] = []
    if shape.verb not in _VERBS:
        problems.append(f"unrecognised statement: {_short(sql)}")
    for table in sorted(shape.writes):
        if not table.startswith("svc_"):
            problems.append(f"write to {table}: {_short(sql)}")
    for table in sorted(shape.reads):
        if not table.startswith("svc_") and table != "users":
            problems.append(f"read of {table}: {_short(sql)}")
    for table in sorted(shape.mentioned - {"users"}):
        problems.append(f"statement names the Hours table {table}: {_short(sql)}")
    user_read = False
    if "users" in shape.mentioned | shape.reads | shape.writes:
        by_key = (
            shape.verb == "SELECT"
            and shape.reads == {"users"}
            and not shape.writes
            and _USERS_BY_PK.search(sql.strip()) is not None
        )
        if not by_key:
            problems.append(f"access to users that is not a primary-key SELECT: {_short(sql)}")
        elif any(column in sql.lower() for column in _SECRET_COLUMNS):
            problems.append(f"the users read selects a secret column: {_short(sql)}")
        else:
            user_read = True
    return problems, user_read


def _same_uuid(value: object, expected: uuid.UUID) -> bool:
    return str(value).replace("-", "").lower() == expected.hex


# --------------------------------------------------------------------------------- Hours data snapshot


def hours_snapshot(session: Session) -> dict[str, list[str]]:
    """Every row of every non-ticket table, as sorted strings."""
    snapshot = {
        name: sorted(repr(tuple(row)) for row in session.execute(text(f"SELECT * FROM {name}")))
        for name in sorted(hours_tables())
    }
    session.rollback()
    return snapshot


#: Enum words (`technician`, `OPERATIONS_ADMIN`) identify nothing and occur inside error codes.
_ENUM_NAME = re.compile(r"[A-Za-z_]+")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d")
_HEX32 = re.compile(r"[0-9a-f]{32}")


def hours_values(session: Session) -> set[str]:
    """Identifying text values held by non-ticket tables: usernames, ids, hashes. UUIDs also dashed."""
    values: set[str] = set()
    for name in sorted(hours_tables()):
        for row in session.execute(text(f"SELECT * FROM {name}")):
            for value in row:
                if not isinstance(value, str) or len(value) < 8:
                    continue
                if _ENUM_NAME.fullmatch(value) or _TIMESTAMP.match(value):
                    continue
                values.add(value)
                if _HEX32.fullmatch(value):
                    values.add(str(uuid.UUID(value)))
    session.rollback()
    return values


def _walk(value: Any, key: str | None = None) -> Iterator[tuple[str | None, str]]:
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _walk(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, key)
    elif isinstance(value, str):
        yield key, value


def response_leaks(
    response: Any, values: set[str], usernames: set[str], actor: User | None, stored_names: set[str]
) -> list[str]:
    """Hours values in a response. A staff username is allowed in a copy field when it is the actor's,
    or when it is already stored in a ticket table as a name (an earlier copy)."""
    try:
        leaves = list(_walk(response.json()))
    except ValueError:
        leaves = [(None, response.text)]
    found: list[str] = []
    for key, leaf in leaves:
        for value in sorted(values):
            if value not in leaf:
                continue
            actors_own = actor is not None and value == actor.username
            copied = (
                key in COPY_KEYS
                and leaf == value
                and value in usernames
                and (actors_own or value in stored_names)
            )
            if not copied:
                found.append(f"body field {key!r} carries the Hours value {value!r}")
    for name, header in response.headers.items():
        found.extend(f"header {name} carries the Hours value {v!r}" for v in sorted(values) if v in header)
    return found


# --------------------------------------------------------------------------------- the object store


def object_store_keys(store: FakePhotoStore) -> list[str]:
    """Every object key read, written or deleted by the ticket system during the run.

    The app of this file gets `store` through the `get_photo_store` dependency override, and the store
    records the key of every call it receives, in order. It has no list operation, like the real store,
    so there is nothing else to record. Objects the fixtures seed are not recorded.
    """
    return [key for _operation, key in store.ops]


# --------------------------------------------------------------------------------- calls and requests


@dataclass
class Req:
    """One request. `reads_user` is the staff user whose `users` row the request must read, if any."""

    path: str
    headers: dict[str, str] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    reads_user: User | None = None


@dataclass(frozen=True)
class Call:
    label: str
    status: int
    build: Callable[[World], Req]
    code: str | None = None


def as_staff(w: World, user: User, path: str, **kwargs: Any) -> Req:
    return Req(path, w.token(user), kwargs, reads_user=user)


def as_customer(w: World, customer: Customer, path: str, **kwargs: Any) -> Req:
    return Req(path, w.ctoken(customer), kwargs)


def anonymous(path: str, **kwargs: Any) -> Req:
    return Req(path, {}, kwargs)


def _form(fields: dict[str, str]) -> list[Any]:
    """A multipart body without a real file: every field is a plain form part."""
    return [(name, (None, value)) for name, value in fields.items()]


def _photo_part(content: bytes) -> tuple[str, Any]:
    return ("photo", ("IMG_0001.JPG", content, "image/jpeg"))


def _good_form(w: World, **overrides: str) -> list[Any]:
    fields = {
        "city": "Haifa",
        "apartment": "12",
        "phone": "050-555-6677",
        "category_id": str(w.cat_a.id),
        "description": "The kitchen tap leaks.",
        "urgent": "false",
    }
    fields.update(overrides)
    return _form(fields)


def own(
    w: World,
    who: Customer,
    suffix: str = "",
    *,
    status: str = "received",
    owner: Customer | None = None,
    **kw: Any,
) -> Req:
    """A customer request on a fresh ticket owned by `owner` (default: the caller)."""
    ticket = w.ticket(status, owner=owner or who)
    return as_customer(w, who, f"/tickets/{ticket.ticket_number}{suffix}", **kw)


def on_ticket(
    w: World, user: User, suffix: str = "", *, status: str = "received", name: str = "", **kw: Any
) -> Req:
    """A staff request on a fresh ticket."""
    ticket = w.ticket(status, name=name)
    return as_staff(w, user, f"/staff/tickets/{ticket.ticket_number}{suffix}", **kw)


def on_entry(w: World, user: User, **kw: Any) -> Req:
    ticket = w.ticket("in_progress")
    entry = w.entry(ticket)
    return as_staff(w, user, f"/staff/tickets/{ticket.ticket_number}/history/{entry.id}", **kw)


def _missing_id() -> str:
    return str(uuid.uuid4())


def _explicit_fixtures() -> dict[Key, list[Call]]:
    """The calls of every route: successes and the failures worth sending. Run in this order."""
    table: dict[Key, list[Call]] = {}

    def add(method: str, template: str, *calls: Call) -> None:
        assert _key(method, template) not in table, f"{method} {template} is listed twice"
        table[_key(method, template)] = list(calls)

    # ------------------------------------------------------------------ customer: authentication
    def sign_in(
        who: Callable[[World], str], password: str = PASSWORD, **extra: Any
    ) -> Callable[[World], Req]:
        return lambda w: anonymous("/auth/sign-in", json={"phone": who(w), "password": password, **extra})

    add(
        "POST",
        "/auth/sign-in",
        Call("good password", 200, sign_in(lambda w: w.signer.phone)),
        Call(
            "a staff token in the header is ignored",
            200,
            lambda w: Req(
                "/auth/sign-in", w.token(w.admin), {"json": {"phone": w.signer.phone, "password": PASSWORD}}
            ),
        ),
        Call("wrong password", 401, sign_in(lambda w: w.cust.phone, "0000"), "invalid_credentials"),
        Call("unknown phone", 401, sign_in(lambda _w: "0509990000"), "invalid_credentials"),
        Call("a staff username is no phone", 422, sign_in(lambda w: w.admin.username), "invalid_phone"),
        Call("unknown field", 422, sign_in(lambda w: w.signer.phone, extra=1), "unknown_field"),
    )
    add(
        "POST",
        "/auth/sign-out",
        Call("signs out", 204, lambda w: as_customer(w, w.leaver, "/auth/sign-out")),
        Call(
            "the token is revoked",
            401,
            lambda w: Req("/auth/sign-out", w.ctoken(w.leaver, version=1)),
            "invalid_token",
        ),
    )
    add(
        "GET",
        "/auth/me",
        Call("own profile", 200, lambda w: as_customer(w, w.cust, "/auth/me")),
        Call("while a password change is owed", 200, lambda w: as_customer(w, w.owing, "/auth/me")),
        Call(
            "a deactivated customer", 401, lambda w: as_customer(w, w.inactive, "/auth/me"), "invalid_token"
        ),
    )

    def change(current: str, new: str) -> Callable[[World], Req]:
        return lambda w: as_customer(
            w, w.changer, "/auth/change-password", json={"current_password": current, "new_password": new}
        )

    add(
        "POST",
        "/auth/change-password",
        Call("wrong current password", 401, change("0000", "5678"), "invalid_credentials"),
        Call("new password is not four digits", 422, change(PASSWORD, "12"), "invalid_password_format"),
        Call("changes the password", 200, change(PASSWORD, "5678")),
    )

    # ------------------------------------------------------------------ customer: form and tickets
    owed = "password_change_required"
    add(
        "GET",
        "/form-config",
        Call("the form", 200, lambda w: as_customer(w, w.cust, "/form-config")),
        Call("a password change is owed", 403, lambda w: as_customer(w, w.owing, "/form-config"), owed),
    )

    def submit(who: str, **overrides: str) -> Callable[[World], Req]:
        return lambda w: as_customer(w, getattr(w, who), "/tickets", files=_good_form(w, **overrides))

    def submit_exhausted(w: World) -> Req:
        w.numbers.exhausted_once = True
        return as_customer(w, w.cust, "/tickets", files=_good_form(w))

    def submit_with(*parts: Any) -> Callable[[World], Req]:
        return lambda w: as_customer(w, w.cust, "/tickets", files=[*_good_form(w), *parts])

    def store_down(w: World) -> Req:
        w.photos.fail_put_once = True
        return as_customer(w, w.cust, "/tickets", files=[*_good_form(w), _photo_part(jpeg_bytes())])

    add(
        "POST",
        "/tickets",
        Call("opens a ticket", 201, submit("cust")),
        Call("opens a ticket with a photo", 201, submit_with(_photo_part(jpeg_bytes()))),
        Call("invalid city", 422, submit("cust", city="")),
        Call("unknown form field", 422, submit("cust", surprise="1"), "unknown_field"),
        Call(
            "the photo is not an image",
            422,
            submit_with(_photo_part(b"not an image")),
            "invalid_photo_type",
        ),
        Call(
            "two photos",
            422,
            submit_with(_photo_part(jpeg_bytes()), _photo_part(jpeg_bytes())),
            "too_many_photos",
        ),
        Call("the object store is down", 503, store_down, "photo_storage_unavailable"),
        Call("ticket numbers are used up", 503, submit_exhausted, "ticket_numbers_exhausted"),
        Call("a password change is owed", 403, submit("owing"), owed),
    )
    add(
        "GET",
        "/tickets",
        Call("own tickets", 200, lambda w: as_customer(w, w.cust, "/tickets", params={"page": "1"})),
        Call(
            "invalid page",
            422,
            lambda w: as_customer(w, w.cust, "/tickets", params={"page": "0"}),
            "invalid_page",
        ),
        Call("a password change is owed", 403, lambda w: as_customer(w, w.owing, "/tickets"), owed),
    )
    add(
        "GET",
        "/tickets/{ticket_number}",
        Call("own ticket", 200, lambda w: own(w, w.cust)),
        Call("someone else's ticket", 404, lambda w: own(w, w.cust, owner=w.cust2), "ticket_not_found"),
        Call("no such ticket", 404, lambda w: as_customer(w, w.cust, "/tickets/99999"), "ticket_not_found"),
        Call("malformed number", 404, lambda w: as_customer(w, w.cust, "/tickets/abc"), "ticket_not_found"),
        Call("a password change is owed", 403, lambda w: as_customer(w, w.owing, "/tickets/20000"), owed),
    )
    add(
        "POST",
        "/tickets/{ticket_number}/cancel",
        Call("cancels", 200, lambda w: own(w, w.cust, "/cancel", json={"expected_status": "received"})),
        Call(
            "stale expectation",
            409,
            lambda w: own(w, w.cust, "/cancel", json={"expected_status": "in_progress"}),
            "ticket_status_changed",
        ),
        Call(
            "already closed",
            409,
            lambda w: own(w, w.cust, "/cancel", status="closed", json={"expected_status": "closed"}),
            "invalid_transition",
        ),
        Call(
            "no expected status", 422, lambda w: own(w, w.cust, "/cancel", json={}), "invalid_expected_status"
        ),
        Call(
            "someone else's ticket",
            404,
            lambda w: own(w, w.cust, "/cancel", owner=w.cust2, json={"expected_status": "received"}),
            "ticket_not_found",
        ),
        Call(
            "a password change is owed",
            403,
            lambda w: as_customer(w, w.owing, "/tickets/20000/cancel", json={"expected_status": "received"}),
            owed,
        ),
    )

    # ------------------------------------------------------------------ shared: the ticket photo
    def photo_of(w: World, who: Customer, *, owner: Customer | None = None, stored: bool = True) -> Req:
        ticket = w.photo_ticket(owner or who, stored=stored)
        return as_customer(w, who, f"/tickets/{ticket.ticket_number}/photo")

    def photo_as_staff(w: World, user: User) -> Req:
        ticket = w.photo_ticket(w.cust)
        return as_staff(w, user, f"/tickets/{ticket.ticket_number}/photo")

    def photo_store_down(w: World) -> Req:
        w.photos.fail_get_once = True
        return photo_of(w, w.cust)

    add(
        "GET",
        "/tickets/{ticket_number}/photo",
        Call("the owner", 200, lambda w: photo_of(w, w.cust)),
        Call("an administrator", 200, lambda w: photo_as_staff(w, w.admin)),
        Call("an operations administrator", 200, lambda w: photo_as_staff(w, w.ops)),
        Call("a technician", 200, lambda w: photo_as_staff(w, w.tech)),
        Call(
            "someone else's photo",
            404,
            lambda w: photo_of(w, w.cust, owner=w.cust2),
            "ticket_not_found",
        ),
        Call(
            "a ticket without a photo",
            404,
            lambda w: as_customer(w, w.cust, f"/tickets/{w.ticket().ticket_number}/photo"),
            "ticket_not_found",
        ),
        Call(
            "no such ticket",
            404,
            lambda w: as_customer(w, w.cust, "/tickets/99999/photo"),
            "ticket_not_found",
        ),
        Call(
            "malformed number",
            404,
            lambda w: as_customer(w, w.cust, "/tickets/abc/photo"),
            "ticket_not_found",
        ),
        Call(
            "the object is gone",
            404,
            lambda w: photo_of(w, w.cust, stored=False),
            "ticket_not_found",
        ),
        Call("the object store is down", 503, photo_store_down, "photo_storage_unavailable"),
        Call(
            "a password change is owed",
            403,
            lambda w: photo_of(w, w.owing),
            owed,
        ),
        Call(
            "a deactivated technician",
            401,
            lambda w: photo_as_staff(w, w.gone),
            "invalid_token",
        ),
    )

    # ------------------------------------------------------------------ staff: dashboard, tickets
    add(
        "GET",
        "/staff/dashboard",
        Call(
            "configuration staff, with the numbers warning",
            200,
            lambda w: as_staff(w, w.admin, "/staff/dashboard"),
        ),
        Call("a technician", 200, lambda w: as_staff(w, w.tech, "/staff/dashboard")),
        Call(
            "a role of Hours",
            403,
            lambda w: as_staff(w, w.accounting, "/staff/dashboard"),
            "insufficient_role",
        ),
        Call(
            "a deactivated technician",
            401,
            lambda w: as_staff(w, w.gone, "/staff/dashboard"),
            "invalid_token",
        ),
    )
    add(
        "GET",
        "/staff/tickets",
        Call(
            "filtered list",
            200,
            lambda w: as_staff(
                w,
                w.admin,
                "/staff/tickets",
                params={"status": "received,in_progress", "city": "haifa", "urgent_first": "true"},
            ),
        ),
        Call(
            "exact search",
            200,
            lambda w: as_staff(w, w.tech, "/staff/tickets", params={"search": str(w.ticket().ticket_number)}),
        ),
        Call(
            "invalid page",
            422,
            lambda w: as_staff(w, w.admin, "/staff/tickets", params={"page": "0"}),
            "invalid_page",
        ),
        Call(
            "inverted date range",
            422,
            lambda w: as_staff(
                w, w.admin, "/staff/tickets", params={"date_from": "2025-03-10", "date_to": "2025-03-09"}
            ),
            "invalid_date_range",
        ),
    )
    add(
        "GET",
        "/staff/form-config",
        Call("the form, as a technician", 200, lambda w: as_staff(w, w.tech, "/staff/form-config")),
        Call("the form, as an administrator", 200, lambda w: as_staff(w, w.admin, "/staff/form-config")),
    )

    def staff_open(
        who: str, customer: str = "cust", *parts: Any, **overrides: str
    ) -> Callable[[World], Req]:
        """Staff `who` opens a ticket for customer `customer`; extra `parts` are appended to the form."""

        def build(w: World) -> Req:
            fields = _form({"customer_id": str(getattr(w, customer).id)})
            return as_staff(
                w, getattr(w, who), "/staff/tickets", files=[*_good_form(w, **overrides), *fields, *parts]
            )

        return build

    def staff_open_without_customer(w: World) -> Req:
        return as_staff(w, w.tech, "/staff/tickets", files=_good_form(w))

    def staff_open_unknown(w: World) -> Req:
        fields = _form({"customer_id": _missing_id()})
        return as_staff(w, w.tech, "/staff/tickets", files=[*_good_form(w), *fields])

    def staff_store_down(w: World) -> Req:
        w.photos.fail_put_once = True
        return staff_open("tech", "cust", _photo_part(jpeg_bytes()))(w)

    def staff_exhausted(w: World) -> Req:
        w.numbers.exhausted_once = True
        return staff_open("tech")(w)

    add(
        "POST",
        "/staff/tickets",
        Call("a technician opens a ticket", 201, staff_open("tech")),
        Call(
            "an administrator opens a ticket with a photo",
            201,
            staff_open("admin", "cust2", _photo_part(jpeg_bytes())),
        ),
        Call("an operations administrator opens a ticket", 201, staff_open("ops", "owing")),
        Call("no customer id", 422, staff_open_without_customer, "customer_not_found"),
        Call("no such customer", 404, staff_open_unknown, "customer_not_found"),
        Call("an inactive customer", 409, staff_open("tech", "inactive"), "customer_inactive"),
        Call("invalid city", 422, staff_open("tech", city="")),
        Call("unknown form field", 422, staff_open("tech", surprise="1"), "unknown_field"),
        Call(
            "the photo is not an image",
            422,
            staff_open("tech", "cust", _photo_part(b"not an image")),
            "invalid_photo_type",
        ),
        Call("the object store is down", 503, staff_store_down, "photo_storage_unavailable"),
        Call("ticket numbers are used up", 503, staff_exhausted, "ticket_numbers_exhausted"),
    )
    add(
        "GET",
        "/staff/tickets/{ticket_number}",
        Call("a ticket", 200, lambda w: on_ticket(w, w.admin)),
        Call("a ticket, as a technician", 200, lambda w: on_ticket(w, w.tech, name="Moshe Tech")),
        Call(
            "no such ticket", 404, lambda w: as_staff(w, w.admin, "/staff/tickets/99999"), "ticket_not_found"
        ),
        Call(
            "malformed number", 404, lambda w: as_staff(w, w.admin, "/staff/tickets/abc"), "ticket_not_found"
        ),
    )

    RESOLUTION = {
        "entry_text": "Replaced the washer",
        "technician_name": "Dana",
        "parts_replaced": False,
        "part_ids": [],
    }

    def status_body(expected: str, target: str, **extra: Any) -> dict[str, Any]:
        return {"expected_status": expected, "target_status": target, **extra}

    add(
        "POST",
        "/staff/tickets/{ticket_number}/status",
        Call(
            "approval names the acting username",
            200,
            lambda w: on_ticket(w, w.ops, "/status", json=status_body("received", "in_progress")),
        ),
        Call(
            "approval with a name",
            200,
            lambda w: on_ticket(
                w,
                w.admin,
                "/status",
                json=status_body("received", "in_progress", assigned_technician_name="Moshe Tech"),
            ),
        ),
        Call(
            "closes",
            200,
            lambda w: on_ticket(
                w,
                w.tech,
                "/status",
                status="in_progress",
                json=status_body("in_progress", "closed", resolution=RESOLUTION),
            ),
        ),
        Call(
            "stale expectation",
            409,
            lambda w: on_ticket(w, w.admin, "/status", json=status_body("in_progress", "closed")),
            "ticket_status_changed",
        ),
        Call(
            "staff may not cancel",
            409,
            lambda w: on_ticket(w, w.admin, "/status", json=status_body("received", "cancelled")),
            "invalid_transition",
        ),
        Call(
            "unknown target",
            422,
            lambda w: on_ticket(w, w.admin, "/status", json=status_body("received", "approved")),
            "invalid_target_status",
        ),
        Call(
            "unknown body field",
            422,
            lambda w: on_ticket(w, w.admin, "/status", json=status_body("received", "in_progress", extra=1)),
            "unknown_field",
        ),
        Call(
            "no such ticket",
            404,
            lambda w: as_staff(
                w, w.admin, "/staff/tickets/99999/status", json=status_body("received", "in_progress")
            ),
            "ticket_not_found",
        ),
    )
    add(
        "PUT",
        "/staff/tickets/{ticket_number}/assigned-technician",
        Call(
            "assigns",
            200,
            lambda w: on_ticket(
                w, w.admin, "/assigned-technician", json={"assigned_technician_name": "Moshe Tech"}
            ),
        ),
        Call(
            "clears",
            200,
            lambda w: on_ticket(
                w, w.tech, "/assigned-technician", name="Moshe Tech", json={"assigned_technician_name": ""}
            ),
        ),
        Call(
            "name too long",
            422,
            lambda w: on_ticket(
                w, w.admin, "/assigned-technician", json={"assigned_technician_name": "x" * 101}
            ),
            "invalid_assigned_technician_name",
        ),
        Call(
            "no such ticket",
            404,
            lambda w: as_staff(
                w, w.admin, "/staff/tickets/99999/assigned-technician", json={"assigned_technician_name": "A"}
            ),
            "ticket_not_found",
        ),
    )

    # ------------------------------------------------------------------ staff: history
    add(
        "POST",
        "/staff/tickets/{ticket_number}/history",
        Call(
            "name defaults to the acting username",
            201,
            lambda w: on_ticket(w, w.tech, "/history", json={"entry_text": "replaced the valve"}),
        ),
        Call(
            "explicit name",
            201,
            lambda w: on_ticket(
                w,
                w.ops,
                "/history",
                status="closed",
                json={"entry_text": "checked", "technician_name": "Moshe Tech"},
            ),
        ),
        Call(
            "empty text",
            422,
            lambda w: on_ticket(w, w.admin, "/history", json={"entry_text": "  "}),
            "invalid_history_text",
        ),
        Call(
            "no such ticket",
            404,
            lambda w: as_staff(w, w.admin, "/staff/tickets/99999/history", json={"entry_text": "x"}),
            "ticket_not_found",
        ),
    )
    edit = {"entry_text": "edited note", "technician_name": "Moshe Tech"}
    add(
        "PUT",
        "/staff/tickets/{ticket_number}/history/{entry_id}",
        Call("edits a note", 200, lambda w: on_entry(w, w.admin, json=edit)),
        Call(
            "invalid text",
            422,
            lambda w: on_entry(w, w.tech, json={**edit, "entry_text": ""}),
            "invalid_history_text",
        ),
        Call(
            "no such entry",
            404,
            lambda w: on_ticket(w, w.admin, f"/history/{_missing_id()}", json=edit),
            "history_entry_not_found",
        ),
        Call(
            "malformed entry id",
            404,
            lambda w: on_ticket(w, w.admin, "/history/abc", json=edit),
            "history_entry_not_found",
        ),
    )

    # ------------------------------------------------------------------ staff: customers and reports
    new_customer = {"phone": "050-555-7788", "full_name": "Rina Cohen", "city": "Haifa", "apartment": "7"}
    add(
        "GET",
        "/staff/customers",
        Call(
            "search and state filter",
            200,
            lambda w: as_staff(
                w, w.admin, "/staff/customers", params={"search": "Cohen", "active": "true", "page": "1"}
            ),
        ),
        Call("a technician", 200, lambda w: as_staff(w, w.tech, "/staff/customers")),
        Call(
            "unknown state filter",
            422,
            lambda w: as_staff(w, w.admin, "/staff/customers", params={"active": "maybe"}),
            "invalid_filter",
        ),
    )
    add(
        "POST",
        "/staff/customers",
        Call("adds a customer", 201, lambda w: as_staff(w, w.ops, "/staff/customers", json=new_customer)),
        Call(
            "phone already registered",
            409,
            lambda w: as_staff(w, w.ops, "/staff/customers", json={**new_customer, "phone": w.cust.phone}),
            "phone_already_registered",
        ),
        Call(
            "invalid phone",
            422,
            lambda w: as_staff(w, w.ops, "/staff/customers", json={**new_customer, "phone": "123"}),
            "invalid_phone",
        ),
        Call(
            "unknown field",
            422,
            lambda w: as_staff(w, w.ops, "/staff/customers", json={**new_customer, "extra": 1}),
            "unknown_field",
        ),
    )
    edited = {"phone": "0505559900", "full_name": "Edited Name", "city": "Acre", "apartment": "3"}
    add(
        "PUT",
        "/staff/customers/{customer_id}",
        Call("edits", 200, lambda w: as_staff(w, w.admin, f"/staff/customers/{w.editable.id}", json=edited)),
        Call(
            "invalid name",
            422,
            lambda w: as_staff(
                w, w.admin, f"/staff/customers/{w.editable.id}", json={**edited, "full_name": ""}
            ),
            "invalid_full_name",
        ),
        Call(
            "no such customer",
            404,
            lambda w: as_staff(w, w.admin, f"/staff/customers/{_missing_id()}", json=edited),
            "customer_not_found",
        ),
    )
    add(
        "POST",
        "/staff/customers/{customer_id}/deactivate",
        Call("deactivates", 200, lambda w: as_staff(w, w.tech, f"/staff/customers/{w.victim.id}/deactivate")),
        Call(
            "no such customer",
            404,
            lambda w: as_staff(w, w.tech, f"/staff/customers/{_missing_id()}/deactivate"),
            "customer_not_found",
        ),
    )
    add(
        "POST",
        "/staff/customers/{customer_id}/reactivate",
        Call(
            "reactivates", 200, lambda w: as_staff(w, w.tech, f"/staff/customers/{w.sleeper.id}/reactivate")
        ),
        Call(
            "malformed id",
            404,
            lambda w: as_staff(w, w.tech, "/staff/customers/abc/reactivate"),
            "customer_not_found",
        ),
    )
    add(
        "POST",
        "/staff/customers/{customer_id}/reset-password",
        Call(
            "resets",
            204,
            lambda w: as_staff(w, w.admin, f"/staff/customers/{w.resettable.id}/reset-password"),
        ),
        Call(
            "no such customer",
            404,
            lambda w: as_staff(w, w.admin, f"/staff/customers/{_missing_id()}/reset-password"),
            "customer_not_found",
        ),
    )
    add(
        "GET",
        "/staff/reports",
        Call(
            "report",
            200,
            lambda w: as_staff(
                w,
                w.admin,
                "/staff/reports",
                params={"date_from": "2025-01-01", "date_to": "2025-12-31", "city": "Haifa"},
            ),
        ),
        Call(
            "inverted date range",
            422,
            lambda w: as_staff(
                w, w.tech, "/staff/reports", params={"date_from": "2025-03-10", "date_to": "2025-03-09"}
            ),
            "invalid_date_range",
        ),
        Call(
            "part filter",
            200,
            lambda w: as_staff(w, w.admin, "/staff/reports", params={"part_filter": "yes"}),
        ),
        Call(
            "unknown part filter",
            422,
            lambda w: as_staff(w, w.tech, "/staff/reports", params={"part_filter": "maybe"}),
            "invalid_filter",
        ),
    )
    add(
        "GET",
        "/staff/reports/export",
        Call(
            "export",
            200,
            lambda w: as_staff(
                w,
                w.admin,
                "/staff/reports/export",
                params={"date_from": "2025-01-01", "date_to": "2025-12-31", "city": "Haifa", "lang": "en"},
            ),
        ),
        Call(
            "unknown language",
            422,
            lambda w: as_staff(w, w.tech, "/staff/reports/export", params={"lang": "fr"}),
            "invalid_filter",
        ),
        Call(
            "inverted date range",
            422,
            lambda w: as_staff(
                w,
                w.tech,
                "/staff/reports/export",
                params={"date_from": "2025-03-10", "date_to": "2025-03-09"},
            ),
            "invalid_date_range",
        ),
    )

    # ------------------------------------------------------------------ staff: configuration
    add(
        "GET",
        "/staff/categories",
        Call("as configuration staff", 200, lambda w: as_staff(w, w.admin, "/staff/categories")),
        Call("as a technician", 200, lambda w: as_staff(w, w.tech, "/staff/categories")),
    )
    names = {"name_he": "ניסיון", "name_en": "Trial"}
    add(
        "POST",
        "/staff/categories",
        Call("adds", 201, lambda w: as_staff(w, w.ops, "/staff/categories", json=names)),
        Call(
            "duplicate name",
            409,
            lambda w: as_staff(
                w, w.ops, "/staff/categories", json={"name_he": w.cat_a.name_he, "name_en": "Other"}
            ),
            "duplicate_category",
        ),
        Call(
            "empty name",
            422,
            lambda w: as_staff(w, w.ops, "/staff/categories", json={"name_he": "", "name_en": "x"}),
            "invalid_name_he",
        ),
    )
    add(
        "PUT",
        "/staff/categories/{category_id}",
        Call(
            "renames",
            200,
            lambda w: as_staff(
                w,
                w.admin,
                f"/staff/categories/{w.cat_b.id}",
                json={"name_he": "שם חדש", "name_en": "Renamed"},
            ),
        ),
        Call(
            "no such category",
            404,
            lambda w: as_staff(w, w.admin, f"/staff/categories/{_missing_id()}", json=names),
            "category_not_found",
        ),
    )
    add(
        "POST",
        "/staff/categories/{category_id}/deactivate",
        Call(
            "deactivates", 200, lambda w: as_staff(w, w.admin, f"/staff/categories/{w.cat_c.id}/deactivate")
        ),
        Call(
            "no such category",
            404,
            lambda w: as_staff(w, w.admin, f"/staff/categories/{_missing_id()}/deactivate"),
            "category_not_found",
        ),
    )
    add(
        "POST",
        "/staff/categories/{category_id}/reactivate",
        Call(
            "reactivates", 200, lambda w: as_staff(w, w.ops, f"/staff/categories/{w.cat_off.id}/reactivate")
        ),
        Call(
            "no such category",
            404,
            lambda w: as_staff(w, w.ops, f"/staff/categories/{_missing_id()}/reactivate"),
            "category_not_found",
        ),
    )
    add(
        "DELETE",
        "/staff/categories/{category_id}",
        Call(
            "removes an unused category",
            200,
            lambda w: as_staff(w, w.admin, f"/staff/categories/{w.cat_spare.id}"),
        ),
        Call(
            "deactivates a used category",
            200,
            lambda w: as_staff(w, w.admin, f"/staff/categories/{w.cat_used.id}"),
        ),
        Call(
            "no such category",
            404,
            lambda w: as_staff(w, w.admin, f"/staff/categories/{_missing_id()}"),
            "category_not_found",
        ),
    )
    add(
        "GET",
        "/staff/settings",
        Call("reads", 200, lambda w: as_staff(w, w.admin, "/staff/settings")),
    )
    options = {"description_required": False, "photo_upload_enabled": True}
    add(
        "PUT",
        "/staff/settings",
        Call("sets", 200, lambda w: as_staff(w, w.ops, "/staff/settings", json=options)),
        Call(
            "options must be booleans",
            422,
            lambda w: as_staff(w, w.ops, "/staff/settings", json={**options, "description_required": "yes"}),
        ),
    )
    add(
        "PUT",
        "/staff/labels",
        Call(
            "replaces a label",
            200,
            lambda w: as_staff(
                w, w.admin, "/staff/labels", json={"labels": {"city": {"he": "עיר חדשה", "en": "New city"}}}
            ),
        ),
        Call(
            "unknown field key",
            422,
            lambda w: as_staff(
                w, w.admin, "/staff/labels", json={"labels": {"nope": {"he": "x", "en": "y"}}}
            ),
            "unknown_field",
        ),
    )

    # ------------------------------------------------------------------ branding (R28)
    def logo_as_customer(w: World, *, present: bool = True, stored: bool = True) -> Req:
        w.set_logo(present=present, stored=stored)
        return as_customer(w, w.cust, "/branding/logo")

    def logo_as_staff(w: World, user: User) -> Req:
        w.set_logo()
        return as_staff(w, user, "/branding/logo")

    def logo_store_down(w: World) -> Req:
        w.photos.fail_get_once = True
        return logo_as_customer(w)

    def logo_owing(w: World) -> Req:
        w.set_logo()
        return as_customer(w, w.owing, "/branding/logo")

    add(
        "GET",
        "/branding/logo",
        Call("a customer", 200, logo_as_customer),
        Call("an administrator", 200, lambda w: logo_as_staff(w, w.admin)),
        Call("an operations administrator", 200, lambda w: logo_as_staff(w, w.ops)),
        Call("a technician", 200, lambda w: logo_as_staff(w, w.tech)),
        Call("no logo is stored", 404, lambda w: logo_as_customer(w, present=False)),
        Call("the object is gone", 404, lambda w: logo_as_customer(w, stored=False)),
        Call("the object store is down", 503, logo_store_down, "photo_storage_unavailable"),
        Call("a password change is owed", 403, logo_owing, owed),
        Call(
            "a deactivated technician",
            401,
            lambda w: as_staff(w, w.gone, "/branding/logo"),
            "invalid_token",
        ),
    )
    add(
        "GET",
        "/staff/branding",
        Call("reads", 200, lambda w: as_staff(w, w.admin, "/staff/branding")),
        Call("as operations administrator", 200, lambda w: as_staff(w, w.ops, "/staff/branding")),
    )

    def branding_form(w: World, user: User, *parts: Any, **fields: str) -> Req:
        return as_staff(w, user, "/staff/branding", files=[*_form(fields), *parts])

    def logo_file(content: bytes) -> tuple[str, Any]:
        return ("logo", ("logo.png", content, "image/png"))

    add(
        "PUT",
        "/staff/branding",
        Call("sets the name", 200, lambda w: branding_form(w, w.admin, company_name="Acme Service")),
        Call("uploads a logo", 200, lambda w: branding_form(w, w.ops, logo_file(png_bytes((600, 300))))),
        Call("replaces the logo", 200, lambda w: branding_form(w, w.admin, logo_file(jpeg_bytes((90, 40))))),
        Call("removes the name", 200, lambda w: branding_form(w, w.admin, remove_name="true")),
        Call("removes the logo", 200, lambda w: branding_form(w, w.admin, remove_logo="true")),
        Call(
            "the name is too long",
            422,
            lambda w: branding_form(w, w.admin, company_name="x" * 101),
            "invalid_company_name",
        ),
        Call(
            "the logo is not an image",
            422,
            lambda w: branding_form(w, w.admin, logo_file(b"not an image")),
            "invalid_logo_type",
        ),
        Call(
            "a part nobody knows",
            422,
            lambda w: branding_form(w, w.admin, surprise="1"),
            "unknown_field",
        ),
        Call(
            "a technician",
            403,
            lambda w: branding_form(w, w.tech, company_name="Acme"),
            "insufficient_role",
        ),
    )

    # ------------------------------------------------------------------ staff: settlements and parts
    def named_list(
        path: str,
        id_name: str,
        model: type[Locality] | type[Part],
        *,
        invalid_he: str,
        duplicate: str,
        not_found: str,
    ) -> None:
        base = f"/staff/{path}"
        item = f"{base}/{{{id_name}}}"
        names = {"name_he": f"{path}-new-he", "name_en": f"{path}-new"}

        def target(w: World, name: str, suffix: str = "") -> str:
            return f"{base}/{w.named(model, name).id}{suffix}"

        add("GET", base, Call("as configuration staff", 200, lambda w: as_staff(w, w.admin, base)))
        add(
            "POST",
            base,
            Call("adds", 201, lambda w: as_staff(w, w.ops, base, json=names)),
            Call(
                "duplicate name",
                409,
                lambda w: as_staff(
                    w,
                    w.ops,
                    base,
                    json={"name_he": w.named(model, f"{path}-dup").name_he, "name_en": "Other"},
                ),
                duplicate,
            ),
            Call(
                "empty name",
                422,
                lambda w: as_staff(w, w.ops, base, json={"name_he": "", "name_en": "x"}),
                invalid_he,
            ),
        )
        add(
            "PUT",
            item,
            Call(
                "renames",
                200,
                lambda w: as_staff(
                    w,
                    w.admin,
                    target(w, f"{path}-rename"),
                    json={"name_he": f"{path}-renamed-he", "name_en": f"{path}-renamed"},
                ),
            ),
            Call(
                "no such item",
                404,
                lambda w: as_staff(w, w.admin, f"{base}/{_missing_id()}", json=names),
                not_found,
            ),
        )
        add(
            "POST",
            f"{item}/deactivate",
            Call(
                "deactivates", 200, lambda w: as_staff(w, w.admin, target(w, f"{path}-off", "/deactivate"))
            ),
            Call(
                "no such item",
                404,
                lambda w: as_staff(w, w.admin, f"{base}/{_missing_id()}/deactivate"),
                not_found,
            ),
        )
        add(
            "POST",
            f"{item}/reactivate",
            Call(
                "reactivates",
                200,
                lambda w: as_staff(
                    w, w.ops, f"{base}/{w.named(model, f'{path}-on', active=False).id}/reactivate"
                ),
            ),
            Call(
                "no such item",
                404,
                lambda w: as_staff(w, w.ops, f"{base}/{_missing_id()}/reactivate"),
                not_found,
            ),
        )
        add(
            "DELETE",
            item,
            Call("removes an unused item", 200, lambda w: as_staff(w, w.admin, target(w, f"{path}-spare"))),
            Call(
                "deactivates a used item",
                200,
                lambda w: as_staff(w, w.admin, f"{base}/{w.used_named(model, f'{path}-used').id}"),
            ),
            Call("no such item", 404, lambda w: as_staff(w, w.admin, f"{base}/{_missing_id()}"), not_found),
        )

    named_list(
        "localities",
        "locality_id",
        Locality,
        invalid_he="invalid_settlement_name_he",
        duplicate="duplicate_settlement",
        not_found="settlement_not_found",
    )
    named_list(
        "parts",
        "part_id",
        Part,
        invalid_he="invalid_part_name_he",
        duplicate="duplicate_part",
        not_found="part_not_found",
    )
    add(
        "GET",
        "/staff/parts/active",
        Call("as configuration staff", 200, lambda w: as_staff(w, w.admin, "/staff/parts/active")),
        Call("as a technician", 200, lambda w: as_staff(w, w.tech, "/staff/parts/active")),
    )
    return table


FIXTURES: dict[Key, list[Call]] = _explicit_fixtures()

#: The principal class of each route, from the endpoint table of the classification test.
CLASSES: dict[Key, str] = {_key(entry.method, entry.path): entry.klass for entry in ENDPOINT_TABLE}


def _concrete(key: Key) -> str:
    """A path for the route in which every parameter is a value that matches no record."""
    segments = key[1].split("/")
    out = []
    for index, segment in enumerate(segments):
        if segment == "{}":
            out.append("10000" if segments[index - 1] == "tickets" else str(uuid.uuid4()))
        else:
            out.append(segment)
    return "/".join(out)


def _generated(key: Key) -> list[Call]:
    """The refusals every route of a class has, from its class alone: none needs data or reaches a handler."""
    method = key[0]
    klass = CLASSES.get(key)
    path = _concrete(key)
    kwargs: dict[str, Any] = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
    calls: list[Call] = []
    if klass in (CUSTOMER, CUSTOMER_UNGATED):
        calls.append(Call("no token", 401, lambda _w: anonymous(path, **kwargs), "not_authenticated"))
        calls.append(
            Call(
                "a staff token",
                403,
                lambda w: Req(path, w.token(w.admin), dict(kwargs)),
                "staff_token_not_accepted",
            )
        )
    elif klass in (TICKET_STAFF, CONFIG_STAFF):
        calls.append(Call("no token", 401, lambda _w: anonymous(path, **kwargs), "not_authenticated"))
        calls.append(
            Call(
                "a customer token",
                403,
                lambda w: Req(path, w.ctoken(w.cust), dict(kwargs)),
                "customer_token_not_accepted",
            )
        )
        calls.append(
            Call(
                "a role of Hours",
                403,
                lambda w: as_staff(w, w.accounting, path, **kwargs),
                "insufficient_role",
            )
        )
        if klass == CONFIG_STAFF:
            calls.append(
                Call("a technician", 403, lambda w: as_staff(w, w.tech, path, **kwargs), "insufficient_role")
            )
    elif klass == SHARED_PHOTO:
        calls.append(Call("no token", 401, lambda _w: anonymous(path, **kwargs), "not_authenticated"))
        calls.append(
            Call(
                "a role of Hours",
                403,
                lambda w: as_staff(w, w.accounting, path, **kwargs),
                "insufficient_role",
            )
        )
    return calls


def all_calls() -> list[tuple[Key, Call]]:
    calls: list[tuple[Key, Call]] = []
    for key, explicit in FIXTURES.items():
        calls.extend((key, call) for call in [*explicit, *_generated(key)])
    return calls


# --------------------------------------------------------------------------------- coverage of routes


def enumerate_routes(app: FastAPI) -> set[Key]:
    routes: set[Key] = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(API + "/"):
            routes |= {_key(method, route.path[len(API) :]) for method in route.methods - {"HEAD", "OPTIONS"}}
    return routes


def coverage_problems(routes: set[Key], fixtures: set[Key]) -> list[str]:
    """Routes without fixtures, fixtures without a route, and table routes the routers do not have."""
    problems = [f"{m} {p} has no entry in the fixtures table" for m, p in sorted(routes - fixtures)]
    problems += [f"{m} {p} has fixtures but no such route exists" for m, p in sorted(fixtures - routes)]
    listed = {_key(e.method, e.path) for e in ENDPOINT_TABLE if e.pending is None}
    problems += [f"{m} {p} is in ENDPOINT_TABLE but not in the routers" for m, p in sorted(listed - routes)]
    problems += [
        f"{m} {p} is a route that is not in ENDPOINT_TABLE" for m, p in sorted(routes - set(CLASSES))
    ]
    return problems


def _pattern(key: Key) -> re.Pattern[str]:
    return re.compile(re.escape(key[1]).replace(re.escape("{}"), "[^/]+"))


def run_all_calls(
    world: World,
    client: TestClient,
    recorder: Recorder,
    check: Callable[[Key, Call, Req, Any, list[Statement]], None] | None = None,
) -> set[Key]:
    """Send every call in order, assert its status and code, and return the routes that were called."""
    called: set[Key] = set()
    for key, call in all_calls():
        request = call.build(world)
        where = f"{key[0]} {key[1]} / {call.label}"
        assert _pattern(key).fullmatch(request.path), f"{where}: the fixture requests {request.path}"
        with recorder.window() as statements:
            response = client.request(key[0], API + request.path, headers=request.headers, **request.kwargs)
        assert response.status_code == call.status, f"{where}: {response.status_code} {response.text[:300]}"
        if call.code is not None:
            assert response.json()["detail"]["error"]["code"] == call.code, f"{where}: {response.text[:300]}"
        called.add(key)
        if check is not None:
            check(key, call, request, response, statements)
    return called


# --------------------------------------------------------------------------------- the tests


def test_ticket_api_audit_log_count_unchanged(
    client: TestClient, world: World, session: Session, recorder
) -> None:
    """R20.4, R16.11: calling every ticket endpoint adds, changes and deletes no audit entry."""
    problems = coverage_problems(enumerate_routes(client.app), set(FIXTURES))
    assert not problems, "\n" + "\n".join(problems)
    session.add(
        ChangeLog(entity_type="users", entity_id=world.admin.id, changed_by_user_id=world.admin.id, field="x")
    )
    session.commit()

    def audit_ids() -> list[uuid.UUID]:
        session.rollback()
        return sorted(session.execute(select(ChangeLog.id)).scalars())

    before_count = session.execute(select(func.count()).select_from(ChangeLog)).scalar_one()
    before = audit_ids()
    called = run_all_calls(world, client, recorder)
    after_count = session.execute(select(func.count()).select_from(ChangeLog)).scalar_one()

    assert before_count == 1  # the counter looks at a table that holds a row, so a drop would show too
    assert after_count == before_count
    assert audit_ids() == before
    assert called == enumerate_routes(client.app)


def test_ticket_api_sql_isolation(
    client: TestClient, world: World, session: Session, recorder: Recorder, photos: FakePhotoStore
) -> None:
    """Property 28: no Hours write, no Hours read but the caller's `users` row, no Hours value returned."""
    problems = coverage_problems(enumerate_routes(client.app), set(FIXTURES))
    assert not problems, "\n" + "\n".join(problems)
    snapshot = hours_snapshot(session)
    values = hours_values(session)
    usernames = {user.username for user in world.users}
    assert values >= usernames and len(values) > len(usernames)  # the sentinels exist
    written: set[str] = set()
    findings: list[str] = []
    user_reads = 0

    def stored_names() -> set[str]:
        session.rollback()
        names = set(session.execute(select(Ticket.assigned_technician_name)).scalars())
        names |= set(session.execute(select(HistoryEntry.technician_name)).scalars())
        names |= set(session.execute(select(Ticket.opened_by_username)).scalars())  # R26.4: a stored copy
        session.rollback()
        return {name for name in names if name}

    def check(key: Key, call: Call, request: Req, response: Any, statements: list[Statement]) -> None:
        nonlocal user_reads
        where = f"{key[0]} {key[1]} / {call.label}"
        reads: list[Statement] = []
        for statement in statements:
            problems, is_user_read = judge(statement)
            findings.extend(f"{where}: {problem}" for problem in problems)
            written.update(inspect_sql(statement.sql).writes if not _CONTROL.match(statement.sql) else ())
            if is_user_read:
                reads.append(statement)
        user_reads += len(reads)
        actor = request.reads_user
        if actor is None:
            if reads:
                findings.append(f"{where}: a request without a staff identity read a users row")
        elif len(reads) != 1:
            findings.append(f"{where}: expected one users read of the caller, saw {len(reads)}")
        elif not (len(reads[0].parameters) == 1 and _same_uuid(reads[0].parameters[0], actor.id)):
            findings.append(f"{where}: the users read is not for the calling user: {reads[0].parameters!r}")
        findings.extend(
            f"{where}: {leak}" for leak in response_leaks(response, values, usernames, actor, stored_names())
        )

    called = run_all_calls(world, client, recorder, check)

    assert not findings, "\n" + "\n".join(findings)
    assert hours_snapshot(session) == snapshot  # no Hours row was added, changed or removed
    assert called == enumerate_routes(client.app)
    # The run really exercised writes and staff reads, so the assertions above were not empty.
    assert user_reads > 40
    assert {
        "svc_tickets",
        "svc_customers",
        "svc_categories",
        "svc_history_entries",
        "svc_settings",
        "svc_field_labels",
        "svc_signin_failures",
        "svc_localities",
        "svc_parts",
        "svc_branding",
    } <= written
    # R20.11: every object key starts with the photo prefix, and the run really touched the store: a
    # submission put a photo and the delivery route read photos.
    keys = object_store_keys(photos)
    assert keys and all(key.startswith(PHOTO_PREFIX) for key in keys), keys
    assert {operation for operation, _ in photos.ops} >= {"put", "get"}


# --------------------------------------------------------------------------------- the checks themselves


def test_a_route_without_fixtures_fails_the_coverage_check() -> None:
    routes = {("GET", "/new-route"), ("GET", "/form-config")}
    fixtures = {("GET", "/form-config"), ("GET", "/gone-route")}

    problems = coverage_problems(routes, fixtures)

    assert any("GET /new-route has no entry" in problem for problem in problems)
    assert any("GET /gone-route has fixtures but no such route" in problem for problem in problems)
    assert coverage_problems(set(FIXTURES), set(FIXTURES)) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT svc_tickets.id FROM svc_tickets JOIN svc_categories ON svc_categories.id = svc_tickets.cat",
        "INSERT INTO svc_tickets (id) VALUES (?)",
        "UPDATE svc_customers SET token_version=(svc_customers.token_version + ?) WHERE svc_customers.id = ?",
        "DELETE FROM svc_signin_failures WHERE svc_signin_failures.phone = ?",
        "SELECT nextval('svc_ticket_number_seq')",
        "SAVEPOINT sa_savepoint_1",
        "SELECT count(*) FILTER (WHERE svc_tickets.urgent) FROM svc_tickets",
    ],
)
def test_the_judge_accepts_ticket_table_statements(sql: str) -> None:
    assert judge(Statement(sql, ())) == ([], False)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO change_logs (id) VALUES (?)",
        "UPDATE users SET failed_login_count = ? WHERE users.id = ?",
        "DELETE FROM employees WHERE employees.id = ?",
        "SELECT * FROM svc_tickets, clients",
        "SELECT svc_tickets.id FROM svc_tickets JOIN sites ON sites.id = svc_tickets.id",
        "SELECT (SELECT count(*) FROM time_entries) FROM svc_tickets",
        "SELECT users.id FROM users WHERE users.username = ?",
        "SELECT users.id FROM users",
        "SELECT users.id, users.password_hash FROM users WHERE users.id = ?",
        "SELECT users.id FROM users JOIN svc_customers ON 1 WHERE users.id = ?",
        "SELECT nextval('hours_sequence')",
        "DROP TABLE documents",
        "PRAGMA table_info(users)",
    ],
)
def test_the_judge_rejects_hours_access(sql: str) -> None:
    problems, is_user_read = judge(Statement(sql, ()))

    assert problems
    assert not is_user_read


def test_the_judge_accepts_the_staff_identity_read_and_nothing_wider() -> None:
    narrow = "SELECT users.id, users.username, users.role FROM users WHERE users.id = ?"

    assert judge(Statement(narrow, ("a" * 32,))) == ([], True)


def test_the_recorder_sees_a_real_hours_read_and_the_judge_flags_it(
    session: Session, recorder: Recorder
) -> None:
    with recorder.window() as statements:
        session.execute(text("SELECT count(*) FROM employees")).scalar_one()
    session.rollback()

    assert [problem for s in statements for problem in judge(s)[0]]


def test_the_audit_counter_sees_a_new_row(session: Session, make_user) -> None:
    admin = make_user(role=UserRole.ADMIN)
    count = select(func.count()).select_from(ChangeLog)
    before = session.execute(count).scalar_one()

    session.add(ChangeLog(entity_type="users", entity_id=admin.id, changed_by_user_id=admin.id, field="x"))
    session.commit()

    assert session.execute(count).scalar_one() == before + 1


def test_a_leak_of_another_users_name_or_id_is_found(world: World, session: Session) -> None:
    values = hours_values(session)
    usernames = {user.username for user in world.users}

    def leaks(body: dict[str, Any], actor: User | None, stored: set[str]) -> list[str]:
        response = SimpleNamespace(json=lambda: body, text=str(body), headers={})
        return response_leaks(response, values, usernames, actor, stored)

    own_name = {"assigned_technician_name": world.tech.username}
    assert leaks(own_name, world.tech, set()) == []
    assert leaks(own_name, world.admin, {world.tech.username}) == []  # an earlier copy, stored in svc_
    assert leaks(own_name, world.admin, set())  # another user's name that was never copied
    assert leaks({"customer_name": world.tech.username}, world.tech, set())  # wrong field
    assert leaks({"assigned_technician_name": f"x {world.tech.username}"}, world.tech, set())  # not a copy
    assert leaks({"items": [{"id": str(world.bystander.id)}]}, world.tech, set())  # an id
