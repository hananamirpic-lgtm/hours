"""Configuration of the service-ticket system: the named lists, field labels, settings and the form data.

Requirements 19.2 to 19.13 (settings, labels, category add, edit, deactivate, reactivate, delete), 9.3 (the
ticket form lists active categories, oldest first), 29.6 (replaceable parts) and 30.1 (settlements).

**Three named lists, one implementation.** Issue categories, settlements (`svc_localities`) and
replaceable parts (`svc_parts`) are the same kind of list: a Hebrew and an English name, an active flag,
an identity `seq` that fixes the order, one unique folded key per language. The operations that are the
same for the three (create, rename, deactivate, reactivate, delete-or-deactivate, the duplicate check,
the ordering) are written once, as functions that take a `NameList`: the model, the length limit, and
the error codes of that list. `CATEGORIES`, `LOCALITIES` and `PARTS` are the three of them. Categories
keep their own named functions (`create_category`, ...) as thin wrappers, with the behaviour and the codes
they always had. The one rule that is not shared is "the last active category stays": it is switched on
by `NameList.keep_one_active`, which only `CATEGORIES` sets (there is no such rule for settlements or
parts, which may all be inactive or absent).

Rules kept here, so no router has to repeat them:

* A name or label is trimmed and must then hold 1 to the list's limit characters (60 for categories,
  parts and labels, 100 for settlements; counted as Unicode characters, as PostgreSQL's `length` counts
  them). Each failing text has its own code (`invalid_name_he`, `invalid_settlement_name_en`,
  `invalid_part_name_he`, `invalid_label_he`, ...); `ConfigValidationError.fields` carries every failing
  text, in form order, and a request with any failing text changes nothing in either language
  (Requirement 19.5). A value that PostgreSQL cannot store (a NUL character or a lone surrogate) counts as
  invalid, so it cannot reach the database and surface as a 500.
* `name_he_key` and `name_en_key` hold `fold_key(name)` (NFC, trimmed, case-folded), which the unique
  constraints turn into "no two items of a list share a name in one language, active or not" (Requirements
  19.7, 29.6, 30.1). The service checks first, to name the clashing languages, and also converts a unique
  violation from a concurrent writer, caught inside a savepoint, into the same duplicate error of the list.
* For categories, deactivation and deletion first lock the set of active categories (`SELECT ... FOR
  UPDATE`, ignored by SQLite) and refuse with `LastActiveCategory` when no active category would remain.
  Two requests that each take a different one of the last two categories queue on the lock; the second one
  re-reads the set after the first has committed and is refused (Requirement 19.13). The locked rows are
  read in `seq` order, so two such requests cannot deadlock each other.
* Delete tries the `DELETE` inside a savepoint. A row that uses the item makes the foreign key (`RESTRICT`)
  refuse it, and the item is deactivated instead, which `delete_named` reports as `DEACTIVATED`; an item
  nothing uses is removed and reported as `REMOVED` (Requirements 19.11, 19.12, 29.6, 30.1). "Used" is
  therefore decided by the foreign keys: a category by `svc_tickets.category_id`, a settlement by
  `svc_tickets.locality_id` or `svc_customers.locality_id`, a part by `svc_ticket_parts.part_id`. Doing it
  this way, not "count the rows, then delete", also covers a row that is created while the delete runs.

This module never commits and never raises HTTP errors. Writes are flushed, so a constraint failure
surfaces here, and the router owns the commit and the rollback. Business failures are the exceptions
below, each with a `code`, which the router maps to a status.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.service_tickets.error_codes import (
    CODE_CATEGORY_NOT_FOUND,
    CODE_DUPLICATE_CATEGORY,
    CODE_DUPLICATE_PART,
    CODE_DUPLICATE_SETTLEMENT,
    CODE_INVALID_LABEL_EN,
    CODE_INVALID_LABEL_HE,
    CODE_INVALID_NAME_EN,
    CODE_INVALID_NAME_HE,
    CODE_INVALID_PART_NAME_EN,
    CODE_INVALID_PART_NAME_HE,
    CODE_INVALID_SETTLEMENT_NAME_EN,
    CODE_INVALID_SETTLEMENT_NAME_HE,
    CODE_LAST_ACTIVE_CATEGORY,
    CODE_PART_NOT_FOUND,
    CODE_SETTLEMENT_NOT_FOUND,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import (
    FIELD_LABEL_KEYS,
    Branding,
    Category,
    FieldLabel,
    Locality,
    Part,
    ServiceSettings,
)

#: Shortest and longest category name, part name or field label, in characters, after trimming
#: (Requirements 19.4, 19.5, 19.6, 29.6).
TEXT_MIN_LENGTH = 1
TEXT_MAX_LENGTH = 60

#: Longest settlement name, in characters (Requirement 30.1).
SETTLEMENT_NAME_MAX_LENGTH = 100

#: The two results of `delete_named` and `delete_category`.
REMOVED: Final = "removed"
DEACTIVATED: Final = "deactivated"

#: The field names `ConfigValidationError.fields` and `DuplicateName.fields` use for the two names.
FIELD_NAME_HE = "name_he"
FIELD_NAME_EN = "name_en"
#: A label field is reported as `<field_key>.label_he` or `<field_key>.label_en`.
LABEL_HE_SUFFIX = "label_he"
LABEL_EN_SUFFIX = "label_en"


class ConfigError(Exception):
    """Base of the business failures of this module. `code` is the API error code."""

    code: str = ""


class ConfigValidationError(ConfigError):
    """One or more submitted texts are invalid (HTTP 422).

    `fields` maps each failing field to its code, in form order; `code` is the first of them, which is
    what the error envelope puts at the top level.
    """

    def __init__(self, fields: Mapping[str, str]) -> None:
        if not fields:
            raise ValueError("ConfigValidationError needs at least one failing field")
        self.fields: dict[str, str] = dict(fields)
        self.code = next(iter(self.fields.values()))
        super().__init__(self.code)


class DuplicateName(ConfigError):
    """Another item of the same list, active or inactive, has the same name in the same language (HTTP 409).

    `fields` names the clashing languages (`name_he`, `name_en`). The subclass of each list sets `code`.
    """

    def __init__(self, fields: Iterable[str] = ()) -> None:
        self.fields: tuple[str, ...] = tuple(fields)
        super().__init__(self.code)


class DuplicateCategory(DuplicateName):
    """Another category has the same name in the same language."""

    code = CODE_DUPLICATE_CATEGORY


class DuplicateSettlement(DuplicateName):
    """Another settlement has the same name in the same language."""

    code = CODE_DUPLICATE_SETTLEMENT


class DuplicatePart(DuplicateName):
    """Another part has the same name in the same language."""

    code = CODE_DUPLICATE_PART


class LastActiveCategory(ConfigError):
    """The change would leave no active category (HTTP 409). Nothing was changed."""

    code = CODE_LAST_ACTIVE_CATEGORY


class ItemNotFound(ConfigError):
    """No item of the list has the given id (HTTP 404). The subclass of each list sets `code`."""


class CategoryNotFound(ItemNotFound):
    """No category has the given id (HTTP 404)."""

    code = CODE_CATEGORY_NOT_FOUND


class SettlementNotFound(ItemNotFound):
    """No settlement has the given id (HTTP 404)."""

    code = CODE_SETTLEMENT_NOT_FOUND


class PartNotFound(ItemNotFound):
    """No part has the given id (HTTP 404)."""

    code = CODE_PART_NOT_FOUND


#: The three mapped classes that share the shape of a named list (they have the same columns).
NamedRow = Category | Locality | Part


@dataclass(frozen=True, slots=True)
class NameList:
    """What differs between the named lists: the model, the length limit and the error codes.

    `keep_one_active` is the "last active category" rule of Requirement 19.13; only categories have it.
    """

    model: type[Category] | type[Locality] | type[Part]
    max_length: int
    invalid_he_code: str
    invalid_en_code: str
    duplicate_error: type[DuplicateName]
    not_found_error: type[ItemNotFound]
    keep_one_active: bool = False


CATEGORIES: Final = NameList(
    model=Category,
    max_length=TEXT_MAX_LENGTH,
    invalid_he_code=CODE_INVALID_NAME_HE,
    invalid_en_code=CODE_INVALID_NAME_EN,
    duplicate_error=DuplicateCategory,
    not_found_error=CategoryNotFound,
    keep_one_active=True,
)
LOCALITIES: Final = NameList(
    model=Locality,
    max_length=SETTLEMENT_NAME_MAX_LENGTH,
    invalid_he_code=CODE_INVALID_SETTLEMENT_NAME_HE,
    invalid_en_code=CODE_INVALID_SETTLEMENT_NAME_EN,
    duplicate_error=DuplicateSettlement,
    not_found_error=SettlementNotFound,
)
PARTS: Final = NameList(
    model=Part,
    max_length=TEXT_MAX_LENGTH,
    invalid_he_code=CODE_INVALID_PART_NAME_HE,
    invalid_en_code=CODE_INVALID_PART_NAME_EN,
    duplicate_error=DuplicatePart,
    not_found_error=PartNotFound,
)


@dataclass(frozen=True, slots=True)
class FormCategory:
    id: uuid.UUID
    name_he: str
    name_en: str


@dataclass(frozen=True, slots=True)
class FormLocality:
    id: uuid.UUID
    name_he: str
    name_en: str


@dataclass(frozen=True, slots=True)
class FormLabel:
    he: str
    en: str


@dataclass(frozen=True, slots=True)
class FormSettings:
    description_required: bool
    photo_upload_enabled: bool


@dataclass(frozen=True, slots=True)
class FormBranding:
    """The branding the portal shows: the company name and whether a logo is stored (never its key)."""

    company_name: str | None
    has_logo: bool


@dataclass(frozen=True, slots=True)
class FormConfig:
    """What the ticket form needs: active categories and settlements oldest first, the labels, the two
    options and the branding. `settlements_required` is true when at least one settlement is active."""

    categories: tuple[FormCategory, ...]
    labels: dict[str, FormLabel]
    settings: FormSettings
    localities: tuple[FormLocality, ...] = ()
    settlements_required: bool = False
    branding: FormBranding = FormBranding(company_name=None, has_logo=False)


# --- validation ----------------------------------------------------------------------------------


def _clean_text(value: object, max_length: int = TEXT_MAX_LENGTH) -> str | None:
    """The trimmed text when it is a valid name or label of at most `max_length` characters, else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not TEXT_MIN_LENGTH <= len(text) <= max_length:
        return None
    if "\x00" in text:  # PostgreSQL text cannot hold NUL
        return None
    try:
        text.encode("utf-8")  # a lone surrogate cannot be stored either
    except UnicodeEncodeError:
        return None
    return text


