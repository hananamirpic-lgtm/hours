"""Audit writer tests.

The two claims that carry Requirement 13 are here: one row per field that actually changed, and the
rows live or die with the caller's transaction. The second is the one a plausible refactor breaks —
the moment someone gives the writer its own session or commits inside it, a change can exist without
its audit record and nothing in the application will say so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.change_log import REDACTED, ChangeLog
from app.services.audit import (
    AuditContext,
    entity_type_of,
    format_value,
    record_change,
    record_model_changes,
    sensitive_field_names,
    snapshot,
    values_equal,
)
from sample_models import SampleEntity

ACTOR = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONTEXT = AuditContext(actor_user_id=ACTOR, request_id="req-abc123")


@pytest.fixture
def entity(session: Session) -> SampleEntity:
    created = SampleEntity(
        full_name="אברהם כהן",
        position="Labourer",
        notice_period_days=30,
        passport_number="A1234567",
        passport_number_hash="A1234567",
        date_of_birth=date(1985, 3, 17),
    )
    session.add(created)
    session.commit()
    return created


def _logs(session: Session) -> list[ChangeLog]:
    return list(session.scalars(select(ChangeLog).order_by(ChangeLog.field)))


def _by_field(session: Session) -> dict[str, ChangeLog]:
    return {entry.field: entry for entry in _logs(session)}


# --------------------------------------------------------------------------- one row per field


def test_one_row_per_changed_field(session: Session, entity: SampleEntity):
    before = snapshot(entity)
    entity.position = "Foreman"
    entity.notice_period_days = 60
    record_model_changes(session, entity, before, context=CONTEXT, reason="Promotion")
    session.commit()

    assert {entry.field for entry in _logs(session)} == {"position", "notice_period_days"}


def test_row_records_every_detail_requirement_13_1_asks_for(session: Session, entity: SampleEntity):
    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=CONTEXT, reason="Promotion")
    session.commit()

    entry = _by_field(session)["position"]
    assert entry.entity_type == "sample_entities"
    assert entry.entity_id == entity.id
    assert entry.changed_by_user_id == ACTOR
    assert entry.changed_at is not None
    assert entry.old_value == "Labourer"
    assert entry.new_value == "Foreman"
    assert entry.reason == "Promotion"
    assert entry.request_id == "req-abc123"


def test_unchanged_fields_produce_no_rows(session: Session, entity: SampleEntity):
    """A save with nothing changed must leave the table alone, or the audit becomes unreadable."""
    before = snapshot(entity)
    entity.position = "Labourer"  # the value it already had
    written = record_model_changes(session, entity, before, context=CONTEXT)
    session.commit()

    assert written == []
    assert _logs(session) == []


def test_bookkeeping_columns_are_not_audited(session: Session, entity: SampleEntity):
    before = snapshot(entity)
    entity.updated_at = datetime(2025, 8, 30, 18, 42, tzinfo=UTC)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=CONTEXT)
    session.commit()

    assert {entry.field for entry in _logs(session)} == {"position"}


def test_setting_a_value_from_null_is_audited(session: Session, entity: SampleEntity):
    entity.position = None
    session.commit()

    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=CONTEXT)
    session.commit()

    entry = _by_field(session)["position"]
    assert entry.old_value is None
    assert entry.new_value == "Foreman"


def test_clearing_a_value_is_audited(session: Session, entity: SampleEntity):
    before = snapshot(entity)
    entity.position = None
    record_model_changes(session, entity, before, context=CONTEXT)
    session.commit()

    entry = _by_field(session)["position"]
    assert entry.old_value == "Labourer"
    assert entry.new_value is None


def test_fields_can_be_narrowed(session: Session, entity: SampleEntity):
    """A service that only means to audit part of an update can say so."""
    before = snapshot(entity)
    entity.position = "Foreman"
    entity.notice_period_days = 60
    record_model_changes(session, entity, before, context=CONTEXT, fields=["position"])
    session.commit()

    assert {entry.field for entry in _logs(session)} == {"position"}


def test_a_system_change_has_no_actor(session: Session, entity: SampleEntity):
    """A shift closed by a site transition is nobody's edit, and pretending otherwise is a lie in a
    table whose purpose is attribution."""
    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(
        session,
        entity,
        before,
        context=AuditContext(reason="system_transition"),
    )
    session.commit()

    entry = _by_field(session)["position"]
    assert entry.changed_by_user_id is None
    assert entry.reason == "system_transition"


def test_context_reason_is_the_default_and_a_call_reason_overrides_it(session: Session, entity: SampleEntity):
    context = AuditContext(actor_user_id=ACTOR, reason="Bulk import")

    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=context)

    before = snapshot(entity)
    entity.notice_period_days = 60
    record_model_changes(session, entity, before, context=context, reason="Correction")
    session.commit()

    logs = _by_field(session)
    assert logs["position"].reason == "Bulk import"
    assert logs["notice_period_days"].reason == "Correction"


def test_auditing_an_unflushed_instance_is_refused(session: Session):
    """Without an id there is nothing to attribute the change to, and a null entity_id would make the
    row unfindable rather than merely incomplete."""
    created = SampleEntity(full_name="Not Saved")  # id is assigned at flush, so there is none yet
    with pytest.raises(ValueError, match="no id"):
        record_model_changes(session, created, {}, context=CONTEXT)


# --------------------------------------------------------------------------- sensitive fields


def test_sensitive_values_are_redacted_not_recorded(session: Session, entity: SampleEntity):
    """Requirement 20.2 says these values are encrypted at rest. `change_logs` holds plain text and is
    never deleted, so recording the plaintext here would undo the encryption on the column beside it.
    The change is still attributable, which is what Requirement 13 needs."""
    before = snapshot(entity)
    entity.passport_number = "B7654321"
    entity.date_of_birth = date(1990, 1, 1)
    record_model_changes(session, entity, before, context=CONTEXT, reason="Renewed passport")
    session.commit()

    logs = _by_field(session)
    assert logs["passport_number"].old_value == REDACTED
    assert logs["passport_number"].new_value == REDACTED
    assert logs["date_of_birth"].old_value == REDACTED

    recorded = " ".join(
        value for entry in _logs(session) for value in (entry.old_value, entry.new_value) if value
    )
    assert "A1234567" not in recorded
    assert "B7654321" not in recorded
    assert "1985" not in recorded


def test_a_sensitive_field_set_from_null_records_null_not_a_marker(session: Session, entity: SampleEntity):
    """Redaction hides a value; it must not invent one. "was empty, now set" is not sensitive."""
    entity.passport_number = None
    session.commit()

    before = snapshot(entity)
    entity.passport_number = "B7654321"
    record_model_changes(session, entity, before, context=CONTEXT)
    session.commit()

    entry = _by_field(session)["passport_number"]
    assert entry.old_value is None
    assert entry.new_value == REDACTED


def test_sensitive_fields_are_read_from_the_column_types(entity: SampleEntity):
    """A hand-maintained list would drift, and it drifts in the direction that leaks."""
    assert sensitive_field_names(entity) == {
        "passport_number",
        "passport_number_hash",
        "date_of_birth",
    }


# --------------------------------------------------------------------------- transactional fate


def test_audit_rows_roll_back_with_the_transaction(session: Session, entity: SampleEntity):
    """Requirement 13.2 in one test: the audit row cannot survive a change that did not happen."""
    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=CONTEXT)
    session.flush()
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 1

    session.rollback()
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0
    session.expire_all()
    assert session.get(SampleEntity, entity.id).position == "Labourer"  # type: ignore[union-attr]


def test_audit_rows_die_with_a_failing_write(session: Session, entity: SampleEntity):
    """The realistic shape of the failure: the audit row is added, and then the change it describes
    breaks a constraint. Neither may end up in the database."""
    before = snapshot(entity)
    entity.position = "Foreman"
    record_model_changes(session, entity, before, context=CONTEXT)
    session.add(SampleEntity(full_name=None))  # type: ignore[arg-type]  # NOT NULL

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0
    session.expire_all()
    assert session.get(SampleEntity, entity.id).position == "Labourer"  # type: ignore[union-attr]


def test_the_writer_neither_flushes_nor_commits(session: Session, entity: SampleEntity):
    """The caller owns the transaction boundary. A writer that flushed would surface its own errors at
    the wrong moment, and one that committed would also commit whatever half-finished work the caller
    still had in flight."""
    before = snapshot(entity)
    entity.position = "Foreman"
    written = record_model_changes(session, entity, before, context=CONTEXT)

    assert written and all(entry in session.new for entry in written)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(ChangeLog)) == 0


def test_record_change_renders_the_values_it_was_given(session: Session):
    record_change(
        session,
        entity_type="time_entries",
        entity_id=uuid.uuid4(),
        field="check_out_at",
        old_value=datetime(2025, 8, 30, 12, 30, tzinfo=UTC),
        new_value=datetime(2025, 8, 30, 13, 0, tzinfo=UTC),
        context=CONTEXT,
        reason="Forgot to scan out",
    )
    session.commit()

    entry = _by_field(session)["check_out_at"]
    assert entry.old_value == "2025-08-30T12:30:00+00:00"
    assert entry.new_value == "2025-08-30T13:00:00+00:00"


# --------------------------------------------------------------------------- rendering and diffing


def test_entity_type_is_the_table_name(entity: SampleEntity):
    assert entity_type_of(entity) == "sample_entities"


class _Status(Enum):
    APPROVED = "approved"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("Foreman", "Foreman"),
        (True, "true"),
        (False, "false"),
        (30, "30"),
        (Decimal("35.00"), "35.00"),
        (Decimal("1E+2"), "100"),
        (date(2025, 8, 30), "2025-08-30"),
        (datetime(2025, 8, 30, 18, 42, tzinfo=UTC), "2025-08-30T18:42:00+00:00"),
        (_Status.APPROVED, "approved"),
        (uuid.UUID("22222222-2222-2222-2222-222222222222"), "22222222-2222-2222-2222-222222222222"),
    ],
)
def test_value_rendering(value: object, expected: str | None):
    assert format_value(value) == expected


def test_booleans_do_not_render_as_python_repr():
    """The audit view puts these strings in front of a user, in Hebrew or English. `True` is neither."""
    assert format_value(True) != "True"


@pytest.mark.parametrize(
    ("old", "new", "equal"),
    [
        (None, None, True),
        (None, "Foreman", False),
        ("Foreman", None, False),
        ("Foreman", "Foreman", True),
        ("Foreman", "Labourer", False),
        (Decimal("35.00"), Decimal("35.0"), True),
        (Decimal("35.00"), Decimal("35.50"), False),
        (date(2025, 8, 30), "2025-08-30", True),
        (30, "30", True),
    ],
)
def test_change_detection(old: object, new: object, equal: bool):
    assert values_equal(old, new) is equal


def test_snapshot_captures_plaintext_for_encrypted_columns(session: Session, entity: SampleEntity):
    """The diff compares what the service sees, so it must compare plaintext against plaintext — a
    ciphertext differs on every write and would report a change on every save."""
    session.expire_all()
    captured = snapshot(session.get(SampleEntity, entity.id))
    assert captured["passport_number"] == "A1234567"
    assert captured["date_of_birth"] == date(1985, 3, 17)


def test_a_reloaded_entity_shows_no_change(session: Session, entity: SampleEntity):
    """The consequence of the above, stated as the behaviour that matters: load, save, and the audit
    table stays empty. Compared as ciphertext, every save would report every encrypted field changed."""
    session.expire_all()
    reloaded = session.get(SampleEntity, entity.id)
    assert reloaded is not None
    record_model_changes(session, reloaded, snapshot(reloaded), context=CONTEXT)
    session.commit()

    assert _logs(session) == []
