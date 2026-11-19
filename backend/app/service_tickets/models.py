"""The `svc_` tables of the service-ticket feature.

A mapping onto the tables migration 0011 creates, not a definition of them: the same relationship every
model in `app.models` has to its migration. Seven tables, all prefixed `svc_`. Relations exist only
between `svc_` tables, and every one of them is `RESTRICT`. Nothing here references `users`,
`employees`, `sites` or any other existing table (Requirement 20.2); the Hours `users` table is read by
primary key elsewhere and never joined.

Names follow `NAMING_CONVENTION` in `app.db.base`. Primary keys, foreign keys and check constraints get
their names from it (a check constraint is given only the suffix, the convention prefixes `ck_<table>_`).
Unique constraints and indexes are named in full, as `app.models.user` does, so the name in the
migration and the name here are the same string.

Two things are not declared, for the reasons `app.models.user` gives for its own omissions:

* `gen_random_uuid()` as the server default of the uuid primary keys. It is not valid on SQLite, which
  `tests/conftest.py` uses with `Base.metadata.create_all`; the Python-side `default=uuid.uuid4` covers
  the ORM and the migration carries the server default for any other writer.
* The sequence `svc_ticket_number_seq`. A sequence is not a column attribute here; migration 0011 owns
  it, and the numbering service reads it with `nextval`.

`Category.seq` and `SigninFailure.id` are identity columns (`GENERATED ALWAYS AS IDENTITY` on
PostgreSQL). SQLite has no identity columns, so under `create_all` only `SigninFailure.id`, an integer
primary key there, numbers itself. A test that inserts a `Category` on SQLite supplies `seq`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The four ticket states (Requirement 14.9). The API and the database use the same machine values; the
#: front ends translate them. Repeated as literals in migration 0011, which is a frozen record.
TICKET_STATUSES = ("received", "in_progress", "closed", "cancelled")

#: The seven configurable field labels (the `field_key` check of `svc_field_labels`).
FIELD_LABEL_KEYS = ("city", "apartment", "phone", "category", "description", "photo", "urgent")

#: Smallest and largest ticket number (Requirement 10.1). `svc_ticket_number_seq` has the same bounds.
TICKET_NUMBER_MIN = 10000
TICKET_NUMBER_MAX = 99999


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _quoted_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Customer(Base):
    """A person who signs in to the portal and raises tickets. Never a row in `users`."""

    __tablename__ = "svc_customers"

    __table_args__ = (
        UniqueConstraint("phone", name="uq_svc_customers_phone"),
        CheckConstraint("length(phone) = 10", name="phone_length"),
        CheckConstraint("length(full_name) BETWEEN 1 AND 100", name="full_name_length"),
        CheckConstraint("city IS NULL OR length(city) BETWEEN 1 AND 100", name="city_length"),
        CheckConstraint("apartment IS NULL OR length(apartment) BETWEEN 1 AND 20", name="apartment_length"),
        Index("ix_svc_customers_full_name_key", "full_name_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Ten digits, the sign-in name.
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)

    #: `unicodedata.normalize("NFC", full_name).strip().casefold()`, computed in Python. The database
    #: is created with `--locale=C`, where `lower()` and `ILIKE` fold only ASCII, so list order and
    #: search use this column (Requirement 3.10).
    full_name_key: Mapped[str] = mapped_column(Text, nullable=False)

    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    apartment: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: bcrypt only.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    #: Bumped on deactivation, reset and password change, so outstanding customer tokens stop working.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Customer(active={self.is_active})"


class Category(Base):
    """An issue category. Deleted when unused, deactivated when a ticket uses it."""

    __tablename__ = "svc_categories"

    __table_args__ = (
        UniqueConstraint("seq", name="uq_svc_categories_seq"),
        UniqueConstraint("name_he_key", name="uq_svc_categories_name_he_key"),
        UniqueConstraint("name_en_key", name="uq_svc_categories_name_en_key"),
        CheckConstraint("length(name_he) BETWEEN 1 AND 60", name="name_he_length"),
        CheckConstraint("length(name_en) BETWEEN 1 AND 60", name="name_en_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Display order, oldest first (Requirement 9.3). `GENERATED ALWAYS AS IDENTITY`: a writer cannot
    #: supply it, so no Python default is declared.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)

    name_he: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str] = mapped_column(Text, nullable=False)

    #: NFC, stripped and casefolded names, the case-insensitive uniqueness per language (Requirement 19.7).
    name_he_key: Mapped[str] = mapped_column(Text, nullable=False)
    name_en_key: Mapped[str] = mapped_column(Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Category({self.name_en!r} active={self.is_active})"


class Ticket(Base):
    """A service ticket. Owned by one customer, classified by one category."""

    __tablename__ = "svc_tickets"

    __table_args__ = (
        UniqueConstraint("ticket_number", name="uq_svc_tickets_ticket_number"),
        UniqueConstraint("photo_key", name="uq_svc_tickets_photo_key"),
        CheckConstraint(
            f"ticket_number BETWEEN {TICKET_NUMBER_MIN} AND {TICKET_NUMBER_MAX}", name="ticket_number_range"
        ),
        CheckConstraint(f"status IN ({_quoted_list(TICKET_STATUSES)})", name="status_valid"),
        CheckConstraint("length(city) BETWEEN 1 AND 100", name="city_length"),
        CheckConstraint("length(apartment) BETWEEN 1 AND 20", name="apartment_length"),
        CheckConstraint("length(phone) = 10", name="phone_length"),
        CheckConstraint("length(description) <= 2000", name="description_length"),
        CheckConstraint("length(assigned_technician_name) <= 100", name="assigned_technician_name_length"),
        Index("ix_svc_tickets_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_svc_tickets_created_at_ticket_number", "created_at", "ticket_number"),
        Index("ix_svc_tickets_status_urgent", "status", "urgent"),
        Index("ix_svc_tickets_category_id", "category_id"),
        Index("ix_svc_tickets_city_key", "city_key"),
        Index("ix_svc_tickets_created_date", "created_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    #: Five digits, allocated from `svc_ticket_number_seq` (Requirement 10.1).
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("svc_customers.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("svc_categories.id", ondelete="RESTRICT"), nullable=False
    )

    city: Mapped[str] = mapped_column(Text, nullable=False)

    #: Normalized `city`, as `Customer.full_name_key` is for the name; the staff city filter reads it.
    city_key: Mapped[str] = mapped_column(Text, nullable=False)

    apartment: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))

    #: Object-store key of the one photo, when there is one.
    photo_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="received", server_default=text("'received'")
    )

    #: Empty string is the one "unassigned" state (Requirement 16.2), not NULL.
    assigned_technician_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )

    #: Set by the application right after number allocation, so there is no server default.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: The creation date in the application time zone, stored once so report and list filters are plain
    #: `date` comparisons. A ticket's creation instant never changes, so it cannot go stale.
    created_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: The last-modified time. History entries have their own timestamps and do not touch it.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"Ticket(#{self.ticket_number} status={self.status})"


class HistoryEntry(Base):
    """A technician's note on a ticket."""

    __tablename__ = "svc_history_entries"

    __table_args__ = (
        CheckConstraint("length(entry_text) BETWEEN 1 AND 2000", name="entry_text_length"),
        CheckConstraint("length(technician_name) BETWEEN 1 AND 100", name="technician_name_length"),
        Index("ix_svc_history_entries_ticket_id_created_at", "ticket_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("svc_tickets.id", ondelete="RESTRICT"), nullable=False
    )

    #: Named `entry_text`, not `text`, to avoid the SQL type name.
    entry_text: Mapped[str] = mapped_column(Text, nullable=False)
    technician_name: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: Null means never edited (Requirement 16.5).
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"HistoryEntry(ticket={self.ticket_id})"


class ServiceSettings(Base):
    """The single settings row of the feature.

    Not named `Settings` or `Setting`: `app.core.config.Settings` and `app.models.setting.Setting`
    already exist. The table is `svc_settings`.
    """

    __tablename__ = "svc_settings"

    __table_args__ = (CheckConstraint("id = 1", name="id_is_one"),)

    #: Always 1: the check constraint is what makes this a one-row table.
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)

    description_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    photo_upload_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"ServiceSettings(description_required={self.description_required})"


