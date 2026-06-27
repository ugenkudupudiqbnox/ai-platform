#!/usr/bin/env bash
# =============================================================================
# healthcheck.selftest.sh — offline self-test for healthcheck.sh.
#
# healthcheck.sh is read-only, so this runs the real script with a purpose-built
# `docker` mock that can simulate per-service state. Asserts: all-healthy ->
# exit 0; a down service -> exit 1 with a clear message; an unhealthy status ->
# exit 1; a bad NGINX config -> exit 1; missing .env -> hard error; and that the
# expected report sections are produced. Requires no Docker.
#
# Mock controls (env vars read by the mock):
#   HC_PS_EMPTY="<svc> ..."   services whose `ps -q` returns empty (not running)
#   HC_INSPECT_STATUS=<s>     status returned by `docker inspect` (default healthy)
#   HC_NGINX_BAD=1            make `nginx -t` fail
#
# Run with: ./scripts/healthcheck.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SELFTEST_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

ENVF="${WORK}/.env"
cat > "${ENVF}" <<'EOF'
COMPOSE_PROJECT_NAME=testproj
REDIS_PASSWORD=redispw
REDIS_DB_LANGFLOW_BROKER=1
POSTGRES_SUPER_USER=postgres
POSTGRES_SUPER_PASSWORD=pgpw
EOF

# Controllable docker mock.
MOCKBIN="${WORK}/bin"; mkdir -p "${MOCKBIN}"
cat > "${MOCKBIN}/docker" <<'MOCK'
#!/usr/bin/env bash
cmd="$*"
case "$1" in
  inspect) echo "${HC_INSPECT_STATUS:-healthy}"; exit 0 ;;
  stats)   exit 0 ;;
esac
case "$cmd" in
  *" ps -q "*)
    svc="${cmd##* }"                       # last token is the service name
    for d in ${HC_PS_EMPTY:-}; do
      [ "$svc" = "$d" ] && exit 0          # empty output => "not running"
    done
    echo "cid-${svc}" ;;
  *" ps --format"*) echo "langflow-worker" ;;
  *"nginx -t"*) [ -n "${HC_NGINX_BAD:-}" ] && exit 1 || exit 0 ;;
  *) : ;;
esac
exit 0
MOCK
chmod +x "${MOCKBIN}/docker"

OUT="${WORK}/out"
run_hc() { env "$@" ENV_FILE="${ENVF}" PATH="${MOCKBIN}:${PATH}" bash "${REPO}/healthcheck.sh" >"${OUT}" 2>&1; }

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_rc()    { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
assert_grep()  { if grep -qF -- "$1" "${OUT}"; then ok "$2"; else bad "$2 (missing '$1')"; fi; }
assert_nogrep(){ if grep -qF -- "$1" "${OUT}"; then bad "$2 (unexpected '$1')"; else ok "$2"; fi; }

echo "== healthcheck.sh self-test =="

# --- 1. All healthy ----------------------------------------------------------
echo "[1] all services healthy"
run_hc; rc=$?
assert_rc 0 "exit 0 when everything is healthy" "${rc}"
assert_grep "All required services are healthy." "success summary printed"
assert_grep "nginx: healthy" "per-service healthy status reported"

# --- 2. Report sections present ----------------------------------------------
echo "[2] report sections"
assert_grep "Container status"   "container status section"
assert_grep "Redis metrics"      "redis metrics section"
assert_grep "PostgreSQL metrics" "postgres metrics section"
assert_grep "celery queue length:" "queue depth reported"
assert_grep "langflow-worker replicas running:" "worker count reported"

# --- 3. A service is down ----------------------------------------------------
echo "[3] a service down"
run_hc HC_PS_EMPTY="nginx"; rc=$?
assert_rc 1 "exit 1 when a service is not running" "${rc}"
assert_grep "nginx: not running" "down service flagged"
assert_grep "One or more services are unhealthy" "failure summary printed"

# --- 4. A service is unhealthy -----------------------------------------------
echo "[4] a service unhealthy"
run_hc HC_INSPECT_STATUS="unhealthy"; rc=$?
assert_rc 1 "exit 1 when a service is unhealthy" "${rc}"
assert_grep "unhealthy" "unhealthy status surfaced"

# --- 5. Bad NGINX config -----------------------------------------------------
echo "[5] bad nginx config"
run_hc HC_NGINX_BAD=1; rc=$?
assert_rc 1 "exit 1 when nginx config test fails" "${rc}"
assert_grep "nginx configuration test failed" "nginx failure reported"

# --- 6. Missing .env ---------------------------------------------------------
echo "[6] missing .env"
env ENV_FILE="${WORK}/nope.env" PATH="${MOCKBIN}:${PATH}" bash "${REPO}/healthcheck.sh" >"${OUT}" 2>&1; rc=$?
if [ "${rc}" -ne 0 ]; then ok "errors out when .env is missing (rc=${rc})"; else bad "did not error on missing .env"; fi
assert_grep ".env not found" "missing .env message"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
