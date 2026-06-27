#!/usr/bin/env bash
# =============================================================================
# gen-secrets.selftest.sh — offline self-test for gen-secrets.sh.
#
# gen-secrets.sh is pure (openssl + .env manipulation, no Docker), so this runs
# it as a black box against a throwaway .env (using the repo's real .env.example
# as the template) and asserts: full placeholder replacement, correct file
# permissions, exact-length crypto material, key prefixes, mirrored secrets,
# idempotency, and --force regeneration.
#
# Run with: ./scripts/gen-secrets.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Use a throwaway env file; common.sh honours an exported ENV_FILE. The real
# .env.example (resolved via REPO_ROOT) is used as the source template.
export ENV_FILE="${WORK}/.env"
# shellcheck source=common.sh
source "${SELFTEST_DIR}/common.sh"

GEN="${SELFTEST_DIR}/gen-secrets.sh"

# --- Tiny assertion harness --------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_eq()   { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$1', got '$2')"; fi; }
assert_ne()   { if [ "$1" != "$2" ]; then ok "$3"; else bad "$3 (value unexpectedly unchanged: '$1')"; fi; }
assert_len()  { local v; v="$(get_env "$1" | tr -d '\n')"; if [ "${#v}" = "$2" ]; then ok "$3 (len ${#v})"; else bad "$3 (expected len $2, got ${#v})"; fi; }
assert_match(){ local v; v="$(get_env "$1")"; if printf '%s' "${v}" | grep -Eq "$2"; then ok "$3"; else bad "$3 (value '${v}' !~ /$2/)"; fi; }

echo "== gen-secrets.sh self-test =="

# --- 1. Fresh generation -----------------------------------------------------
echo "[1] fresh generation"
[ -f "${ENV_FILE}" ] && rm -f "${ENV_FILE}"
if bash "${GEN}" >/dev/null 2>&1; then ok "gen-secrets.sh exits 0 on a fresh run"; else bad "gen-secrets.sh failed on fresh run"; fi
if [ -f "${ENV_FILE}" ]; then ok ".env created from template"; else bad ".env was not created"; fi
assert_eq "600" "$(stat -c '%a' "${ENV_FILE}")" ".env permissions are 600"

# No placeholders remain in actual KEY=VALUE lines (comments are ignored).
if grep -E '^[A-Za-z0-9_]+=.*__GENERATED__' "${ENV_FILE}" >/dev/null 2>&1; then
  bad "no __GENERATED__ placeholders remain in values"
  grep -nE '^[A-Za-z0-9_]+=.*__GENERATED__' "${ENV_FILE}" | sed 's/^/      /'
else
  ok "no __GENERATED__ placeholders remain in values"
fi

# --- 2. Exact-length crypto material -----------------------------------------
echo "[2] crypto material lengths/format"
assert_len LIBRECHAT_CREDS_KEY      64 "LIBRECHAT_CREDS_KEY is 64 hex chars"
assert_len LIBRECHAT_CREDS_IV       32 "LIBRECHAT_CREDS_IV is 32 hex chars"
assert_len LANGFUSE_ENCRYPTION_KEY  64 "LANGFUSE_ENCRYPTION_KEY is 64 hex chars"
assert_match LIBRECHAT_CREDS_KEY     '^[0-9a-f]{64}$' "LIBRECHAT_CREDS_KEY is lowercase hex"
assert_match LANGFUSE_ENCRYPTION_KEY '^[0-9a-f]{64}$' "LANGFUSE_ENCRYPTION_KEY is lowercase hex"

# Langfuse key prefixes.
assert_match LANGFUSE_INIT_PROJECT_PUBLIC_KEY '^pk-lf-' "Langfuse public key has pk-lf- prefix"
assert_match LANGFUSE_INIT_PROJECT_SECRET_KEY '^sk-lf-' "Langfuse secret key has sk-lf- prefix"

# oauth2-proxy uses the cookie secret directly as an AES key, so the raw string
# length must be exactly 16, 24 or 32 bytes (it does NOT base64-decode it).
COOKIE_LEN="$(get_env OAUTH2_PROXY_COOKIE_SECRET | tr -d '\n' | wc -c | tr -d ' ')"
case "${COOKIE_LEN}" in
  16|24|32) ok "oauth2-proxy cookie secret is ${COOKIE_LEN} bytes" ;;
  *) bad "oauth2-proxy cookie secret is ${COOKIE_LEN} bytes (want 16/24/32)" ;;
esac

# --- 3. Mirrored secrets (single source of truth) ----------------------------
echo "[3] mirrored secrets"
assert_eq "$(get_env KEYCLOAK_CLIENT_SECRET_LIBRECHAT)" "$(get_env OPENID_CLIENT_SECRET)" \
  "OPENID_CLIENT_SECRET mirrors librechat client secret"
assert_eq "$(get_env KEYCLOAK_CLIENT_SECRET_LANGFLOW)"  "$(get_env OAUTH2_PROXY_CLIENT_SECRET)" \
  "OAUTH2_PROXY_CLIENT_SECRET mirrors langflow client secret"
assert_eq "$(get_env KEYCLOAK_CLIENT_SECRET_LANGFUSE)"  "$(get_env LANGFUSE_AUTH_KEYCLOAK_CLIENT_SECRET)" \
  "LANGFUSE_AUTH_KEYCLOAK_CLIENT_SECRET mirrors langfuse client secret"
assert_eq "$(get_env LANGFUSE_INIT_PROJECT_PUBLIC_KEY)" "$(get_env LANGFUSE_PUBLIC_KEY)" \
  "LANGFUSE_PUBLIC_KEY mirrors init project public key"
assert_eq "$(get_env LANGFUSE_INIT_PROJECT_SECRET_KEY)" "$(get_env LANGFUSE_SECRET_KEY)" \
  "LANGFUSE_SECRET_KEY mirrors init project secret key"

# LangFlow Celery queue URL embeds the generated Redis password.
REDIS_PW="$(get_env REDIS_PASSWORD)"
if printf '%s' "$(get_env LANGFLOW_REDIS_QUEUE)" | grep -qF "${REDIS_PW}"; then
  ok "LANGFLOW_REDIS_QUEUE embeds the Redis password"
else
  bad "LANGFLOW_REDIS_QUEUE does not embed the Redis password"
fi

# --- 4. Idempotency (re-run preserves existing values) -----------------------
echo "[4] idempotency"
V1="$(get_env REDIS_PASSWORD)"
bash "${GEN}" >/dev/null 2>&1
assert_eq "${V1}" "$(get_env REDIS_PASSWORD)" "re-run without --force preserves REDIS_PASSWORD"
assert_eq "$(get_env KEYCLOAK_CLIENT_SECRET_LIBRECHAT)" "$(get_env OPENID_CLIENT_SECRET)" \
  "mirrors remain consistent after re-run"

# --- 5. --force regenerates --------------------------------------------------
echo "[5] --force regeneration"
bash "${GEN}" --force >/dev/null 2>&1
assert_ne "${V1}" "$(get_env REDIS_PASSWORD)" "--force regenerates REDIS_PASSWORD"
assert_eq "$(get_env KEYCLOAK_CLIENT_SECRET_LANGFUSE)" "$(get_env LANGFUSE_AUTH_KEYCLOAK_CLIENT_SECRET)" \
  "mirrors stay consistent after --force"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
