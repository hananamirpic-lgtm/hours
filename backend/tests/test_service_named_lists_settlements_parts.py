"""The settlement and replaceable-parts lists in the configuration service (task 30.2; R30.1, R29.6).

Both lists run through the generic functions of `services/config_admin.py` that the categories share
(`create_named`, `rename_named`, ... taking a `NameList`). Every rule is checked for both lists with one
parametrised test, so a rule that holds for one and not the other fails here. The categories' own tests
(`test_service_config_admin.py`) stay as they were and prove the shared code did not change them.

Unit tests on SQLite through the shared `session` fixture. As in the categories' tests, the identity
column `seq` cannot be numbered by SQLite, so the autouse fixture `numbered` numbers each inserted row on
insert, and `foreign_keys_on` turns SQLite's foreign key check on so that deleting a used row meets a
refusing `RESTRICT` key, exactly the path PostgreSQL takes. What only PostgreSQL can show (identity
numbering, the unique-violation race) is outside these tests.

"Used" rows are created through the models: a settlement is used by `svc_tickets.locality_id` or by
`svc_customers.locality_id`, a part by `svc_ticket_parts`.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_DUPLICATE_PART,
    CODE_DUPLICATE_SETTLEMENT,
    CODE_INVALID_PART_NAME_EN,
    CODE_INVALID_PART_NAME_HE,
    CODE_INVALID_SETTLEMENT_NAME_EN,
    CODE_INVALID_SETTLEMENT_NAME_HE,
    CODE_PART_NOT_FOUND,
    CODE_SETTLEMENT_NOT_FOUND,
)
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import (
    Branding,
    Category,
    Customer,
    Locality,
    Part,
    Ticket,
    TicketPart,
)
from app.service_tickets.services import config_admin as svc
from app.service_tickets.services.config_admin import (
    DEACTIVATED,
    LOCALITIES,
    PARTS,
    REMOVED,
    ConfigValidationError,
    DuplicatePart,
    DuplicateSettlement,
    NameList,
    PartNotFound,
    SettlementNotFound,
)


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `seq` of settlements, parts and categories on insert, as PostgreSQL's identity does."""
    counter = itertools.count(1)

    def assign(_mapper, _connection, target) -> None:
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


# --- the two lists, side by side --------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """What differs between the lists in these tests."""

    name: str
    spec: NameList
    model: type
    limit: int
    invalid_he: str
    invalid_en: str
    duplicate: type[Exception]
    duplicate_code: str
    not_found: type[Exception]
    not_found_code: str
    #: Makes a row that uses the item, so the delete must deactivate it.
    use: tuple[Callable[[Session, uuid.UUID], None], ...]

    def __str__(self) -> str:
        return self.name


def _customer(session: Session, **values) -> Customer:
    customer = Customer(
        phone=values.pop("phone", "0501234567"),
        full_name="Dana",
        full_name_key=fold_key("Dana"),
        password_hash="not-a-hash",
        **values,
    )
    session.add(customer)
    session.flush()
    return customer


def _ticket(session: Session, **values) -> Ticket:
    customer = _customer(session, phone="0507654321")
    category = Category(
        name_he="כללי", name_en="General", name_he_key=fold_key("כללי"), name_en_key="general", is_active=True
    )
    session.add(category)
    session.flush()
    ticket = Ticket(
        ticket_number=values.pop("ticket_number", 10000),
        customer_id=customer.id,
        category_id=category.id,
        city="Haifa",
        city_key=fold_key("Haifa"),
        apartment="4",
        phone="0507654321",
        created_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        **values,
    )
    session.add(ticket)
    session.flush()
    return ticket


def _used_by_ticket(session: Session, locality_id: uuid.UUID) -> None:
    _ticket(session, locality_id=locality_id)
    session.commit()


def _used_by_customer(session: Session, locality_id: uuid.UUID) -> None:
    _customer(session, city="Haifa", locality_id=locality_id)
    session.commit()


def _used_by_resolved_ticket(session: Session, part_id: uuid.UUID) -> None:
    ticket = _ticket(session, status="closed")
    session.add(TicketPart(ticket_id=ticket.id, part_id=part_id))
    session.commit()