class FieldLabel(Base):
    """The Hebrew and English label of one form field, keyed by the field."""

    __tablename__ = "svc_field_labels"

    __table_args__ = (
        CheckConstraint(f"field_key IN ({_quoted_list(FIELD_LABEL_KEYS)})", name="field_key_valid"),
        CheckConstraint("length(label_he) BETWEEN 1 AND 60", name="label_he_length"),
        CheckConstraint("length(label_en) BETWEEN 1 AND 60", name="label_en_length"),
    )

    field_key: Mapped[str] = mapped_column(Text, primary_key=True)
    label_he: Mapped[str] = mapped_column(Text, nullable=False)
    label_en: Mapped[str] = mapped_column(Text, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), onupdate=_utcnow
    )

    def __repr__(self) -> str:
        return f"FieldLabel({self.field_key!r})"


class SigninFailure(Base):
    """One failed sign-in attempt. Append-only, with no unique key on `phone`.

    Unregistered phone numbers get rows too, which is what makes a registered and an unregistered
    number indistinguishable to the caller (Requirement 5.7).
    """

    __tablename__ = "svc_signin_failures"

    __table_args__ = (
        Index("ix_svc_signin_failures_phone_failed_at", "phone", "failed_at"),
        Index("ix_svc_signin_failures_failed_at", "failed_at"),
    )

    #: `bigint GENERATED ALWAYS AS IDENTITY` on PostgreSQL. The SQLite variant is an integer primary
    #: key, the only kind SQLite numbers by itself, so `create_all` in the unit tests still works.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), Identity(always=True), primary_key=True
    )

    phone: Mapped[str] = mapped_column(Text, nullable=False)
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"SigninFailure(at={self.failed_at})"
