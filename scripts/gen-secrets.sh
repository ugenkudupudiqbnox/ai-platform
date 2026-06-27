#!/usr/bin/env bash
# =============================================================================
# gen-secrets.sh — create .env from .env.example and fill every secret with a
# strong random value. Idempotent: existing real values are preserved; only
# placeholders (__GENERATED__ / empty) are (re)generated. Derived values (hosts,
# mirrored secrets) are always kept consistent.
#
# Usage: gen-secrets.sh [--force]
#   --force   regenerate ALL secrets even if already set
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_cmd openssl

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# Create .env from the template if needed.
if [ ! -f "${ENV_FILE}" ]; then
  [ -f "${ENV_EXAMPLE}" ] || die ".env.example not found at ${ENV_EXAMPLE}"
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  log "Created ${ENV_FILE} from template."
fi
chmod 600 "${ENV_FILE}"

# Generate a value for KEY only if it is still a placeholder (or --force).
# Usage: ensure KEY <generator-command...>
ensure() {
  local key="$1"; shift
  local cur; cur="$(get_env "${key}" || true)"
  if [ "${FORCE}" -eq 1 ] || [ -z "${cur}" ] || [[ "${cur}" == *"__GENERATED__"* ]]; then
    set_env "${key}" "$("$@")"
  fi
}

gen_pk() { printf 'pk-lf-%s' "$(rand_uuid)"; }
gen_sk() { printf 'sk-lf-%s' "$(rand_uuid)"; }

heading "Generating platform secrets"

# --- Databases ---------------------------------------------------------------
ensure POSTGRES_SUPER_PASSWORD   rand_b64url 24
ensure KEYCLOAK_DB_PASSWORD      rand_b64url 24
ensure LANGFLOW_DB_PASSWORD      rand_b64url 24
ensure LANGFUSE_DB_PASSWORD      rand_b64url 24
ensure CLICKHOUSE_PASSWORD       rand_b64url 24
ensure MONGO_INITDB_ROOT_PASSWORD rand_b64url 24

# --- Redis -------------------------------------------------------------------
ensure REDIS_PASSWORD            rand_b64url 24

# --- MinIO -------------------------------------------------------------------
ensure MINIO_ROOT_PASSWORD       rand_b64url 24

# --- Keycloak ----------------------------------------------------------------
ensure KC_BOOTSTRAP_ADMIN_PASSWORD       rand_b64url 18
ensure KEYCLOAK_CLIENT_SECRET_LIBRECHAT  rand_uuid
ensure KEYCLOAK_CLIENT_SECRET_LANGFLOW   rand_uuid
ensure KEYCLOAK_CLIENT_SECRET_LANGFUSE   rand_uuid
ensure KEYCLOAK_SEED_ADMIN_PASSWORD      rand_b64url 18
ensure KEYCLOAK_SEED_DEVELOPER_PASSWORD  rand_b64url 18
ensure KEYCLOAK_SEED_USER_PASSWORD       rand_b64url 18

# --- LibreChat (exact-length crypto material) --------------------------------
ensure LIBRECHAT_CREDS_KEY            rand_hex 32   # 64 hex chars
ensure LIBRECHAT_CREDS_IV             rand_hex 16   # 32 hex chars
ensure LIBRECHAT_JWT_SECRET           rand_b64url 32
ensure LIBRECHAT_JWT_REFRESH_SECRET   rand_b64url 32
ensure OPENID_SESSION_SECRET          rand_b64url 32

# --- LangFlow ----------------------------------------------------------------
ensure LANGFLOW_SUPERUSER_PASSWORD    rand_b64url 18
ensure LANGFLOW_SECRET_KEY            rand_b64url 32
ensure FLOWER_PASSWORD                rand_b64url 18

# --- Langfuse ----------------------------------------------------------------
ensure LANGFUSE_NEXTAUTH_SECRET       rand_b64url 32
ensure LANGFUSE_SALT                  rand_b64url 24
ensure LANGFUSE_ENCRYPTION_KEY        rand_hex 32   # 64 hex chars
ensure LANGFUSE_INIT_USER_PASSWORD    rand_b64url 18
ensure LANGFUSE_INIT_PROJECT_PUBLIC_KEY gen_pk
ensure LANGFUSE_INIT_PROJECT_SECRET_KEY gen_sk

# --- oauth2-proxy ------------------------------------------------------------
# Cookie secret must be a string of exactly 16, 24 or 32 bytes (oauth2-proxy
# uses it directly as an AES key; it does NOT base64-decode it). 32 hex chars
# = 32 bytes.
ensure OAUTH2_PROXY_COOKIE_SECRET     rand_hex 16

# --- Monitoring --------------------------------------------------------------
ensure GRAFANA_ADMIN_PASSWORD         rand_b64url 18

# --- Mirror linked secrets (single source of truth) --------------------------
set_env OPENID_CLIENT_SECRET                  "$(get_env KEYCLOAK_CLIENT_SECRET_LIBRECHAT)"
set_env OAUTH2_PROXY_CLIENT_SECRET            "$(get_env KEYCLOAK_CLIENT_SECRET_LANGFLOW)"
set_env LANGFUSE_AUTH_KEYCLOAK_CLIENT_SECRET  "$(get_env KEYCLOAK_CLIENT_SECRET_LANGFUSE)"
set_env LANGFUSE_PUBLIC_KEY                   "$(get_env LANGFUSE_INIT_PROJECT_PUBLIC_KEY)"
set_env LANGFUSE_SECRET_KEY                   "$(get_env LANGFUSE_INIT_PROJECT_SECRET_KEY)"
# Keep the LangFlow queue URL password in sync with the Redis password.
set_env LANGFLOW_REDIS_QUEUE "redis://default:$(get_env REDIS_PASSWORD)@redis:6379/1"

chmod 600 "${ENV_FILE}"
success "Secrets generated and written to ${ENV_FILE} (permissions 600)."
