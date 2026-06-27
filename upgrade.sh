#!/usr/bin/env bash
# =============================================================================
# upgrade.sh — pull newer images, rebuild local images, apply migrations and
# recreate services with zero manual steps. Run after editing image tags in .env.
#
#   sudo ./upgrade.sh
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

require_root
[ -f "${ENV_FILE}" ] || die ".env not found — run ./install.sh first."

heading "Upgrading the Enterprise AI Platform"

# Optional pre-upgrade backup (recommended).
if [ "${SKIP_BACKUP:-0}" != "1" ]; then
  log "Taking a pre-upgrade backup (set SKIP_BACKUP=1 to skip)..."
  bash "${REPO_ROOT}/scripts/backup.sh" || warn "Backup failed; continuing with upgrade."
fi

# Re-render config in case templates changed.
log "Re-rendering LibreChat configuration..."
render_librechat

log "Pulling updated images..."
dc pull --ignore-buildable || dc pull || warn "Some images could not be pulled."

log "Rebuilding local images (Keycloak, LangFlow)..."
dc build --pull

# Bring datastores up first so migrations can run against them.
log "Ensuring datastores are running..."
dc up -d postgres redis mongo clickhouse minio
wait_for_service postgres 240

# Keycloak, Langfuse and LangFlow apply their own migrations on startup.
log "Recreating services with the new images..."
dc up -d --remove-orphans

# Wait for the edge to be healthy as a smoke test.
wait_for_service keycloak 360 || warn "Keycloak not healthy after upgrade."
wait_for_service langfuse-web 360 || warn "Langfuse not healthy after upgrade."
wait_for_service nginx 120 || warn "NGINX not healthy after upgrade."

log "Pruning dangling images..."
docker image prune -f >/dev/null 2>&1 || true

heading "Running post-upgrade health check"
bash "${REPO_ROOT}/healthcheck.sh" || warn "Health check reported issues."

success "Upgrade complete."
