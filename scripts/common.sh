#!/usr/bin/env bash
# =============================================================================
# Shared helpers sourced by every platform script.
# =============================================================================

# Resolve the repository root (parent of this scripts/ directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
# Used by gen-secrets.sh (sourcing script).
# shellcheck disable=SC2034
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

# --- Colored logging ---------------------------------------------------------
if [ -t 1 ]; then
  C_RESET="\033[0m"; C_RED="\033[31m"; C_GREEN="\033[32m"
  C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_BOLD="\033[1m"
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

log()     { echo -e "${C_BLUE}[*]${C_RESET} $*"; }
info()    { echo -e "${C_BLUE}[i]${C_RESET} $*"; }
success() { echo -e "${C_GREEN}[✓]${C_RESET} $*"; }
warn()    { echo -e "${C_YELLOW}[!]${C_RESET} $*" >&2; }
error()   { echo -e "${C_RED}[✗]${C_RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

heading() {
  echo
  echo -e "${C_BOLD}==================================================================${C_RESET}"
  echo -e "${C_BOLD} $*${C_RESET}"
  echo -e "${C_BOLD}==================================================================${C_RESET}"
}

# --- Docker Compose wrapper --------------------------------------------------
# Always run from the repo root with the generated env file.
dc() {
  ( cd "${REPO_ROOT}" && docker compose --env-file "${ENV_FILE}" "$@" )
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "This script must be run as root (use: sudo $0)"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

# --- .env read/write helpers -------------------------------------------------
# Read a key's value from the env file (everything after the first '=').
get_env() {
  local key="$1" file="${2:-$ENV_FILE}"
  [ -f "$file" ] || return 1
  grep -E "^${key}=" "$file" | head -n1 | cut -d= -f2-
}

# Idempotently set KEY=VALUE in the env file (update in place or append).
set_env() {
  local key="$1" value="$2" file="${3:-$ENV_FILE}"
  touch "$file"
  if grep -qE "^${key}=" "$file"; then
    # Use '|' as the sed delimiter; escape any '|' and '&' in the value.
    local esc
    esc="$(printf '%s' "$value" | sed -e 's/[\&|]/\\&/g')"
    sed -i "s|^${key}=.*|${key}=${esc}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# --- Secret generators -------------------------------------------------------
rand_hex()   { openssl rand -hex "${1:-32}"; }                       # 2N hex chars
rand_b64url() { openssl rand -base64 "${1:-24}" | tr -d '\n' | tr '+/' '-_' | tr -d '='; }
rand_b64()   { openssl rand -base64 "${1:-32}" | tr -d '\n'; }
rand_uuid()  {
  if [ -r /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid
  else
    python3 -c 'import uuid; print(uuid.uuid4())'
  fi
}

# --- Health helpers ----------------------------------------------------------
# Wait until a compose service reports healthy (or running, if it has no
# healthcheck). Args: <service> [timeout_seconds]
wait_for_service() {
  local service="$1" timeout="${2:-300}" elapsed=0 cid status
  log "Waiting for service '${service}' to become healthy (timeout ${timeout}s)..."
  while [ "${elapsed}" -lt "${timeout}" ]; do
    cid="$(dc ps -q "${service}" 2>/dev/null || true)"
    if [ -n "${cid}" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${cid}" 2>/dev/null || echo "unknown")"
      case "${status}" in
        healthy|running)
          success "Service '${service}' is ${status}."
          return 0
          ;;
        unhealthy)
          warn "Service '${service}' reported unhealthy; still waiting..."
          ;;
      esac
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  error "Timed out waiting for '${service}' (last status: ${status:-none})."
  return 1
}
