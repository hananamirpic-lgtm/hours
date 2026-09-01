"""The `sites`, `site_rates` and `employee_sites` tables.

Mappings onto the tables migration 0001 created, not definitions of them. Every column name,
constraint name and index name reproduces what the migration wrote, so Alembic autogenerate compares
like with like instead of proposing to change a schema that is already correct.

`Site` lands the `client` relationship the client service reads (Requirement 5.3, 5.4). `SiteRate` is
the effective-dated billing history that makes Requirement 6.6 true — an updated rate chains a new
row rather than editing an old one, so an already-billed period keeps the rate it was billed at.
`EmployeeSite` is the many-to-many placement of Requirement 7.1, which records expectation only and
never restricts where time can be recorded (Requirement 7.2).

No coordinate, radius or location column exists on any of them, by requirement (6.8, 20.10) — the
same absence the migration asserts against the live catalogue.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Imported for the foreign keys below: `client_id` names `clients`, `manager_user_id` names `users`,
# and `employee_sites.employee_id` names `employees`. Resolving any of them needs the referenced
# table registered on the shared metadata, which matters on SQLite where the unit tests build the
# schema from the models rather than from the migration.
from app.models.client import Client
from app.models.employee import Employee  # noqa: F401
from app.models.user import User  # noqa: F401


class SiteStatus(enum.StrEnum):
    """Requirement 6.2. Values match the `site_status` PostgreSQL enum."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ON_HOLD = "on_hold"


class QrMode(enum.StrEnum):
    """Requirement 8.2, 8.3. Values match the `qr_mode` PostgreSQL enum."""

    UNIFIED = "unified"
    SEPARATE = "separate"


class AssignmentMode(enum.StrEnum):
    """Requirement 7.3, 7.4. Values match the `assignment_mode` PostgreSQL enum."""

    OPEN = "open"
    STRICT = "strict"


#: Only an active site accepts a new check-in (Requirement 6.5). Kept beside the enum so the scan
#: path reads the same rule rather than spelling out "== active".
STATUSES_ALLOWING_CHECK_IN = frozenset({SiteStatus.ACTIVE})


