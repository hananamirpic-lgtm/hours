"""Example tests for the configuration service (Requirements 19.2 to 19.13 and 9.3).

Unit tests on SQLite through the shared `session` fixture. Two things SQLite cannot do are handled here:

* `Category.seq` is an identity column, which SQLite cannot number, so the autouse fixture `numbered`
  gives every inserted category the next number through a `before_insert` listener, the stand-in for the
  database's identity numbering. The listener is removed after each test.
* SQLite checks foreign keys only after `PRAGMA foreign_keys=ON`; the fixture `foreign_keys_on` turns it
  on and asserts that it took effect, so the "delete a used category" test really meets a refusing
  foreign key.

What only PostgreSQL can show (identity numbering, `SELECT ... FOR UPDATE` serialising the last-active
rule, the unique-violation race) is in `tests/integration/test_service_config_admin_pg.py`.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_CATEGORY_NOT_FOUND,
    CODE_DUPLICATE_CATEGORY,
    CODE_INVALID_LABEL_EN,
    CODE_INVALID_LABEL_HE,
    CODE_INVALID_NAME_EN,
    CODE_INVALID_NAME_HE,
    CODE_LAST_ACTIVE_CATEGORY,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Category,
    Customer,
    FieldLabel,
    ServiceSettings,
    Ticket,
)
from app.service_tickets.services import config_admin as svc
from app.service_tickets.services.config_admin import (
    DEACTIVATED,
    REMOVED,
    CategoryNotFound,
    ConfigValidationError,
    DuplicateCategory,
    LastActiveCategory,
)


@pytest.fixture(autouse=True)
def numbered() -> Iterator[None]:
    """Number `Category.seq` on insert, as PostgreSQL's identity column does."""
    counter = itertools.count(1)

    def assign(_mapper, _connection, target: Category) -> None:
        if target.seq is None:
            target.seq = next(counter)

    event.listen(Category, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(Category, "before_insert", assign)


@pytest.fixture
def foreign_keys_on(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def _make(session: Session, name_he: str, name_en: str) -> Category:
    category = svc.create_category(session, name_he, name_en)
    session.commit()
    return category


def _three(session: Session) -> tuple[Category, Category, Category]:
    return (
        _make(session, "חשמל", "Electrical"),
        _make(session, "אינסטלציה", "Plumbing"),
        _make(session, "כללי", "General"),
    )


def _ticket_for(session: Session, category: Category) -> Ticket:
    customer = Customer(
        phone="0501234567", full_name="Dana", full_name_key=fold_key("Dana"), password_hash="not-a-hash"
    )
    session.add(customer)
    session.flush()
    ticket = Ticket(
        ticket_number=10000,
        customer_id=customer.id,
        category_id=category.id,
        city="Haifa",
        city_key=fold_key("Haifa"),
        apartment="4",
        phone="0501234567",
        created_date=date(2025, 1, 1),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add(ticket)
    session.commit()
    return ticket


def _count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Category)).scalar_one()


def _snapshot(session: Session) -> list[tuple[str, str, bool]]:
    session.expire_all()
    return [(c.name_he, c.name_en, c.is_active) for c in svc.list_categories(session)]


# --- names: length and trimming (Requirements 19.5, 19.6) ------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["a", "א", "x" * 60, "א" * 60, "  padded  ", " " + "x" * 60 + " ", "\t" + "x" * 60 + "\n"],
)
def test_names_of_one_to_sixty_characters_after_trimming_are_stored_trimmed(
    session: Session, value: str
) -> None:
    category = svc.create_category(session, value, "english")
    assert category.name_he == value.strip()
    assert category.name_he_key == fold_key(value)
    assert category.is_active is True


@pytest.mark.parametrize(
    "bad",
    ["", " ", " \t\n ", "x" * 61, " " + "x" * 61, "א" * 61, "a\x00b", "a\ud800b", None, 7],
)
def test_invalid_hebrew_name_gives_its_own_code_and_stores_nothing(session: Session, bad: object) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_category(session, bad, "Valid")  # type: ignore[arg-type]
    assert raised.value.code == CODE_INVALID_NAME_HE
    assert raised.value.fields == {"name_he": CODE_INVALID_NAME_HE}
    assert _count(session) == 0


@pytest.mark.parametrize("bad", ["", " ", "x" * 61, "a\x00b", None])
def test_invalid_english_name_gives_its_own_code_and_stores_nothing(session: Session, bad: object) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_category(session, "תקין", bad)  # type: ignore[arg-type]
    assert raised.value.code == CODE_INVALID_NAME_EN
    assert raised.value.fields == {"name_en": CODE_INVALID_NAME_EN}
    assert _count(session) == 0


