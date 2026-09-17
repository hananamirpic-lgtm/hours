# Infrastructure

## Local

`../docker-compose.yml` runs the whole system on one machine:

| Service | Image | Purpose |
|---|---|---|
| `api` | built from `backend/` (`dev` target) | FastAPI with reload |
| `web` | built from `frontend/` (`dev` target) | Vite dev server, proxies `/api` to `api` |
| `postgres` | `postgres:16-alpine` | Source of truth |
| `redis` | `redis:7-alpine` | Cache and rate-limit counters |
| `minio` | `minio/minio` | S3-compatible storage for employee documents |
| `minio-init` | `minio/mc` | Creates the documents bucket, sets anonymous access to none, exits |
| `mailhog` | `mailhog/mailhog` | Captures outbound mail; nothing leaves the machine |

Start order is enforced with health checks, so the API only starts once PostgreSQL, Redis and MinIO
answer and the bucket exists.

```bash
cd ..
cp .env.example .env      # fill in real values
docker compose up --build
docker compose config     # validate the file without starting anything
docker compose down -v    # stop and discard local data
```

### Migrations

Migrations are a release step, never an application startup side effect — two API replicas starting
together must not race to migrate the same database.

```bash
docker compose run --rm api alembic upgrade head
```

Migration `0003` seeds the reference data every deployment needs: the settings defaults the
calculation engine reads, and the Israeli national holiday calendar for the current and next year.
Both are idempotent, so re-applying does nothing, and the holiday years are resolved from the clock
when the migration runs.

### Seed and bootstrap admin

After migrating, seed the first administrator so there is a login that can create everything else.
The password comes from `BOOTSTRAP_ADMIN_PASSWORD` in the environment and is never stored in a
migration; set it in `.env` locally, or from a secrets manager in a real deployment.

```bash
docker compose run --rm api python -m app.seed
```

The seed re-runs the reference-data seed too (idempotent), then creates the admin from
`BOOTSTRAP_ADMIN_*`. It skips admin creation when the password is unset or the user already exists,
so it is safe to run on every deploy. The admin starts without 2FA and is sent to enrolment on first
sign-in; change the bootstrap password immediately after.

### Developer fixtures

For local development and demos only, load a coherent sample world — clients, sites, employees with
rates, and the brief's multi-site day (07:00–11:30 at one site, 12:00–17:00 at another):

```bash
docker compose run --rm api python -m app.fixtures
```

Idempotent, and it refuses to run when `ENVIRONMENT=production`. Never run it against a database that
holds real data.

## Low-cost POC target (single EC2 + CloudFront)

For a small proof-of-concept (~10 users, lowest cost, minimal maintenance) there is a separate,
self-contained deployment under [`poc/`](poc/): the whole system on one EC2 instance running
`docker compose`, fronted by CloudFront, in `il-central-1`. It reuses the root compose file via an
overlay and ships one-command upgrade/rollback/backup scripts. Start at
[`poc/RUNBOOK.md`](poc/RUNBOOK.md). This is distinct from the production stack described below.

## Cloud target

The cloud deployment is defined here and rolled out by the release pipeline. Its shape:

- Containers behind a load balancer terminating TLS, with plain HTTP redirected.
- Managed PostgreSQL with encryption at rest and automated snapshots.
- Private object storage for documents, reachable only by short-lived presigned URL.
- A scheduled runner for the daily and weekly jobs.
- Migrations as an explicit pipeline step before the new version takes traffic.
- Secrets from a secrets manager. Nothing in the repository, and no default in the code.

The production frontend image (`frontend` `prod` target) serves the built assets from nginx and does
not proxy `/api`; routing the API path is the load balancer's job in that topology.

### Infrastructure as code

[`cloud/data-and-jobs.yaml`](cloud/data-and-jobs.yaml) is the CloudFormation template for the
stateful and scheduled pieces:

- **Managed PostgreSQL** — encrypted at rest with a customer-managed KMS key, automated daily
  snapshots with point-in-time recovery (30-day retention), Multi-AZ and deletion protection in
  production, private only (no public endpoint). Master credentials are generated straight into
  Secrets Manager; the application reads an assembled `DATABASE_URL` secret and never sees the
  password in plain configuration.
- **Private document storage** — an S3 bucket encrypted with the same key, public access fully
  blocked, versioned, and TLS-only. Reached only by short-lived presigned URL (Requirement 20.3).
- **Encrypted backup storage** — a versioned bucket for the nightly logical dump, with a 30-day
  lifecycle matching the snapshot retention.
- **Scheduled job runner** — an ECS Fargate task running the backend image as
  `python -m app.jobs_runner daily|weekly`, fired by two EventBridge schedules. It runs with
  `SCHEDULER_ENABLED=false`; the dedicated runner owns the jobs, so the API replicas do not fire
  them.

The load balancer, API service and web service definitions attach to this stack via its exports
(the database URL secret, the buckets, the jobs cluster) and are deployed by the same rollout.

Validate the template without deploying:

```bash
aws cloudformation validate-template --template-body file://infra/cloud/data-and-jobs.yaml
# or, offline, with the cfn-lint linter:
cfn-lint infra/cloud/data-and-jobs.yaml
```