def _clean_names(spec: NameList, name_he: object, name_en: object) -> tuple[str, str]:
    he = _clean_text(name_he, spec.max_length)
    en = _clean_text(name_en, spec.max_length)
    failures: dict[str, str] = {}
    if he is None:
        failures[FIELD_NAME_HE] = spec.invalid_he_code
    if en is None:
        failures[FIELD_NAME_EN] = spec.invalid_en_code
    if failures or he is None or en is None:
        raise ConfigValidationError(failures)
    return he, en


# --- the named lists (categories, settlements, parts) ----------------------------------------------


def list_names(session: Session, spec: NameList, *, active_only: bool = False) -> list[NamedRow]:
    """The items of a list, oldest first by `seq`. Inactive ones are included unless `active_only`."""
    model = spec.model
    statement = select(model).order_by(model.seq)
    if active_only:
        statement = statement.where(model.is_active.is_(True))
    return list(session.execute(statement).scalars())


def _load(session: Session, spec: NameList, item_id: uuid.UUID, *, lock: bool) -> NamedRow:
    """The item, re-read from the database (not from a stale identity map), optionally locked."""
    model = spec.model
    statement = select(model).where(model.id == item_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    item = session.execute(statement).scalar_one_or_none()
    if item is None:
        raise spec.not_found_error
    return item


def get_named(session: Session, spec: NameList, item_id: uuid.UUID) -> NamedRow:
    """One item, active or not. Raises the list's not-found error."""
    return _load(session, spec, item_id, lock=False)


def _clashing_fields(
    session: Session, spec: NameList, he: str, en: str, *, exclude: uuid.UUID | None
) -> list[str]:
    """The languages whose folded name another item of the list already has."""
    model = spec.model
    key_he, key_en = fold_key(he), fold_key(en)
    statement = select(model.name_he_key, model.name_en_key).where(
        or_(model.name_he_key == key_he, model.name_en_key == key_en)
    )
    if exclude is not None:
        statement = statement.where(model.id != exclude)
    clashing: list[str] = []
    for existing_he, existing_en in session.execute(statement):
        if existing_he == key_he and FIELD_NAME_HE not in clashing:
            clashing.append(FIELD_NAME_HE)
        if existing_en == key_en and FIELD_NAME_EN not in clashing:
            clashing.append(FIELD_NAME_EN)
    return clashing


def create_named(session: Session, spec: NameList, name_he: str, name_en: str) -> NamedRow:
    """Add an active item (Requirements 19.6, 29.6, 30.1). Its `seq` is numbered by the database."""
    he, en = _clean_names(spec, name_he, name_en)
    clashing = _clashing_fields(session, spec, he, en, exclude=None)
    if clashing:
        raise spec.duplicate_error(clashing)
    item = spec.model(
        name_he=he, name_en=en, name_he_key=fold_key(he), name_en_key=fold_key(en), is_active=True
    )
    try:
        with session.begin_nested():
            session.add(item)
            session.flush()
    except IntegrityError as error:  # a concurrent writer took the name after the check
        raise spec.duplicate_error() from error
    return item


def rename_named(
    session: Session, spec: NameList, item_id: uuid.UUID, name_he: str, name_en: str
) -> NamedRow:
    """Set both names of an item (Requirements 19.5, 19.7, 19.8). Tickets show the new names at once.

    A name equal to the item's own current name is not a duplicate.
    """
    he, en = _clean_names(spec, name_he, name_en)
    item = _load(session, spec, item_id, lock=True)
    clashing = _clashing_fields(session, spec, he, en, exclude=item.id)
    if clashing:
        raise spec.duplicate_error(clashing)
    item.name_he, item.name_en = he, en
    item.name_he_key, item.name_en_key = fold_key(he), fold_key(en)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as error:
        raise spec.duplicate_error() from error
    return item


def _lock_for_removal(session: Session, spec: NameList, item_id: uuid.UUID) -> NamedRow:
    """Lock the item for a deactivation or a delete.

    For a list with `keep_one_active` (categories) the active items are locked first, in `seq` order, and
    are read after the lock is granted, so a request that waited sees the result of the one it waited
    for; the request is refused with `LastActiveCategory` when no active item would remain. A list
    without the rule locks only the item.
    """
    if not spec.keep_one_active:
        return _load(session, spec, item_id, lock=True)
    model = spec.model
    active = session.execute(
        select(model)
        .where(model.is_active.is_(True))
        .order_by(model.seq)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars()
    active_ids = {row.id for row in active}
    item = _load(session, spec, item_id, lock=True)
    if not active_ids - {item.id}:
        raise LastActiveCategory
    return item


def deactivate_named(session: Session, spec: NameList, item_id: uuid.UUID) -> NamedRow:
    """Hide an item from the forms (Requirement 19.9). Existing tickets keep its name."""
    item = _lock_for_removal(session, spec, item_id)
    if item.is_active:
        item.is_active = False
        session.flush()
    return item


def reactivate_named(session: Session, spec: NameList, item_id: uuid.UUID) -> NamedRow:
    """List an item in the forms again (Requirement 19.10)."""
    item = _load(session, spec, item_id, lock=True)
    if not item.is_active:
        item.is_active = True
        session.flush()
    return item


def delete_named(session: Session, spec: NameList, item_id: uuid.UUID) -> str:
    """Remove an item nothing uses (`REMOVED`), else deactivate it (`DEACTIVATED`).

    Requirements 19.11, 19.12, 19.13, 29.6 and 30.1.
    """
    item = _lock_for_removal(session, spec, item_id)
    try:
        with session.begin_nested():
            session.delete(item)
            session.flush()
    except IntegrityError:  # a row uses it: the RESTRICT foreign key refused the DELETE
        item.is_active = False
        session.flush()
        return DEACTIVATED
    return REMOVED


# --- categories: the named functions of the list of Requirement 19 ---------------------------------


def list_categories(session: Session, *, active_only: bool = False) -> list[Category]:
    """Categories oldest first by `seq`. Inactive ones are included unless `active_only`.

    The staff filters and reports read the whole list, so a deactivated category stays selectable
    there (Requirement 19.9).
    """
    return list_names(session, CATEGORIES, active_only=active_only)  # type: ignore[return-value]


def get_category(session: Session, category_id: uuid.UUID) -> Category:
    """One category, active or not. Raises `CategoryNotFound`."""
    return get_named(session, CATEGORIES, category_id)  # type: ignore[return-value]


def create_category(session: Session, name_he: str, name_en: str) -> Category:
    """Add an active category (Requirement 19.6). Its `seq` is numbered by the database."""
    return create_named(session, CATEGORIES, name_he, name_en)  # type: ignore[return-value]


def rename_category(session: Session, category_id: uuid.UUID, name_he: str, name_en: str) -> Category:
    """Set both names of a category (Requirements 19.5, 19.7, 19.8)."""
    return rename_named(session, CATEGORIES, category_id, name_he, name_en)  # type: ignore[return-value]


def deactivate_category(session: Session, category_id: uuid.UUID) -> Category:
    """Hide a category from the ticket form (Requirement 19.9); refused for the last active one."""
    return deactivate_named(session, CATEGORIES, category_id)  # type: ignore[return-value]


def reactivate_category(session: Session, category_id: uuid.UUID) -> Category:
    """List a category in the ticket form again (Requirement 19.10)."""
    return reactivate_named(session, CATEGORIES, category_id)  # type: ignore[return-value]


def delete_category(session: Session, category_id: uuid.UUID) -> str:
    """Remove a category no ticket uses (`REMOVED`), else deactivate it (`DEACTIVATED`).

    Requirements 19.11, 19.12 and 19.13.
    """
    return delete_named(session, CATEGORIES, category_id)


# --- settings ------------------------------------------------------------------------------------


def get_service_settings(session: Session) -> ServiceSettings:
    """The single settings row. When it is missing, the defaults of the migration, not stored."""
    row = session.get(ServiceSettings, 1, populate_existing=True)
    if row is None:
        return ServiceSettings(id=1, description_required=True, photo_upload_enabled=True)
    return row


def update_service_settings(
    session: Session,
    *,
    description_required: bool | None = None,
    photo_upload_enabled: bool | None = None,
) -> ServiceSettings:
    """Change the given options and leave the others as they are (Requirements 19.2, 19.3).

    Last save wins. Existing tickets are not touched; submissions read the row on every request.
    """
    row = session.get(ServiceSettings, 1, populate_existing=True, with_for_update=True)
    if row is None:
        row = ServiceSettings(id=1, description_required=True, photo_upload_enabled=True)
        session.add(row)
    if description_required is not None:
        row.description_required = description_required
    if photo_upload_enabled is not None:
        row.photo_upload_enabled = photo_upload_enabled
    session.flush()
    return row


# --- field labels --------------------------------------------------------------------------------


def get_labels(session: Session) -> dict[str, FieldLabel]:
    """The stored labels by field key, in the order of `FIELD_LABEL_KEYS`."""
    rows = {
        row.field_key: row
        for row in session.execute(select(FieldLabel).execution_options(populate_existing=True)).scalars()
    }
    return {key: rows[key] for key in FIELD_LABEL_KEYS if key in rows}


def _label_order(key: str) -> int:
    """Form reading order; unknown keys come last."""
    return FIELD_LABEL_KEYS.index(key) if key in FIELD_LABEL_KEYS else len(FIELD_LABEL_KEYS)


def update_labels(session: Session, labels: Mapping[str, tuple[str, str]]) -> dict[str, FieldLabel]:
    """Set the Hebrew and English label of each field in `labels` (`field_key -> (he, en)`).

    Every text is validated before anything is stored, so one bad text changes nothing (Requirement
    19.5). An unknown field key is reported as `unknown_field`. Fields not named keep their labels.
    """
    ordered = sorted(labels, key=_label_order)
    failures: dict[str, str] = {}
    cleaned: dict[str, tuple[str, str]] = {}
    for key in ordered:
        if key not in FIELD_LABEL_KEYS:
            failures[key] = CODE_UNKNOWN_FIELD
            continue
        pair = labels[key]
        he = _clean_text(pair[0])
        en = _clean_text(pair[1])
        if he is None:
            failures[f"{key}.{LABEL_HE_SUFFIX}"] = CODE_INVALID_LABEL_HE
        if en is None:
            failures[f"{key}.{LABEL_EN_SUFFIX}"] = CODE_INVALID_LABEL_EN
        if he is not None and en is not None:
            cleaned[key] = (he, en)
    if failures:
        raise ConfigValidationError(failures)
    if cleaned:
        existing = {
            row.field_key: row
            for row in session.execute(
                select(FieldLabel).where(FieldLabel.field_key.in_(cleaned)).with_for_update()
            ).scalars()
        }
        for key, (he, en) in cleaned.items():
            row = existing.get(key)
            if row is None:
                session.add(FieldLabel(field_key=key, label_he=he, label_en=en))
            else:
                row.label_he, row.label_en = he, en
        session.flush()
    return get_labels(session)


# --- the form ------------------------------------------------------------------------------------


def get_form_branding(session: Session) -> FormBranding:
    """The company name and whether a logo exists, from the single `svc_branding` row.

    An absent row (a database that has not been migrated, or a unit-test database) reads as empty.
    The object-store key never leaves this function.
    """
    row = session.get(Branding, 1, populate_existing=True)
    if row is None:
        return FormBranding(company_name=None, has_logo=False)
    return FormBranding(company_name=row.company_name, has_logo=row.logo_key is not None)


def get_form_config(session: Session) -> FormConfig:
    """The data of the ticket form: active categories and settlements oldest first, labels, the two
    options and the branding.

    Inactive categories and settlements are left out (Requirements 9.3, 19.9, 30.2). The result holds
    plain values, not mapped objects, so a caller cannot change the database through it.
    """
    categories = tuple(
        FormCategory(id=c.id, name_he=c.name_he, name_en=c.name_en)
        for c in list_categories(session, active_only=True)
    )
    localities = tuple(
        FormLocality(id=row.id, name_he=row.name_he, name_en=row.name_en)
        for row in list_names(session, LOCALITIES, active_only=True)
    )
    labels = {key: FormLabel(he=row.label_he, en=row.label_en) for key, row in get_labels(session).items()}
    settings = get_service_settings(session)
    return FormConfig(
        categories=categories,
        labels=labels,
        settings=FormSettings(
            description_required=settings.description_required,
            photo_upload_enabled=settings.photo_upload_enabled,
        ),
        localities=localities,
        settlements_required=bool(localities),
        branding=get_form_branding(session),
    )