def test_both_names_invalid_reports_both_codes_hebrew_first(session: Session) -> None:
    with pytest.raises(ConfigValidationError) as raised:
        svc.create_category(session, "", "x" * 61)
    assert raised.value.code == CODE_INVALID_NAME_HE
    assert list(raised.value.fields.items()) == [
        ("name_he", CODE_INVALID_NAME_HE),
        ("name_en", CODE_INVALID_NAME_EN),
    ]


def test_length_counts_characters_not_bytes(session: Session) -> None:
    # 60 Hebrew letters are 120 bytes in UTF-8; 61 are one too many.
    assert svc.create_category(session, "א" * 60, "ok").name_he == "א" * 60
    with pytest.raises(ConfigValidationError):
        svc.create_category(session, "ב" * 61, "other")


# --- names: duplicates (Requirement 19.7) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Electrical", "electrical"),
        ("Electrical", "  ELECTRICAL \t"),
        ("Straße", "STRASSE"),  # full case folding
        ("Café", "Cafe\u0301"),  # precomposed letter against base letter plus combining accent
        ("Привет", "пРиВеТ"),  # beyond ASCII, which a C-locale database cannot fold
    ],
)
def test_english_duplicate_is_refused_ignoring_case_whitespace_and_composition(
    session: Session, first: str, second: str
) -> None:
    _make(session, "שם ראשון", first)
    before = _snapshot(session)
    with pytest.raises(DuplicateCategory) as raised:
        svc.create_category(session, "שם אחר", second)
    assert raised.value.code == CODE_DUPLICATE_CATEGORY
    assert raised.value.fields == ("name_en",)
    assert _snapshot(session) == before


def test_hebrew_duplicate_is_refused_with_surrounding_whitespace(session: Session) -> None:
    _make(session, "חשמל", "Electrical")
    with pytest.raises(DuplicateCategory) as raised:
        svc.create_category(session, "  חשמל  ", "Another")
    assert raised.value.fields == ("name_he",)
    assert _count(session) == 1


def test_duplicate_in_both_languages_names_both(session: Session) -> None:
    _make(session, "חשמל", "Electrical")
    with pytest.raises(DuplicateCategory) as raised:
        svc.create_category(session, "חשמל", "electrical")
    assert raised.value.fields == ("name_he", "name_en")


def test_duplicate_is_checked_against_inactive_categories_too(session: Session) -> None:
    first, second, _ = _three(session)
    svc.deactivate_category(session, first.id)
    session.commit()
    with pytest.raises(DuplicateCategory):
        svc.create_category(session, "חדש", "ELECTRICAL")
    assert second.is_active


def test_same_text_in_different_languages_is_not_a_duplicate(session: Session) -> None:
    _make(session, "Plumbing", "אינסטלציה")
    created = svc.create_category(session, "אינסטלציה", "Plumbing")
    assert created.name_he == "אינסטלציה"


def test_validation_is_reported_before_duplication(session: Session) -> None:
    _make(session, "חשמל", "Electrical")
    with pytest.raises(ConfigValidationError):
        svc.create_category(session, "חשמל", "")


# --- rename (Requirements 19.5, 19.7, 19.8) ---------------------------------------------------------------


def test_rename_changes_both_names_and_keys_and_keeps_the_category(session: Session) -> None:
    electrical, _, _ = _three(session)
    renamed = svc.rename_category(session, electrical.id, "  חשמלאי ", " Electrician ")
    session.commit()
    assert renamed.id == electrical.id
    assert (renamed.name_he, renamed.name_en) == ("חשמלאי", "Electrician")
    assert (renamed.name_he_key, renamed.name_en_key) == ("חשמלאי", "electrician")
    assert renamed.seq == electrical.seq


def test_rename_to_its_own_current_names_is_not_a_duplicate(session: Session) -> None:
    electrical, _, _ = _three(session)
    again = svc.rename_category(session, electrical.id, "חשמל", "ELECTRICAL")  # only the case differs
    assert again.name_en == "ELECTRICAL"


def test_rename_into_another_categorys_name_is_refused_and_changes_nothing(session: Session) -> None:
    electrical, _, _ = _three(session)
    before = _snapshot(session)
    with pytest.raises(DuplicateCategory) as raised:
        svc.rename_category(session, electrical.id, "חדש לגמרי", " plumbing")
    assert raised.value.fields == ("name_en",)
    assert _snapshot(session) == before


