"""Example tests for the customers service (Requirements 3.2 to 3.14 and 6.7).

Runs on the shared SQLite `session` fixture. What only PostgreSQL can show (two real connections racing
for one phone, `LIKE` on a `C` locale) is in `tests/integration/test_service_customers_pg.py`.

Rows that a test only needs as background (a page of 55 customers) are inserted directly with one shared
bcrypt hash; every behaviour under test goes through the service itself.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.user import User
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import Category, Customer, SigninFailure, Ticket
from app.service_tickets.services import customers
from app.service_tickets.services.customers import (
    CustomerNotFound,
    CustomerValidationError,
    PhoneAlreadyRegistered,
    create_customer,
    deactivate_customer,
    get_customer,
    list_customers,
    reactivate_customer,
    reset_password,
    update_customer,
)

PHONE = "0505556677"
OTHER_PHONE = "0521234567"


def _row(session: Session, customer_id: uuid.UUID) -> Customer:
    row = session.get(Customer, customer_id, populate_existing=True)
    assert row is not None
    return row


def _same(view: customers.CustomerView) -> dict[str, object]:
    """The view as a dict with naive timestamps: SQLite returns `timestamptz` values without a zone."""
    values = dataclasses.asdict(view)
    for name in ("created_at", "updated_at"):
        values[name] = values[name].replace(tzinfo=None)
    return values


def _count(session: Session, model: type) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


@pytest.fixture
def seed(session: Session, default_password_hash: str):
    """Insert a customer row directly, with a precomputed hash. Returns the row."""
    numbers = itertools.count(1)

    def _seed(name: str, phone: str | None = None, **overrides: object) -> Customer:
        fields: dict[str, object] = {
            "phone": phone or f"05{next(numbers):08d}",
            "full_name": name,
            "full_name_key": fold_key(name),
            "password_hash": default_password_hash,
        }
        fields.update(overrides)
        row = Customer(**fields)
        session.add(row)
        session.flush()
        return row

    return _seed


# --------------------------------------------------------------------------- create


def test_create_stores_normalized_phone_trimmed_fields_and_the_initial_password(session: Session) -> None:
    view = create_customer(
        session, phone="+972 50-555-6677", full_name="  Avi Cohen \n", city=" Haifa ", apartment=" 4B "
    )
    row = _row(session, view.id)

    assert (view.phone, view.full_name, view.city, view.apartment) == (PHONE, "Avi Cohen", "Haifa", "4B")
    assert (row.phone, row.full_name, row.city, row.apartment) == (PHONE, "Avi Cohen", "Haifa", "4B")
    assert row.full_name_key == fold_key("Avi Cohen")
    assert row.is_active is True
    assert row.must_change_password is True
    assert row.token_version == 1
    assert row.password_hash.startswith("$2")
    assert verify_password("1234", row.password_hash)
    assert not verify_password("0000", row.password_hash)


@pytest.mark.parametrize("empty", [None, "", "   ", "\t\n"])
def test_create_stores_empty_city_and_apartment_as_absent(session: Session, empty: str | None) -> None:
    view = create_customer(session, phone=PHONE, full_name="Avi", city=empty, apartment=empty)
    row = _row(session, view.id)
    assert view.city is None and view.apartment is None
    assert row.city is None and row.apartment is None


def test_create_adds_no_row_to_users(session: Session) -> None:
    create_customer(session, phone=PHONE, full_name="Avi")
    assert _count(session, User) == 0
    assert _count(session, Customer) == 1


def test_two_new_customers_get_different_hashes_of_the_same_password(session: Session) -> None:
    first = create_customer(session, phone=PHONE, full_name="A")
    second = create_customer(session, phone=OTHER_PHONE, full_name="B")
    hashes = {_row(session, first.id).password_hash, _row(session, second.id).password_hash}
    assert len(hashes) == 2  # salted
    assert all(verify_password("1234", value) for value in hashes)


def test_the_service_does_not_commit(session: Session) -> None:
    # pysqlite opens a transaction only at the first INSERT/UPDATE/DELETE, and a SAVEPOINT issued before
    # that point is released as a COMMIT. A first write puts the session in a real transaction, as on
    # PostgreSQL, where the integration test repeats this without the workaround.
    session.add(SigninFailure(phone=OTHER_PHONE, failed_at=datetime.now(UTC)))
    session.flush()
    created = create_customer(session, phone=PHONE, full_name="Avi")
    deactivate_customer(session, created.id)
    reset_password(session, created.id)
    update_customer(session, created.id, phone=OTHER_PHONE, full_name="Avi 2")
    session.rollback()
    assert _count(session, Customer) == 0
    assert _count(session, SigninFailure) == 0


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("name", "ok"),
    [("a", True), ("a" * 100, True), ("א" * 100, True), (" " + "a" * 100 + " ", True), ("a" * 101, False)],
)
def test_full_name_limit_counts_characters_after_trimming(session: Session, name: str, ok: bool) -> None:
    if ok:
        assert create_customer(session, phone=PHONE, full_name=name).full_name == name.strip()
        return
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone=PHONE, full_name=name)
    assert raised.value.fields == {"full_name": "invalid_full_name"}
    assert _count(session, Customer) == 0


@pytest.mark.parametrize("name", ["", "   ", "\t\n ", None, 5])
def test_empty_or_non_text_full_name_is_invalid(session: Session, name: object) -> None:
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone=PHONE, full_name=name)  # type: ignore[arg-type]
    assert raised.value.code == "invalid_full_name"
    assert _count(session, Customer) == 0


@pytest.mark.parametrize(
    ("field", "limit", "code"),
    [("city", 100, "invalid_city"), ("apartment", 20, "invalid_apartment")],
)
def test_city_and_apartment_limits(session: Session, field: str, limit: int, code: str) -> None:
    at_limit = create_customer(session, phone=PHONE, full_name="A", **{field: "x" * limit})
    assert getattr(at_limit, field) == "x" * limit
    # Trailing whitespace does not count towards the limit.
    padded = create_customer(session, phone=OTHER_PHONE, full_name="B", **{field: "x" * limit + "   "})
    assert getattr(padded, field) == "x" * limit

    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone="0531111111", full_name="C", **{field: "x" * (limit + 1)})
    assert raised.value.fields == {field: code}
    assert _count(session, Customer) == 2


@pytest.mark.parametrize("value", [5, ["x"], b"x"])
def test_city_and_apartment_that_are_not_text_are_invalid(session: Session, value: object) -> None:
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone=PHONE, full_name="A", city=value, apartment=value)  # type: ignore[arg-type]
    assert raised.value.fields == {"city": "invalid_city", "apartment": "invalid_apartment"}


def test_every_failing_field_gets_its_own_code_in_reading_order(session: Session) -> None:
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone="12", full_name=" ", city="c" * 101, apartment="a" * 21)
    assert list(raised.value.fields.items()) == [
        ("phone", "invalid_phone"),
        ("full_name", "invalid_full_name"),
        ("city", "invalid_city"),
        ("apartment", "invalid_apartment"),
    ]
    assert raised.value.code == "invalid_phone"  # the first failing field
    assert _count(session, Customer) == 0


def test_only_the_failing_fields_are_reported(session: Session) -> None:
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone=PHONE, full_name="A", city="c" * 101, apartment="ok")
    assert raised.value.fields == {"city": "invalid_city"}
    assert raised.value.code == "invalid_city"


@pytest.mark.parametrize(
    "phone",
    [
        "", None, "050-555-667", "05055566778", "+9720505556677", "1505556677", "050.555.6677",
        "050\t5556677", "0" * 31,
    ],
)
def test_invalid_phone_gives_invalid_phone_and_stores_nothing(session: Session, phone: str | None) -> None:
    with pytest.raises(CustomerValidationError) as raised:
        create_customer(session, phone=phone, full_name="Avi")  # type: ignore[arg-type]
    assert raised.value.fields == {"phone": "invalid_phone"}
    assert _count(session, Customer) == 0


# --------------------------------------------------------------------------- duplicate phone


def test_duplicate_phone_is_refused_whatever_the_spelling_and_the_state(session: Session) -> None:
    first = create_customer(session, phone=PHONE, full_name="First")
    with pytest.raises(PhoneAlreadyRegistered) as active_case:
        create_customer(session, phone="050 555 6677", full_name="Second")
    assert active_case.value.code == "phone_already_registered"

    deactivate_customer(session, first.id)
    with pytest.raises(PhoneAlreadyRegistered):
        create_customer(session, phone="972505556677", full_name="Third")
    assert _count(session, Customer) == 1


def test_edit_to_the_phone_of_another_customer_is_refused_and_changes_nothing(session: Session) -> None:
    create_customer(session, phone=PHONE, full_name="First")
    second = create_customer(session, phone=OTHER_PHONE, full_name="Second", city="Haifa")
    with pytest.raises(PhoneAlreadyRegistered):
        update_customer(session, second.id, phone="(050) 555-6677", full_name="Renamed", city="Akko")
    row = _row(session, second.id)
    assert (row.phone, row.full_name, row.city) == (OTHER_PHONE, "Second", "Haifa")


def test_edit_may_keep_the_own_phone_in_another_spelling(session: Session) -> None:
    view = create_customer(session, phone=PHONE, full_name="First")
    edited = update_customer(session, view.id, phone="+972-50-555-6677", full_name="First Renamed")
    assert edited.phone == PHONE
    assert edited.full_name == "First Renamed"


def test_lost_race_on_the_phone_becomes_a_conflict_and_the_transaction_survives(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests passed the existence check; the unique constraint decides, inside a savepoint."""
    winner = create_customer(session, phone=PHONE, full_name="Winner")  # pending, never committed
    real = customers._phone_taken
    calls: list[str] = []

    def blind_on_first_call(*args: object, **kwargs: object) -> bool:
        calls.append("call")
        return False if len(calls) == 1 else real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(customers, "_phone_taken", blind_on_first_call)
    with pytest.raises(PhoneAlreadyRegistered):
        create_customer(session, phone="050-555-6677", full_name="Loser")

    assert len(calls) >= 2  # the pre-check was blind, the read after the violation was not
    # The savepoint rolled back only the loser. The earlier pending insert is intact and commits.
    assert list_customers(session).total == 1
    session.commit()
    assert _row(session, winner.id).full_name == "Winner"
    assert _count(session, Customer) == 1


