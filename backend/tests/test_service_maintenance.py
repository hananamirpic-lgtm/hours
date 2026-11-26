"""The orphan-sweep maintenance command (task 16.6; R11.13, R20.11).

A fake object-store client sits under a real `PhotoStore` (so every delete goes through the store's
prefix check), a fake lister stands in for the bucket listing, and the ticket table is the SQLite test
database. The real `PhotoLister` is tested against a fake client that pages.
"""

from __future__ import annotations

import io
import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.service_tickets import maintenance
from app.service_tickets.maintenance import (
    EXIT_DATABASE_FAILED,
    EXIT_DELETE_FAILED,
    EXIT_LISTING_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    ListingFailed,
    PhotoLister,
    StoredObject,
    main,
    parse_age,
    referenced_keys,
)
from app.service_tickets.models import Category, Customer, Ticket
from app.service_tickets.photo_store import KEY_PREFIX, PhotoStore

BUCKET = "ticket-test-bucket"
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=2)
RECENT = NOW - timedelta(minutes=10)

_numbers = itertools.count(10000)
_phones = itertools.count(1)


def key(name: str) -> str:
    return f"{KEY_PREFIX}{name}.jpg"


# --------------------------------------------------------------------------- fakes


class FakeClient:
    """Object-store client for `PhotoStore`: records deletes. `fail_keys` make delete_object raise."""

    def __init__(self, fail_keys: frozenset[str] = frozenset()) -> None:
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.fail_keys = fail_keys

    def delete_object(self, **kwargs):
        self.calls.append("delete_object")
        if kwargs["Key"] in self.fail_keys:
            error = {"Error": {"Code": "InternalError", "Message": "secret detail"}}
            raise ClientError(error, "DeleteObject")
        self.deleted.append(kwargs["Key"])
        return {}

    def __getattr__(self, name: str):  # anything else would be a write or a read the sweep must not make
        raise AssertionError(f"unexpected object-store call: {name}")


class FakeLister:
    """Returns the given pages as they are, including keys a real prefix listing would never return."""

    def __init__(self, *pages: list[StoredObject], error: Exception | None = None) -> None:
        self._pages = pages
        self._error = error

    def pages(self):
        yield from self._pages
        if self._error is not None:
            raise self._error


