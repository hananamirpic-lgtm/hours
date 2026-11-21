"""Example tests for ticket validation and creation (Requirements 9.4, 9.5, 9.6, 9.8, 9.9, 9.13, 9.14,
10.3, 10.6, 14.1).

`validate_submission` is pure and runs on plain mappings. `create_ticket` runs on the SQLite test
database with an injected allocator, since SQLite has no sequences.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.service_tickets import error_codes
from app.service_tickets.models import Category, Customer, Ticket
from app.service_tickets.services.config_admin import FormSettings
from app.service_tickets.services.numbering import TicketNumbersExhausted
from app.service_tickets.services.tickets import (
    CleanSubmission,
    SubmissionValidationError,
    create_ticket,
    validate_submission,
)

CATEGORY_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_CATEGORY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
ACTIVE = frozenset({CATEGORY_ID})

DESCRIPTION_REQUIRED = FormSettings(description_required=True, photo_upload_enabled=True)
DESCRIPTION_OPTIONAL = FormSettings(description_required=False, photo_upload_enabled=True)

FORM_ORDER = ("city", "apartment", "phone", "category_id", "description", "urgent")


def good_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "city": "Haifa",
        "apartment": "12",
        "phone": "050-123-4567",
        "category_id": str(CATEGORY_ID),
        "description": "The kitchen tap leaks.",
        "urgent": "false",
    }
    values.update(overrides)
    return values


def failures(values: dict[str, object], settings: FormSettings = DESCRIPTION_REQUIRED) -> dict[str, str]:
    with pytest.raises(SubmissionValidationError) as caught:
        validate_submission(values, settings, ACTIVE)
    return caught.value.fields


# --------------------------------------------------------------------------------- validation


def test_valid_submission_returns_trimmed_and_normalized_values() -> None:
    clean = validate_submission(
        good_values(city="  Haifa  ", apartment=" 12a ", phone="+972 50-123-4567", urgent=True),
        DESCRIPTION_REQUIRED,
        ACTIVE,
    )
    assert clean == CleanSubmission(
        city="Haifa",
        apartment="12a",
        phone="0501234567",
        category_id=CATEGORY_ID,
        description="The kitchen tap leaks.",
        urgent=True,
    )


def test_every_field_failing_gives_one_code_per_field_in_form_order() -> None:
    fields = failures(
        {
            "city": "",
            "apartment": "x" * 21,
            "phone": "12345",
            "category_id": "not-a-uuid",
            "description": "",
            "urgent": "maybe",
        }
    )
    assert fields == {
        "city": error_codes.CODE_INVALID_CITY,
        "apartment": error_codes.CODE_INVALID_APARTMENT,
        "phone": error_codes.CODE_INVALID_PHONE,
        "category_id": error_codes.CODE_INVALID_CATEGORY,
        "description": error_codes.CODE_INVALID_DESCRIPTION,
        "urgent": error_codes.CODE_INVALID_URGENT,
    }
    assert tuple(fields) == FORM_ORDER


def test_the_top_level_code_is_the_first_failing_field() -> None:
    with pytest.raises(SubmissionValidationError) as caught:
        validate_submission(good_values(phone="x", urgent="maybe"), DESCRIPTION_REQUIRED, ACTIVE)
    assert caught.value.code == error_codes.CODE_INVALID_PHONE
    assert tuple(caught.value.fields) == ("phone", "urgent")


def test_an_empty_form_fails_every_required_field() -> None:
    fields = failures({})
    assert set(fields) == {"city", "apartment", "phone", "category_id", "description"}
    assert failures({}, DESCRIPTION_OPTIONAL).keys() == {"city", "apartment", "phone", "category_id"}


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("city", "c" * 100, True),
        ("city", "c" * 101, False),
        ("city", "   ", False),
        ("apartment", "a" * 20, True),
        ("apartment", "a" * 21, False),
        ("apartment", "", False),
        ("description", "d" * 2000, True),
        ("description", "d" * 2001, False),
        ("description", "  " + "d" * 2000 + "  ", True),
    ],
)
def test_length_limits_count_characters_after_trimming(field: str, value: str, ok: bool) -> None:
    values = good_values(**{field: value})
    if ok:
        assert validate_submission(values, DESCRIPTION_REQUIRED, ACTIVE)
    else:
        assert field in failures(values)


def test_length_counts_unicode_characters_not_bytes() -> None:
    validate_submission(good_values(city="ח" * 100), DESCRIPTION_REQUIRED, ACTIVE)
    assert "city" in failures(good_values(city="ח" * 101))


def test_whitespace_only_description_is_allowed_when_optional_and_stored_empty() -> None:
    clean = validate_submission(good_values(description=" \t\n "), DESCRIPTION_OPTIONAL, ACTIVE)
    assert clean.description == ""


def test_a_missing_description_is_empty_when_optional_and_invalid_when_required() -> None:
    values = good_values()
    del values["description"]
    assert validate_submission(values, DESCRIPTION_OPTIONAL, ACTIVE).description == ""
    assert failures(values, DESCRIPTION_REQUIRED) == {"description": error_codes.CODE_INVALID_DESCRIPTION}


def test_whitespace_only_description_fails_when_required() -> None:
    assert failures(good_values(description="   ")) == {"description": error_codes.CODE_INVALID_DESCRIPTION}


def test_too_long_description_fails_even_when_optional() -> None:
    assert "description" in failures(good_values(description="d" * 2001), DESCRIPTION_OPTIONAL)


def test_markup_is_kept_verbatim() -> None:
    clean = validate_submission(
        good_values(city="<b>x</b>", apartment="<i>1</i>", description="  <script>alert(1)</script> &amp; "),
        DESCRIPTION_REQUIRED,
        ACTIVE,
    )
    assert clean.city == "<b>x</b>"
    assert clean.apartment == "<i>1</i>"
    assert clean.description == "<script>alert(1)</script> &amp;"


def test_urgent_defaults_to_false_and_accepts_booleans_and_their_text() -> None:
    values = good_values()
    del values["urgent"]
    assert validate_submission(values, DESCRIPTION_REQUIRED, ACTIVE).urgent is False
    for raw, expected in ((True, True), (False, False), ("true", True), (" TRUE ", True), ("False", False)):
        assert validate_submission(good_values(urgent=raw), DESCRIPTION_REQUIRED, ACTIVE).urgent is expected


@pytest.mark.parametrize("raw", ["", "1", "0", "yes", "on", None, 1, 0])
def test_other_urgent_values_fail(raw: object) -> None:
    assert failures(good_values(urgent=raw)) == {"urgent": error_codes.CODE_INVALID_URGENT}


def test_an_inactive_or_unknown_category_fails() -> None:
    assert failures(good_values(category_id=str(OTHER_CATEGORY_ID))) == {
        "category_id": error_codes.CODE_INVALID_CATEGORY
    }
    assert "category_id" in failures(good_values(category_id=str(uuid.uuid4())))


def test_category_may_be_a_uuid_object_and_is_rejected_when_missing_or_malformed() -> None:
    assert validate_submission(good_values(category_id=CATEGORY_ID), DESCRIPTION_REQUIRED, ACTIVE)
    for raw in (None, "", 7, "{" + str(CATEGORY_ID) + "}", CATEGORY_ID.hex):
        assert failures(good_values(category_id=raw)) == {"category_id": error_codes.CODE_INVALID_CATEGORY}


def test_an_empty_active_set_rejects_every_category() -> None:
    with pytest.raises(SubmissionValidationError) as caught:
        validate_submission(good_values(), DESCRIPTION_REQUIRED, frozenset())
    assert caught.value.fields == {"category_id": error_codes.CODE_INVALID_CATEGORY}


@pytest.mark.parametrize("phone", ["", "050123456", "0501234567890", "abc", None, 501234567, "٠٥٠١٢٣٤٥٦٧"])
def test_invalid_phones_fail(phone: object) -> None:
    assert failures(good_values(phone=phone)) == {"phone": error_codes.CODE_INVALID_PHONE}


@pytest.mark.parametrize(
    "field", ["customer_id", "status", "ticket_number", "assigned_technician_name", "history"]
)
def test_an_extra_field_is_an_unknown_field(field: str) -> None:
    fields = failures(good_values(**{field: "x"}))
    assert fields == {field: error_codes.CODE_UNKNOWN_FIELD}


def test_unknown_fields_come_after_the_validated_ones() -> None:
    fields = failures(good_values(city="", status="closed"))
    assert tuple(fields) == ("city", "status")
    assert fields["status"] == error_codes.CODE_UNKNOWN_FIELD


def test_the_photo_key_is_a_known_field() -> None:
    assert validate_submission(good_values(photo=b"bytes"), DESCRIPTION_REQUIRED, ACTIVE)


def test_values_postgresql_cannot_store_are_invalid() -> None:
    assert failures(good_values(city="Hai\x00fa")) == {"city": error_codes.CODE_INVALID_CITY}
    assert failures(good_values(description="bad \ud800")) == {
        "description": error_codes.CODE_INVALID_DESCRIPTION
    }


def test_non_text_values_fail_their_field() -> None:
    fields = failures(good_values(city=5, apartment=["1"], description={"a": 1}))
    assert fields == {
        "city": error_codes.CODE_INVALID_CITY,
        "apartment": error_codes.CODE_INVALID_APARTMENT,
        "description": error_codes.CODE_INVALID_DESCRIPTION,
    }


def test_the_validation_error_needs_a_failing_field() -> None:
    with pytest.raises(ValueError):
        SubmissionValidationError({})


# --------------------------------------------------------------------------------- creation


class Numbers:
    """An allocator that records how many numbers it handed out and what happened around it."""

    def __init__(self, first: int = 10000) -> None:
        self.next = first
        self.calls = 0
        self.events: list[str] = []

    def __call__(self, _session: Session) -> int:
        self.calls += 1
        self.events.append("allocate")
        number = self.next
        self.next += 1
        return number


@pytest.fixture
def owner(session: Session) -> Customer:
    customer = Customer(phone="0501234567", full_name="Owner", full_name_key="owner", password_hash="x")
    session.add(customer)
    session.commit()
    return customer


@pytest.fixture
def category(session: Session) -> Category:
    row = Category(
        id=CATEGORY_ID, seq=1, name_he="חשמל", name_en="Electric", name_he_key="חשמל", name_en_key="electric"
    )
    session.add(row)
    session.commit()
    return row


def submit(
    session: Session,
    customer: Customer,
    values: dict[str, object],
    allocator: Callable[[Session], int],
    *,
    settings: FormSettings = DESCRIPTION_REQUIRED,
    photo_key: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> Ticket:
    """The order the router follows: validate first, create only when validation passed."""
    clean = validate_submission(values, settings, ACTIVE)
    return create_ticket(session, customer, clean, photo_key, now, allocator)


def ticket_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Ticket)).scalar_one()


def test_create_stores_trimmed_verbatim_text_normalized_phone_and_initial_state(
    session: Session, owner: Customer, category: Category
) -> None:
    numbers = Numbers()
    ticket = submit(
        session,
        owner,
        good_values(
            city="  <b>x</b>  ",
            apartment=" 7 ",
            phone="+972-50-765-4321",
            description="  <script>1</script>  ",
            urgent="true",
        ),
        numbers,
        photo_key="service-tickets/abc.jpg",
    )
    session.commit()
    stored = session.execute(select(Ticket)).scalar_one()
    assert stored.ticket_number == 10000 == ticket.ticket_number
    assert stored.customer_id == owner.id
    assert stored.category_id == CATEGORY_ID
    assert stored.city == "<b>x</b>"
    assert stored.city_key == "<b>x</b>"
    assert stored.apartment == "7"
    assert stored.phone == "0507654321"
    assert stored.description == "<script>1</script>"
    assert stored.urgent is True
    assert stored.status == "received"
    assert stored.assigned_technician_name == ""
    assert stored.photo_key == "service-tickets/abc.jpg"


def test_a_ticket_without_a_photo_has_no_photo_key(
    session: Session, owner: Customer, category: Category
) -> None:
    assert submit(session, owner, good_values(), Numbers()).photo_key is None


def test_an_optional_whitespace_description_is_stored_empty(
    session: Session, owner: Customer, category: Category
) -> None:
    ticket = submit(session, owner, good_values(description="   "), Numbers(), settings=DESCRIPTION_OPTIONAL)
    assert ticket.description == ""


def test_the_owner_comes_only_from_the_customer_argument(
    session: Session, owner: Customer, category: Category
) -> None:
    with pytest.raises(SubmissionValidationError):
        submit(session, owner, good_values(customer_id=str(uuid.uuid4())), Numbers())
    assert ticket_count(session) == 0


def test_created_at_is_taken_after_the_number_is_allocated(
    session: Session, owner: Customer, category: Category
) -> None:
    numbers = Numbers()
    instant = datetime(2031, 6, 15, 8, 30, tzinfo=UTC)

    def clock() -> datetime:
        numbers.events.append("clock")
        return instant

    ticket = submit(session, owner, good_values(), numbers, now=clock)
    assert numbers.events == ["allocate", "clock"]
    assert ticket.created_at == instant
    assert ticket.updated_at == instant


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2031, 6, 15, 20, 59, tzinfo=UTC), "2031-06-15"),
        (datetime(2031, 6, 15, 21, 0, tzinfo=UTC), "2031-06-16"),  # summer time: UTC+3
        (datetime(2031, 12, 15, 21, 30, tzinfo=UTC), "2031-12-15"),
        (datetime(2031, 12, 15, 22, 0, tzinfo=UTC), "2031-12-16"),  # winter time: UTC+2
    ],
)
def test_created_date_is_the_calendar_date_in_the_application_zone(
    session: Session, owner: Customer, category: Category, instant: datetime, expected: str
) -> None:
    ticket = submit(session, owner, good_values(), Numbers(), now=lambda: instant)
    assert ticket.created_date.isoformat() == expected


def test_created_date_uses_the_zone_given(session: Session, owner: Customer, category: Category) -> None:
    clean = validate_submission(good_values(), DESCRIPTION_REQUIRED, ACTIVE)
    instant = datetime(2031, 6, 15, 21, 0, tzinfo=UTC)
    ticket = create_ticket(session, owner, clean, None, lambda: instant, Numbers(), timezone="UTC")
    assert ticket.created_date.isoformat() == "2031-06-15"


def test_numbers_rise_in_allocation_order(session: Session, owner: Customer, category: Category) -> None:
    numbers = Numbers(10041)
    first = submit(session, owner, good_values(), numbers)
    second = submit(session, owner, good_values(), numbers)
    assert (first.ticket_number, second.ticket_number) == (10041, 10042)


def test_an_invalid_submission_burns_no_number(session: Session, owner: Customer, category: Category) -> None:
    numbers = Numbers()
    inactive = good_values(category_id=str(OTHER_CATEGORY_ID))
    for bad in (good_values(city=""), good_values(extra="x"), inactive):
        with pytest.raises(SubmissionValidationError):
            submit(session, owner, bad, numbers)
    assert numbers.calls == 0
    assert ticket_count(session) == 0
    assert submit(session, owner, good_values(), numbers).ticket_number == 10000


def test_exhausted_numbers_create_nothing(session: Session, owner: Customer, category: Category) -> None:
    def exhausted(_session: Session) -> int:
        raise TicketNumbersExhausted

    with pytest.raises(TicketNumbersExhausted):
        submit(session, owner, good_values(), exhausted)
    assert ticket_count(session) == 0


def test_creating_a_ticket_leaves_the_customer_unchanged(
    session: Session, owner: Customer, category: Category
) -> None:
    def snapshot() -> tuple[object, ...]:
        row = session.execute(
            select(
                Customer.phone,
                Customer.full_name,
                Customer.full_name_key,
                Customer.city,
                Customer.apartment,
                Customer.password_hash,
                Customer.must_change_password,
                Customer.is_active,
                Customer.token_version,
                Customer.created_at,
                Customer.updated_at,
            ).where(Customer.id == owner.id)
        ).one()
        return tuple(row)

    before = snapshot()
    submit(session, owner, good_values(city="Elsewhere", apartment="99", phone="0529999999"), Numbers())
    session.commit()
    assert snapshot() == before
    assert owner.city is None and owner.apartment is None