def test_lost_race_on_an_edit_leaves_the_customer_unchanged(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_customer(session, phone=PHONE, full_name="First")
    second = create_customer(session, phone=OTHER_PHONE, full_name="Second")
    session.commit()
    real = customers._phone_taken
    calls: list[str] = []

    def blind_on_first_call(*args: object, **kwargs: object) -> bool:
        calls.append("call")
        return False if len(calls) == 1 else real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(customers, "_phone_taken", blind_on_first_call)
    with pytest.raises(PhoneAlreadyRegistered):
        update_customer(session, second.id, phone=PHONE, full_name="Renamed")
    row = _row(session, second.id)
    assert (row.phone, row.full_name) == (OTHER_PHONE, "Second")


# --------------------------------------------------------------------------- edit


def test_edit_changes_the_four_fields_and_nothing_else(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi", city="Haifa", apartment="1")
    before = _row(session, created.id)
    snapshot = (before.password_hash, before.must_change_password, before.is_active, before.token_version)

    edited = update_customer(
        session, created.id, phone=OTHER_PHONE, full_name=" Dana Levi ", city=None, apartment="  "
    )
    row = _row(session, created.id)
    expected = (OTHER_PHONE, "Dana Levi", None, None)
    assert (edited.phone, edited.full_name, edited.city, edited.apartment) == expected
    assert (row.phone, row.full_name, row.city, row.apartment) == expected
    assert row.full_name_key == fold_key("Dana Levi")
    assert (row.password_hash, row.must_change_password, row.is_active, row.token_version) == snapshot


def test_edit_validates_before_it_looks_for_the_customer(session: Session) -> None:
    with pytest.raises(CustomerValidationError):
        update_customer(session, uuid.uuid4(), phone="bad", full_name="A")


def test_edit_keeps_the_phone_city_and_apartment_of_existing_tickets(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi", city="Haifa", apartment="4")
    category = Category(
        seq=1, name_he="א", name_en="a", name_he_key=fold_key("א"), name_en_key=fold_key("a")
    )
    session.add(category)
    session.flush()
    ticket = Ticket(
        ticket_number=10000,
        customer_id=created.id,
        category_id=category.id,
        city="Haifa",
        city_key=fold_key("Haifa"),
        apartment="4",
        phone=PHONE,
        created_at=datetime(2025, 1, 5, 9, 0, tzinfo=UTC),
        created_date=date(2025, 1, 5),
    )
    session.add(ticket)
    session.flush()

    update_customer(session, created.id, phone=OTHER_PHONE, full_name="Dana", city="Akko", apartment="9")

    session.refresh(ticket)
    assert (ticket.city, ticket.apartment, ticket.phone) == ("Haifa", "4", PHONE)
    assert _row(session, created.id).phone == OTHER_PHONE


# --------------------------------------------------------------------------- deactivate, reactivate, reset


def test_deactivate_sets_inactive_and_bumps_the_version(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    view = deactivate_customer(session, created.id)
    row = _row(session, created.id)
    assert view.is_active is False and row.is_active is False
    assert row.token_version == 2
    assert deactivate_customer(session, created.id).is_active is False
    assert _row(session, created.id).token_version == 3  # a repeat still revokes whatever was issued


def test_deactivate_keeps_hash_flag_and_tickets_state(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    before = _row(session, created.id)
    password_hash, must_change = before.password_hash, before.must_change_password
    deactivate_customer(session, created.id)
    row = _row(session, created.id)
    assert (row.password_hash, row.must_change_password) == (password_hash, must_change)


def test_reactivation_keeps_the_hash_the_flag_and_the_version(
    session: Session, default_password_hash: str
) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    row = _row(session, created.id)
    row.password_hash = default_password_hash  # a password the customer chose
    row.must_change_password = False
    session.flush()

    deactivate_customer(session, created.id)
    version_after_deactivation = _row(session, created.id).token_version
    view = reactivate_customer(session, created.id)

    row = _row(session, created.id)
    assert view.is_active is True and row.is_active is True
    assert row.password_hash == default_password_hash
    assert row.must_change_password is False
    assert row.token_version == version_after_deactivation == 2


def test_reset_replaces_the_hash_sets_the_flag_and_bumps_the_version(
    session: Session, default_password_hash: str
) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    row = _row(session, created.id)
    row.password_hash = default_password_hash
    row.must_change_password = False
    session.flush()

    reset_password(session, created.id)

    row = _row(session, created.id)
    assert row.password_hash != default_password_hash
    assert verify_password("1234", row.password_hash)
    assert row.must_change_password is True
    assert row.token_version == 2
    assert row.is_active is True


def test_reset_works_for_an_inactive_customer_and_leaves_them_inactive(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    deactivate_customer(session, created.id)
    view = reset_password(session, created.id)
    row = _row(session, created.id)
    assert view.is_active is False and row.is_active is False
    assert row.token_version == 3
    assert verify_password("1234", row.password_hash)


def test_reset_clears_only_the_failed_sign_ins_of_that_phone(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    now = datetime.now(UTC)
    session.add_all(
        [SigninFailure(phone=PHONE, failed_at=now) for _ in range(5)]
        + [SigninFailure(phone=OTHER_PHONE, failed_at=now)]
    )
    session.flush()

    reset_password(session, created.id)

    remaining = session.execute(select(SigninFailure.phone)).scalars().all()
    assert remaining == [OTHER_PHONE]


def test_edit_deactivate_and_reactivate_leave_the_failed_sign_ins_alone(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    session.add(SigninFailure(phone=PHONE, failed_at=datetime.now(UTC)))
    session.flush()
    update_customer(session, created.id, phone=PHONE, full_name="Avi 2")
    deactivate_customer(session, created.id)
    reactivate_customer(session, created.id)
    assert _count(session, SigninFailure) == 1


def test_a_phone_edit_keeps_tokens_valid_and_the_old_phone_free(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    update_customer(session, created.id, phone=OTHER_PHONE, full_name="Avi")
    assert _row(session, created.id).token_version == 1
    # The previous phone is unregistered again, so someone else can take it.
    assert create_customer(session, phone=PHONE, full_name="Other").phone == PHONE


# --------------------------------------------------------------------------- missing customer


@pytest.mark.parametrize(
    "action",
    [
        lambda s, i: update_customer(s, i, phone=PHONE, full_name="A"),
        deactivate_customer,
        reactivate_customer,
        reset_password,
        get_customer,
    ],
    ids=["update", "deactivate", "reactivate", "reset", "get"],
)
def test_missing_customer_gives_not_found_and_changes_nothing(session: Session, action) -> None:
    bystander = create_customer(session, phone=OTHER_PHONE, full_name="Bystander")
    session.add(SigninFailure(phone=PHONE, failed_at=datetime.now(UTC)))
    session.flush()
    before = _same(bystander)

    with pytest.raises(CustomerNotFound) as raised:
        action(session, uuid.uuid4())

    assert raised.value.code == "customer_not_found"
    assert _same(get_customer(session, bystander.id)) == before
    assert _count(session, Customer) == 1
    assert _count(session, SigninFailure) == 1
    assert _row(session, bystander.id).token_version == 1


def test_get_customer_returns_the_view(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi", city="Haifa")
    assert _same(get_customer(session, created.id)) == _same(created)


# --------------------------------------------------------------------------- no secrets in results


def test_no_result_carries_a_hash_or_the_initial_password(session: Session) -> None:
    created = create_customer(session, phone=PHONE, full_name="Avi")
    results = [
        created,
        update_customer(session, created.id, phone=PHONE, full_name="Avi"),
        deactivate_customer(session, created.id),
        reactivate_customer(session, created.id),
        reset_password(session, created.id),
        get_customer(session, created.id),
    ]
    page = list_customers(session)
    results.extend(page.items)

    forbidden_names = {"password", "hash", "token"}
    for result in results:
        values = dataclasses.asdict(result)
        assert not any(word in name for name in values for word in forbidden_names)
        assert "$2" not in repr(result)
        assert "1234" not in values.values()
    assert "$2" not in repr(page)


# --------------------------------------------------------------------------- list


def test_list_is_sorted_by_name_key_and_paged_by_fifty(session: Session, seed) -> None:
    for number in range(55):
        seed(f"Name {number:02d}")
    first = list_customers(session)
    second = list_customers(session, page=2)
    beyond = list_customers(session, page=3)

    assert (first.page, first.page_size, first.total) == (1, 50, 55)
    assert len(first.items) == 50 and len(second.items) == 5
    assert [item.full_name for item in first.items + second.items] == [f"Name {n:02d}" for n in range(55)]
    assert (beyond.items, beyond.total, beyond.page) == ((), 55, 3)


def test_list_sorts_case_insensitively_and_by_the_folded_key(session: Session, seed) -> None:
    for name in ["bravo", "Alpha", "charlie", "ÉCLAIR", "Delta", "אבי"]:
        seed(name)
    names = [item.full_name for item in list_customers(session).items]
    assert names == sorted(names, key=fold_key)
    assert names[:3] == ["Alpha", "bravo", "charlie"]


def test_list_of_nothing(session: Session) -> None:
    page = list_customers(session)
    assert (page.items, page.total, page.page, page.page_size) == ((), 0, 1, 50)


def test_list_includes_every_state_by_default(session: Session, seed) -> None:
    seed("Active One")
    seed("Inactive One", is_active=False)
    assert list_customers(session).total == 2
    assert [c.full_name for c in list_customers(session, active=True).items] == ["Active One"]
    assert [c.full_name for c in list_customers(session, active=False).items] == ["Inactive One"]


def test_search_by_name_ignores_case_and_matches_any_part(session: Session, seed) -> None:
    seed("Avi Cohen")
    seed("Dana Levi")
    seed("Éli Katz")
    assert [c.full_name for c in list_customers(session, search="COH").items] == ["Avi Cohen"]
    assert [c.full_name for c in list_customers(session, search="i c").items] == ["Avi Cohen"]
    assert [c.full_name for c in list_customers(session, search="éLI").items] == ["Éli Katz"]
    assert [c.full_name for c in list_customers(session, search="  levi ").items] == ["Dana Levi"]
    assert list_customers(session, search="zzz").total == 0


def test_search_by_phone_substring_after_removing_separators(session: Session, seed) -> None:
    seed("One", phone="0505556677")
    seed("Two", phone="0521234567")
    assert [c.full_name for c in list_customers(session, search="555").items] == ["One"]
    assert [c.full_name for c in list_customers(session, search="(050) 555-").items] == ["One"]
    assert [c.full_name for c in list_customers(session, search="52 123").items] == ["Two"]
    assert [c.full_name for c in list_customers(session, search="6677").items] == ["One"]


def test_search_matches_name_or_phone(session: Session, seed) -> None:
    seed("Number 0521", phone="0501111111")
    seed("Plain", phone="0529999999")
    seed("Other", phone="0535210000")
    found = {c.full_name for c in list_customers(session, search="0521").items}
    assert found == {"Number 0521"}
    found = {c.full_name for c in list_customers(session, search="521").items}
    assert found == {"Number 0521", "Other"}


def test_search_and_the_active_filter_are_combined_by_and(session: Session, seed) -> None:
    seed("Avi Active", phone="0501000001")
    seed("Avi Inactive", phone="0501000002", is_active=False)
    seed("Dana Active", phone="0501000003")
    assert [c.full_name for c in list_customers(session, search="avi", active=True).items] == ["Avi Active"]
    assert [c.full_name for c in list_customers(session, search="avi", active=False).items] == [
        "Avi Inactive"
    ]
    assert list_customers(session, search="dana", active=False).total == 0
    assert list_customers(session, search="avi").total == 2


def test_total_counts_all_matches_not_only_the_page(session: Session, seed) -> None:
    for number in range(60):
        seed(f"Match {number:02d}")
    seed("Other")
    page = list_customers(session, search="match", page=2)
    assert page.total == 60 and len(page.items) == 10


@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("%", {"100% sure"}),
        ("_", {"a_b"}),
        ("a_b", {"a_b"}),
        ("0%", {"100% sure"}),
        ("/", {"x/y"}),
        ("\\", {"back\\slash"}),
    ],
)
def test_like_wildcards_in_the_search_match_only_themselves(
    session: Session, seed, search: str, expected: set[str]
) -> None:
    for name in ["100% sure", "a_b", "axb", "x/y", "back\\slash", "plain"]:
        seed(name)
    assert {c.full_name for c in list_customers(session, search=search).items} == expected


@pytest.mark.parametrize("search", ["not a phone", "abc", "12", "+97", "05055566778899", "\t"])
def test_a_search_that_is_not_a_valid_phone_never_raises(session: Session, seed, search: str) -> None:
    seed("Someone")
    assert list_customers(session, search=search).total >= 0


def test_search_of_only_separators_matches_everything_like_an_empty_phone_fragment(
    session: Session, seed
) -> None:
    seed("One")
    seed("Two")
    assert list_customers(session, search=" - ()").total == 2
    assert list_customers(session, search="").total == 2
    assert list_customers(session, search=None).total == 2