@pytest.mark.parametrize(("name_he", "name_en"), [("תקין", ""), ("", "Valid"), ("x" * 61, "Valid")])
def test_rename_with_one_invalid_text_changes_neither_language(
    session: Session, name_he: str, name_en: str
) -> None:
    electrical, _, _ = _three(session)
    before = _snapshot(session)
    with pytest.raises(ConfigValidationError):
        svc.rename_category(session, electrical.id, name_he, name_en)
    assert _snapshot(session) == before


def test_rename_can_free_a_name_for_reuse(session: Session) -> None:
    electrical, _, _ = _three(session)
    svc.rename_category(session, electrical.id, "חשמל חדש", "Electrical new")
    session.commit()
    assert svc.create_category(session, "חשמל", "Electrical").name_en == "Electrical"


def test_renamed_category_shows_its_new_name_to_existing_tickets(session: Session) -> None:
    electrical, _, _ = _three(session)
    ticket = _ticket_for(session, electrical)
    svc.rename_category(session, electrical.id, "חשמלאות", "Electrics")
    session.commit()
    session.expire_all()
    assert session.get(Category, ticket.category_id).name_en == "Electrics"


# --- order, activation and reading inactive categories (Requirements 9.3, 19.9, 19.10) --------------------


def test_form_categories_are_active_only_and_oldest_first(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, second.id)
    session.commit()
    config = svc.get_form_config(session)
    assert [c.id for c in config.categories] == [first.id, third.id]
    assert [c.name_en for c in config.categories] == ["Electrical", "General"]


def test_order_follows_seq_and_survives_rename_and_reactivation(session: Session) -> None:
    first, second, third = _three(session)
    svc.rename_category(session, first.id, "חשמל חדש", "Zeta")  # a name that would sort last
    svc.deactivate_category(session, first.id)
    svc.reactivate_category(session, first.id)
    session.commit()
    assert [c.id for c in svc.get_form_config(session).categories] == [first.id, second.id, third.id]
    assert [c.id for c in svc.list_categories(session)] == [first.id, second.id, third.id]


def test_inactive_categories_stay_readable_for_filters_and_tickets(session: Session) -> None:
    first, second, _ = _three(session)
    ticket = _ticket_for(session, second)
    svc.deactivate_category(session, second.id)
    session.commit()

    assert [c.id for c in svc.list_categories(session)][1] == second.id  # staff filters keep it
    assert second.id not in [c.id for c in svc.list_categories(session, active_only=True)]
    assert second.id not in [c.id for c in svc.get_form_config(session).categories]
    assert svc.get_category(session, second.id).is_active is False
    assert session.get(Category, ticket.category_id).name_en == "Plumbing"  # the ticket still shows it
    assert first.is_active


def test_reactivation_lists_the_category_again(session: Session) -> None:
    _, second, _ = _three(session)
    svc.deactivate_category(session, second.id)
    session.commit()
    svc.reactivate_category(session, second.id)
    session.commit()
    assert second.id in [c.id for c in svc.get_form_config(session).categories]


def test_deactivating_an_inactive_category_and_reactivating_an_active_one_are_no_ops(
    session: Session,
) -> None:
    first, second, _ = _three(session)
    svc.deactivate_category(session, second.id)
    assert svc.deactivate_category(session, second.id).is_active is False
    assert svc.reactivate_category(session, first.id).is_active is True


# --- the last active category (Requirement 19.13) ---------------------------------------------------------


def test_the_last_active_category_cannot_be_deactivated(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, first.id)
    svc.deactivate_category(session, second.id)
    session.commit()
    before = _snapshot(session)
    with pytest.raises(LastActiveCategory) as raised:
        svc.deactivate_category(session, third.id)
    assert raised.value.code == CODE_LAST_ACTIVE_CATEGORY
    assert _snapshot(session) == before
    assert [c.id for c in svc.get_form_config(session).categories] == [third.id]


def test_the_last_active_category_cannot_be_deleted(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, first.id)
    svc.deactivate_category(session, second.id)
    session.commit()
    before = _snapshot(session)
    with pytest.raises(LastActiveCategory):
        svc.delete_category(session, third.id)
    assert _snapshot(session) == before


def test_a_single_category_is_the_last_active_one(session: Session) -> None:
    only = _make(session, "חשמל", "Electrical")
    with pytest.raises(LastActiveCategory):
        svc.deactivate_category(session, only.id)
    with pytest.raises(LastActiveCategory):
        svc.delete_category(session, only.id)
    assert only.is_active and _count(session) == 1


def test_inactive_categories_do_not_count_as_active(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, second.id)
    svc.deactivate_category(session, third.id)
    session.commit()
    with pytest.raises(LastActiveCategory):
        svc.deactivate_category(session, first.id)


