#!/usr/bin/env bash
# ============================================================================
# test_post_deploy_checks.sh - tests for the pre-flight and post-deploy checks
# in infra/poc/scripts/deploy.sh (requirements 25.1, 25.2, 25.7, 25.10, 25.11).
#
# Runs a COPY of deploy.sh in a temporary directory with stub curl, docker, aws,
# sleep, backup.sh and rollback.sh on PATH. Nothing real is contacted and no
# real host is touched. The stub curl answers from per-route files, logs every
# call (arguments only) to an events file, and mimics curl's --max-time by
# sleeping a small fraction of it when a route is set to HANG.
#
# Usage:  bash infra/poc/scripts/tests/test_post_deploy_checks.sh
# Needs:  bash 4+, and coreutils (head, tail, tr, wc, awk, grep, sed, mktemp).
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SRC="${HERE}/../deploy.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

REAL_SLEEP="$(command -v sleep)"
SMOKE_USER='smoke-user'
SMOKE_PASS='p"w\d#1'
TOKEN='tok.en-SECRET123'

PASSED=0
FAILED=0
T=""
RC=0

# --- assertions -------------------------------------------------------------
ok() { PASSED=$((PASSED + 1)); }
bad() {
  FAILED=$((FAILED + 1))
  echo "FAIL: $1"
  if [ -n "${T}" ] && [ -f "${T}/out" ]; then
    sed 's/^/    | /' "${T}/out" | tail -n 25
  fi
}
assert_eq() { # label expected actual
  if [ "$2" = "$3" ]; then ok; else bad "$1 (expected '$2', got '$3')"; fi
}
assert_contains() { # label file text
  if grep -qF -- "$3" "$2"; then ok; else bad "$1 (missing '$3')"; fi
}
assert_not_contains() { # label file text
  if grep -qF -- "$3" "$2"; then bad "$1 (found '$3')"; else ok; fi
}

# --- stubs ------------------------------------------------------------------
write_stubs() {
  mkdir -p "${T}/bin" "${T}/scripts" "${T}/compose" "${T}/state" "${T}/routes"

  cat > "${T}/bin/curl" <<'STUB'
#!/usr/bin/env bash
method=GET; out=""; wfmt=""; maxt=""; url=""; data=""; hfile=""; fail=0; argv="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --max-time) maxt="$2"; shift 2 ;;
    --output) out="$2"; shift 2 ;;
    --write-out) wfmt="$2"; shift 2 ;;
    --request) method="$2"; shift 2 ;;
    --header) case "$2" in @*) hfile="${2#@}" ;; esac; shift 2 ;;
    --data-binary) data="${2#@}"; shift 2 ;;
    -fsS) fail=1; shift ;;
    http://*|https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
rest="${url#*://}"; path="/${rest#*/}"; path="${path%%\?*}"
key="${method}_${path//\//_}"
size=""
if [ -n "$data" ] && [ -f "$data" ]; then size=" data_bytes=$(wc -c < "$data" | tr -d ' ')"; fi
echo "CURL ${method} ${url} max_time=${maxt}${size} argv: ${argv}" >> "$STUB_EVENTS"
route="$STUB_ROUTES/$key"
if [ ! -f "$route" ]; then status=404; body=""; else status="$(head -n1 "$route")"; body="$(tail -n +2 "$route")"; fi
if [ "$status" = HANG ]; then
  "$STUB_REAL_SLEEP" "$(awk -v t="${maxt:-30}" -v s="$STUB_HANG_SCALE" 'BEGIN{print t*s}')"
  if [ -n "$wfmt" ]; then printf '000'; fi
  exit 28
fi
if [ -f "$route.expect_auth" ]; then
  if [ -z "$hfile" ] || [ ! -f "$hfile" ] || [ "$(cat "$hfile")" != "$(cat "$route.expect_auth")" ]; then
    status=401; body=""
  fi
fi
if [ -f "$route.expect_body" ]; then
  if [ -z "$data" ] || [ "$(cat "$data")" != "$(cat "$route.expect_body")" ]; then
    status=401; body=""
  fi
fi
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; elif [ -n "$body" ]; then printf '%s' "$body"; fi
if [ -n "$wfmt" ]; then printf '%s' "$status"; fi
if [ "$fail" = 1 ] && [ "$status" -ge 400 ]; then exit 22; fi
exit 0
STUB

  cat > "${T}/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "DOCKER $*" >> "$STUB_EVENTS"
case "$1" in login) cat > /dev/null ;; esac
exit 0
STUB

  cat > "${T}/bin/aws" <<'STUB'
