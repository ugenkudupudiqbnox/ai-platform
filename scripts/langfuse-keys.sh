#!/usr/bin/env bash
# =============================================================================
# langfuse-keys.sh — verify Langfuse is up and confirm the deterministic API
# keys are in effect, then ensure LangFlow's tracing variables mirror them.
#
# Langfuse is bootstrapped with fixed keys via LANGFUSE_INIT_* (see .env), so no
# scraping/headless login is required — the keys are known ahead of time and
# already wired into the LangFlow services. This script makes that wiring
# idempotent and validates connectivity.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

heading "Configuring Langfuse API keys for LangFlow tracing"

PUBLIC_KEY="$(get_env LANGFUSE_INIT_PROJECT_PUBLIC_KEY)"
SECRET_KEY="$(get_env LANGFUSE_INIT_PROJECT_SECRET_KEY)"

[ -n "${PUBLIC_KEY}" ] || die "LANGFUSE_INIT_PROJECT_PUBLIC_KEY is empty (run gen-secrets.sh)."
[ -n "${SECRET_KEY}" ] || die "LANGFUSE_INIT_PROJECT_SECRET_KEY is empty (run gen-secrets.sh)."

# Wait for Langfuse to report healthy.
wait_for_service langfuse-web 300 || warn "langfuse-web not healthy yet; keys still applied."

# Validate the public API responds.
if dc exec -T langfuse-web node -e \
   "require('http').get('http://localhost:3000/api/public/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))" 2>/dev/null; then
  success "Langfuse public API is responding."
else
  warn "Langfuse health check did not pass; continuing with key configuration."
fi

# Ensure the LangFlow tracing variables mirror the Langfuse project keys.
set_env LANGFUSE_PUBLIC_KEY "${PUBLIC_KEY}"
set_env LANGFUSE_SECRET_KEY "${SECRET_KEY}"
success "LangFlow tracing keys synchronized with the Langfuse project."

# Apply the (idempotent) environment to the running LangFlow services.
log "Recreating LangFlow services so tracing picks up the keys..."
dc up -d langflow langflow-worker
success "LangFlow tracing -> Langfuse configured."
echo
info "Langfuse project public key: ${PUBLIC_KEY}"
info "Langfuse host (internal):    $(get_env LANGFUSE_HOST)"