class PagingClient:
    """`list_objects_v2` over a sorted key map, `size` keys per page, honouring `Prefix`."""

    def __init__(self, objects: dict[str, datetime], size: int = 2, fail_on_page: int | None = None) -> None:
        self.objects = objects
        self.size = size
        self.fail_on_page = fail_on_page
        self.requests: list[dict] = []

    def list_objects_v2(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail_on_page is not None and len(self.requests) == self.fail_on_page:
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "secret detail"}}, "ListObjectsV2")
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken", "0"))
        chunk = keys[start : start + self.size]
        response: dict = {
            "Contents": [{"Key": k, "LastModified": self.objects[k]} for k in chunk],
            "IsTruncated": start + self.size < len(keys),
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(start + self.size)
        return response


def objects(*pairs: tuple[str, datetime | None]) -> list[StoredObject]:
    return [StoredObject(k, t) for k, t in pairs]


# --------------------------------------------------------------------------- database


@pytest.fixture
def factory(sqlite_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def make_ticket(session: Session):
    customer = Customer(
        phone=f"05{next(_phones):08d}", full_name="Dana Levi", full_name_key="dana levi", password_hash="x"
    )
    category = Category(
        seq=1, name_he="a", name_en="Electric", name_he_key="a", name_en_key="electric", is_active=True
    )
    session.add_all([customer, category])
    session.commit()

    def _make(photo_key: str | None) -> Ticket:
        ticket = Ticket(
            ticket_number=next(_numbers),
            customer_id=customer.id,
            category_id=category.id,
            city="Haifa",
            city_key="haifa",
            apartment="12",
            phone="0501234567",
            description="leaking tap, call Dana",
            photo_key=photo_key,
            urgent=False,
            status="received",
            assigned_technician_name="",
            created_at=OLD,
            created_date=date(2025, 5, 30),
            updated_at=OLD,
        )
        session.add(ticket)
        session.commit()
        return ticket

    return _make


def run(
    argv: list[str],
    lister,
    factory: sessionmaker[Session],
    client: FakeClient | None = None,
) -> tuple[int, str, str, FakeClient]:
    client = client or FakeClient()
    out, err = io.StringIO(), io.StringIO()
    code = main(
        argv,
        lister=lister,
        store=PhotoStore(client, bucket=BUCKET),
        session_factory=factory,
        now=NOW,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue(), client


# --------------------------------------------------------------------------- sweep behaviour


def test_referenced_and_recent_keys_are_never_listed_or_deleted(factory, make_ticket) -> None:
    orphan, kept, young = key("orphan"), key("kept"), key("young")
    make_ticket(kept)
    make_ticket(None)  # a ticket without a photo references nothing
    lister = FakeLister(objects((orphan, OLD), (kept, OLD), (young, RECENT)))

    code, out, err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK and err == ""
    assert client.deleted == [orphan]
    assert f"ORPHAN {orphan}" in out and f"DELETED {orphan}" in out
    assert kept not in out and young not in out
    assert "scanned=3 ignored=0 recent=1 referenced=1 orphans=1 deleted=1 failed=0" in out


def test_without_delete_nothing_is_deleted(factory, make_ticket) -> None:
    make_ticket(key("kept"))
    lister = FakeLister(objects((key("a"), OLD), (key("b"), OLD), (key("kept"), OLD)))

    code, out, _err, client = run(["sweep-orphans", "--older-than", "1h"], lister, factory)

    assert code == EXIT_OK
    assert client.calls == []
    assert f"ORPHAN {key('a')}" in out and f"ORPHAN {key('b')}" in out
    assert "DELETED" not in out and "DRY RUN" in out
    assert "orphans=2 deleted=0 failed=0" in out


def test_delete_removes_exactly_the_orphans(factory, make_ticket) -> None:
    make_ticket(key("kept1"))
    make_ticket(key("kept2"))
    lister = FakeLister(
        objects((key("o1"), OLD), (key("kept1"), OLD), (key("young"), RECENT)),
        objects((key("o2"), OLD), (key("kept2"), OLD)),
    )

    code, _out, _err, client = run(["sweep-orphans", "--older-than", "30m", "--delete"], lister, factory)

    assert code == EXIT_OK
    assert sorted(client.deleted) == [key("o1"), key("o2")]


def test_keys_outside_the_prefix_are_ignored_even_if_the_lister_returns_them(factory) -> None:
    outside = [
        "employees/1/photo.jpg",
        "exports/report.pdf",
        "Service-Tickets/x.jpg",
        "/service-tickets/x.jpg",
        "service-tickets",
        "service-tickets/",
        "service-tickets/../employees/1/photo.jpg",
        "service-tickets//x.jpg",
        "service-tickets/a\\b.jpg",
    ]
    inside = key("orphan")
    lister = FakeLister(objects(*[(k, OLD) for k in outside], (inside, OLD)))

    code, out, _err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK
    assert client.deleted == [inside]
    assert all(k.startswith(KEY_PREFIX) for k in client.deleted)
    assert f"ignored={len(outside)}" in out
    reported = [line.split()[1] for line in out.splitlines() if line.startswith("ORPHAN ")]
    assert reported == [inside]


def test_unknown_age_and_boundary_age_count_as_recent(factory) -> None:
    exactly = key("boundary")
    lister = FakeLister(
        objects((exactly, NOW - timedelta(hours=1)), (key("unknown"), None), (key("old"), OLD))
    )

    code, out, _err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK
    assert client.deleted == [key("old")]
    assert "recent=2" in out


def test_a_naive_last_modified_is_read_as_utc() -> None:
    client = PagingClient({key("a"): datetime(2025, 5, 1)})  # noqa: DTZ001 - the point of the test
    (page,) = list(PhotoLister(client, BUCKET).pages())
    assert page[0].last_modified == datetime(2025, 5, 1, tzinfo=UTC)


def test_output_never_contains_ticket_customer_data(factory, make_ticket) -> None:
    make_ticket(key("kept"))
    lister = FakeLister(objects((key("kept"), OLD), (key("o"), OLD)))

    _code, out, err, _client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    for text in (out, err):
        for private in ("Dana", "0501234567", "Haifa", "leaking", "dana levi"):
            assert private not in text


def test_a_page_of_more_than_one_lookup_chunk_is_handled(session, make_ticket) -> None:
    keys = [key(f"k{n:04d}") for n in range(1200)]
    for k in keys[::3]:
        make_ticket(k)

    assert referenced_keys(session, keys) == set(keys[::3])
    assert referenced_keys(session, []) == set()


# --------------------------------------------------------------------------- listing and pagination


def test_the_lister_pages_and_only_asks_for_the_prefix() -> None:
    stored = {key(f"{n}"): OLD for n in range(5)}
    stored["employees/1/x.jpg"] = OLD
    client = PagingClient(stored, size=2)

    pages = list(PhotoLister(client, BUCKET).pages())

    assert [len(page) for page in pages] == [2, 2, 1]
    listed = sorted(o.key for page in pages for o in page)
    assert listed == sorted(k for k in stored if k.startswith(KEY_PREFIX))
    assert all(r["Prefix"] == KEY_PREFIX and r["Bucket"] == BUCKET for r in client.requests)
    assert [r.get("ContinuationToken") for r in client.requests] == [None, "2", "4"]


def test_a_sweep_over_a_paged_listing_deletes_the_orphans_of_every_page(factory, make_ticket) -> None:
    stored = {key(f"{n}"): OLD for n in range(7)}
    stored[key("fresh")] = RECENT
    stored["employees/1/x.jpg"] = OLD
    referenced = {key("1"), key("4")}
    for k in referenced:
        make_ticket(k)
    paging = PagingClient(stored, size=3)

    code, out, _err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"], PhotoLister(paging, BUCKET), factory
    )

    expected = sorted(k for k in stored if k.startswith(KEY_PREFIX) and k not in referenced | {key("fresh")})
    assert code == EXIT_OK
    assert sorted(client.deleted) == expected
    assert len(paging.requests) == 3
    assert "scanned=8" in out


def test_a_listing_error_exits_non_zero_with_a_clear_message_and_deletes_nothing_more(factory) -> None:
    stored = {key(f"{n}"): OLD for n in range(6)}
    paging = PagingClient(stored, size=2, fail_on_page=2)

    code, out, err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"], PhotoLister(paging, BUCKET), factory
    )

    assert code == EXIT_LISTING_FAILED
    assert "object store listing failed" in err and "SlowDown" in err
    assert "secret detail" not in err and "Traceback" not in err
    assert sorted(client.deleted) == [key("0"), key("1")]  # only the first page, before the failure
    assert "deleted=2" in out


def test_a_listing_failure_before_any_page(factory) -> None:
    lister = FakeLister(error=ListingFailed("object store listing failed (EndpointConnectionError)"))

    code, _out, err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_LISTING_FAILED
    assert "EndpointConnectionError" in err
    assert client.calls == []


def test_a_listing_that_does_not_advance_stops() -> None:
    class Stuck:
        def list_objects_v2(self, **kwargs):
            return {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same"}

    with pytest.raises(ListingFailed):
        list(PhotoLister(Stuck(), BUCKET).pages())


# --------------------------------------------------------------------------- failures


def test_a_delete_failure_is_reported_and_sets_the_exit_code(factory) -> None:
    bad, good = key("bad"), key("good")
    lister = FakeLister(objects((bad, OLD), (good, OLD)))

    code, out, err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory, FakeClient(frozenset({bad}))
    )

    assert code == EXIT_DELETE_FAILED
    assert f"FAILED {bad}" in out and f"DELETED {good}" in out
    assert "failed=1" in out and "1 delete call(s) failed" in err
    assert "secret detail" not in out + err
    assert client.deleted == [good]


def test_a_database_failure_exits_non_zero_and_deletes_nothing(factory) -> None:
    def broken():
        raise OperationalError("SELECT ...", {}, Exception("password=hunter2"))

    lister = FakeLister(objects((key("a"), OLD)))

    code, out, err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory=broken)

    assert code == EXIT_DATABASE_FAILED
    assert "database read failed (OperationalError)" in err
    assert "hunter2" not in out + err
    assert client.calls == []


