# Deployment runbook

Operational procedures for the Hours system in a cloud deployment. This runbook covers the four
procedures Task 39 calls out — **bootstrap admin creation**, **backup restore**, **period unlock**
and **QR rotation** — plus the release flow they sit inside.

It assumes the cloud target described in [`README.md`](README.md) and provisioned by
[`cloud/data-and-jobs.yaml`](cloud/data-and-jobs.yaml):

- API and web containers behind a load balancer terminating TLS.
- Managed PostgreSQL with encryption at rest and automated daily snapshots (30-day retention, PITR).
- A private, encrypted documents bucket reachable only by short-lived presigned URL.
- An encrypted backup bucket for the nightly logical dump.
- A dedicated scheduled runner firing the daily and weekly jobs (`python -m app.jobs_runner`).

Every secret comes from the secrets manager. Nothing in this runbook asks you to put a credential in
a file, a template or an image.

---

## Release flow (context for the procedures below)

A release is a git tag `vMAJOR.MINOR.PATCH`. Pushing it runs
[`.github/workflows/release.yml`](../.github/workflows/release.yml), which:

1. **builds** the backend (`prod` target) and frontend (`prod` target) images and pushes them to the
   registry, tagged with the version and the commit sha;
2. **migrates** — runs `alembic upgrade head` and then the idempotent seed **inside the released
   backend image**, against the target database, as a gated step. A migration failure stops the
   release with the old version still serving;
3. **deploys** — rolls the API, the web tier and the scheduled job runner onto the new image, only
   after the migrate gate passes.

Migrations are always a release step and never an application startup side effect, so two API
replicas starting together can never race to migrate the same database.

**Pre-release checklist** (the operational half of Requirement 20.7 is item 3):

- [ ] CI is green on the commit being tagged.
- [ ] The new migrations apply and roll back cleanly in CI (`alembic upgrade head` / `downgrade base`).
- [ ] The **backup restore rehearsal below has been run** since the last release and signed off.
- [ ] Any new secret the release needs exists in the secrets manager for the target environment.

---

## 1. Bootstrap admin creation

A fresh deployment has no users. The first administrator is created by the **seed step**, which reads
`BOOTSTRAP_ADMIN_*` from the environment — never from a migration, because the password is a secret
and a migration is a committed, frozen artefact.

### First release of an environment

1. Put the first admin's credentials in the secrets manager for the environment:
   - `BOOTSTRAP_ADMIN_USERNAME` (optional, defaults to `admin`)
   - `BOOTSTRAP_ADMIN_PASSWORD` (**required to create the admin**)
   - `BOOTSTRAP_ADMIN_LANGUAGE` (optional, `he` or `en`, defaults to `he`)
2. Run the release. The `migrate` job applies migrations and then runs the seed, which creates the
   admin exactly once.

To create the admin by hand instead (for example on a database migrated out-of-band), run the seed
against the released image:

```bash
docker run --rm \
  -e ENVIRONMENT=production \
  -e DATABASE_URL="<from secrets manager>" \
  -e REDIS_URL="redis://unused:6379/0" \
  -e JWT_SECRET_KEY="<any 32+ char value; unused by seed>" \
  -e ENCRYPTION_KEY="<any 32+ char value; unused by seed>" \
  -e S3_ENDPOINT_URL="http://unused:9000" \
  -e S3_ACCESS_KEY_ID="unused" -e S3_SECRET_ACCESS_KEY="unused" \
  -e S3_BUCKET_DOCUMENTS="unused" \
  -e BOOTSTRAP_ADMIN_USERNAME="admin" \
  -e BOOTSTRAP_ADMIN_PASSWORD="<the first admin password>" \
  <registry>/hours/backend:<version> python -m app.seed
```

### What the seed does, and its idempotency

- Creates the admin **only if** `BOOTSTRAP_ADMIN_PASSWORD` is set **and** the username does not
  already exist. On every subsequent run it is a no-op — no second account, no password overwrite —
  so the seed is safe to run on every deploy.
- The admin is created **without 2FA enrolled**. The admin role is *required* to enrol, so the first
  sign-in is sent straight to 2FA enrolment. This is the correct place to bind the second factor.

### After first sign-in

1. Sign in as the bootstrap admin and complete 2FA enrolment.
2. **Change the bootstrap password immediately**, then remove `BOOTSTRAP_ADMIN_PASSWORD` from the
   environment's active secret (or blank it) so it is not lying around. The seed skips admin creation
   when it is absent, so later releases are unaffected.
3. Create the real administrator, accounting and manager users through the user-management screens.

---

## 2. Backup restore

Two independent copies protect the ledger, both encrypted at rest:

- **Managed snapshots** — the managed instance takes an automated daily snapshot with point-in-time
  recovery, retained 30 days (`BackupRetentionPeriod` in the template).
- **Logical dump** — a nightly `pg_dump` written to the encrypted, versioned backup bucket with a
  30-day lifecycle, as a portable second copy.

The nightly dump command (runs on the scheduled runner; `DATABASE_URL` points at the managed
instance):

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  | aws s3 cp - "s3://$BACKUP_BUCKET/hours/$(date -u +%Y-%m-%dT%H%M%SZ).dump"
```

### Restore rehearsal (run before every release, sign off in the checklist)

Always restore to a **fresh, non-production database** and verify before considering cutover. Never
restore over a live database as the first step.

```bash
# 1. Provision an empty target and point RESTORE_URL at it.
#    (A new managed instance, or a throwaway database on an existing server.)

