#!/usr/bin/env bash
# =============================================================================
# uninstall.selftest.sh — offline integration self-test for uninstall.sh.
#
# Copies the repo into a sandbox, mocks docker/systemctl/id on PATH, then runs
# uninstall.sh in its modes and asserts: argument handling, the confirmation
# gate for destructive modes, `compose down` with/without --volumes, systemd
# timer disable, and --purge removal of units + rendered config (while keeping
# .env). Requires no Docker/root.
#
# Run with: ./scripts/uninstall.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SRC="$(cd "${SELFTEST_DIR}/.." && pwd)"
# shellcheck source=selftest-lib.sh
source "${SELFTEST_DIR}/selftest-lib.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
st_make_sandbox "${REPO_SRC}" "${WORK}"

# Minimal .env so dc has an --env-file (docker is mocked, contents don't matter).
printf 'COMPOSE_PROJECT_NAME=testproj\n' > "${SANDBOX}/.env"

export SYSTEMD_DIR="${WORK}/systemd"
mkdir -p "${SYSTEMD_DIR}"

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_rc()    { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
assert_grep()  { if grep -qE -- "$1" "$2"; then ok "$3"; else bad "$3 (missing /$1/ in $2)"; fi; }
assert_nogrep(){ if grep -qE -- "$1" "$2"; then bad "$3 (unexpected /$1/ in $2)"; else ok "$3"; fi; }
assert_file()  { if [ -f "$1" ]; then ok "$2"; else bad "$2 (missing: $1)"; fi; }
assert_nofile(){ if [ ! -e "$1" ]; then ok "$2"; else bad "$2 (still present: $1)"; fi; }

run_uninstall() { # <stdin-text> <args...>
  local input="$1"; shift
  : > "${DOCKER_MOCK_LOG}"; : > "${SYSTEMCTL_MOCK_LOG}"
  ( cd "${SANDBOX}" && printf '%s\n' "${input}" | bash ./uninstall.sh "$@" ) \
    >"${WORK}/out" 2>&1
  return $?
}

echo "== uninstall.sh self-test =="

# --- 1. Default mode (keep data) ---------------------------------------------
echo "[1] default mode (keep volumes)"
run_uninstall "" ; rc=$?
assert_rc 0 "uninstall (no args) exits 0" "${rc}"
assert_grep 'down --remove-orphans' "${DOCKER_MOCK_LOG}" "compose down invoked"
assert_nogrep 'down --volumes' "${DOCKER_MOCK_LOG}" "volumes preserved (no --volumes)"
assert_grep 'disable --now aiplatform-certbot-renew.timer' "${SYSTEMCTL_MOCK_LOG}" "certbot timer disabled"
assert_grep 'disable --now aiplatform-backup.timer' "${SYSTEMCTL_MOCK_LOG}" "backup timer disabled"

# --- 2. Invalid argument -----------------------------------------------------
echo "[2] invalid argument"
run_uninstall "" --bogus ; rc=$?
if [ "${rc}" -ne 0 ]; then ok "rejects unknown argument (rc=${rc})"; else bad "did not reject unknown argument"; fi

# --- 3. Confirmation gate (destructive modes) --------------------------------
echo "[3] confirmation gate"
run_uninstall "no" --volumes ; rc=$?
if [ "${rc}" -ne 0 ]; then ok "aborts when confirmation is not 'DELETE'"; else bad "did not abort on wrong confirmation"; fi
assert_nogrep 'down --volumes' "${DOCKER_MOCK_LOG}" "no volume teardown on aborted confirmation"

# --- 4. --volumes (confirmed) ------------------------------------------------
echo "[4] --volumes (confirmed)"
run_uninstall "DELETE" --volumes ; rc=$?
assert_rc 0 "--volumes exits 0 when confirmed" "${rc}"
assert_grep 'down --volumes --remove-orphans' "${DOCKER_MOCK_LOG}" "volumes removed with --volumes"

# --- 5. --purge --------------------------------------------------------------
echo "[5] --purge"
# Pre-create the artifacts that --purge should remove.
touch "${SYSTEMD_DIR}/aiplatform-certbot-renew.service" \
      "${SYSTEMD_DIR}/aiplatform-certbot-renew.timer" \
      "${SYSTEMD_DIR}/aiplatform-backup.service" \
      "${SYSTEMD_DIR}/aiplatform-backup.timer"
mkdir -p "${SANDBOX}/docker/keycloak" "${SANDBOX}/docker/librechat"
echo '{}' > "${SANDBOX}/docker/keycloak/realm.json"
echo 'x'  > "${SANDBOX}/docker/librechat/librechat.yaml"

run_uninstall "DELETE" --purge ; rc=$?
assert_rc 0 "--purge exits 0 when confirmed" "${rc}"
assert_grep 'down --volumes --remove-orphans' "${DOCKER_MOCK_LOG}" "purge tears down volumes"
assert_nofile "${SYSTEMD_DIR}/aiplatform-certbot-renew.timer" "purge removes certbot timer unit"
assert_nofile "${SYSTEMD_DIR}/aiplatform-backup.service" "purge removes backup service unit"
assert_nofile "${SANDBOX}/docker/keycloak/realm.json" "purge removes rendered realm.json"
assert_nofile "${SANDBOX}/docker/librechat/librechat.yaml" "purge removes rendered librechat.yaml"
assert_file "${SANDBOX}/.env" "purge keeps .env (secrets) in place"
assert_grep 'daemon-reload' "${SYSTEMCTL_MOCK_LOG}" "systemctl daemon-reload after purge"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