#!/usr/bin/env bash
echo "AWS $*" >> "$STUB_EVENTS"
echo "stub-registry-password"
exit 0
STUB

  cat > "${T}/bin/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

  cat > "${T}/scripts/backup.sh" <<'STUB'
#!/usr/bin/env bash
echo "BACKUP $*" >> "$STUB_EVENTS"
if [ -f "$STUB_DIR/backup_fail" ]; then echo "backup failed" >&2; exit 1; fi
echo "20260101T000000Z_$1"
STUB

  cat > "${T}/scripts/rollback.sh" <<'STUB'
#!/usr/bin/env bash
echo "ROLLBACK $*" >> "$STUB_EVENTS"
if [ -f "$STUB_DIR/rollback_fail" ]; then exit 1; fi
exit 0
STUB

  chmod +x "${T}/bin/"* "${T}/scripts/backup.sh" "${T}/scripts/rollback.sh"
  cp "${DEPLOY_SRC}" "${T}/scripts/deploy.sh"
}

# route METHOD PATH STATUS [BODY]: set the stub curl's answer ("HANG" as STATUS never answers).
route() {
  local file="${T}/routes/${1}_${2//\//_}"
  printf '%s\n%s' "$3" "${4:-}" > "${file}"
}

# write_env LINE...: write the env file (flag, contact, ...) from the given lines.
write_env() { printf '%s\n' "ENVIRONMENT=production" "$@" > "${T}/env.prod"; }

write_smoke() { # USER PASSWORD
  printf 'SMOKE_USERNAME=%s\nSMOKE_PASSWORD=%s\n' "$1" "$2" > "${T}/state/smoke.env"
  chmod 600 "${T}/state/smoke.env"
}

# new_case on|off: a fresh sandbox where every check passes for that flag state.
new_case() {
  T="$(mktemp -d "${WORK}/case.XXXXXX")"
  write_stubs
  : > "${T}/events.log"
  write_smoke "${SMOKE_USER}" "${SMOKE_PASS}"
  route GET /api/health 200 '{"status":"ok"}'
  route POST /api/auth/login 200 "{\"access_token\":\"${TOKEN}\",\"refresh_token\":\"r\",\"token_type\":\"bearer\",\"expires_in\":900}"
  printf '{"username":"smoke-user","password":"p\\"w\\\\d#1"}' > "${T}/routes/POST__api_auth_login.expect_body"
  route GET /api/auth/me 200 '{"id":"1","username":"smoke-user","role":"site_manager"}'
  printf 'Authorization: Bearer %s\n' "${TOKEN}" > "${T}/routes/GET__api_auth_me.expect_auth"
  if [ "$1" = on ]; then
    write_env "SERVICE_TICKETS_ENABLED=true" "SERVICE_ACCESSIBILITY_CONTACT=help@example.com"
    route GET /api/service/auth/me 401 '{"error":"unauthorized"}'
    route POST /api/service/tickets 401 '{"error":"unauthorized"}'
    route GET /api/features 200 '{"service_tickets":true,"accessibility_contact":"help@example.com"}'
  else
    write_env "SERVICE_TICKETS_ENABLED=false" "SERVICE_ACCESSIBILITY_CONTACT="
    route GET /api/service/auth/me 404 '{"detail":"Not Found"}'
    route GET /api/features 200 '{"service_tickets":false}'
  fi
}

# run_deploy [VAR=value...]: run the copied deploy.sh; sets RC and writes ${T}/out.
run_deploy() {
  (
    cd "${T}" || exit 99
    env PATH="${T}/bin:${PATH}" \
      COMPOSE_DIR="${T}/compose" ECR_REGISTRY="example.invalid" ENV_FILE="${T}/env.prod" \
      STATE_DIR="${T}/state" STUB_DIR="${T}" STUB_ROUTES="${T}/routes" STUB_EVENTS="${T}/events.log" \
      STUB_REAL_SLEEP="${REAL_SLEEP}" STUB_HANG_SCALE="0.02" \
      "$@" bash "${T}/scripts/deploy.sh" v9.9.9
  ) > "${T}/out" 2>&1
  RC=$?
  # The credentials and the token must never reach the output or a command line.
  assert_not_contains "no password in output" "${T}/out" "${SMOKE_PASS}"
  assert_not_contains "no token in output" "${T}/out" "${TOKEN}"
  assert_not_contains "no password in command lines" "${T}/events.log" "${SMOKE_PASS}"
  assert_not_contains "no token in command lines" "${T}/events.log" "${TOKEN}"
}

