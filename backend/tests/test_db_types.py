"""Column type tests.

Two claims: a service sees plaintext in both directions, and the database sees only ciphertext. The
second is the one worth a test — an encryption layer that quietly stores the plaintext looks
identical from the ORM side, and the only way to notice is to read the raw column.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.core.crypto import Encryptor, get_encryptor
from app.db.types import DeterministicHash, EncryptedDate, EncryptedString
from sample_models import SampleEntity

PASSPORT = "A1234567"
BIRTH_DATE = date(1985, 3, 17)


@pytest.fixture
def stored(session: Session) -> SampleEntity:
    entity = SampleEntity(
        full_name="אברהם כהן",
        passport_number=PASSPORT,
        passport_number_hash=PASSPORT,
        date_of_birth=BIRTH_DATE,
    )
    session.add(entity)
    session.commit()
    return entity


def _raw(engine: Engine, column: str, entity_id: uuid.UUID) -> str:
    """The value as the database holds it, read outside the ORM so no type decorator can help.

    SQLite has no uuid type, so SQLAlchemy stores the hex form without separators; the parameter has
    to match that rather than `str(uuid)`.
    """
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT {column} FROM sample_entities WHERE id = :id"),
            {"id": entity_id.hex},
        ).scalar_one()


# --------------------------------------------------------------------------- round trip


def test_encrypted_string_round_trips_through_the_database(session: Session, stored: SampleEntity):
    session.expire_all()
    loaded = session.get(SampleEntity, stored.id)
    assert loaded is not None
    assert loaded.passport_number == PASSPORT


def test_encrypted_date_round_trips_as_a_date(session: Session, stored: SampleEntity):
    session.expire_all()
    loaded = session.get(SampleEntity, stored.id)
    assert loaded is not None
    assert loaded.date_of_birth == BIRTH_DATE
    assert isinstance(loaded.date_of_birth, date)


def test_null_stays_null_in_both_directions(session: Session):
    entity = SampleEntity(full_name="No Passport")
    session.add(entity)
    session.commit()
    session.expire_all()

    loaded = session.get(SampleEntity, entity.id)
    assert loaded is not None
    assert loaded.passport_number is None
    assert loaded.date_of_birth is None
    assert loaded.passport_number_hash is None


# --------------------------------------------------------------------------- what the database holds


def test_database_holds_ciphertext_not_plaintext(sqlite_engine: Engine, stored: SampleEntity):
    raw = _raw(sqlite_engine, "passport_number", stored.id)
    assert PASSPORT not in raw
    assert Encryptor.is_encrypted(raw)


def test_database_holds_the_date_encrypted(sqlite_engine: Engine, stored: SampleEntity):
    raw = _raw(sqlite_engine, "date_of_birth", stored.id)
    assert "1985" not in raw
    assert Encryptor.is_encrypted(raw)


def test_two_rows_with_the_same_value_hold_different_ciphertexts(session: Session, sqlite_engine: Engine):
    """Otherwise the column leaks equality, and counting duplicates needs no key at all."""
    first = SampleEntity(full_name="First", passport_number=PASSPORT)
    second = SampleEntity(full_name="Second", passport_number=PASSPORT)
    session.add_all([first, second])
    session.commit()
    assert _raw(sqlite_engine, "passport_number", first.id) != _raw(
        sqlite_engine, "passport_number", second.id
    )


# --------------------------------------------------------------------------- deterministic hash


def test_hash_column_stores_the_digest_of_the_assigned_plaintext(sqlite_engine: Engine, stored: SampleEntity):
    expected = get_encryptor().deterministic_hash(PASSPORT)
    assert _raw(sqlite_engine, "passport_number_hash", stored.id) == expected


def test_hash_column_is_queryable_from_a_plaintext_parameter(session: Session, stored: SampleEntity):
    """This is what makes the partial unique index usable: look up by the value, not by the digest."""
    found = session.scalars(select(SampleEntity).where(SampleEntity.passport_number_hash == PASSPORT)).one()
    assert found.id == stored.id


def test_hash_lookup_normalises_the_parameter(session: Session, stored: SampleEntity):
    found = session.scalars(
        select(SampleEntity).where(SampleEntity.passport_number_hash == " a1234567 ")
    ).one()
    assert found.id == stored.id


def test_an_already_hashed_value_passes_through_unchanged(session: Session, stored: SampleEntity):
    """A load returns the digest, so a digest is what a caller has in hand when it compares two rows
    or copies a value between them. Hashing it a second time would move the value out from under its
    own unique index and stop it colliding with the duplicate it exists to catch."""
    session.expire_all()
    loaded = session.get(SampleEntity, stored.id)
    assert loaded is not None
    digest = loaded.passport_number_hash
    assert digest is not None and Encryptor.is_hashed(digest)

    found = session.scalars(select(SampleEntity).where(SampleEntity.passport_number_hash == digest)).one()
    assert found.id == stored.id


def test_hash_bind_is_idempotent():
    digest = get_encryptor().deterministic_hash(PASSPORT)
    assert DeterministicHash().process_bind_param(digest, None) == digest


def test_hash_refuses_a_non_string():
    with pytest.raises(TypeError, match="expected str"):
        DeterministicHash().process_bind_param(1234567, None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- misuse
# Exercised on the type directly: SQLAlchemy wraps an error raised while binding parameters, and the
# session is unusable afterwards, so going through a flush would test the wrapper rather than this.


def test_encrypted_string_refuses_a_non_string():
    with pytest.raises(TypeError, match="expected str"):
        EncryptedString().process_bind_param(1234567, None)  # type: ignore[arg-type]


def test_encrypted_date_refuses_a_string():
    with pytest.raises(TypeError, match="expected date"):
        EncryptedDate().process_bind_param("1985-03-17", None)  # type: ignore[arg-type]


def test_encrypted_date_accepts_only_dates_and_round_trips_them():
    column = EncryptedDate()
    sealed = column.process_bind_param(BIRTH_DATE, None)
    assert sealed is not None
    assert column.process_result_value(sealed, None) == BIRTH_DATE
