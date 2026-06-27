#!/usr/bin/env bash
# =============================================================================
# install.selftest.sh — offline integration self-test for install.sh.
#
# Copies the repo into a sandbox, mocks docker/systemctl/id on PATH, then runs
# the real install.sh end-to-end with --skip-deps --skip-ssl and a test domain.
# Asserts: the generated .env (domain/hosts/email/secrets), rendered realm +
# librechat config, the orchestration sequence (build/up of each tier, TLS
# bootstrap) and the installed systemd timer units. Requires no Docker/root.
#
# Run with: ./scripts/install.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SRC="$(cd "${SELFTEST_DIR}/.." && pwd)"
# shellcheck source=selftest-lib.sh
source "${SELFTEST_DIR}/selftest-lib.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
st_make_sandbox "${REPO_SRC}" "${WORK}"

export SYSTEMD_DIR="${WORK}/systemd"
ENVF="${SANDBOX}/.env"
OUT="${WORK}/install.out"

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_grep() { if grep -qE -- "$1" "$2"; then ok "$3"; else bad "$3 (missing /$1/ in $2)"; fi; }
assert_file() { if [ -f "$1" ]; then ok "$2"; else bad "$2 (missing file: $1)"; fi; }

echo "== install.sh self-test =="

# --- Run the installer -------------------------------------------------------
echo "[0] running installer (mocked docker/systemctl, --skip-deps --skip-ssl)"
( cd "${SANDBOX}" && bash ./install.sh \
    --domain test.example.com --email admin@test.example.com \
    --skip-deps --skip-ssl ) </dev/null >"${OUT}" 2>&1
RC=$?
if [ "${RC}" -eq 0 ]; then ok "install.sh exits 0"; else bad "install.sh exit ${RC}"; echo "----- output tail -----"; tail -n 25 "${OUT}"; fi

# --- 1. Generated .env -------------------------------------------------------
echo "[1] generated .env"
assert_file "${ENVF}" ".env created"
assert_grep '^BASE_DOMAIN=test\.example\.com$'  "${ENVF}" "BASE_DOMAIN set from --domain"
assert_grep '^CHAT_HOST=chat\.test\.example\.com$'   "${ENVF}" "CHAT_HOST derived"
assert_grep '^AUTH_HOST=auth\.test\.example\.com$'   "${ENVF}" "AUTH_HOST derived"
assert_grep '^FLOW_HOST=flow\.test\.example\.com$'   "${ENVF}" "FLOW_HOST derived"
assert_grep '^TRACE_HOST=trace\.test\.example\.com$' "${ENVF}" "TRACE_HOST derived"
assert_grep '^ACME_EMAIL=admin@test\.example\.com$'  "${ENVF}" "ACME_EMAIL set from --email"
if grep -Eq '^[A-Za-z0-9_]+=.*__GENERATED__' "${ENVF}"; then
  bad "no __GENERATED__ placeholders remain"
else
  ok "no __GENERATED__ placeholders remain"
fi
# A representative secret is actually populated.
assert_grep '^REDIS_PASSWORD=.+'  "${ENVF}" "secrets are generated"

# --- 2. Rendered config ------------------------------------------------------
echo "[2] rendered config"
REALM="${SANDBOX}/docker/keycloak/realm.json"
assert_file "${REALM}" "realm.json rendered"
if ! command -v jq >/dev/null 2>&1; then
  ok "jq absent — JSON check skipped"
elif jq empty "${REALM}" >/dev/null 2>&1; then
  ok "realm.json is valid JSON"
else
  bad "realm.json is not valid JSON"
fi
assert_grep 'https://chat\.test\.example\.com/oauth/openid/callback' "${REALM}" "realm uses the new domain"
assert_file "${SANDBOX}/docker/librechat/librechat.yaml" "librechat.yaml rendered"

# --- 3. Orchestration sequence (recorded docker calls) -----------------------
echo "[3] orchestration sequence"
assert_grep 'compose .* build'              "${DOCKER_MOCK_LOG}" "images are built"
assert_grep 'up -d postgres redis mongo'    "${DOCKER_MOCK_LOG}" "datastores started"
assert_grep 'up -d keycloak'                "${DOCKER_MOCK_LOG}" "keycloak started"
assert_grep 'up -d langfuse-web'            "${DOCKER_MOCK_LOG}" "langfuse started"
assert_grep 'up -d langflow'                "${DOCKER_MOCK_LOG}" "langflow started"
assert_grep 'up -d nginx'                   "${DOCKER_MOCK_LOG}" "nginx started"
assert_grep 'run --rm .* certbot'           "${DOCKER_MOCK_LOG}" "TLS bootstrap invoked certbot"

# --- 4. systemd timers -------------------------------------------------------
echo "[4] systemd timers"
assert_file "${SYSTEMD_DIR}/aiplatform-certbot-renew.service" "certbot-renew.service installed"
assert_file "${SYSTEMD_DIR}/aiplatform-certbot-renew.timer"   "certbot-renew.timer installed"
assert_file "${SYSTEMD_DIR}/aiplatform-backup.service"        "backup.service installed"
assert_file "${SYSTEMD_DIR}/aiplatform-backup.timer"          "backup.timer installed"
assert_grep 'daemon-reload' "${SYSTEMCTL_MOCK_LOG}" "systemctl daemon-reload called"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