failed_names() {
  { grep -o 'failed check: .*' "${T}/out" || true; } | sed 's/failed check: //' | tr '\n' ' ' | sed 's/ $//'
}

# check LABEL EXPECTED_RC "EXPECTED FAILED CHECK NAMES"
check() {
  local label="$1" erc="$2" enames="$3" name
  assert_eq "${label}: exit status" "${erc}" "${RC}"
  assert_eq "${label}: failed checks named" "${enames}" "$(failed_names)"
  for name in ${enames}; do
    assert_contains "${label}: ${name} result printed" "${T}/out" "   FAIL ${name}:"
  done
  if [ -n "${enames}" ]; then
    assert_contains "${label}: deploy reported failed" "${T}/out" "Post-deploy checks FAILED"
    assert_not_contains "${label}: no success message" "${T}/out" "succeeded and is healthy"
    assert_not_contains "${label}: no automatic rollback" "${T}/events.log" "ROLLBACK"
    assert_eq "${label}: version still recorded" "v9.9.9" "$(cat "${T}/state/CURRENT_VERSION")"
  fi
}

# ---------------------------------------------------------------------------
echo "== all checks pass"

new_case off
run_deploy
check "flag off, all pass" 0 ""
for name in hours_signin hours_read ticket_api_off; do
  assert_contains "flag off: ${name} passes" "${T}/out" "   PASS ${name}"
done
for name in ticket_api_on ticket_body_path accessibility_contact; do
  assert_contains "flag off: ${name} skipped" "${T}/out" "   SKIP ${name}"
done
assert_contains "flag off: deploy succeeds" "${T}/out" "succeeded and is healthy"
assert_contains "checks use curl --max-time 30 on http://localhost" "${T}/events.log" \
  "CURL GET http://localhost/api/service/auth/me max_time=30"
assert_contains "login goes to /api/auth/login" "${T}/events.log" \
  "CURL POST http://localhost/api/auth/login max_time=30"

new_case on
run_deploy
check "flag on, all pass" 0 ""
for name in hours_signin hours_read ticket_api_on ticket_body_path accessibility_contact; do
  assert_contains "flag on: ${name} passes" "${T}/out" "   PASS ${name}"
done
assert_contains "flag on: ticket_api_off skipped" "${T}/out" "   SKIP ticket_api_off"
assert_contains "ticket_body_path sends 2 MiB" "${T}/events.log" \
  "CURL POST http://localhost/api/service/tickets max_time=30 data_bytes=2097152"
assert_contains "checks run after the migration" "${T}/events.log" "alembic upgrade head"

new_case on
route GET /api/service/auth/me 200 '{}'
route GET /api/features 200 '{"accessibility_contact":"0501234567"}'
run_deploy
check "flag on, status 200 and a phone contact pass" 0 ""

new_case off
run_deploy POST_DEPLOY_BASE_URL=http://127.0.0.1:8080
check "base URL override" 0 ""
assert_contains "base URL override is used" "${T}/events.log" "CURL GET http://127.0.0.1:8080/api/auth/me"

new_case off
write_smoke "smoke-user" 'a #b "c" \d'
printf '{"username":"smoke-user","password":"a #b \\"c\\" \\\\d"}' > "${T}/routes/POST__api_auth_login.expect_body"
run_deploy
check "password with space-hash, quotes and backslash is sent intact" 0 ""

# ---------------------------------------------------------------------------
echo "== each check fails"

new_case off; route POST /api/auth/login 401 '{"detail":"bad"}'
run_deploy; check "hours_signin: status 401" 1 "hours_signin hours_read"

new_case off; route POST /api/auth/login 200 '{"refresh_token":"r"}'
run_deploy; check "hours_signin: no access_token" 1 "hours_signin hours_read"

new_case off; rm -f "${T}/state/smoke.env"
run_deploy; check "hours_signin: smoke.env missing" 1 "hours_signin hours_read"
assert_contains "smoke.env problem is explained" "${T}/out" "missing in ${T}/state/smoke.env"

new_case off; printf 'SMOKE_USERNAME=smoke-user\n' > "${T}/state/smoke.env"
run_deploy; check "hours_signin: password missing" 1 "hours_signin hours_read"

new_case off; route GET /api/auth/me 500 ''
run_deploy; check "hours_read: status 500" 1 "hours_read"

new_case off; route GET /api/auth/me 200 '{"role":"site_manager"}'
run_deploy; check "hours_read: no username" 1 "hours_read"

