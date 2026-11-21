"""Configuration of the service-ticket system: categories, field labels, settings and the form data.

Requirements 19.2 to 19.13 (settings, labels, category add, edit, deactivate, reactivate, delete) and
9.3 (the ticket form lists active categories, oldest first).

Rules kept here, so no router has to repeat them:

* A name or label is trimmed and must then hold 1 to 60 characters (counted as Unicode characters, as
  PostgreSQL's `length` counts them). Each failing text has its own code (`invalid_name_he`,
  `invalid_name_en`, `invalid_label_he`, `invalid_label_en`); `ConfigValidationError.fields` carries every
  failing text, in form order, and a request with any failing text changes nothing in either language
  (Requirement 19.5). A value that PostgreSQL cannot store (a NUL character or a lone surrogate) counts as
  invalid, so it cannot reach the database and surface as a 500.
* `name_he_key` and `name_en_key` hold `fold_key(name)` (NFC, trimmed, case-folded), which the unique
  constraints turn into "no two categories share a name in one language, active or not" (Requirement
  19.7). The service checks first, to name the clashing languages, and also converts a unique violation
  from a concurrent writer, caught inside a savepoint, into the same `DuplicateCategory`.
* Deactivation and deletion first lock the set of active categories (`SELECT ... FOR UPDATE`, ignored by
  SQLite) and refuse with `LastActiveCategory` when no active category would remain. Two requests that each
  take a different one of the last two categories queue on the lock; the second one re-reads the set after
  the first has committed and is refused (Requirement 19.13). The locked rows are read in `seq` order, so
  two such requests cannot deadlock each other.
* Delete tries the `DELETE` inside a savepoint. A ticket that uses the category makes the foreign key
  (`RESTRICT`) refuse it, and the category is deactivated instead, which `delete_category` reports as
  `DEACTIVATED`; a category nothing uses is removed and reported as `REMOVED` (Requirements 19.11, 19.12).
  Doing it this way, not "count the tickets, then delete", also covers a ticket that is created while the
  delete runs.

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
    CODE_INVALID_LABEL_EN,
    CODE_INVALID_LABEL_HE,
    CODE_INVALID_NAME_EN,
    CODE_INVALID_NAME_HE,
    CODE_LAST_ACTIVE_CATEGORY,
    CODE_UNKNOWN_FIELD,
)
from app.service_tickets.grammars import fold_key
from app.service_tickets.models import FIELD_LABEL_KEYS, Category, FieldLabel, ServiceSettings

#: Shortest and longest name or label, in characters, after trimming (Requirements 19.4, 19.5, 19.6).
TEXT_MIN_LENGTH = 1
TEXT_MAX_LENGTH = 60

#: The two results of `delete_category`.
REMOVED: Final = "removed"
DEACTIVATED: Final = "deactivated"

#: The field names `ConfigValidationError.fields` and `DuplicateCategory.fields` use for the two names.
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


class DuplicateCategory(ConfigError):
    """Another category, active or inactive, has the same name in the same language (HTTP 409).

    `fields` names the clashing languages (`name_he`, `name_en`).
    """

    code = CODE_DUPLICATE_CATEGORY

    def __init__(self, fields: Iterable[str] = ()) -> None:
        self.fields: tuple[str, ...] = tuple(fields)
        super().__init__(self.code)


class LastActiveCategory(ConfigError):
    """The change would leave no active category (HTTP 409). Nothing was changed."""

    code = CODE_LAST_ACTIVE_CATEGORY


class CategoryNotFound(ConfigError):
    """No category has the given id (HTTP 404)."""

    code = CODE_CATEGORY_NOT_FOUND


@dataclass(frozen=True, slots=True)
class FormCategory:
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
class FormConfig:
    """What the ticket form needs: active categories oldest first, the labels, the two options."""

    categories: tuple[FormCategory, ...]
    labels: dict[str, FormLabel]
    settings: FormSettings


# --- validation ----------------------------------------------------------------------------------


def _clean_text(value: object) -> str | None:
    """The trimmed text when it is a valid name or label, else `None`."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not TEXT_MIN_LENGTH <= len(text) <= TEXT_MAX_LENGTH:
        return None
    if "\x00" in text:  # PostgreSQL text cannot hold NUL
        return None
    try:
        text.encode("utf-8")  # a lone surrogate cannot be stored either
    except UnicodeEncodeError:
        return None
    return text


def _clean_names(name_he: object, name_en: object) -> tuple[str, str]:
    he = _clean_text(name_he)
    en = _clean_text(name_en)
    failures: dict[str, str] = {}
    if he is None:
        failures[FIELD_NAME_HE] = CODE_INVALID_NAME_HE
    if en is None:
        failures[FIELD_NAME_EN] = CODE_INVALID_NAME_EN
    if failures or he is None or en is None:
        raise ConfigValidationError(failures)
    return he, en


# --- categories ----------------------------------------------------------------------------------


