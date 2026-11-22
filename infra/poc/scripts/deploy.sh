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
#   6. health-check; if it fails, AUTOMATICALLY roll back;
#   7. run the post-deploy checks (sign-in, a read, the ticket API for the
#      current feature-flag state). A failed check is named and the script
#      exits 1, but it does NOT roll back: the operator decides, because
#      --restore-db discards data written after the backup.
#
# Before step 1, a flag-on deploy fails a pre-flight check while
# SERVICE_ACCESSIBILITY_CONTACT is empty in the env file (nothing is changed).
#
# Smoke credentials for the sign-in check come from ${STATE_DIR}/smoke.env
# (/data/deploy/smoke.env, mode 600, outside the repository), one per line:
#   SMOKE_USERNAME=...
#   SMOKE_PASSWORD=...
# They are never printed and never put on a command line.
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
STATE_DIR="${STATE_DIR:-/data/deploy}"
LAST_GOOD_FILE="${STATE_DIR}/LAST_GOOD_VERSION"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Post-deploy checks: Caddy on this host, and the smoke user's credentials.
POST_DEPLOY_BASE_URL="${POST_DEPLOY_BASE_URL:-http://localhost}"
SMOKE_ENV_FILE="${SMOKE_ENV_FILE:-${STATE_DIR}/smoke.env}"
CHECK_MAX_TIME=30          # seconds; a check with no response by then has failed (R25.7)
CHECK_BODY_BYTES=2097152   # 2 MiB body for ticket_body_path

API_IMAGE="${ECR_REGISTRY}/hours-api:${VERSION}"
WEB_IMAGE="${ECR_REGISTRY}/hours-web:${VERSION}"

# --- helpers for the pre-flight and post-deploy checks ----------------------

# trim_ws VALUE -> VALUE without leading/trailing whitespace.
trim_ws() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "${v}"
}

# env_value FILE KEY STRIP_COMMENT -> value of the last KEY=... line, or nothing.
# The file is read as data, never sourced. Surrounding whitespace and one pair of
# matching quotes are removed (as docker compose does); with STRIP_COMMENT=1 an
# unquoted value also loses a trailing " # comment".
env_value() {
  local file="$1" key="$2" strip_comment="$3" line value
  [ -f "${file}" ] || return 0
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "${file}" | tail -n1 || true)"
  [ -n "${line}" ] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="$(trim_ws "${value}")"
  case "${value}" in
    \"*\") value="${value:1:${#value}-2}" ;;
    \'*\') value="${value:1:${#value}-2}" ;;
    *)
      if [ "${strip_comment}" = "1" ]; then
        case "${value}" in
          \#*) value="" ;;
          *) value="${value%%[[:space:]]#*}" ;;
        esac
      fi
      ;;
  esac
  printf '%s' "$(trim_ws "${value}")"
}

# service_tickets_flag_on -> success only when SERVICE_TICKETS_ENABLED in the env
# file is, after trimming and lowercasing, the word "true". This is the rule of
# backend/app/service_tickets/config.py (flag_enabled): "1", "yes", empty or unset are off.
service_tickets_flag_on() {
  local raw
  raw="$(env_value "${ENV_FILE}" SERVICE_TICKETS_ENABLED 1)"
  raw="${raw,,}"
  [ "${raw}" = "true" ]
}

# json_escape VALUE -> VALUE escaped for use inside a JSON string.
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\n'/\\n}"
  printf '%s' "${s}"
}

# preflight_service_tickets: a flag-on deploy needs an accessibility contact
# (shown on the customer portal). Runs before anything is changed.
preflight_service_tickets() {
  local contact
  if service_tickets_flag_on; then
    contact="$(env_value "${ENV_FILE}" SERVICE_ACCESSIBILITY_CONTACT 1)"
    if [ -z "${contact}" ]; then
      echo "!! FAIL accessibility_contact_preflight: SERVICE_TICKETS_ENABLED is on but SERVICE_ACCESSIBILITY_CONTACT is empty in ${ENV_FILE}."
      echo "!! Set it (or set SERVICE_TICKETS_ENABLED=false) and run the deploy again. Nothing was changed."
      exit 1
    fi
  fi
}