CASES = (
    Case(
        "settlements",
        LOCALITIES,
        Locality,
        100,
        CODE_INVALID_SETTLEMENT_NAME_HE,
        CODE_INVALID_SETTLEMENT_NAME_EN,
        DuplicateSettlement,
        CODE_DUPLICATE_SETTLEMENT,
        SettlementNotFound,
        CODE_SETTLEMENT_NOT_FOUND,
        (_used_by_ticket, _used_by_customer),
    ),
    Case(
        "parts",
        PARTS,
        Part,
        60,
        CODE_INVALID_PART_NAME_HE,
        CODE_INVALID_PART_NAME_EN,
        DuplicatePart,
        CODE_DUPLICATE_PART,
        PartNotFound,
        CODE_PART_NOT_FOUND,
        (_used_by_resolved_ticket,),
    ),
)

every_list = pytest.mark.parametrize("case", CASES, ids=str)


def _make(session: Session, case: Case, name_he: str, name_en: str):
    row = svc.create_named(session, case.spec, name_he, name_en)
    session.commit()
    return row


def _three(session: Session, case: Case):
    return (
        _make(session, case, "חיפה", "Haifa"),
        _make(session, case, "עכו", "Akko"),
        _make(session, case, "נהריה", "Nahariya"),
    )


def _count(session: Session, case: Case) -> int:
    return session.execute(select(func.count()).select_from(case.model)).scalar_one()


def _snapshot(session: Session, case: Case) -> list[tuple[str, str, bool]]:
    session.expire_all()
    return [(r.name_he, r.name_en, r.is_active) for r in svc.list_names(session, case.spec)]


# --- no seeds ---------------------------------------------------------------------------------------------


@every_list
def test_a_new_database_has_no_item_and_the_form_lists_none(session: Session, case: Case) -> None:
    assert svc.list_names(session, case.spec) == []
    config = svc.get_form_config(session)
    assert config.localities == ()
    assert config.settlements_required is False


# --- names: length and trimming ---------------------------------------------------------------------------


@every_list
def test_names_up_to_the_limit_are_stored_trimmed_with_their_keys(session: Session, case: Case) -> None:
    values = ("a", "א", "x" * case.limit, "א" * case.limit, "  padded  ", f" {'y' * case.limit} ")
    for index, value in enumerate(values):
        row = svc.create_named(session, case.spec, value, f"english {index}")
        assert row.name_he == value.strip()
        assert row.name_he_key == fold_key(value)
        assert row.is_active is True
        session.commit()


@every_list
@pytest.mark.parametrize("bad", ["", " ", " \t\n ", "a\x00b", "a\ud800b", None, 7])
def test_invalid_hebrew_name_gives_the_lists_own_code_and_stores_nothing(
    session: Session, case: Case, bad: object
) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_named(session, case.spec, bad, "Valid")  # type: ignore[arg-type]
    assert raised.value.code == case.invalid_he
    assert raised.value.fields == {"name_he": case.invalid_he}
    assert _count(session, case) == 0


@every_list
@pytest.mark.parametrize("bad", ["", " ", "a\x00b", None])
def test_invalid_english_name_gives_the_lists_own_code_and_stores_nothing(
    session: Session, case: Case, bad: object
) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_named(session, case.spec, "תקין", bad)  # type: ignore[arg-type]
    assert raised.value.code == case.invalid_en
    assert raised.value.fields == {"name_en": case.invalid_en}
    assert _count(session, case) == 0


@every_list
def test_one_character_over_the_limit_is_invalid_in_each_language(session: Session, case: Case) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_named(session, case.spec, "x" * (case.limit + 1), "y" * (case.limit + 1))
    assert list(raised.value.fields.items()) == [("name_he", case.invalid_he), ("name_en", case.invalid_en)]
    with pytest.raises(ConfigValidationError):
        svc.create_named(session, case.spec, " " + "x" * (case.limit + 1), "ok")
    assert _count(session, case) == 0


@every_list
def test_length_counts_characters_not_bytes(session: Session, case: Case) -> None:
    assert svc.create_named(session, case.spec, "א" * case.limit, "ok").name_he == "א" * case.limit
    with pytest.raises(ConfigValidationError):
        svc.create_named(session, case.spec, "ב" * (case.limit + 1), "other")