# --------------------------------------------------------------------------- age parsing and CLI


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5m", timedelta(minutes=5)),
        ("30m", timedelta(minutes=30)),
        ("1h", timedelta(hours=1)),
        ("36h", timedelta(hours=36)),
        ("2d", timedelta(days=2)),
        ("365d", timedelta(days=365)),
        (" 1h ", timedelta(hours=1)),
    ],
)
def test_parse_age_accepts_durations(text: str, expected: timedelta) -> None:
    assert parse_age(text) == expected


REJECTED_AGES = [
    "", "1", "h", "1x", "1.5h", "-1h", "+1h", "1h30m", "1 h", "one hour", "0m", "0h", "4m", "1s",
    "366d", "999999d", "1000000d", "1H", "\u0661h", "1h\n2h", None, 5,
]  # fmt: skip


@pytest.mark.parametrize("text", REJECTED_AGES)
def test_parse_age_rejects_nonsense_and_unsafe_values(text) -> None:
    with pytest.raises(ValueError):
        parse_age(text)


@pytest.mark.parametrize(
    "argv",
    [
        ["sweep-orphans", "--older-than", "1m", "--delete"],
        ["sweep-orphans", "--older-than", "soon"],
        ["sweep-orphans", "--older-than", "0h"],
        ["sweep-orphans", "--delete"],
        ["sweep-orphans"],
        ["unknown", "--older-than", "1h"],
        [],
    ],
)
def test_a_bad_command_line_exits_with_the_usage_code_and_touches_nothing(factory, argv) -> None:
    lister = FakeLister(objects((key("a"), OLD)))

    code, out, _err, client = run(argv, lister, factory)

    assert code == EXIT_USAGE
    assert out == "" and client.calls == []


def test_help_exits_zero(factory) -> None:
    assert run(["--help"], FakeLister(), factory)[0] == EXIT_OK


def test_the_module_is_runnable_and_imports_no_scheduler() -> None:
    text = Path(maintenance.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in text
    imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if any(w in line for w in ("apscheduler", "celery", "sched"))]
