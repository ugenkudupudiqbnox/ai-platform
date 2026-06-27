#!/usr/bin/env bash
# =============================================================================
# install.sh — one-command installer for the Enterprise AI Platform.
# Target: a fresh Ubuntu Server 24.04 LTS host.
#
#   sudo ./install.sh --domain example.com --email admin@example.com
#
# Flags (all optional; values fall back to prompts / .env / sane defaults):
#   --domain <d>   Base domain (subdomains chat./auth./flow./trace. are derived)
#   --email <e>    Email for Let's Encrypt registration
#   --staging      Use the Let's Encrypt staging CA (testing)
#   --skip-ssl     Skip Let's Encrypt issuance (keep self-signed certs)
#   --skip-deps    Do not install OS packages (Docker etc. assumed present)
#   --force-secrets Regenerate ALL secrets even if .env already has values
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

# --- Parse arguments ---------------------------------------------------------
ARG_DOMAIN=""; ARG_EMAIL=""; ARG_STAGING=""; SKIP_SSL=0; SKIP_DEPS=0; FORCE_SECRETS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) ARG_DOMAIN="$2"; shift 2 ;;
    --email)  ARG_EMAIL="$2";  shift 2 ;;
    --staging) ARG_STAGING="1"; shift ;;
    --skip-ssl) SKIP_SSL=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --force-secrets) FORCE_SECRETS=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

require_root

heading "Enterprise AI Platform — Installer"

# --- 1. Detect OS ------------------------------------------------------------
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "Detected OS: ${PRETTY_NAME:-unknown}"
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    warn "This installer targets Ubuntu 24.04 LTS. Continuing on ${PRETTY_NAME:-this OS} (unsupported)."
  fi
else
  warn "Could not read /etc/os-release; assuming a compatible Linux host."
fi

# --- 2. Install prerequisites ------------------------------------------------
# NOTE: NGINX and Certbot run as CONTAINERS in this platform (they would
# otherwise conflict with the host on ports 80/443). We only install Docker and
# a few CLI utilities on the host.
install_prereqs() {
  log "Installing host prerequisites (Docker, openssl, curl, jq, gettext)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg openssl jq gettext-base

  if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine + Compose plugin via get.docker.com..."
    curl -fsSL https://get.docker.com | sh
  else
    log "Docker already installed: $(docker --version)"
  fi

  if ! docker compose version >/dev/null 2>&1; then
    log "Installing the Docker Compose plugin..."
    apt-get install -y --no-install-recommends docker-compose-plugin
  fi

  systemctl enable --now docker >/dev/null 2>&1 || true
}

if [ "${SKIP_DEPS}" -eq 0 ]; then
  install_prereqs
else
  log "Skipping OS package installation (--skip-deps)."
fi

require_cmd docker
docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is required."
require_cmd openssl
require_cmd envsubst

# --- 3. Resolve domain / email -----------------------------------------------
# Precedence: flag > existing .env > prompt > default.
EXISTING_DOMAIN="$(get_env BASE_DOMAIN 2>/dev/null || echo "")"
BASE_DOMAIN="${ARG_DOMAIN:-${EXISTING_DOMAIN}}"
if [ -z "${BASE_DOMAIN}" ] || [ "${BASE_DOMAIN}" = "example.com" ]; then
  if [ -t 0 ]; then
    read -r -p "Enter the base domain (e.g. ai.acme.com) [example.com]: " BASE_DOMAIN
  fi
  BASE_DOMAIN="${BASE_DOMAIN:-example.com}"
fi

EXISTING_EMAIL="$(get_env ACME_EMAIL 2>/dev/null || echo "")"
ACME_EMAIL="${ARG_EMAIL:-${EXISTING_EMAIL}}"
if [ -z "${ACME_EMAIL}" ] || [ "${ACME_EMAIL}" = "admin@example.com" ]; then
  if [ -t 0 ]; then
    read -r -p "Enter the Let's Encrypt email [admin@${BASE_DOMAIN}]: " ACME_EMAIL
  fi
  ACME_EMAIL="${ACME_EMAIL:-admin@${BASE_DOMAIN}}"