# http_call METHOD PATH [curl args...] -> sets HTTP_CODE (000 on timeout or no
# response) and HTTP_BODY. Never uses -f, so a 4xx/5xx status is returned, not an error.
http_call() {
  local method="$1" path="$2"
  shift 2
  HTTP_CODE="$(curl --silent --max-time "${CHECK_MAX_TIME}" \
    --output "${CHECK_TMP}/response" --write-out '%{http_code}' \
    --request "${method}" "$@" "${POST_DEPLOY_BASE_URL}${path}" 2>/dev/null)" || HTTP_CODE="000"
  [[ "${HTTP_CODE}" =~ ^[0-9]{3}$ ]] || HTTP_CODE="000"
  HTTP_BODY=""
  if [ -f "${CHECK_TMP}/response" ]; then
    HTTP_BODY="$(cat "${CHECK_TMP}/response")"
    rm -f "${CHECK_TMP}/response"
  fi
}

# check_pass NAME / check_fail NAME REASON: print one result line; failures are collected.
CHECK_FAILURES=()
check_pass() { echo "   PASS ${1}"; }
check_fail() {
  echo "   FAIL ${1}: ${2}"
  CHECK_FAILURES+=("${1}")
}
check_skip() { echo "   SKIP ${1}: ${2}"; }