# 2. Restore the most recent logical dump.
aws s3 cp "s3://$BACKUP_BUCKET/hours/<timestamp>.dump" ./restore.dump
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname "$RESTORE_URL" ./restore.dump

# 3. Bring the schema to head — the dump may predate the latest migration.
DATABASE_URL="$RESTORE_URL" alembic upgrade head

# 4. Smoke-test: row counts and a sign-in against the restored database.
psql "$RESTORE_URL" -c "SELECT count(*) FROM employees;"
psql "$RESTORE_URL" -c "SELECT count(*) FROM time_entries;"
```

**Sign-off criteria:** the restore completes, `alembic upgrade head` is a no-op or applies cleanly,
the row counts are plausible, and a sign-in against the restored database succeeds.

### Point-in-time recovery (from a managed snapshot)

For a recovery to a specific moment (for example, just before a bad bulk change), restore the managed
snapshot to a chosen timestamp into a **new** instance, verify with the smoke test above, then cut
over by repointing the application's `DATABASE_URL` secret and rolling the API. Exercise PITR at least
quarterly so the procedure is known to work when it is needed.

### Restoring a document

Documents live in the versioned, encrypted documents bucket. To recover a document deleted or
overwritten in error, restore the prior object version rather than reaching for a database backup —
the database only holds the object key, the bytes are in the bucket.

---

## 3. Period unlock

Locking a calendar month freezes its approved entries so payroll and billing compute from a stable
ledger. Reopening a locked month is an **administrator** action and always records a reason in the
audit log (Requirement 15.6).

### When to unlock

Only when a locked month must be corrected — a discovered error in approved hours, a late manual
entry that changes a total already used downstream. Prefer an **admin override on a single entry**
over unlocking the whole month when the correction is to one entry, because unlocking reopens the
entire period.

### How

Through the admin period screen, or directly:

```
POST /api/periods/{year}/{month}/unlock
Authorization: Bearer <admin access token>
Content-Type: application/json

{ "reason": "Correcting approved hours for employee 12345 after payroll dispute" }
```

- Admin only. A non-admin, or a missing/blank reason, is rejected.
- Unlocking a month that is **not** locked returns `409`.
- Unlock reopens the month; it does **not** walk locked entries back down the status ladder — they
  stay `Locked` until edited. Make the correction, re-approve and re-lock.

### After unlocking

1. Make and audit the correction (every time-entry change records actor, timestamp and reason).
2. **Recalculate any downstream figures** for the month: `POST /api/payroll/calculate` and
   `POST /api/billing/calculate` are idempotent per employee/month and replace the existing draft.
3. Re-lock the month: `POST /api/periods/{year}/{month}/lock`. If it warns about unapproved entries,
   act on the list before forcing.
4. Confirm the audit trail shows the unlock reason, the correction and the re-lock.

---

## 4. QR rotation

Each site's QR encodes a signed, versioned token. Rotating bumps the site's QR version and mints a
fresh token; **every previously printed code for that site is rejected on the next scan**
(Requirement 8.6, 8.7). It is an **administrator** action, because it invalidates codes in the field.

### When to rotate

- A printed code is suspected compromised (photographed and shared, leaked online).
- Routine rotation on a policy interval.
- A site changes hands or its posted code can no longer be trusted.

### How

Through the site screen's QR section, or directly:

```
POST /api/sites/{site_id}/qr/regenerate
Authorization: Bearer <admin access token>
```

Then download and reprint the new code(s):

```
GET /api/sites/{site_id}/qr?format=pdf              # unified site
GET /api/sites/{site_id}/qr?format=pdf&action=check_in
GET /api/sites/{site_id}/qr?format=pdf&action=check_out   # separate-mode site: two codes
```

The downloaded file encodes the site's **current** version, so a file pulled after rotation replaces
the printed one. A unified site has one code; a separate check-in/check-out site has two, selected
with `action`.

### Rollout, to avoid stranding employees mid-shift

Rotation takes effect immediately: the old code stops working the moment the version bumps. To avoid
an employee arriving at a site with a dead code:

1. Rotate the token.
2. **Print and physically post the new code at the site before announcing the rotation** — or rotate
   during a window when no one is scanning that site.
3. Confirm a live scan of the new code checks in successfully.
4. Remove and destroy the old printed codes.

An open shift already checked in on the old code is unaffected — the token is validated at scan time,
so the employee can still check out. Only new scans need the new code.

---

## Quick reference

| Procedure | Entry point | Who |
|---|---|---|
| Create first admin | `python -m app.seed` (release `migrate` job) reading `BOOTSTRAP_ADMIN_*` | Deploy |
| Restore rehearsal | `pg_restore` + `alembic upgrade head` + smoke test | Ops, pre-release |
| Point-in-time recovery | Managed snapshot restore to new instance, then repoint `DATABASE_URL` | Ops |
| Unlock a month | `POST /api/periods/{year}/{month}/unlock` (reason required) | Admin |
| Rotate a site's QR | `POST /api/sites/{id}/qr/regenerate`, then reprint via `GET /api/sites/{id}/qr` | Admin |
| Daily jobs | `python -m app.jobs_runner daily` (EventBridge daily schedule) | Scheduler |
| Weekly summary | `python -m app.jobs_runner weekly` (EventBridge weekly schedule) | Scheduler |