fi

# --- 4. Generate .env + secrets ----------------------------------------------
heading "Step 1/9 — Generating configuration and secrets"
if [ "${FORCE_SECRETS}" -eq 1 ]; then
  bash "${REPO_ROOT}/scripts/gen-secrets.sh" --force
else
  bash "${REPO_ROOT}/scripts/gen-secrets.sh"
fi

# Persist domain/email and derive per-service hostnames.
set_env BASE_DOMAIN "${BASE_DOMAIN}"
set_env ACME_EMAIL  "${ACME_EMAIL}"
set_env CHAT_HOST   "chat.${BASE_DOMAIN}"
set_env AUTH_HOST   "auth.${BASE_DOMAIN}"
set_env FLOW_HOST   "flow.${BASE_DOMAIN}"
set_env TRACE_HOST  "trace.${BASE_DOMAIN}"
[ -n "${ARG_STAGING}" ] && set_env ACME_STAGING "1"
# Align service-account emails with the chosen domain.
set_env KEYCLOAK_SEED_ADMIN_EMAIL     "admin@${BASE_DOMAIN}"
set_env KEYCLOAK_SEED_DEVELOPER_EMAIL "dev@${BASE_DOMAIN}"
set_env KEYCLOAK_SEED_USER_EMAIL      "user@${BASE_DOMAIN}"
set_env LANGFUSE_INIT_USER_EMAIL      "admin@${BASE_DOMAIN}"
chmod 600 "${ENV_FILE}"

# --- 5. Render templated configuration ---------------------------------------
heading "Step 2/9 — Rendering service configuration"
render_realm
render_librechat

