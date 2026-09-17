#!/usr/bin/env bash
# ============================================================================
# rollback.sh - return the POC to the previous known-good version.
#
# Re-selects the previous image tag (recorded by deploy.sh in LAST_GOOD_VERSION)
# and restarts api/web on it. The database keeps running throughout, so in the
# common case (a forward-compatible schema) no data restore is needed and this
# is a fast switch-back.
#
# If the failed upgrade ran a migration that is NOT compatible with the old
# code, pass --restore-db to also restore the pre-upgrade database dump that
# deploy.sh took. This overwrites the current database with the pre-upgrade
# state -- only use it when a schema change forces it.
#
# Usage:
#   ./rollback.sh                 # switch code back to LAST_GOOD_VERSION
#   ./rollback.sh --restore-db    # also restore the pre-upgrade DB dump
# ============================================================================
set -euo pipefail

RESTORE_DB=0
[ "${1:-}" = "--restore-db" ] && RESTORE_DB=1

# --- config -----------------------------------------------------------------
COMPOSE_DIR="${COMPOSE_DIR:-/opt/hours}"
ECR_REGISTRY="${ECR_REGISTRY:?set ECR_REGISTRY}"
AWS_REGION="${AWS_REGION:-il-central-1}"
ENV_FILE="${ENV_FILE:-infra/poc/.env.prod}"
COMPOSE_FILES=(-f docker-compose.yml -f infra/poc/docker-compose.prod.yml)
BACKUP_BUCKET="${BACKUP_BUCKET:?set BACKUP_BUCKET}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${POSTGRES_USER:-hours}"
PG_DB="${POSTGRES_DB:-hours}"
STATE_DIR="/data/deploy"
LAST_GOOD_FILE="${STATE_DIR}/LAST_GOOD_VERSION"

cd "${COMPOSE_DIR}"

if [ ! -f "${LAST_GOOD_FILE}" ]; then
  echo "!! No LAST_GOOD_VERSION recorded; cannot roll back automatically."
  exit 2
fi
TARGET="$(cat "${LAST_GOOD_FILE}")"
echo "==> Rolling back to ${TARGET}"

export API_IMAGE="${ECR_REGISTRY}/hours-api:${TARGET}"
export WEB_IMAGE="${ECR_REGISTRY}/hours-web:${TARGET}"

aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
docker pull "${API_IMAGE}"
docker pull "${WEB_IMAGE}"

# --- optional: restore the pre-upgrade database dump ------------------------
if [ "${RESTORE_DB}" -eq 1 ]; then
  if [ ! -f "${STATE_DIR}/LAST_PRE_UPGRADE_BACKUP" ]; then
    echo "!! No pre-upgrade backup recorded; cannot restore DB."
    exit 2
  fi
  BK="$(cat "${STATE_DIR}/LAST_PRE_UPGRADE_BACKUP")"
  echo "==> Restoring database from ${BACKUP_BUCKET}/${BK}/db.dump"
  TMP="/data/restore/${BK}"
  mkdir -p "${TMP}"
  aws s3 cp "${BACKUP_BUCKET}/${BK}/db.dump" "${TMP}/db.dump" --only-show-errors
  # Restore into the running Postgres container. --clean drops objects first.
  docker compose "${COMPOSE_FILES[@]}" --env-file "${ENV_FILE}" \
    exec -T "${PG_SERVICE}" pg_restore -U "${PG_USER}" -d "${PG_DB}" --clean --if-exists \
    < "${TMP}/db.dump"
  echo "==> Database restored to pre-upgrade state."
fi

# --- switch code back -------------------------------------------------------
echo "==> Starting api and web on ${TARGET}"
docker compose "${COMPOSE_FILES[@]}" --env-file "${ENV_FILE}" up -d api web caddy

# --- verify -----------------------------------------------------------------
HEALTH_URL="${HEALTH_URL:-http://localhost/api/health}"
for i in $(seq 1 20); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "${TARGET}" > "${STATE_DIR}/CURRENT_VERSION"
    echo "==> Rollback to ${TARGET} succeeded and is healthy."
    exit 0
  fi
  sleep 3
done

echo "!! Rollback started but health check did not pass. Investigate on the box."
exit 1