def test_the_two_lists_have_their_own_limits_and_codes() -> None:
    assert (LOCALITIES.max_length, PARTS.max_length) == (100, 60)
    assert LOCALITIES.invalid_he_code == "invalid_settlement_name_he"
    assert PARTS.invalid_en_code == "invalid_part_name_en"
    assert svc.CATEGORIES.max_length == 60
    assert svc.CATEGORIES.invalid_he_code == "invalid_name_he"
    assert [spec.keep_one_active for spec in (svc.CATEGORIES, LOCALITIES, PARTS)] == [True, False, False]


def test_a_settlement_name_of_61_to_100_characters_is_valid_but_a_part_name_is_not(session: Session) -> None:
    name = "ת" * 80
    assert svc.create_named(session, LOCALITIES, name, "Long settlement").name_he == name
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_named(session, PARTS, name, "Long part")
    assert raised.value.code == CODE_INVALID_PART_NAME_HE


# --- names: duplicates ------------------------------------------------------------------------------------


@every_list
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Haifa", "haifa"),
        ("Haifa", "  HAIFA \t"),
        ("Straße", "STRASSE"),  # full case folding
        ("Café", "Cafe\u0301"),  # precomposed letter against base letter plus combining accent
        ("Привет", "пРиВеТ"),  # beyond ASCII, which a C-locale database cannot fold
    ],
)
def test_english_duplicate_is_refused_ignoring_case_whitespace_and_composition(
    session: Session, case: Case, first: str, second: str
) -> None:
    _make(session, case, "שם ראשון", first)
    before = _snapshot(session, case)
    with pytest.raises(case.duplicate) as raised:
        svc.create_named(session, case.spec, "שם אחר", second)
    assert raised.value.code == case.duplicate_code
    assert raised.value.fields == ("name_en",)
    assert _snapshot(session, case) == before


@every_list
def test_hebrew_duplicate_is_refused_with_surrounding_whitespace(session: Session, case: Case) -> None:
    _make(session, case, "חיפה", "Haifa")
    with pytest.raises(case.duplicate) as raised:
        svc.create_named(session, case.spec, "  חיפה  ", "Another")
    assert raised.value.fields == ("name_he",)
    assert _count(session, case) == 1


@every_list
def test_duplicate_in_both_languages_names_both(session: Session, case: Case) -> None:
    _make(session, case, "חיפה", "Haifa")
    with pytest.raises(case.duplicate) as raised:
        svc.create_named(session, case.spec, "חיפה", "haifa")
    assert raised.value.fields == ("name_he", "name_en")


