"""Client service rules that do not need HTTP (Requirement 5).

The claims here are the ones the requirement pins to the service: that a company number is unique
when present and the conflict is named, that a client whose sites carry time entries cannot be
deleted, and that archival succeeds in exactly that case and leaves the row and its sites intact.

These run against the in-memory SQLite session the suite provides. The database's unique constraint
on `company_number` is the backstop; what is tested here is the application enforcement that must hold
on both engines, plus the delete-versus-archive decision, which is application logic on either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.change_log import ChangeLog
from app.models.client import Client
from app.models.site import Site, SiteStatus
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryStatus
from app.schemas.client import ClientCreate, ClientUpdate
from app.services import client as client_service
from app.services.audit import AuditContext


def _context() -> AuditContext:
    return AuditContext(actor_user_id=uuid.uuid4(), reason=None)


def _create_payload(name: str = "Acme Construction", **overrides) -> ClientCreate:
    fields: dict[str, object] = {"name": name}
    fields.update(overrides)
    return ClientCreate(**fields)


def _make_client(session: Session, **overrides) -> Client:
    client = client_service.create_client(session, _create_payload(**overrides), context=_context())
    session.commit()
    return client


def _make_site(session: Session, client_id: uuid.UUID, *, number: str = "S-1") -> Site:
    site = Site(
        name=f"Site {number}",
        site_number=number,
        client_id=client_id,
        status=SiteStatus.ACTIVE,
        qr_token=f"token-{number}-{uuid.uuid4().hex}",
    )
    session.add(site)
    session.commit()
    return site


def _make_employee_id(session: Session) -> uuid.UUID:
    """A bare employee row is needed as the time entry's parent; only its id matters here."""
    from app.models.employee import Employee

    employee = Employee(
        full_name="Worker",
        full_name_en="Worker",
        passport_number="P0000001",
        passport_number_hash="P0000001",
        phone="+972500000000",
        country="Israel",
        emergency_contact_name="Contact",
        emergency_contact_phone="+972500000001",
        start_date=date(2025, 1, 1),
    )
    session.add(employee)
    session.commit()
    return employee.id


def _make_time_entry(session: Session, employee_id: uuid.UUID, site_id: uuid.UUID) -> TimeEntry:
    entry = TimeEntry(
        employee_id=employee_id,
        site_id=site_id,
        work_date=date(2025, 8, 1),
        check_in_at=datetime(2025, 8, 1, 5, 0, tzinfo=UTC),
        source=TimeEntrySource.QR_SCAN,
        status=TimeEntryStatus.DRAFT,
        flags=[],
    )
    session.add(entry)
    session.commit()
    return entry


# --------------------------------------------------------------------------- company-number uniqueness


def test_a_duplicate_company_number_is_rejected_and_names_the_conflict(session: Session):
    """Requirement 5.2. A second client with the same company number collides, and the conflicting
    client is reported."""
    first = _make_client(session, company_number="514000000")

    with pytest.raises(client_service.DuplicateCompanyNumber) as raised:
        client_service.create_client(
            session,
            _create_payload(name="Other Co", company_number="514000000"),
            context=_context(),
        )

    assert raised.value.conflicting.id == first.id


def test_clients_without_a_company_number_do_not_collide(session: Session):
    """Requirement 5.2: uniqueness is only "when provided"; several clients may have none."""
    first = _make_client(session, name="Alpha")
    second = _make_client(session, name="Beta")

    assert first.company_number is None
    assert second.company_number is None
    assert first.id != second.id


def test_updating_a_company_number_to_one_in_use_is_rejected(session: Session):
    first = _make_client(session, name="Alpha", company_number="111111111")
    second = _make_client(session, name="Beta", company_number="222222222")

    with pytest.raises(client_service.DuplicateCompanyNumber) as raised:
        client_service.update_client(
            session, second.id, ClientUpdate(company_number="111111111"), context=_context()
        )
    assert raised.value.conflicting.id == first.id


