# Hours Project — Employee Management System

Bilingual (Hebrew / English) employee management system: multi-site attendance by QR scan, hour
classification, payroll, client billing and profitability reporting.

Location capture is out of scope. No coordinate, radius or geofence data is collected, stored or
transmitted anywhere in this system.

## Layout

```
Hours_project/
├── backend/            FastAPI service (routers → services → repositories, pure calculation modules)
├── frontend/           React 18 + TypeScript + Vite (employee mobile app + management console)
├── infra/              Local and cloud infrastructure assets
├── docker-compose.yml  Local runtime: api, web, postgres, redis, minio, mailhog
└── .env.example        Every variable the stack needs. Secrets have no defaults.
```

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic |
| Database | PostgreSQL 16 |
| Cache / rate limiting | Redis |
| Object storage | MinIO locally, S3-compatible in cloud |
| Mail | MailHog locally |
| Frontend | React 18, TypeScript, Vite, TanStack Query, i18next |
| Tests | pytest (backend), `tsc --noEmit` + Vite build (frontend) |

## Running locally

Prerequisites: Docker Desktop (or another Docker Engine) with Compose v2.

```bash
cp .env.example .env          # then fill in real values — see "Secrets" below
docker compose up --build
```

Compose reads a variable from your shell environment before it reads `.env`, so a stale exported
value silently wins over the file. If a container behaves as though `.env` were ignored, check the
shell first (`docker compose config` prints what Compose actually resolved).

| Service | URL |
|---|---|
| API | http://localhost:8000/api/health |
| API docs | http://localhost:8000/docs |
| Web | http://localhost:5173 |
| MinIO console | http://localhost:9001 |
| MailHog | http://localhost:8025 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

Every published port binds to `127.0.0.1` only, so the stack is reachable from this machine and
nowhere else. Nothing here is intended to be exposed to a network.

Health endpoints:

- `GET /api/health` — liveness. Answers without touching any dependency.
- `GET /api/health/ready` — readiness. Reports per-dependency status for PostgreSQL, Redis and object
  storage. Returns `200` when every dependency is up, `503` when any is down.

```bash
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/api/health/ready
```

### Secrets

Every secret is read from the environment and has **no default**. The API refuses to start if one is
missing, which is deliberate — a development fallback secret has a way of reaching production.

Generate values with:

```bash
openssl rand -base64 48   # JWT_SECRET_KEY
openssl rand -base64 32   # ENCRYPTION_KEY (AES-256 key material)
```

`.env` is git-ignored. No secret belongs in this repository.

## Running without Docker

Backend:

```bash
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev            # dev server on :5173, proxies /api to the backend
npm run typecheck
npm run build
```

## Conventions

- **Layering.** Routers do HTTP only. Services own business rules and transaction boundaries.
  Repositories own SQL. Calculation modules (`app/calculations/`) are pure functions over plain values
  with no database or HTTP access.
- **Time.** Stored `timestamptz` in UTC, classified in `Asia/Jerusalem`. Durations are integer minutes.
- **Money.** `NUMERIC(12,2)` in the database, `Decimal` with `ROUND_HALF_UP` in Python. No floats.
- **Locale.** The API is locale-neutral: ISO 8601 timestamps, raw decimals, and machine error codes.
  All translation and formatting happens in the frontend and the export renderers.
- **Schema changes.** Versioned Alembic migrations only.