@every_list
def test_duplicate_is_checked_against_inactive_items_too(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    svc.deactivate_named(session, case.spec, first.id)
    session.commit()
    with pytest.raises(case.duplicate):
        svc.create_named(session, case.spec, "חדש", "HAIFA")


@every_list
def test_same_text_in_different_languages_is_not_a_duplicate(session: Session, case: Case) -> None:
    _make(session, case, "Haifa", "חיפה")
    assert svc.create_named(session, case.spec, "חיפה", "Haifa").name_he == "חיפה"


@every_list
def test_validation_is_reported_before_duplication(session: Session, case: Case) -> None:
    _make(session, case, "חיפה", "Haifa")
    with pytest.raises(ConfigValidationError):
        svc.create_named(session, case.spec, "חיפה", "")


def test_the_same_name_may_exist_in_both_lists_and_in_categories(session: Session) -> None:
    svc.create_named(session, LOCALITIES, "מסנן", "Filter")
    svc.create_named(session, PARTS, "מסנן", "Filter")
    svc.create_category(session, "מסנן", "Filter")
    session.commit()
    assert len(svc.list_names(session, LOCALITIES)) == len(svc.list_names(session, PARTS)) == 1


# --- rename -----------------------------------------------------------------------------------------------


@every_list
def test_rename_changes_both_names_and_keys_and_keeps_id_and_order(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    renamed = svc.rename_named(session, case.spec, first.id, "  חיפה החדשה ", " New Haifa ")
    session.commit()
    assert renamed.id == first.id
    assert (renamed.name_he, renamed.name_en) == ("חיפה החדשה", "New Haifa")
    assert (renamed.name_he_key, renamed.name_en_key) == ("חיפה החדשה", "new haifa")
    assert renamed.seq == first.seq


@every_list
def test_rename_to_its_own_current_names_is_not_a_duplicate(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    again = svc.rename_named(session, case.spec, first.id, "חיפה", "HAIFA")  # only the case differs
    assert again.name_en == "HAIFA"


@every_list
def test_rename_into_another_items_name_is_refused_and_changes_nothing(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    before = _snapshot(session, case)
    with pytest.raises(case.duplicate) as raised:
        svc.rename_named(session, case.spec, first.id, "חדש לגמרי", " akko")
    assert raised.value.fields == ("name_en",)
    assert _snapshot(session, case) == before


@every_list
@pytest.mark.parametrize("bad", [("תקין", ""), ("", "Valid"), ("x" * 101, "Valid")])
def test_rename_with_one_invalid_text_changes_neither_language(
    session: Session, case: Case, bad: tuple[str, str]
) -> None:
    first, _, _ = _three(session, case)
    before = _snapshot(session, case)
    with pytest.raises(ConfigValidationError):
        svc.rename_named(session, case.spec, first.id, *bad)
    assert _snapshot(session, case) == before


@every_list
def test_rename_can_free_a_name_for_reuse(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    svc.rename_named(session, case.spec, first.id, "חיפה חדשה", "Haifa new")
    session.commit()
    assert svc.create_named(session, case.spec, "חיפה", "Haifa").name_en == "Haifa"


@every_list
def test_rename_of_an_unknown_item_is_not_found_with_the_lists_code(session: Session, case: Case) -> None:
    with pytest.raises(case.not_found) as raised:
        svc.rename_named(session, case.spec, uuid.uuid4(), "חיפה", "Haifa")
    assert raised.value.code == case.not_found_code


# --- order, activation (there is no "last active" rule) ---------------------------------------------------


@every_list
def test_items_are_listed_oldest_first_by_seq_and_the_form_lists_active_ones(
    session: Session, case: Case
) -> None:
    first, second, third = _three(session, case)
    svc.rename_named(session, case.spec, first.id, "זזז", "Zeta")  # a name that would sort last
    svc.deactivate_named(session, case.spec, second.id)
    session.commit()

    assert [r.id for r in svc.list_names(session, case.spec)] == [first.id, second.id, third.id]
    assert [r.id for r in svc.list_names(session, case.spec, active_only=True)] == [first.id, third.id]


@every_list
def test_deactivate_and_reactivate_set_and_clear_the_flag_and_are_idempotent(
    session: Session, case: Case
) -> None:
    first, _, _ = _three(session, case)
    assert svc.deactivate_named(session, case.spec, first.id).is_active is False
    assert svc.deactivate_named(session, case.spec, first.id).is_active is False
    assert svc.reactivate_named(session, case.spec, first.id).is_active is True
    assert svc.reactivate_named(session, case.spec, first.id).is_active is True


@every_list
def test_every_item_may_be_deactivated_including_the_last_active_one(session: Session, case: Case) -> None:
    first, second, third = _three(session, case)
    for row in (first, second, third):
        svc.deactivate_named(session, case.spec, row.id)
    session.commit()
    assert svc.list_names(session, case.spec, active_only=True) == []
    assert len(svc.list_names(session, case.spec)) == 3


@every_list
def test_deactivate_and_reactivate_of_an_unknown_item_are_not_found(session: Session, case: Case) -> None:
    with pytest.raises(case.not_found):
        svc.deactivate_named(session, case.spec, uuid.uuid4())
    with pytest.raises(case.not_found):
        svc.reactivate_named(session, case.spec, uuid.uuid4())


def test_the_form_config_lists_only_active_settlements_oldest_first_and_requires_when_any(
    session: Session,
) -> None:
    first, second, third = _three(session, CASES[0])
    svc.deactivate_named(session, LOCALITIES, second.id)
    session.commit()

    config = svc.get_form_config(session)

    assert [row.id for row in config.localities] == [first.id, third.id]
    assert [row.name_en for row in config.localities] == ["Haifa", "Nahariya"]
    assert config.settlements_required is True

    svc.deactivate_named(session, LOCALITIES, first.id)
    svc.deactivate_named(session, LOCALITIES, third.id)
    session.commit()
    config = svc.get_form_config(session)
    assert config.localities == ()
    assert config.settlements_required is False


def test_the_parts_list_is_not_part_of_the_form_config(session: Session) -> None:
    _three(session, CASES[1])
    assert not hasattr(svc.get_form_config(session), "parts")


# --- delete: removed or deactivated -----------------------------------------------------------------------


@every_list
def test_delete_of_an_unused_item_removes_it(session: Session, case: Case) -> None:
    first, second, third = _three(session, case)
    assert svc.delete_named(session, case.spec, first.id) == REMOVED
    session.commit()
    assert [r.id for r in svc.list_names(session, case.spec)] == [second.id, third.id]
    assert session.get(case.model, first.id) is None


@every_list
def test_delete_of_an_inactive_unused_item_removes_it_too(session: Session, case: Case) -> None:
    first, _, _ = _three(session, case)
    svc.deactivate_named(session, case.spec, first.id)
    assert svc.delete_named(session, case.spec, first.id) == REMOVED
    session.commit()
    assert session.get(case.model, first.id) is None


@every_list
def test_delete_of_the_only_item_is_allowed(session: Session, case: Case) -> None:
    only = _make(session, case, "חיפה", "Haifa")
    assert svc.delete_named(session, case.spec, only.id) == REMOVED
    session.commit()
    assert _count(session, case) == 0


@every_list
def test_delete_of_a_used_item_deactivates_it_and_keeps_the_row(
    session: Session, case: Case, foreign_keys_on: None
) -> None:
    for index, use in enumerate(case.use):
        row = _make(session, case, f"שם {index}", f"Name {index}")
        use(session, row.id)

        assert svc.delete_named(session, case.spec, row.id) == DEACTIVATED
        session.commit()

        session.expire_all()
        kept = session.get(case.model, row.id)
        assert kept is not None and kept.is_active is False
        assert (kept.name_he, kept.name_en) == (f"שם {index}", f"Name {index}")


@every_list
def test_a_deactivated_used_item_can_be_reactivated_and_deleted_again_stays_deactivated(
    session: Session, case: Case, foreign_keys_on: None
) -> None:
    row = _make(session, case, "חיפה", "Haifa")
    case.use[0](session, row.id)
    assert svc.delete_named(session, case.spec, row.id) == DEACTIVATED
    assert svc.reactivate_named(session, case.spec, row.id).is_active is True
    assert svc.delete_named(session, case.spec, row.id) == DEACTIVATED
    session.commit()
    assert session.get(case.model, row.id) is not None


@every_list
def test_delete_of_an_unknown_item_is_not_found(session: Session, case: Case) -> None:
    with pytest.raises(case.not_found) as raised:
        svc.delete_named(session, case.spec, uuid.uuid4())
    assert raised.value.code == case.not_found_code


def test_a_settlement_used_only_by_a_customer_is_not_removed(session: Session, foreign_keys_on: None) -> None:
    row = _make(session, CASES[0], "חיפה", "Haifa")
    _used_by_customer(session, row.id)
    assert svc.delete_named(session, LOCALITIES, row.id) == DEACTIVATED


def test_a_part_that_no_ticket_uses_is_removed_even_when_other_parts_are_used(
    session: Session, foreign_keys_on: None
) -> None:
    used = _make(session, CASES[1], "מסנן", "Filter")
    spare = _make(session, CASES[1], "אטם", "Seal")
    _used_by_resolved_ticket(session, used.id)

    assert svc.delete_named(session, PARTS, spare.id) == REMOVED
    assert svc.delete_named(session, PARTS, used.id) == DEACTIVATED
    session.commit()
    assert [p.name_en for p in svc.list_names(session, PARTS)] == ["Filter"]


# --- the branding block of the form -----------------------------------------------------------------------


def test_form_branding_reads_empty_without_a_row_and_from_the_row_when_there_is_one(session: Session) -> None:
    assert svc.get_form_branding(session) == svc.FormBranding(company_name=None, has_logo=False)
    assert svc.get_form_config(session).branding == svc.FormBranding(company_name=None, has_logo=False)

    session.add(Branding(id=1, company_name=None, logo_key=None))
    session.commit()
    assert svc.get_form_branding(session) == svc.FormBranding(company_name=None, has_logo=False)

    row = session.get(Branding, 1)
    row.company_name = "Acme"
    row.logo_key = "service-tickets/branding/x.png"
    session.commit()
    assert svc.get_form_config(session).branding == svc.FormBranding(company_name="Acme", has_logo=True)