def _enum_column(enum_type: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Site(Base):
    """A place work is done, owned by a client and billed at its own rate."""

    __tablename__ = "sites"

    # Names given explicitly so they match migration 0001. The unique constraints and the two check
    # constraints are declared under the plain names the migration used, so autogenerate sees no
    # rename.
    __table_args__ = (
        UniqueConstraint("site_number", name="uq_sites_site_number"),
        UniqueConstraint("qr_token", name="uq_sites_qr_token"),
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="dates_ordered",
        ),
        CheckConstraint("qr_token_version >= 1", name="qr_token_version_positive"),
        Index("ix_sites_client_id", "client_id"),
        Index("ix_sites_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    site_number: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)

    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", name="fk_sites_client_id_clients"),
        nullable=False,
    )

    #: At most one manager per site (Requirement 6.7). Nullable: a site may exist before a manager is
    #: assigned.
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_sites_manager_user_id_users"),
        nullable=True,
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[SiteStatus] = mapped_column(
        _enum_column(SiteStatus, "site_status"), nullable=False, default=SiteStatus.ACTIVE
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    qr_mode: Mapped[QrMode] = mapped_column(
        _enum_column(QrMode, "qr_mode"), nullable=False, default=QrMode.UNIFIED
    )
    qr_token: Mapped[str] = mapped_column(Text, nullable=False)
    qr_token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    assignment_mode: Mapped[AssignmentMode] = mapped_column(
        _enum_column(AssignmentMode, "assignment_mode"), nullable=False, default=AssignmentMode.OPEN
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    #: The owning client. Read by the client service to list a client's sites (Requirement 5.3).
    client: Mapped[Client] = relationship()

    #: Billing-rate history, oldest first. Ordered the same direction as the resolver reads it, so a
    #: mid-month rate change splits a month correctly (Requirement 6.6, 17.1). Never cascaded on a
    #: site delete for the same reason employee rates are not: history a bill was computed from must
    #: outlive a careless delete. Deleting a rate row happens only through `replace_rate_history`,
    #: which reconciles the chain deliberately.
    rates: Mapped[list[SiteRate]] = relationship(
        back_populates="site",
        order_by="SiteRate.effective_from",
        cascade="all, delete-orphan",
    )

    @property
    def can_check_in(self) -> bool:
        """Whether this site's status permits a new check-in (Requirement 6.5)."""
        return self.status in STATUSES_ALLOWING_CHECK_IN

    def __repr__(self) -> str:
        return f"Site({self.name!r} number={self.site_number!r} status={self.status})"


class SiteRate(Base):
    """One effective-dated billing row for a site (Requirement 6.4, 6.6, 17.1, 17.2).

    Non-overlapping per site: the database enforces it with a GiST exclusion constraint in
    PostgreSQL, and the service enforces it everywhere by validating the submitted chain before it
    writes. `effective_to = NULL` means "still in force"; the resolver picks the row whose period
    covers a given work date, which is what makes updating a rate leave an already-billed period
    untouched — the old row is not edited, a new one is chained after it.

    `overtime_billing_rate` is optional: where none is configured, overtime bills at the standard
    `billing_rate` (Requirement 17.2). The billing figures on this row are visible only to
    administrators and accounting (Requirement 2.5, 17.7); the redaction that enforces that lives at
    the response boundary, keyed on the field names in `app.core.authz.BILLING_FIELDS`.
    """

    __tablename__ = "site_rates"

    # The two check constraints migration 0001 created (`period_ordered`, `non_negative`) and the
    # GiST exclusion constraint (`ex_site_rates_no_overlapping_periods`) are deliberately not
    # declared here, exactly as `EmployeeRate` documents for its own: the exclusion constraint cannot
    # be a model attribute, and declaring the checks would make autogenerate propose a rename against
    # the plain names the migration used. The database enforces all three in PostgreSQL; the service
    # enforces the non-overlap rule in application code too, for the SQLite unit-test engine.
    __table_args__ = (
        Index("ix_site_rates_site_id_effective_from", "site_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sites.id", name="fk_site_rates_site_id_sites"),
        nullable=False,
    )

    billing_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    #: NULL means overtime bills at the standard rate (Requirement 17.2).
    overtime_billing_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: NULL means still in force. Billing resolves the row covering each work date (Requirement 17.1).
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    site: Mapped[Site] = relationship(back_populates="rates")

    def covers(self, on_date: date) -> bool:
        """Whether this row is the billing rate in force on `on_date`.

        The period is inclusive at both ends, matching the `daterange(..., '[]')` the exclusion
        constraint uses: a row effective 1–15 August and a row effective from 16 August do not
        overlap, and every August date is covered by exactly one of them.
        """
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to

    def __repr__(self) -> str:
        return f"SiteRate(site={self.site_id} from={self.effective_from} to={self.effective_to})"


class EmployeeSite(Base):
    """One (employee, site) assignment: the `employee_sites` table (Requirement 7.1).

    A mapping onto the table migration 0001 created. The pair is the fact, so the primary key is the
    two ids together and there is no surrogate id, matching `UserSite`.

    This table records *expected* placement only. It never restricts where time can be recorded —
    an employee works at several sites a day whether assigned or not (Requirement 7.2). Whether an
    unassigned check-in is flagged or rejected is decided by `sites.assignment_mode`, not by the
    presence of a row here, and even the strict mode is an expectation control, not a location one.
    """

    __tablename__ = "employee_sites"

    __table_args__ = (
        CheckConstraint(
            "assigned_to IS NULL OR assigned_to >= assigned_from",
            name="dates_ordered",
        ),
        Index("ix_employee_sites_site_id", "site_id"),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("employees.id", name="fk_employee_sites_employee_id_employees", ondelete="CASCADE"),
        primary_key=True,
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sites.id", name="fk_employee_sites_site_id_sites", ondelete="CASCADE"),
        primary_key=True,
    )

    #: When the assignment began. Defaults to the day the row is written; carried so a future report
    #: can say who was expected where and when.
    assigned_from: Mapped[date] = mapped_column(Date, nullable=False)
    assigned_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self) -> str:
        return f"EmployeeSite(employee={self.employee_id} site={self.site_id})"