new_case off; route GET /api/auth/me 200 '{"username":""}'
run_deploy; check "hours_read: empty username" 1 "hours_read"

new_case off; route GET /api/service/auth/me 401 '{}'
run_deploy; check "ticket_api_off: 401 while flag off" 1 "ticket_api_off"

new_case off; route GET /api/service/auth/me 200 '{}'
run_deploy; check "ticket_api_off: 200 while flag off" 1 "ticket_api_off"

for code in 404 500 502 503; do
  new_case on; route GET /api/service/auth/me "${code}" ''
  run_deploy; check "ticket_api_on: status ${code}" 1 "ticket_api_on"
done

for code in 200 404 413 500 502; do
  new_case on; route POST /api/service/tickets "${code}" ''
  run_deploy; check "ticket_body_path: status ${code}" 1 "ticket_body_path"
done

new_case on; route GET /api/features 200 '{"service_tickets":true}'
run_deploy; check "accessibility_contact: absent" 1 "accessibility_contact"

new_case on; route GET /api/features 200 '{"service_tickets":true,"accessibility_contact":""}'
run_deploy; check "accessibility_contact: empty string" 1 "accessibility_contact"

new_case on; route GET /api/features 200 '{"service_tickets":true,"accessibility_contact":"   "}'
run_deploy; check "accessibility_contact: blank string" 1 "accessibility_contact"

new_case on; route GET /api/features 200 '{"service_tickets":true,"accessibility_contact":null}'
run_deploy; check "accessibility_contact: null" 1 "accessibility_contact"

new_case on; route GET /api/features 500 ''
run_deploy; check "accessibility_contact: status 500" 1 "accessibility_contact"

new_case on; route GET /api/service/auth/me 404 ''; route GET /api/features 200 '{"service_tickets":true}'
run_deploy; check "several failures are all named" 1 "ticket_api_on accessibility_contact"
assert_contains "failures print the backup name" "${T}/out" "Pre-upgrade backup: 20260101T000000Z_pre-v9.9.9"

# ---------------------------------------------------------------------------
echo "== timeouts count as failures"

new_case on; route GET /api/service/auth/me HANG
SECONDS=0
run_deploy
check "one hanging check" 1 "ticket_api_on"
assert_contains "timeout is reported" "${T}/out" "ticket_api_on: no response within 30s"
assert_eq "hang test finished quickly" "1" "$([ "${SECONDS}" -lt 20 ] && echo 1 || echo 0)"

new_case on
for r in "POST /api/auth/login" "GET /api/service/auth/me" "POST /api/service/tickets" "GET /api/features"; do
  # shellcheck disable=SC2086
  route ${r} HANG
done
run_deploy
check "hanging sign-in and ticket endpoints" 1 "hours_signin hours_read ticket_api_on ticket_body_path accessibility_contact"
assert_eq "every check call uses --max-time 30" "0" \
  "$(grep '^CURL' "${T}/events.log" | grep -vF '/api/health' | grep -cvF 'max_time=30' || true)"

new_case off; route GET /api/auth/me HANG
run_deploy
check "hanging read endpoint" 1 "hours_read"
assert_contains "read timeout is reported" "${T}/out" "hours_read: no response within 30s"

# ---------------------------------------------------------------------------
echo "== the flag is read with the application's rule"

for raw in 'true' 'TRUE' '  True  ' '"true"' "'True'" 'true # comment'; do
  new_case off
  write_env "SERVICE_TICKETS_ENABLED=${raw}" "SERVICE_ACCESSIBILITY_CONTACT=help@example.com"
  route GET /api/service/auth/me 401 '{}'
  route POST /api/service/tickets 401 '{}'
  route GET /api/features 200 '{"accessibility_contact":"help@example.com"}'
  run_deploy
  check "flag '${raw}' reads as on" 0 ""
  assert_contains "flag '${raw}': on-checks run" "${T}/out" "   PASS ticket_body_path"
done

for raw in 'false' '1' 'yes' 'on' 'trueish' '' '"  "'; do
  new_case off
  write_env "SERVICE_TICKETS_ENABLED=${raw}" "SERVICE_ACCESSIBILITY_CONTACT="
  run_deploy
  check "flag '${raw}' reads as off" 0 ""
  assert_contains "flag '${raw}': ticket_api_off runs" "${T}/out" "   PASS ticket_api_off"
done

new_case off; write_env "ENVIRONMENT=production"
run_deploy
check "flag unset reads as off" 0 ""
assert_contains "flag unset: ticket_api_off runs" "${T}/out" "   PASS ticket_api_off"

