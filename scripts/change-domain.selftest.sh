#!/usr/bin/env bash
# =============================================================================
# change-domain.selftest.sh — offline self-test for change-domain.sh.
#
# Sources change-domain.sh (functions only, main is guarded), points it at a
# throwaway .env and REPO_ROOT, mocks `dc` (docker compose) and a few helpers,
# then asserts the pure logic: domain validation, .env mutation, realm
# rendering, Keycloak client updates and the service-recreation set.
#
# Requires no Docker/Keycloak. Run with: ./scripts/change-domain.selftest.sh
# (also wired into `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sandbox: temp REPO_ROOT + .env so we never touch the real repo state.
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/docker/keycloak"
cp "${SELFTEST_DIR}/../docker/keycloak/realm.json.tmpl" "${WORK}/docker/keycloak/realm.json.tmpl"

export ENV_FILE="${WORK}/.env"
cat > "${ENV_FILE}" <<'EOF'
KEYCLOAK_REALM=AIPlatform
KC_BOOTSTRAP_ADMIN_USERNAME=admin
KC_BOOTSTRAP_ADMIN_PASSWORD=secret
KEYCLOAK_CLIENT_SECRET_LIBRECHAT=sec-lc
KEYCLOAK_CLIENT_SECRET_LANGFLOW=sec-lf
KEYCLOAK_CLIENT_SECRET_LANGFUSE=sec-lfuse
KEYCLOAK_SEED_ADMIN_USERNAME=platform-admin
KEYCLOAK_SEED_ADMIN_EMAIL=admin@old.example.com
KEYCLOAK_SEED_ADMIN_PASSWORD=pw1
KEYCLOAK_SEED_DEVELOPER_USERNAME=platform-dev
KEYCLOAK_SEED_DEVELOPER_EMAIL=dev@old.example.com
KEYCLOAK_SEED_DEVELOPER_PASSWORD=pw2
KEYCLOAK_SEED_USER_USERNAME=platform-user
KEYCLOAK_SEED_USER_EMAIL=user@old.example.com
KEYCLOAK_SEED_USER_PASSWORD=pw3
BASE_DOMAIN=old.example.com
CHAT_HOST=chat.old.example.com
AUTH_HOST=auth.old.example.com
FLOW_HOST=flow.old.example.com
TRACE_HOST=trace.old.example.com
MONGO_INITDB_ROOT_USERNAME=librechat
MONGO_INITDB_ROOT_PASSWORD=mongopw
MONGO_DB_NAME=LibreChat
EOF

# Source the unit-under-test (functions only; main is guarded out).
# shellcheck source=change-domain.sh
source "${SELFTEST_DIR}/change-domain.sh"

# Redirect rendering/config at our sandbox and neutralize side-effecting helpers.
REPO_ROOT="${WORK}"
DC_LOG="${WORK}/dc.log"
: > "${DC_LOG}"

require_root() { :; }
wait_for_service() { :; }

# Mock `dc` (docker compose): log every call, return canned output.
dc() {
  echo "dc $*" >> "${DC_LOG}"
  case "$*" in
    *"get clients"*) echo "deadbeef-0000-0000-0000-000000000000" ;;
  esac
  return 0
}

# --- Tiny assertion harness --------------------------------------------------
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }

assert_rc() { # <expected-rc> <desc> <actual-rc>
  local exp="$1" desc="$2" act="$3"
  if [ "${act}" = "${exp}" ]; then ok "${desc} (rc=${act})"; else bad "${desc} (expected rc ${exp}, got ${act})"; fi
}
assert_eq() { # <expected> <actual> <desc>
  if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$1', got '$2')"; fi
}
assert_grep() { # <pattern> <file> <desc>
  if grep -qF -- "$1" "$2"; then ok "$3"; else bad "$3 (missing: $1)"; fi
}

echo "== change-domain.sh self-test =="

# --- 1. Domain validation ----------------------------------------------------
echo "[1] domain validation"
rc=0; cd_validate_domain "nodots" "old.example.com" >/dev/null 2>&1 || rc=$?
assert_rc 1 "rejects domain without a dot" "${rc}"
rc=0; cd_validate_domain "" "old.example.com" >/dev/null 2>&1 || rc=$?
assert_rc 1 "rejects empty domain" "${rc}"
rc=0; cd_validate_domain "same.example.com" "same.example.com" >/dev/null 2>&1 || rc=$?
assert_rc 2 "flags unchanged domain as no-op" "${rc}"
rc=0; cd_validate_domain "new.example.com" "old.example.com" >/dev/null 2>&1 || rc=$?
assert_rc 0 "accepts a valid new domain" "${rc}"