def list_categories(session: Session, *, active_only: bool = False) -> list[Category]:
    """Categories oldest first by `seq`. Inactive ones are included unless `active_only`.

    The staff filters and reports read the whole list, so a deactivated category stays selectable
    there (Requirement 19.9).
    """
    statement = select(Category).order_by(Category.seq)
    if active_only:
        statement = statement.where(Category.is_active.is_(True))
    return list(session.execute(statement).scalars())


def _load_category(session: Session, category_id: uuid.UUID, *, lock: bool) -> Category:
    """The category, re-read from the database (not from a stale identity map), optionally locked."""
    statement = select(Category).where(Category.id == category_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    category = session.execute(statement).scalar_one_or_none()
    if category is None:
        raise CategoryNotFound
    return category


def get_category(session: Session, category_id: uuid.UUID) -> Category:
    """One category, active or not. Raises `CategoryNotFound`."""
    return _load_category(session, category_id, lock=False)


def _clashing_fields(session: Session, he: str, en: str, *, exclude: uuid.UUID | None) -> list[str]:
    """The languages whose folded name another category already has."""
    key_he, key_en = fold_key(he), fold_key(en)
    statement = select(Category.name_he_key, Category.name_en_key).where(
        or_(Category.name_he_key == key_he, Category.name_en_key == key_en)
    )
    if exclude is not None:
        statement = statement.where(Category.id != exclude)
    clashing: list[str] = []
    for existing_he, existing_en in session.execute(statement):
        if existing_he == key_he and FIELD_NAME_HE not in clashing:
            clashing.append(FIELD_NAME_HE)
        if existing_en == key_en and FIELD_NAME_EN not in clashing:
            clashing.append(FIELD_NAME_EN)
    return clashing


def create_category(session: Session, name_he: str, name_en: str) -> Category:
    """Add an active category (Requirement 19.6). Its `seq` is numbered by the database."""
    he, en = _clean_names(name_he, name_en)
    clashing = _clashing_fields(session, he, en, exclude=None)
    if clashing:
        raise DuplicateCategory(clashing)
    category = Category(
        name_he=he, name_en=en, name_he_key=fold_key(he), name_en_key=fold_key(en), is_active=True
    )
    try:
        with session.begin_nested():
            session.add(category)
            session.flush()
    except IntegrityError as error:  # a concurrent writer took the name after the check
        raise DuplicateCategory() from error
    return category


def rename_category(session: Session, category_id: uuid.UUID, name_he: str, name_en: str) -> Category:
    """Set both names of a category (Requirements 19.5, 19.7, 19.8). Tickets show the new names at once.

    A name equal to the category's own current name is not a duplicate.
    """
    he, en = _clean_names(name_he, name_en)
    category = _load_category(session, category_id, lock=True)
    clashing = _clashing_fields(session, he, en, exclude=category.id)
    if clashing:
        raise DuplicateCategory(clashing)
    category.name_he, category.name_en = he, en
    category.name_he_key, category.name_en_key = fold_key(he), fold_key(en)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as error:
        raise DuplicateCategory() from error
    return category


def _lock_for_removal(session: Session, category_id: uuid.UUID) -> Category:
    """Lock the active categories, then the target; refuse when no active category would remain.

    The active set is locked first, in `seq` order, and is read after the lock is granted, so a request
    that waited sees the result of the one it waited for.
    """
    active = session.execute(
        select(Category)
        .where(Category.is_active.is_(True))
        .order_by(Category.seq)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars()
    active_ids = {category.id for category in active}
    category = _load_category(session, category_id, lock=True)
    if not active_ids - {category.id}:
        raise LastActiveCategory
    return category


def deactivate_category(session: Session, category_id: uuid.UUID) -> Category:
    """Hide a category from the ticket form (Requirement 19.9). Existing tickets keep its name."""
    category = _lock_for_removal(session, category_id)
    if category.is_active:
        category.is_active = False
        session.flush()
    return category


def reactivate_category(session: Session, category_id: uuid.UUID) -> Category:
    """List a category in the ticket form again (Requirement 19.10)."""
    category = _load_category(session, category_id, lock=True)
    if not category.is_active:
        category.is_active = True
        session.flush()
    return category


def delete_category(session: Session, category_id: uuid.UUID) -> str:
    """Remove a category no ticket uses (`REMOVED`), else deactivate it (`DEACTIVATED`).

    Requirements 19.11, 19.12 and 19.13.
    """
    category = _lock_for_removal(session, category_id)
    try:
        with session.begin_nested():
            session.delete(category)
            session.flush()
    except IntegrityError:  # a ticket uses it: the RESTRICT foreign key refused the DELETE
        category.is_active = False
        session.flush()
        return DEACTIVATED
    return REMOVED


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


def get_form_config(session: Session) -> FormConfig:
    """The data of the ticket form: active categories oldest first, labels, the two options.

    Inactive categories are left out (Requirements 9.3, 19.9). The result holds plain values, not
    mapped objects, so a caller cannot change the database through it.
    """
    categories = tuple(
        FormCategory(id=c.id, name_he=c.name_he, name_en=c.name_en)
        for c in list_categories(session, active_only=True)
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
    )