def test_two_active_categories_allow_one_removal_and_refuse_the_next(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, first.id)  # three active to two
    assert svc.delete_category(session, second.id) == REMOVED  # two to one
    session.commit()
    with pytest.raises(LastActiveCategory):
        svc.deactivate_category(session, third.id)


def test_reactivating_makes_room_for_the_next_removal(session: Session) -> None:
    first, second, third = _three(session)
    svc.deactivate_category(session, first.id)
    svc.deactivate_category(session, second.id)
    session.commit()
    svc.reactivate_category(session, first.id)  # first and third are active
    session.commit()
    assert svc.deactivate_category(session, third.id).is_active is False
    session.commit()
    with pytest.raises(LastActiveCategory):
        svc.deactivate_category(session, first.id)

# --- delete: unused versus used (Requirements 19.11, 19.12) -----------------------------------------------


def test_deleting_an_unused_category_removes_it(session: Session) -> None:
    first, second, _ = _three(session)
    assert svc.delete_category(session, second.id) == REMOVED
    session.commit()
    assert session.get(Category, second.id) is None
    assert [c.id for c in svc.list_categories(session)][0] == first.id
    assert _count(session) == 2


def test_deleting_a_used_category_deactivates_it_and_keeps_the_ticket(
    session: Session, foreign_keys_on: None
) -> None:
    first, second, third = _three(session)
    ticket = _ticket_for(session, second)
    assert svc.delete_category(session, second.id) == DEACTIVATED
    session.commit()  # the session is still usable after the refused DELETE
    session.expire_all()
    kept = session.get(Category, second.id)
    assert kept is not None and kept.is_active is False
    assert (kept.name_he, kept.name_en) == ("אינסטלציה", "Plumbing")
    assert session.get(Ticket, ticket.id).category_id == second.id
    assert [c.id for c in svc.get_form_config(session).categories] == [first.id, third.id]


def test_deleting_a_used_category_leaves_other_pending_work_intact(
    session: Session, foreign_keys_on: None
) -> None:
    first, second, _ = _three(session)
    _ticket_for(session, second)
    svc.rename_category(session, first.id, "חשמלאי", "Electrician")  # flushed before the failed delete
    assert svc.delete_category(session, second.id) == DEACTIVATED
    session.commit()
    session.expire_all()
    assert session.get(Category, first.id).name_en == "Electrician"


def test_a_used_category_is_not_removed_even_if_it_is_already_inactive(
    session: Session, foreign_keys_on: None
) -> None:
    _, second, _ = _three(session)
    _ticket_for(session, second)
    svc.deactivate_category(session, second.id)
    session.commit()
    assert svc.delete_category(session, second.id) == DEACTIVATED
    assert session.get(Category, second.id) is not None


def test_deleting_an_unused_inactive_category_removes_it(session: Session) -> None:
    _, second, _ = _three(session)
    svc.deactivate_category(session, second.id)
    session.commit()
    assert svc.delete_category(session, second.id) == REMOVED


# --- not found --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        lambda s, i: svc.get_category(s, i),
        lambda s, i: svc.rename_category(s, i, "שם", "Name"),
        lambda s, i: svc.deactivate_category(s, i),
        lambda s, i: svc.reactivate_category(s, i),
        lambda s, i: svc.delete_category(s, i),
    ],
    ids=["get", "rename", "deactivate", "reactivate", "delete"],
)
def test_unknown_category_id_is_not_found(session: Session, operation) -> None:
    _three(session)
    with pytest.raises(CategoryNotFound) as raised:
        operation(session, uuid.uuid4())
    assert raised.value.code == CODE_CATEGORY_NOT_FOUND


# --- settings (Requirements 19.2, 19.3) -------------------------------------------------------------------


def test_settings_default_to_the_migration_values_when_the_row_is_missing(session: Session) -> None:
    current = svc.get_service_settings(session)
    assert (current.description_required, current.photo_upload_enabled) == (True, True)
    assert session.get(ServiceSettings, 1) is None  # reading stores nothing


def test_settings_round_trip_and_partial_updates(session: Session) -> None:
    svc.update_service_settings(session, description_required=False)
    session.commit()
    stored = svc.get_service_settings(session)
    assert (stored.description_required, stored.photo_upload_enabled) == (False, True)

    svc.update_service_settings(session, photo_upload_enabled=False)
    session.commit()
    stored = svc.get_service_settings(session)
    assert (stored.description_required, stored.photo_upload_enabled) == (False, False)

    svc.update_service_settings(session, description_required=True, photo_upload_enabled=True)
    session.commit()
    config = svc.get_form_config(session)
    assert (config.settings.description_required, config.settings.photo_upload_enabled) == (True, True)
    assert session.execute(select(func.count()).select_from(ServiceSettings)).scalar_one() == 1


