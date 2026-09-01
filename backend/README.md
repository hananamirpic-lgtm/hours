# Hours API

FastAPI backend for the employee management system.

## Layout

```
app/
├── main.py               Application factory and wiring
├── api/
│   ├── router.py         Aggregates every router under /api
│   └── routers/          HTTP only: parse, validate, call one service, shape a response
├── services/             Business rules and transaction boundaries
├── repositories/         SQL and external-system access
├── calculations/         Pure functions over plain values (hours, payroll, billing)
├── schemas/              Pydantic request/response models
├── models/               ORM models mapped onto the migrated tables
├── db/                   Engine, session factory, declarative base, column types
└── core/                 Settings and cross-cutting concerns
alembic/                  Versioned migrations
tests/                    pytest; tests/integration needs a live PostgreSQL
```

A router never contains business logic or SQL. A service never builds SQL. A calculation module never
touches the database, the clock or HTTP — that is what makes the money logic testable in isolation.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                                            # tests
pytest tests/integration                          # needs TEST_DATABASE_URL; skips without it
ruff check . && ruff format --check .             # lint and formatting
uvicorn app.main:app --reload                     # serve on :8000
alembic upgrade head                              # apply migrations
alembic revision -m "description"                 # new migration
```

## Configuration

Settings are read from the environment by `app/core/config.py`. Secrets (`JWT_SECRET_KEY`,
`ENCRYPTION_KEY`, `DATABASE_URL`, `REDIS_URL`, the `S3_*` credentials) have no defaults; the process
refuses to start without them. See `../.env.example`.

## Encrypted columns

Sensitive fields (passport number, phone, date of birth, address, TOTP secret) are encrypted with
AES-GCM using a key derived from `ENCRYPTION_KEY`. A service never encrypts or decrypts anything:
declare the column with a type from `app/db/types.py` and assign plaintext.

```python
passport_number: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
passport_number_hash: Mapped[str | None] = mapped_column(DeterministicHash, nullable=True)
```

Encryption is non-deterministic, so an encrypted column cannot be sorted, range-scanned or matched
with `LIKE`. A field that has to stay unique or searchable carries a `DeterministicHash` column
alongside — assign the same plaintext to both, and query the hash column with a plaintext parameter.

## Audit

Every mutation gets one `change_logs` row per changed field, written in the caller's transaction so a
change cannot exist without its audit record. `app/services/audit.py` does the diffing:

```python
before = snapshot(employee)
employee.position = "Foreman"
record_model_changes(session, employee, before, context=context, reason="Promotion")
```

The writer never commits and never flushes — the caller owns the transaction boundary. Values of
encrypted columns are recorded as `[redacted]`, since `change_logs` is plain text and is never
deleted. `change_logs` is append-only, enforced by PostgreSQL privileges; see `app/db/roles.py`.

## Health

| Endpoint | Meaning |
|---|---|
| `GET /api/health` | Liveness. Touches no dependency, so it never fails because Postgres is slow. |
| `GET /api/health/ready` | Readiness. Probes PostgreSQL, Redis and object storage in parallel and reports each. `200` when all are up, `503` when any is down. |
