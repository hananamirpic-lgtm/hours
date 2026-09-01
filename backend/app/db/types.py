"""Column types that make encryption invisible to the service layer.

A service should be able to write `employee.passport_number = "A1234567"` and read it back the same
way. If encryption were a service's job, every new read path would be a chance to forget to decrypt
and every new write path a chance to store plaintext — and the failure is silent in both directions.
Pushing it into the column type means the only code that can get it wrong is this file.

Three types, because the sensitive columns are not all strings:

* `EncryptedString` — passport number, phone, address, TOTP secret.
* `EncryptedDate` — date of birth, held as an ISO date inside the ciphertext so it is still a real
  `date` to the caller.
* `DeterministicHash` — the `*_hash` companion columns. Assign the plaintext; the digest is what
  reaches the database, so `WHERE passport_number_hash = :value` works from a plaintext parameter
  and the partial unique index does its job.

An encrypted column cannot be sorted, range-scanned or matched with `LIKE` in the database — the
ciphertext ordering is meaningless. That is the cost of the requirement, and it is why the fields
that need uniqueness or lookup carry a hash column beside them rather than being searched directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Dialect, Text
from sqlalchemy.types import TypeDecorator

from app.core.crypto import Encryptor, get_encryptor


class _EncryptedType(TypeDecorator[Any]):
    """Shared plumbing: store as `Text`, encrypt on the way in, decrypt on the way out.

    `cache_ok` is true because the type carries no per-instance state that changes the SQL it
    produces; without it SQLAlchemy refuses to cache any statement touching the column and logs a
    warning on every compile.
    """

    impl = Text
    cache_ok = True

    @property
    def _encryptor(self) -> Encryptor:
        # Resolved per call rather than in __init__, because the type is instantiated at class
        # definition time — before settings are guaranteed to be loaded.
        return get_encryptor()

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self._encryptor.encrypt(self._to_text(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        return self._from_text(self._encryptor.decrypt(value))

    # Subclass hooks. Text in, text out; the ciphertext never sees a non-string.
    def _to_text(self, value: Any) -> str:
        raise NotImplementedError

    def _from_text(self, text: str) -> Any:
        raise NotImplementedError


class EncryptedString(_EncryptedType):
    """A string column, encrypted at rest."""

    def _to_text(self, value: Any) -> str:
        if not isinstance(value, str):
            raise TypeError(f"expected str, got {type(value).__name__}")
        return value

    def _from_text(self, text: str) -> str:
        return text


class EncryptedDate(_EncryptedType):
    """A date column, encrypted at rest and still a `date` to the caller.

    ISO 8601 inside the ciphertext: unambiguous, sorts correctly if a future migration ever has to
    decrypt and re-store, and round-trips through `date.fromisoformat` without a format string to
    keep in step.
    """

    def _to_text(self, value: Any) -> str:
        if not isinstance(value, date):
            raise TypeError(f"expected date, got {type(value).__name__}")
        return value.isoformat()

    def _from_text(self, text: str) -> date:
        return date.fromisoformat(text)


class DeterministicHash(TypeDecorator[str]):
    """Stores the keyed digest of a plaintext value, and accepts either form on the way in.

    Reading gives back the digest, because a digest is one-way — the plaintext lives in the
    encrypted column beside it. That asymmetry is why an already-hashed value is passed through
    unchanged: loading an employee and saving it again must not hash the digest a second time and
    quietly move the row out from under its own unique index.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(f"expected str, got {type(value).__name__}")
        if Encryptor.is_hashed(value):
            return value
        return get_encryptor().deterministic_hash(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return value


#: Column types whose values are sensitive. The audit writer consults this to decide what it may
#: record in plaintext, so a new encrypted type added above is covered without editing that module.
SENSITIVE_TYPES: tuple[type[TypeDecorator[Any]], ...] = (_EncryptedType, DeterministicHash)