# --- 2. .env mutation --------------------------------------------------------
echo "[2] .env mutation"
cd_update_env "new.example.com"
assert_eq "new.example.com"      "$(get_env BASE_DOMAIN)" "BASE_DOMAIN updated"
assert_eq "chat.new.example.com" "$(get_env CHAT_HOST)"   "CHAT_HOST derived"
assert_eq "auth.new.example.com" "$(get_env AUTH_HOST)"   "AUTH_HOST derived"
assert_eq "flow.new.example.com" "$(get_env FLOW_HOST)"   "FLOW_HOST derived"
assert_eq "trace.new.example.com" "$(get_env TRACE_HOST)" "TRACE_HOST derived"

# --- 3. realm rendering ------------------------------------------------------
echo "[3] realm rendering"
render_realm >/dev/null 2>&1
REALM_OUT="${WORK}/docker/keycloak/realm.json"
if command -v jq >/dev/null 2>&1; then
  if jq empty "${REALM_OUT}" >/dev/null 2>&1; then ok "rendered realm.json is valid JSON"; else bad "realm.json is not valid JSON"; fi
else
  ok "jq not installed — skipped JSON validity (non-fatal)"
fi
assert_grep "https://chat.new.example.com/oauth/openid/callback" "${REALM_OUT}" "librechat redirect URI uses new domain"
assert_grep "https://flow.new.example.com/oauth2/callback"        "${REALM_OUT}" "langflow redirect URI uses new domain"
assert_grep "https://trace.new.example.com/api/auth/callback/keycloak" "${REALM_OUT}" "langfuse redirect URI uses new domain"
# Only URL fields must be rewritten; seed user emails (admin@old.example.com)
# are identities, not endpoints, and are intentionally left untouched.
if grep -Eq 'https://[a-zA-Z0-9.-]*old\.example\.com' "${REALM_OUT}"; then
  bad "no stale old-domain URLs remain"
else
  ok "no stale old-domain URLs remain (seed emails intentionally unchanged)"
fi

# --- 4. Keycloak client updates (mocked dc) ----------------------------------
echo "[4] keycloak client updates"
: > "${DC_LOG}"
cd_kcadm_login
assert_grep "config credentials --server http://localhost:8080 --realm master --user admin" "${DC_LOG}" "kcadm authenticates as bootstrap admin"
cd_update_all_clients "new.example.com"
assert_grep 'clientId=librechat' "${DC_LOG}" "looks up librechat client"
assert_grep 'clientId=langflow'  "${DC_LOG}" "looks up langflow client"
assert_grep 'clientId=langfuse'  "${DC_LOG}" "looks up langfuse client"
assert_grep 'redirectUris=["https://chat.new.example.com/oauth/openid/callback"]'    "${DC_LOG}" "librechat redirect updated via kcadm"
assert_grep 'redirectUris=["https://flow.new.example.com/oauth2/callback"]'          "${DC_LOG}" "langflow redirect updated via kcadm"
assert_grep 'redirectUris=["https://trace.new.example.com/api/auth/callback/keycloak"]' "${DC_LOG}" "langfuse redirect updated via kcadm"

# --- 4b. LibreChat OIDC issuer migration -------------------------------------
echo "[4b] librechat issuer migration"
: > "${DC_LOG}"
cd_migrate_librechat_issuer
assert_grep "mongosh" "${DC_LOG}" "runs a mongo update against LibreChat"
assert_grep "openidIssuer:'https://auth.new.example.com/realms/AIPlatform'" "${DC_LOG}" "repoints openid users at the new issuer"

# --- 5. Service recreation set -----------------------------------------------
echo "[5] service recreation"
: > "${DC_LOG}"
cd_recreate_services
assert_grep "up -d oauth2-proxy librechat langfuse-web langfuse-worker langflow langflow-worker" "${DC_LOG}" "recreates exactly the hostname-bound services"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
