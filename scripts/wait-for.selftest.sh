#!/usr/bin/env bash
# =============================================================================
# wait-for.selftest.sh — offline self-test for wait-for.sh (and the underlying
# wait_for_service helper in common.sh).
#
# Runs the real wait-for.sh with a controllable `docker` mock and a no-op `sleep`
# so the timeout path is fast. Asserts: usage error with no args, success on a
# healthy/running service, and a timeout (rc 1) when the service never comes up.
# Requires no Docker.
#
# Run with: ./scripts/wait-for.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SELFTEST_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

ENVF="${WORK}/.env"
printf 'COMPOSE_PROJECT_NAME=testproj\n' > "${ENVF}"
OUT="${WORK}/out"

MOCKBIN="${WORK}/bin"; mkdir -p "${MOCKBIN}"
# docker mock: `ps -q` returns a cid unless WAIT_DOWN set; `inspect` returns
# WAIT_STATUS (default healthy).
cat > "${MOCKBIN}/docker" <<'MOCK'
#!/usr/bin/env bash
case "$1" in
  inspect) echo "${WAIT_STATUS:-healthy}"; exit 0 ;;
esac
case "$*" in
  *" ps -q "*) [ -n "${WAIT_DOWN:-}" ] && exit 0 || echo "cid-mock" ;;
esac
exit 0
MOCK
# no-op sleep so the timeout loop doesn't actually wait.
printf '#!/usr/bin/env bash\nexit 0\n' > "${MOCKBIN}/sleep"
chmod +x "${MOCKBIN}/docker" "${MOCKBIN}/sleep"

run_waitfor() { # <env-assignments...> -- <wait-for args...>
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  env "${envs[@]}" ENV_FILE="${ENVF}" PATH="${MOCKBIN}:${PATH}" \
    bash "${REPO}/scripts/wait-for.sh" "$@" >"${OUT}" 2>&1
}

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_rc()   { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
out_grep()    { if grep -qF -- "$1" "${OUT}"; then ok "$2"; else bad "$2 (missing '$1')"; fi; }

echo "== wait-for.sh self-test =="

# --- 1. Usage error ----------------------------------------------------------
echo "[1] usage"
run_waitfor -- ; rc=$?
if [ "${rc}" -ne 0 ]; then ok "errors with no arguments (rc=${rc})"; else bad "did not error without arguments"; fi
out_grep "Usage" "prints usage message"

# --- 2. Healthy service ------------------------------------------------------
echo "[2] healthy"
run_waitfor -- postgres ; rc=$?
assert_rc 0 "returns 0 when service is healthy" "${rc}"
out_grep "is healthy" "reports healthy status"

# --- 3. Running (no healthcheck) service -------------------------------------
echo "[3] running"
run_waitfor WAIT_STATUS=running -- redis ; rc=$?
assert_rc 0 "returns 0 when service is running" "${rc}"
out_grep "is running" "reports running status"

# --- 4. Timeout --------------------------------------------------------------
echo "[4] timeout"
run_waitfor WAIT_DOWN=1 -- nginx 1 ; rc=$?
assert_rc 1 "returns 1 when service never becomes ready" "${rc}"
out_grep "Timed out" "reports a timeout"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