### Release pipeline

[`.github/workflows/release.yml`](../.github/workflows/release.yml) turns a `vX.Y.Z` tag into
deployable images and rolls them out with **migrations as a gated release step**:

1. **build** — backend and frontend `prod` images, pushed to the registry tagged with the version.
2. **migrate** — `alembic upgrade head` then the idempotent seed, run inside the released backend
   image against the target database. A failure stops the release with the old version still live.
3. **deploy** — roll the API, web and job-runner onto the new image, only after migrate passes.

### Operations

[`RUNBOOK.md`](RUNBOOK.md) is the deployment runbook: the release checklist and the step-by-step
procedures for **bootstrap admin creation**, **backup restore** (including the pre-release
rehearsal), **period unlock** and **QR rotation**.

### Scheduled jobs in the cloud

The daily and weekly jobs run on the dedicated runner, not the in-process scheduler:

```bash
python -m app.jobs_runner daily    # no-checkout reminder, over-maximum open shift, document expiry
python -m app.jobs_runner weekly   # missing-report summary for the last seven days
```

The command runs one batch to completion and exits with `0` on success or non-zero on failure, so the
platform scheduler records each run and can alert on a failure. Both delegate to the same
`app.core.scheduler` entry points the in-process scheduler uses, so the two never diverge.

## Security hardening (Requirement 20)

This section is the operational half of the hardening task; the application half lives in code and is
listed here so the two are read together.

### TLS and HTTP redirect (20.1)

All traffic is served over TLS and plain HTTP is redirected to HTTPS. TLS terminates at the ingress /
load balancer in front of both the API and the front-end container:

- The load balancer holds the certificate and listens on 443; a listener on 80 issues a `308`
  redirect to the `https://` form of the same URL.
- Behind TLS, set `HSTS_ENABLED=true` on the API so it emits `Strict-Transport-Security`
  (`backend/app/core/security_headers.py`). The header is off by default so a local HTTP dev server
  never pins a developer's browser to HTTPS.
- Where the front-end container terminates TLS itself instead of the ingress, uncomment the redirect
  server and the HSTS header in `frontend/nginx/default.conf`.

### Response headers and CORS (20.9)

The API stamps the standard hardening headers on every response
(`SecurityHeadersMiddleware`): `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, a
locked-down `Content-Security-Policy`, the cross-origin isolation headers, and a `Permissions-Policy`
that denies geolocation outright (20.10). Cross-origin access is restricted to the known front-end
origins via `CORS_ALLOWED_ORIGINS` — an explicit allowlist, never a wildcard. The SPA's nginx sets
its own headers for the static assets.

### Rate limiting (20.4)

A Redis-backed fixed-window limiter guards the two endpoint groups an outsider can reach — sign-in /
refresh and the scan endpoints — on top of the per-account login lockout and the per-employee scan
duplicate window. Limits are set with `AUTH_RATE_LIMIT_*` and `SCAN_RATE_LIMIT_*`; the limiter fails
open if Redis is unreachable so a cache outage cannot take sign-in down.

### Secrets (20.6)

Every secret is supplied by the environment or a secrets manager and validated at startup with no
default (`app.core.config`). Nothing sensitive is committed: `.env` is git-ignored and only
`.env.example` — placeholders only — is tracked. The backend suite includes a test that scans the
tracked tree for anything resembling a real credential.

### Encryption at rest (20.2)

Sensitive employee columns (passport number, phone, address, date of birth, TOTP secret) are
encrypted with AES-GCM through the column types in `app.db.types`; the searchable ones carry a keyed
HMAC companion column. The managed database is provisioned with storage-layer encryption enabled, and
object storage holding documents is encrypted at rest and reachable only by short-lived presigned URL
(20.3).

### Database backups and restore (20.7)

Automated backups run on a schedule and the restore procedure is rehearsed before each release.

- The managed PostgreSQL instance takes an automated daily snapshot with point-in-time recovery
  enabled, retained for 30 days, inheriting the instance's storage-layer encryption
  (`cloud/data-and-jobs.yaml`).
- A logical dump is taken nightly as a portable second copy and written to the encrypted, versioned
  backup bucket with a 30-day lifecycle.

The full backup, restore-rehearsal and point-in-time-recovery procedures — with the sign-off criteria
that gate a release — are in [`RUNBOOK.md`](RUNBOOK.md) § 2. The rehearsal is signed off in the
release checklist, and PITR is exercised at least quarterly.

### Dependency vulnerability scanning

CI runs a dependency vulnerability scan on every push for both halves of the monorepo (`pip-audit`
for the backend, `npm audit` for the front end); see `.github/workflows/ci.yml`. A finding above the
configured severity fails the build.

### No location data (20.10)

The system does not collect, store or transmit location. There is no coordinate column in the schema,
no location field in any request model (`extra="forbid"` rejects one that is sent), the front end
never calls `navigator.geolocation`, and both the API and the SPA send a `Permissions-Policy` that
denies the geolocation capability. The backend suite asserts the absence across the tracked tree.
