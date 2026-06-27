#!/usr/bin/env bash
# =============================================================================
# uninstall.sh — stop and remove the platform.
#
#   sudo ./uninstall.sh            # stop & remove containers/networks (keep data)
#   sudo ./uninstall.sh --volumes  # ALSO delete all data volumes (DESTRUCTIVE)
#   sudo ./uninstall.sh --purge    # volumes + rendered config + systemd timers
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

require_root

REMOVE_VOLUMES=0
PURGE=0
case "${1:-}" in
  --volumes) REMOVE_VOLUMES=1 ;;
  --purge)   REMOVE_VOLUMES=1; PURGE=1 ;;
  "") ;;
  *) die "Unknown argument: $1 (use --volumes or --purge)" ;;
esac

heading "Uninstalling the Enterprise AI Platform"

if [ "${REMOVE_VOLUMES}" -eq 1 ]; then
  warn "This will permanently DELETE all platform data (databases, traces, files)."
  read -r -p "Type 'DELETE' to confirm volume removal: " confirm
  [ "${confirm}" = "DELETE" ] || die "Aborted."
fi

# Remove systemd timers if present.
if command -v systemctl >/dev/null 2>&1; then
  for unit in aiplatform-certbot-renew aiplatform-backup; do
    systemctl disable --now "${unit}.timer" >/dev/null 2>&1 || true
  done
  if [ "${PURGE}" -eq 1 ]; then
    rm -f /etc/systemd/system/aiplatform-certbot-renew.{service,timer} \
          /etc/systemd/system/aiplatform-backup.{service,timer}
    systemctl daemon-reload >/dev/null 2>&1 || true
    log "Removed systemd units."
  fi
fi

# Tear down containers (and the tools/monitoring profiles too).
log "Stopping and removing containers and networks..."
if [ "${REMOVE_VOLUMES}" -eq 1 ]; then
  dc --profile tools --profile monitoring down --volumes --remove-orphans
  success "Containers, networks and volumes removed."
else
  dc --profile tools --profile monitoring down --remove-orphans
  success "Containers and networks removed (data volumes preserved)."
fi

if [ "${PURGE}" -eq 1 ]; then
  log "Removing rendered configuration..."
  rm -f "${REPO_ROOT}/docker/keycloak/realm.json" \
        "${REPO_ROOT}/docker/librechat/librechat.yaml"
  warn "Left ${ENV_FILE} in place (contains your secrets). Remove it manually if desired."
fi

success "Uninstall complete."