def test_updating_other_fields_does_not_trip_the_company_number_check(session: Session):
    """A patch that leaves the company number unchanged must not collide the row with itself."""
    client = _make_client(session, company_number="333333333")
    updated = client_service.update_client(
        session, client.id, ClientUpdate(contact_person="Dana"), context=_context()
    )
    assert updated.contact_person == "Dana"


# --------------------------------------------------------------------------- delete vs archive


def test_deletion_is_blocked_when_a_site_has_time_entries(session: Session):
    """Requirement 5.4: a client whose sites carry time entries may not be deleted."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    employee_id = _make_employee_id(session)
    _make_time_entry(session, employee_id, site.id)

    with pytest.raises(client_service.ClientHasTimeEntries):
        client_service.delete_client(session, client.id, context=_context())

    # The row is still there — a refused delete changes nothing.
    assert session.get(Client, client.id) is not None


def test_a_client_with_no_time_entries_can_be_deleted(session: Session):
    """The other half: with no recorded work, even a client that owns sites deletes cleanly."""
    client = _make_client(session)
    _make_site(session, client.id)

    client_service.delete_client(session, client.id, context=_context())
    session.commit()

    assert session.get(Client, client.id) is None


def test_archival_succeeds_and_keeps_the_row_and_its_sites(session: Session):
    """Requirement 5.4: archival is the alternative to deletion — the row and its sites survive."""
    client = _make_client(session)
    site = _make_site(session, client.id)
    employee_id = _make_employee_id(session)
    _make_time_entry(session, employee_id, site.id)

    archived = client_service.archive_client(session, client.id, context=_context())
    session.commit()

    assert archived.is_archived is True
    assert session.get(Client, client.id) is not None
    assert session.get(Site, site.id) is not None


def test_archival_is_idempotent(session: Session):
    client = _make_client(session)
    client_service.archive_client(session, client.id, context=_context())
    session.commit()
    again = client_service.archive_client(session, client.id, context=_context())
    assert again.is_archived is True


def test_archived_clients_are_excluded_from_the_default_list(session: Session):
    active = _make_client(session, name="Active Co")
    archived = _make_client(session, name="Archived Co")
    client_service.archive_client(session, archived.id, context=_context())
    session.commit()

    default_page = client_service.list_clients(session)
    with_archived = client_service.list_clients(session, include_archived=True)

    default_ids = {client.id for client in default_page.items}
    all_ids = {client.id for client in with_archived.items}
    assert active.id in default_ids
    assert archived.id not in default_ids
    assert archived.id in all_ids


# --------------------------------------------------------------------------- sites listing


def test_list_sites_returns_a_clients_sites(session: Session):
    """Requirement 5.3."""
    client = _make_client(session)
    _make_site(session, client.id, number="S-1")
    _make_site(session, client.id, number="S-2")
    other = _make_client(session, name="Other")
    _make_site(session, other.id, number="S-9")

    sites = client_service.list_sites(session, client.id)

    assert {site.site_number for site in sites} == {"S-1", "S-2"}


def test_list_sites_of_an_unknown_client_is_not_found(session: Session):
    with pytest.raises(client_service.ClientNotFound):
        client_service.list_sites(session, uuid.uuid4())


# --------------------------------------------------------------------------- audit


def test_creation_writes_an_audit_row_per_field(session: Session):
    """Requirement 13: every field set at creation is audited."""
    client = _make_client(session, company_number="514000000")

    rows = list(
        session.scalars(
            select(ChangeLog).where(
                ChangeLog.entity_type == "clients", ChangeLog.entity_id == client.id
            )
        )
    )
    fields = {row.field for row in rows}
    assert "name" in fields
    assert "company_number" in fields


def test_a_field_edit_writes_one_row_for_the_changed_field(session: Session):
    client = _make_client(session)
    before = list(session.scalars(select(ChangeLog).where(ChangeLog.entity_id == client.id)))

    client_service.update_client(
        session, client.id, ClientUpdate(contact_person="Yossi"), context=_context()
    )
    session.commit()

    after = list(session.scalars(select(ChangeLog).where(ChangeLog.entity_id == client.id)))
    new_rows = [row for row in after if row not in before]
    assert [row.field for row in new_rows] == ["contact_person"]
    assert new_rows[0].new_value == "Yossi"
