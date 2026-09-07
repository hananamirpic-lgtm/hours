#!/usr/bin/env bash
# ============================================================================
# backup.sh - back up the POC database and object storage to S3.
#
# Runs on the EC2 host. Takes a logical pg_dump of the running Postgres
# container and a copy of the MinIO document data, and uploads both to the
# S3 backup bucket under a timestamped prefix. Called automatically by
# deploy.sh BEFORE any upgrade, and also from cron nightly.
#
# Usage:
#   ./backup.sh [label]
#     label - optional tag folded into the backup name (e.g. "pre-vX.Y.Z").
#
# Requires: docker, aws CLI (instance role with s3:PutObject on the bucket).
# Config comes from environment or the values below.
# ============================================================================
set -euo pipefail

# --- config -----------------------------------------------------------------
COMPOSE_DIR="${COMPOSE_DIR:-/opt/hours}"                     # repo checkout on the box
BACKUP_BUCKET="${BACKUP_BUCKET:?set BACKUP_BUCKET, e.g. s3://hours-poc-backups}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${POSTGRES_USER:-hours}"
PG_DB="${POSTGRES_DB:-hours}"
MINIO_DATA="${MINIO_DATA:-/data/minio}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"                             # local copies pruned after N days

LABEL="${1:-nightly}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="${TS}_${LABEL}"
LOCAL_DIR="/data/backups/${NAME}"

echo "[backup] starting ${NAME}"
mkdir -p "${LOCAL_DIR}"

cd "${COMPOSE_DIR}"

# --- database dump (portable, compressed) -----------------------------------
echo "[backup] pg_dump ${PG_DB}"
docker compose exec -T "${PG_SERVICE}" pg_dump -U "${PG_USER}" -Fc "${PG_DB}" \
  > "${LOCAL_DIR}/db.dump"

# --- object storage (documents) --------------------------------------------
# Tar the MinIO data tree. For a POC the document volume is small; a tarball is
# the simplest portable copy. (For larger data, switch to `aws s3 sync`.)
if [ -d "${MINIO_DATA}" ]; then
  echo "[backup] archiving MinIO data"
  tar -czf "${LOCAL_DIR}/minio-data.tar.gz" -C "${MINIO_DATA}" .
else
  echo "[backup] WARNING: ${MINIO_DATA} not found, skipping object storage"
fi

# --- record the currently-running image tags so a restore knows the version --
docker compose config 2>/dev/null | grep -E 'image:.*hours-(api|web)' \
  > "${LOCAL_DIR}/images.txt" || true

# --- upload to S3 -----------------------------------------------------------
echo "[backup] uploading to ${BACKUP_BUCKET}/${NAME}/"
aws s3 cp "${LOCAL_DIR}" "${BACKUP_BUCKET}/${NAME}/" --recursive --only-show-errors

# --- prune old LOCAL copies (S3 lifecycle handles the remote side) ----------
find /data/backups -maxdepth 1 -type d -mtime "+${RETAIN_DAYS}" -exec rm -rf {} \; 2>/dev/null || true

echo "[backup] done: ${BACKUP_BUCKET}/${NAME}/"
echo "${NAME}"   # last line = the backup name, so deploy.sh can capture it