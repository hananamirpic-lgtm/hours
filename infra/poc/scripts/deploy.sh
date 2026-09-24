#!/usr/bin/env bash
# ============================================================================
# deploy.sh - upgrade the POC to a new version, safely.
#
# One command to roll out a new version on the EC2 host:
#   1. back up the DB + object storage FIRST (so rollback is always possible);
#   2. record the currently-running version as LAST_GOOD;
#   3. log in to ECR and pull the new api/web images by tag;
#   4. switch only the api and web containers (postgres/redis/minio keep
#      running on /data -> the database is preserved and transparent to users);
#   5. run alembic migrations inside the new api image;
#   6. health-check; if it fails, AUTOMATICALLY roll back.
#
# Usage:
#   ./deploy.sh vX.Y.Z
#
# Requires: docker, aws CLI (instance role with ECR pull + S3 backup).
# ============================================================================
set -euo pipefail

VERSION="${1:?usage: deploy.sh vX.Y.Z}"

# --- config -----------------------------------------------------------------
COMPOSE_DIR="${COMPOSE_DIR:-/opt/hours}"
ECR_REGISTRY="${ECR_REGISTRY:?set ECR_REGISTRY, e.g. 123456789012.dkr.ecr.il-central-1.amazonaws.com}"
AWS_REGION="${AWS_REGION:-il-central-1}"
ENV_FILE="${ENV_FILE:-infra/poc/.env.prod}"
COMPOSE_FILES=(-f docker-compose.yml -f infra/poc/docker-compose.prod.yml)
HEALTH_URL="${HEALTH_URL:-http://localhost/api/health}"
STATE_DIR="/data/deploy"
LAST_GOOD_FILE="${STATE_DIR}/LAST_GOOD_VERSION"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

API_IMAGE="${ECR_REGISTRY}/hours-api:${VERSION}"
WEB_IMAGE="${ECR_REGISTRY}/hours-web:${VERSION}"

cd "${COMPOSE_DIR}"
mkdir -p "${STATE_DIR}"

echo "==> Deploying ${VERSION}"

# --- 1. capture the current version for rollback ----------------------------
CURRENT_VERSION="none"
if [ -f "${STATE_DIR}/CURRENT_VERSION" ]; then
  CURRENT_VERSION="$(cat "${STATE_DIR}/CURRENT_VERSION")"
fi
echo "==> Current running version: ${CURRENT_VERSION}"

# --- 2. back up BEFORE touching anything ------------------------------------
echo "==> Backing up before upgrade"
BACKUP_NAME="$("${SCRIPT_DIR}/backup.sh" "pre-${VERSION}" | tail -n1)"
echo "${BACKUP_NAME}" > "${STATE_DIR}/LAST_PRE_UPGRADE_BACKUP"
echo "==> Pre-upgrade backup: ${BACKUP_NAME}"

# record the version we are leaving, so rollback re-selects it
if [ "${CURRENT_VERSION}" != "none" ]; then
  echo "${CURRENT_VERSION}" > "${LAST_GOOD_FILE}"
fi

# --- 3. pull the new images from ECR ----------------------------------------
echo "==> Logging in to ECR and pulling ${VERSION}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ECR_REGISTRY}"
export API_IMAGE WEB_IMAGE
docker pull "${API_IMAGE}"
docker pull "${WEB_IMAGE}"

# --- 4. switch only api and web (DB/redis/minio untouched) ------------------
echo "==> Starting api and web on ${VERSION}"
docker compose "${COMPOSE_FILES[@]}" --env-file "${ENV_FILE}" up -d api web caddy

# --- 5. run migrations inside the new api image -----------------------------
echo "==> Running database migrations"
docker compose "${COMPOSE_FILES[@]}" --env-file "${ENV_FILE}" \
  run --rm api alembic upgrade head

# --- 6. health-check, auto-rollback on failure ------------------------------
echo "==> Health check: ${HEALTH_URL}"
OK=0
for i in $(seq 1 20); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then OK=1; break; fi
  echo "   ...waiting for health ($i/20)"
  sleep 3
done

if [ "${OK}" -ne 1 ]; then
  echo "!! Health check FAILED. Rolling back."
  "${SCRIPT_DIR}/rollback.sh" || {
    echo "!! Rollback also failed. Manual intervention needed. Pre-upgrade backup: ${BACKUP_NAME}"
    exit 2
  }
  exit 1
fi

# --- success: record the new version ----------------------------------------
echo "${VERSION}" > "${STATE_DIR}/CURRENT_VERSION"
echo "==> Deploy of ${VERSION} succeeded and is healthy."
echo "==> Previous version kept as rollback target: ${CURRENT_VERSION}"