# post_deploy_checks (R25.7, R25.11): every check runs and prints its result;
# any failure is named and the script exits 1. Never prints the smoke
# credentials or the access token: reasons carry status codes only, and the
# credentials and token travel in files, not on a command line.
post_deploy_checks() {
  local flag_on=0 flag_label=off smoke_user smoke_pass token=""
  CHECK_TMP="$(mktemp -d)"
  trap 'rm -rf "${CHECK_TMP}"' EXIT
  if service_tickets_flag_on; then
    flag_on=1
    flag_label=on
  fi

  echo "==> Post-deploy checks against ${POST_DEPLOY_BASE_URL} (ticket flag: ${flag_label})"
  if [ -f "${SMOKE_ENV_FILE}" ] && [ "$(stat -c '%a' "${SMOKE_ENV_FILE}" 2>/dev/null || echo 600)" != "600" ]; then
    echo "   WARN ${SMOKE_ENV_FILE} should have mode 600"
  fi

  # hours_signin: the smoke user signs in and gets an access_token.
  smoke_user="$(env_value "${SMOKE_ENV_FILE}" SMOKE_USERNAME 0)"
  smoke_pass="$(env_value "${SMOKE_ENV_FILE}" SMOKE_PASSWORD 0)"
  if [ -z "${smoke_user}" ] || [ -z "${smoke_pass}" ]; then
    check_fail hours_signin "SMOKE_USERNAME or SMOKE_PASSWORD missing in ${SMOKE_ENV_FILE}"
  else
    printf '{"username":"%s","password":"%s"}' \
      "$(json_escape "${smoke_user}")" "$(json_escape "${smoke_pass}")" > "${CHECK_TMP}/login.json"
    http_call POST /api/auth/login --header 'Content-Type: application/json' \
      --data-binary "@${CHECK_TMP}/login.json"
    rm -f "${CHECK_TMP}/login.json"
    if [ "${HTTP_CODE}" = "000" ]; then
      check_fail hours_signin "no response within ${CHECK_MAX_TIME}s"
    elif [ "${HTTP_CODE}" != "200" ]; then
      check_fail hours_signin "status ${HTTP_CODE}, expected 200"
    else
      token="$(printf '%s' "${HTTP_BODY}" \
        | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"][^"]*\)".*/\1/p' | head -n1)"
      if [ -z "${token}" ]; then
        check_fail hours_signin "status 200 but no access_token in the response"
      else
        check_pass hours_signin
      fi
    fi
  fi
  HTTP_BODY=""

  # hours_read: an existing Hours read endpoint returns data for that token.
  if [ -z "${token}" ]; then
    check_fail hours_read "no access token (hours_signin failed)"
  else
    printf 'Authorization: Bearer %s\n' "${token}" > "${CHECK_TMP}/auth_header"
    http_call GET /api/auth/me --header "@${CHECK_TMP}/auth_header"
    rm -f "${CHECK_TMP}/auth_header"
    if [ "${HTTP_CODE}" = "000" ]; then
      check_fail hours_read "no response within ${CHECK_MAX_TIME}s"
    elif [ "${HTTP_CODE}" != "200" ]; then
      check_fail hours_read "status ${HTTP_CODE}, expected 200"
    elif ! printf '%s' "${HTTP_BODY}" | grep -Eq '"username"[[:space:]]*:[[:space:]]*"[^"]'; then
      check_fail hours_read "status 200 but no username in the response"
    else
      check_pass hours_read
    fi
  fi
  token=""
  HTTP_BODY=""

  if [ "${flag_on}" -eq 0 ]; then
    # ticket_api_off: with the flag off, every ticket path is 404.
    http_call GET /api/service/auth/me
    if [ "${HTTP_CODE}" = "404" ]; then
      check_pass ticket_api_off
    elif [ "${HTTP_CODE}" = "000" ]; then
      check_fail ticket_api_off "no response within ${CHECK_MAX_TIME}s"
    else
      check_fail ticket_api_off "status ${HTTP_CODE}, expected 404"
    fi
    check_skip ticket_api_on "flag is off"
    check_skip ticket_body_path "flag is off"
    check_skip accessibility_contact "flag is off"
  else
    check_skip ticket_api_off "flag is on"

    # ticket_api_on: the path answers, and is neither 404 nor a 5xx (401 is expected).
    http_call GET /api/service/auth/me
    if [ "${HTTP_CODE}" = "000" ]; then
      check_fail ticket_api_on "no response within ${CHECK_MAX_TIME}s"
    elif [ "${HTTP_CODE}" = "404" ] || [[ "${HTTP_CODE}" == 5* ]]; then
      check_fail ticket_api_on "status ${HTTP_CODE}, expected neither 404 nor 5xx"
    else
      check_pass ticket_api_on
    fi

    # ticket_body_path: a 2 MiB body gets through Caddy and the API to the token check (401).
    head -c "${CHECK_BODY_BYTES}" /dev/zero | tr '\0' 'a' > "${CHECK_TMP}/body.bin"
    http_call POST /api/service/tickets --header 'Content-Type: application/octet-stream' \
      --header 'Expect:' --data-binary "@${CHECK_TMP}/body.bin"
    rm -f "${CHECK_TMP}/body.bin"
    if [ "${HTTP_CODE}" = "401" ]; then
      check_pass ticket_body_path
    elif [ "${HTTP_CODE}" = "000" ]; then
      check_fail ticket_body_path "no response within ${CHECK_MAX_TIME}s"
    else
      check_fail ticket_body_path "status ${HTTP_CODE}, expected 401"
    fi

    # accessibility_contact: /api/features carries a non-empty contact.
    http_call GET /api/features
    if [ "${HTTP_CODE}" = "000" ]; then
      check_fail accessibility_contact "no response within ${CHECK_MAX_TIME}s"
    elif [ "${HTTP_CODE}" != "200" ]; then
      check_fail accessibility_contact "status ${HTTP_CODE}, expected 200"
    elif ! printf '%s' "${HTTP_BODY}" \
        | grep -Eq '"accessibility_contact"[[:space:]]*:[[:space:]]*"[^"[:space:]]'; then
      check_fail accessibility_contact "accessibility_contact is missing or empty"
    else
      check_pass accessibility_contact
    fi
  fi

  if [ "${#CHECK_FAILURES[@]}" -gt 0 ]; then
    echo "!! Post-deploy checks FAILED. Deploy of ${VERSION} is reported as failed."
    for failed in "${CHECK_FAILURES[@]}"; do
      echo "!!   failed check: ${failed}"
    done
    echo "!! The health check passed, so there was no automatic rollback and ${VERSION} is running."
    echo "!! Decide: set SERVICE_TICKETS_ENABLED=false, ${SCRIPT_DIR}/rollback.sh, or rollback.sh --restore-db."
    echo "!! Pre-upgrade backup: ${BACKUP_NAME}"
    exit 1
  fi
  echo "==> Post-deploy checks passed."
}

cd "${COMPOSE_DIR}"
mkdir -p "${STATE_DIR}"

echo "==> Deploying ${VERSION}"

# --- 0. pre-flight: nothing has been changed yet ----------------------------
preflight_service_tickets

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

# --- 7. post-deploy checks (exit 1 on any failure; no automatic rollback) ----
post_deploy_checks

echo "==> Deploy of ${VERSION} succeeded and is healthy."
echo "==> Previous version kept as rollback target: ${CURRENT_VERSION}"