new_case on; write_env "SERVICE_TICKETS_ENABLED=false" "SERVICE_TICKETS_ENABLED=true" "SERVICE_ACCESSIBILITY_CONTACT=help@example.com"
run_deploy
check "last assignment wins" 0 ""
assert_contains "last assignment wins: flag on" "${T}/out" "   PASS ticket_body_path"

# ---------------------------------------------------------------------------
echo "== pre-flight: a flag-on deploy needs SERVICE_ACCESSIBILITY_CONTACT"

for contact in 'SERVICE_ACCESSIBILITY_CONTACT=' 'SERVICE_ACCESSIBILITY_CONTACT=   ' 'SERVICE_ACCESSIBILITY_CONTACT=""' 'SERVICE_ACCESSIBILITY_CONTACT= # nothing' '#SERVICE_ACCESSIBILITY_CONTACT=help@example.com' 'OTHER=1'; do
  new_case on
  write_env "SERVICE_TICKETS_ENABLED=true" "${contact}"
  run_deploy
  assert_eq "pre-flight '${contact}': exit status" "1" "${RC}"
  assert_contains "pre-flight '${contact}': named" "${T}/out" "FAIL accessibility_contact_preflight"
  assert_eq "pre-flight '${contact}': nothing ran" "" "$(cat "${T}/events.log")"
  assert_eq "pre-flight '${contact}': no backup recorded" "0" "$(find "${T}/state" -name LAST_PRE_UPGRADE_BACKUP | wc -l | tr -d ' ')"
done

new_case off
write_env "SERVICE_TICKETS_ENABLED=false" "SERVICE_ACCESSIBILITY_CONTACT="
run_deploy
check "empty contact is allowed while the flag is off" 0 ""

# ---------------------------------------------------------------------------
echo "== backup failure stops before the migration (R25.10)"

for flag in off on; do
  new_case "${flag}"; : > "${T}/backup_fail"
  run_deploy
  assert_eq "backup failure (${flag}): exit status" "1" "${RC}"
  assert_contains "backup failure (${flag}): backup was attempted" "${T}/events.log" "BACKUP pre-v9.9.9"
  assert_not_contains "backup failure (${flag}): no migration" "${T}/events.log" "alembic"
  assert_not_contains "backup failure (${flag}): nothing pulled" "${T}/events.log" "pull"
  assert_not_contains "backup failure (${flag}): containers untouched" "${T}/events.log" " up "
  assert_not_contains "backup failure (${flag}): no login to ECR" "${T}/events.log" "AWS"
  assert_not_contains "backup failure (${flag}): no check ran" "${T}/events.log" "CURL"
  assert_not_contains "backup failure (${flag}): no rollback" "${T}/events.log" "ROLLBACK"
  assert_eq "backup failure (${flag}): no backup recorded" "0" "$(find "${T}/state" -name LAST_PRE_UPGRADE_BACKUP | wc -l | tr -d ' ')"
  assert_eq "backup failure (${flag}): version not recorded" "0" "$(find "${T}/state" -name CURRENT_VERSION | wc -l | tr -d ' ')"
done

new_case on
run_deploy
backup_line="$(grep -n '^BACKUP' "${T}/events.log" | head -n1 | cut -d: -f1)"
migrate_line="$(grep -n 'alembic' "${T}/events.log" | head -n1 | cut -d: -f1)"
assert_eq "backup runs before the migration" "1" "$([ -n "${backup_line}" ] && [ -n "${migrate_line}" ] && [ "${backup_line}" -lt "${migrate_line}" ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
echo "== rollback stays tied to the health check"

new_case on; route GET /api/health 503 ''
run_deploy
assert_eq "health failure: exit status" "1" "${RC}"
assert_contains "health failure: rolls back" "${T}/events.log" "ROLLBACK"
assert_not_contains "health failure: post-deploy checks not run" "${T}/events.log" "/api/auth/login"
assert_not_contains "health failure: post-deploy checks not reported" "${T}/out" "Post-deploy checks"

new_case on; route GET /api/health 503 ''; : > "${T}/rollback_fail"
run_deploy
assert_eq "health and rollback failure: exit status" "2" "${RC}"

new_case on; route POST /api/service/tickets 500 ''
run_deploy
check "post-deploy failure does not roll back" 1 "ticket_body_path"

# ---------------------------------------------------------------------------
echo
echo "${PASSED} passed, ${FAILED} failed"
[ "${FAILED}" -eq 0 ]
