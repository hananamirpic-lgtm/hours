"""The orphan sweep and the company logo (task 30.5; R28.6, R11.13).

The logo object lives under `service-tickets/branding/` and is referenced by `svc_branding.logo_key`, not by
a ticket. The sweep must never report or delete the stored logo, must still report an old logo object that
nothing references, and must treat photos as before.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_service_maintenance import OLD, RECENT, FakeLister, key, objects, run

from app.service_tickets.maintenance import EXIT_OK, referenced_keys
from app.service_tickets.models import Branding, Category, Customer, Ticket
from app.service_tickets.photo_store import KEY_PREFIX, LOGO_KEY_PREFIX, new_key, new_logo_key

_numbers = itertools.count(30000)


def logo_key(char: str = "a") -> str:
    return f"{LOGO_KEY_PREFIX}{char * 32}.png"


@pytest.fixture
def factory(sqlite_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False, future=True)


def store_logo(session: Session, stored_key: str | None) -> None:
    session.add(Branding(id=1, company_name="Acme", logo_key=stored_key))
    session.commit()


def add_ticket(session: Session, photo_key: str | None) -> None:
    customer = Customer(
        phone=f"05{next(_numbers):08d}", full_name="Dana Levi", full_name_key="dana levi", password_hash="x"
    )
    category = Category(
        seq=next(_numbers), name_he="a", name_en="b", name_he_key="a", name_en_key=str(next(_numbers))
    )
    session.add_all([customer, category])
    session.commit()
    session.add(
        Ticket(
            ticket_number=next(_numbers),
            customer_id=customer.id,
            category_id=category.id,
            city="Haifa",
            city_key="haifa",
            apartment="12",
            phone="0501234567",
            description="leaking tap",
            photo_key=photo_key,
            urgent=False,
            status="received",
            assigned_technician_name="",
            created_at=OLD,
            created_date=date(2025, 5, 30),
            updated_at=OLD,
        )
    )
    session.commit()


def test_the_stored_logo_is_referenced_and_other_keys_are_not(session: Session) -> None:
    stored = logo_key("a")
    store_logo(session, stored)

    assert referenced_keys(session, [stored, logo_key("b"), key("photo")]) == {stored}


def test_with_no_logo_stored_nothing_under_the_logo_prefix_is_referenced(session: Session) -> None:
    store_logo(session, None)

    assert referenced_keys(session, [logo_key("a")]) == set()
    assert referenced_keys(session, []) == set()


def test_with_no_branding_row_at_all_the_lookup_still_works(session: Session) -> None:
    assert referenced_keys(session, [logo_key("a"), key("x")]) == set()


def test_photos_and_the_logo_are_both_found_in_one_lookup(session: Session) -> None:
    stored = logo_key("a")
    store_logo(session, stored)
    add_ticket(session, key("kept"))

    assert referenced_keys(session, [stored, key("kept"), key("other")]) == {stored, key("kept")}


def test_the_sweep_never_reports_or_deletes_the_stored_logo(session: Session, factory) -> None:
    stored = logo_key("a")
    store_logo(session, stored)
    lister = FakeLister(objects((stored, OLD)))

    code, out, err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK and err == ""
    assert client.deleted == [] and client.calls == []
    assert stored not in out and "ORPHAN" not in out
    assert "scanned=1 ignored=0 recent=0 referenced=1 orphans=0 deleted=0 failed=0" in out


def test_the_stored_logo_is_not_reported_in_a_dry_run_either(session: Session, factory) -> None:
    stored = new_logo_key()
    store_logo(session, stored)

    code, out, _err, client = run(
        ["sweep-orphans", "--older-than", "1h"], FakeLister(objects((stored, OLD))), factory
    )

    assert code == EXIT_OK and "ORPHAN" not in out and client.calls == []


def test_an_unreferenced_object_under_the_logo_prefix_is_an_orphan(session: Session, factory) -> None:
    current, old_one = logo_key("a"), logo_key("b")
    store_logo(session, current)
    lister = FakeLister(objects((current, OLD), (old_one, OLD)))

    code, out, _err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK
    assert client.deleted == [old_one]
    assert f"ORPHAN {old_one}" in out and f"DELETED {old_one}" in out
    assert current not in out
    assert "referenced=1 orphans=1 deleted=1 failed=0" in out


def test_an_unreferenced_logo_object_is_an_orphan_when_no_logo_is_stored(session: Session, factory) -> None:
    store_logo(session, None)
    leftover = new_logo_key()

    code, out, _err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"], FakeLister(objects((leftover, OLD))), factory
    )

    assert code == EXIT_OK and client.deleted == [leftover]
    assert f"ORPHAN {leftover}" in out


def test_a_young_logo_object_is_never_swept_even_when_unreferenced(session: Session, factory) -> None:
    """The new object is written before the commit, so a young one may be about to be referenced."""
    fresh = logo_key("c")

    code, out, _err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"], FakeLister(objects((fresh, RECENT))), factory
    )

    assert code == EXIT_OK and client.calls == [] and "recent=1" in out


def test_an_object_under_the_prefix_that_is_not_a_photo_or_logo_key_is_ignored(
    session: Session, factory
) -> None:
    odd = [
        f"{KEY_PREFIX}branding/not-a-logo.png",
        f"{KEY_PREFIX}branding/{'A' * 32}.png",
        f"{KEY_PREFIX}branding/{'a' * 32}.jpg",
        f"{KEY_PREFIX}branding/other/{'a' * 32}.png",
        f"{KEY_PREFIX}other/{'a' * 32}.png",
    ]

    code, out, _err, client = run(
        ["sweep-orphans", "--older-than", "1h", "--delete"],
        FakeLister(objects(*[(k, OLD) for k in odd])),
        factory,
    )

    assert code == EXIT_OK and client.calls == []
    assert f"ignored={len(odd)}" in out and "ORPHAN" not in out


def test_photo_behaviour_is_unchanged_next_to_a_stored_logo(session: Session, factory) -> None:
    stored, kept, orphan = logo_key("a"), new_key(), new_key()
    store_logo(session, stored)
    add_ticket(session, kept)
    lister = FakeLister(objects((stored, OLD), (kept, OLD), (orphan, OLD), (key("young"), RECENT)))

    code, out, _err, client = run(["sweep-orphans", "--older-than", "1h", "--delete"], lister, factory)

    assert code == EXIT_OK
    assert client.deleted == [orphan]
    assert "scanned=4 ignored=0 recent=1 referenced=2 orphans=1 deleted=1 failed=0" in out
    assert stored not in out and kept not in out


def test_a_page_with_more_than_one_lookup_chunk_still_finds_the_logo(session: Session) -> None:
    stored = logo_key("a")
    store_logo(session, stored)
    keys = [key(f"k{n:04d}") for n in range(1200)]
    keys.insert(777, stored)

    assert referenced_keys(session, keys) == {stored}
