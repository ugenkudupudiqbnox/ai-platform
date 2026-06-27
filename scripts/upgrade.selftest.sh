#!/usr/bin/env bash
# =============================================================================
# upgrade.selftest.sh — offline integration self-test for upgrade.sh.
#
# Copies the repo into a sandbox, generates a real .env (gen-secrets), mocks
# docker/id on PATH, then runs upgrade.sh end-to-end. Asserts the upgrade
# sequence (pull, build --pull, datastore start, recreate with --remove-orphans,
# image prune), config re-render, and the pre-upgrade backup behaviour
# (skipped vs. taken). Requires no Docker/root.
#
# Run with: ./scripts/upgrade.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SRC="$(cd "${SELFTEST_DIR}/.." && pwd)"
# shellcheck source=selftest-lib.sh
source "${SELFTEST_DIR}/selftest-lib.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
st_make_sandbox "${REPO_SRC}" "${WORK}"

# upgrade.sh requires an existing install: generate a real .env in the sandbox.
( cd "${SANDBOX}" && bash ./scripts/gen-secrets.sh ) >/dev/null 2>&1

OUT="${WORK}/upgrade.out"

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_grep() { if grep -qE -- "$1" "$2"; then ok "$3"; else bad "$3 (missing /$1/ in $2)"; fi; }
assert_file() { if [ -f "$1" ]; then ok "$2"; else bad "$2 (missing file: $1)"; fi; }

echo "== upgrade.sh self-test =="

# --- Run upgrade (skip the pre-upgrade backup for a focused sequence check) ---
echo "[0] running upgrade (mocked docker, SKIP_BACKUP=1)"
: > "${DOCKER_MOCK_LOG}"
( cd "${SANDBOX}" && SKIP_BACKUP=1 bash ./upgrade.sh ) </dev/null >"${OUT}" 2>&1
RC=$?
if [ "${RC}" -eq 0 ]; then ok "upgrade.sh exits 0"; else bad "upgrade.sh exit ${RC}"; echo "----- output tail -----"; tail -n 25 "${OUT}"; fi

# --- 1. Upgrade sequence -----------------------------------------------------
echo "[1] upgrade sequence"
assert_grep 'compose .* pull'                 "${DOCKER_MOCK_LOG}" "images pulled"
assert_grep 'compose .* build --pull'         "${DOCKER_MOCK_LOG}" "local images rebuilt with --pull"
assert_grep 'up -d postgres'                  "${DOCKER_MOCK_LOG}" "datastores started before migration"
assert_grep 'up -d --remove-orphans'          "${DOCKER_MOCK_LOG}" "services recreated with --remove-orphans"
assert_grep 'image prune -f'                  "${DOCKER_MOCK_LOG}" "dangling images pruned"

# --- 2. Config re-render -----------------------------------------------------
echo "[2] config re-render"
assert_file "${SANDBOX}/docker/librechat/librechat.yaml" "librechat.yaml re-rendered"

# --- 3. Pre-upgrade backup behaviour -----------------------------------------
echo "[3] pre-upgrade backup"
# With SKIP_BACKUP=1, no snapshot should be produced.
if find "${SANDBOX}/backups" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | grep -q .; then
  bad "no backup taken when SKIP_BACKUP=1"
else
  ok "no backup taken when SKIP_BACKUP=1"
fi
# Without SKIP_BACKUP, a snapshot directory is created.
( cd "${SANDBOX}" && bash ./upgrade.sh ) </dev/null >"${OUT}" 2>&1
if find "${SANDBOX}/backups" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | grep -q .; then
  ok "pre-upgrade backup taken by default"
else
  bad "pre-upgrade backup was not taken by default"
fi

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