# --- 6. Folders & permissions ------------------------------------------------
mkdir -p "${REPO_ROOT}/backups"
chmod +x "${REPO_ROOT}"/scripts/*.sh "${REPO_ROOT}"/*.sh 2>/dev/null || true
chmod +x "${REPO_ROOT}/docker/postgres/init/"*.sh 2>/dev/null || true

# --- 7. Build & pull images --------------------------------------------------
heading "Step 3/9 — Building and pulling container images"
dc build
dc pull --ignore-buildable || dc pull || warn "Some images could not be pre-pulled (will pull on start)."

# --- 8. Start datastores -----------------------------------------------------
heading "Step 4/9 — Starting datastores"
dc up -d postgres redis mongo clickhouse minio
wait_for_service postgres 240
wait_for_service redis 120
wait_for_service mongo 180
wait_for_service clickhouse 240
log "Creating MinIO bucket..."
dc up -d minio-init

# --- 9. Identity (Keycloak: migrate + import realm) --------------------------
heading "Step 5/9 — Starting Keycloak (DB migration + realm import)"
dc up -d keycloak
wait_for_service keycloak 360

# --- 10. Langfuse (migrate + bootstrap keys) ---------------------------------
heading "Step 6/9 — Starting Langfuse (migrations + API keys)"
dc up -d langfuse-web langfuse-worker
wait_for_service langfuse-web 360
bash "${REPO_ROOT}/scripts/langfuse-keys.sh"

# --- 11. LangFlow + LibreChat + edge auth ------------------------------------
heading "Step 7/9 — Starting LangFlow, LibreChat and edge auth"
dc up -d langflow langflow-worker flower librechat oauth2-proxy
wait_for_service langflow 360
wait_for_service librechat 240

# --- 12. TLS bootstrap + NGINX ----------------------------------------------
heading "Step 8/9 — Bootstrapping TLS and starting NGINX"
bash "${REPO_ROOT}/scripts/bootstrap-certs.sh"
dc up -d nginx
wait_for_service nginx 120

if [ "${SKIP_SSL}" -eq 0 ]; then
  bash "${REPO_ROOT}/scripts/issue-certs.sh" || warn "SSL issuance step returned non-zero (continuing)."
else
  warn "Skipping Let's Encrypt issuance (--skip-ssl); using self-signed certs."
fi

# --- 13. Schedule renewal + backups (systemd) --------------------------------
install_timers() {
  command -v systemctl >/dev/null 2>&1 || { warn "systemd not found; skipping timers."; return 0; }
  log "Installing systemd timers for certificate renewal and daily backups..."

  # Unit directory (overridable for testing).
  local sd="${SYSTEMD_DIR:-/etc/systemd/system}"
  mkdir -p "${sd}"

  cat > "${sd}"/aiplatform-certbot-renew.service <<EOF
[Unit]
Description=AI Platform — renew Let's Encrypt certificates
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
ExecStart=/usr/bin/docker compose --env-file ${ENV_FILE} run --rm certbot renew --webroot --webroot-path /var/www/certbot --quiet
ExecStartPost=/usr/bin/docker compose --env-file ${ENV_FILE} exec -T nginx nginx -s reload
EOF

  cat > "${sd}"/aiplatform-certbot-renew.timer <<EOF
[Unit]
Description=AI Platform — daily certificate renewal

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  cat > "${sd}"/aiplatform-backup.service <<EOF
[Unit]
Description=AI Platform — full backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
ExecStart=${REPO_ROOT}/scripts/backup.sh
EOF

  cat > "${sd}"/aiplatform-backup.timer <<EOF
[Unit]
Description=AI Platform — daily backup

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now aiplatform-certbot-renew.timer >/dev/null 2>&1 || true
  systemctl enable --now aiplatform-backup.timer >/dev/null 2>&1 || true
  success "Timers installed (certificate renewal + daily backups)."
}
install_timers

# --- 14. Health check --------------------------------------------------------
heading "Step 9/9 — Running health checks"
bash "${REPO_ROOT}/healthcheck.sh" || warn "Some health checks did not pass; review with 'make ps' / 'make logs'."

# --- 15. Summary -------------------------------------------------------------
print_summary() {
  heading "Installation complete"
  cat <<EOF

  Access URLs (ensure DNS A/AAAA records point chat/auth/flow/trace.${BASE_DOMAIN} here):

    Chat (LibreChat)     : https://${CHAT_HOST:-chat.${BASE_DOMAIN}}
    Identity (Keycloak)  : https://${AUTH_HOST:-auth.${BASE_DOMAIN}}
    Flows (LangFlow)     : https://${FLOW_HOST:-flow.${BASE_DOMAIN}}
    Tracing (Langfuse)   : https://${TRACE_HOST:-trace.${BASE_DOMAIN}}
    Queue (Flower)       : reachable on the internal network (see docs/scaling.md)

  Credentials (also stored in ${ENV_FILE}, permissions 600):

    Keycloak admin console
      username : $(get_env KC_BOOTSTRAP_ADMIN_USERNAME)
      password : $(get_env KC_BOOTSTRAP_ADMIN_PASSWORD)

    Platform admin (SSO user — realm ${KEYCLOAK_REALM:-AIPlatform})
      username : $(get_env KEYCLOAK_SEED_ADMIN_USERNAME)
      password : $(get_env KEYCLOAK_SEED_ADMIN_PASSWORD)

    Langfuse admin
      email    : $(get_env LANGFUSE_INIT_USER_EMAIL)
      password : $(get_env LANGFUSE_INIT_USER_PASSWORD)

    LangFlow superuser
      username : $(get_env LANGFLOW_SUPERUSER)
      password : $(get_env LANGFLOW_SUPERUSER_PASSWORD)

    Flower (Celery dashboard)
      username : $(get_env FLOWER_USER)
      password : $(get_env FLOWER_PASSWORD)

  Next steps:
    - If SSL was skipped or failed, fix DNS then run: sudo ./scripts/issue-certs.sh
    - Manage the stack with: make ps | make logs | make health
    - Read the docs in ./docs (start with docs/installation.md)

EOF
}
print_summary
success "All done."