def test_a_settings_update_does_not_touch_tickets(session: Session) -> None:
    electrical, _, _ = _three(session)
    ticket = _ticket_for(session, electrical)
    svc.update_service_settings(session, description_required=False, photo_upload_enabled=False)
    session.commit()
    session.expire_all()
    assert session.get(Ticket, ticket.id).description == ""
    assert session.get(Ticket, ticket.id).status == "received"


# --- field labels (Requirements 19.4, 19.5) ---------------------------------------------------------------


def _seed_labels(session: Session) -> None:
    for key in FIELD_LABEL_KEYS:
        session.add(FieldLabel(field_key=key, label_he=f"{key}-he", label_en=f"{key}-en"))
    session.commit()


def test_labels_round_trip_trimmed_and_only_the_named_fields_change(session: Session) -> None:
    _seed_labels(session)
    svc.update_labels(session, {"city": ("  עיר ", " Town "), "urgent": ("דחוף מאוד", "Very urgent")})
    session.commit()
    labels = svc.get_labels(session)
    assert list(labels) == list(FIELD_LABEL_KEYS)
    assert (labels["city"].label_he, labels["city"].label_en) == ("עיר", "Town")
    assert (labels["urgent"].label_he, labels["urgent"].label_en) == ("דחוף מאוד", "Very urgent")
    assert (labels["phone"].label_he, labels["phone"].label_en) == ("phone-he", "phone-en")
    form = svc.get_form_config(session).labels
    assert (form["city"].he, form["city"].en) == ("עיר", "Town")


def test_a_missing_label_row_is_created(session: Session) -> None:
    svc.update_labels(session, {"photo": ("תמונה", "Photo")})
    session.commit()
    assert set(svc.get_labels(session)) == {"photo"}


@pytest.mark.parametrize("bad", ["", "   ", "x" * 61, "a\x00", None])
def test_invalid_labels_give_one_code_per_failing_text(session: Session, bad: object) -> None:
    _seed_labels(session)
    with pytest.raises(ConfigValidationError) as only_he:
        svc.update_labels(session, {"city": (bad, "Town")})  # type: ignore[dict-item]
    assert only_he.value.fields == {"city.label_he": CODE_INVALID_LABEL_HE}
    assert only_he.value.code == CODE_INVALID_LABEL_HE
    with pytest.raises(ConfigValidationError) as only_en:
        svc.update_labels(session, {"city": ("עיר", bad)})  # type: ignore[dict-item]
    assert only_en.value.fields == {"city.label_en": CODE_INVALID_LABEL_EN}


def test_one_bad_label_changes_no_label_in_either_language(session: Session) -> None:
    _seed_labels(session)
    with pytest.raises(ConfigValidationError) as raised:
        svc.update_labels(
            session,
            {"urgent": ("דחוף", ""), "city": ("עיר חדשה", "New city"), "phone": ("טלפון", "x" * 61)},
        )
    # Reported in form reading order, whatever order the request used.
    assert list(raised.value.fields) == ["phone.label_en", "urgent.label_en"]
    assert raised.value.code == CODE_INVALID_LABEL_EN
    session.expire_all()
    labels = svc.get_labels(session)
    assert (labels["city"].label_he, labels["city"].label_en) == ("city-he", "city-en")
    assert (labels["phone"].label_he, labels["phone"].label_en) == ("phone-he", "phone-en")


def test_unknown_label_field_is_rejected_and_nothing_is_stored(session: Session) -> None:
    _seed_labels(session)
    with pytest.raises(ConfigValidationError) as raised:
        svc.update_labels(session, {"city": ("עיר", "Town"), "colour": ("צבע", "Colour")})
    assert raised.value.fields == {"colour": CODE_UNKNOWN_FIELD}
    session.expire_all()
    assert svc.get_labels(session)["city"].label_en == "city-en"


# --- the form data ----------------------------------------------------------------------------------------


def test_form_config_carries_categories_labels_and_both_options(session: Session) -> None:
    first, _, _ = _three(session)
    _seed_labels(session)
    svc.update_service_settings(session, description_required=False)
    session.commit()
    config = svc.get_form_config(session)
    assert len(config.categories) == 3
    assert config.categories[0].id == first.id
    assert (config.categories[0].name_he, config.categories[0].name_en) == ("חשמל", "Electrical")
    assert list(config.labels) == list(FIELD_LABEL_KEYS)
    assert config.settings.description_required is False
    assert config.settings.photo_upload_enabled is True
