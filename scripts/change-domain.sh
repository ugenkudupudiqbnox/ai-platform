#!/usr/bin/env bash
# =============================================================================
# change-domain.sh — change the platform's base domain AFTER installation.
#
# Updates .env, re-renders the Keycloak realm, surgically updates the live
# Keycloak clients (redirect URIs / web origins / root URLs — no realm wipe),
# recreates the services that embed the hostname, and re-issues TLS certificates.
# NGINX needs no change (its server_name matching is domain-agnostic).
#
#   sudo ./scripts/change-domain.sh --domain new.example.com
#
# Flags:
#   --domain <d>   New base domain (prompted if omitted)
#   --skip-ssl     Don't re-issue Let's Encrypt certs (keep current/self-signed)
#   --yes          Don't prompt for confirmation
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_root
require_cmd envsubst
[ -f "${ENV_FILE}" ] || die ".env not found — run ./install.sh first."

NEW_DOMAIN=""; SKIP_SSL=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) NEW_DOMAIN="$2"; shift 2 ;;
    --skip-ssl) SKIP_SSL=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

OLD_DOMAIN="$(get_env BASE_DOMAIN || echo "")"
REALM="$(get_env KEYCLOAK_REALM || echo AIPlatform)"

if [ -z "${NEW_DOMAIN}" ]; then
  [ -t 0 ] || die "No --domain given and not interactive."
  read -r -p "New base domain (current: ${OLD_DOMAIN:-unset}): " NEW_DOMAIN
fi
[ -n "${NEW_DOMAIN}" ] || die "A new domain is required."
# Basic sanity: must look like a dotted hostname.
case "${NEW_DOMAIN}" in
  *.*) : ;;
  *) die "Domain '${NEW_DOMAIN}' does not look valid (expected something like ai.example.com)." ;;
esac

if [ "${NEW_DOMAIN}" = "${OLD_DOMAIN}" ]; then
  warn "New domain equals the current domain (${OLD_DOMAIN}); nothing to change."
  exit 0
fi

heading "Change domain: ${OLD_DOMAIN:-unset} -> ${NEW_DOMAIN}"
cat <<EOF
This will:
  - update BASE_DOMAIN and the chat/auth/flow/trace hostnames in .env
  - re-render the Keycloak realm template
  - update the live Keycloak clients (redirect URIs, web origins, root URLs)
  - recreate keycloak, oauth2-proxy, librechat, langfuse-web/worker, langflow/worker
  - re-issue TLS certificates for the new subdomains$([ "${SKIP_SSL}" -eq 1 ] && echo " (SKIPPED)")

New URLs will be:
  chat.${NEW_DOMAIN}  auth.${NEW_DOMAIN}  flow.${NEW_DOMAIN}  trace.${NEW_DOMAIN}

Make sure DNS A/AAAA records for those names point at this host.
EOF

if [ "${ASSUME_YES}" -ne 1 ]; then
  read -r -p "Proceed? Type 'yes' to continue: " confirm
  [ "${confirm}" = "yes" ] || die "Aborted."
fi

# --- 1. Update .env ----------------------------------------------------------
log "Updating .env..."
set_env BASE_DOMAIN "${NEW_DOMAIN}"
set_env CHAT_HOST  "chat.${NEW_DOMAIN}"
set_env AUTH_HOST  "auth.${NEW_DOMAIN}"
set_env FLOW_HOST  "flow.${NEW_DOMAIN}"
set_env TRACE_HOST "trace.${NEW_DOMAIN}"
chmod 600 "${ENV_FILE}"

# --- 2. Re-render the realm template (keeps the on-disk source in sync) -------
render_realm

# --- 3. Recreate Keycloak first so KC_HOSTNAME reflects the new auth host -----
log "Recreating Keycloak with the new hostname..."
dc up -d keycloak
wait_for_service keycloak 360

# --- 4. Update the live Keycloak clients via kcadm (non-destructive) ----------
KC_ADMIN_USER="$(get_env KC_BOOTSTRAP_ADMIN_USERNAME)"
KC_ADMIN_PASS="$(get_env KC_BOOTSTRAP_ADMIN_PASSWORD)"
KCADM="/opt/keycloak/bin/kcadm.sh"

log "Authenticating to Keycloak admin API..."
dc exec -T keycloak "${KCADM}" config credentials \
  --server http://localhost:8080 --realm master \
  --user "${KC_ADMIN_USER}" --password "${KC_ADMIN_PASS}" >/dev/null

# Update one client's URLs. Args: <clientId> <host> <callback-path>
update_client() {
  local client_id="$1" host="$2" callback="$3" cid
  cid="$(dc exec -T keycloak "${KCADM}" get clients -r "${REALM}" \
          -q "clientId=${client_id}" --fields id --format csv --noquotes 2>/dev/null \
        | tr -d '\r' | head -n1)"
  if [ -z "${cid}" ]; then
    warn "Client '${client_id}' not found in realm ${REALM}; skipping."
    return 0
  fi
  dc exec -T keycloak "${KCADM}" update "clients/${cid}" -r "${REALM}" \
    -s "rootUrl=https://${host}" \
    -s "baseUrl=https://${host}" \
    -s "redirectUris=[\"https://${host}${callback}\"]" \
    -s "webOrigins=[\"https://${host}\"]" \
    -s "attributes={\"post.logout.redirect.uris\":\"https://${host}/*\"}" >/dev/null
  success "Updated Keycloak client '${client_id}' -> https://${host}"
}

log "Updating Keycloak client redirect URIs..."
update_client librechat "chat.${NEW_DOMAIN}"  "/oauth/openid/callback"
update_client langflow  "flow.${NEW_DOMAIN}"  "/oauth2/callback"
update_client langfuse  "trace.${NEW_DOMAIN}" "/api/auth/callback/keycloak"

# --- 5. Recreate the services that embed the hostname ------------------------
log "Recreating dependent services with the new domain..."
dc up -d oauth2-proxy librechat langfuse-web langfuse-worker langflow langflow-worker

# --- 6. Re-issue TLS certificates -------------------------------------------
if [ "${SKIP_SSL}" -eq 0 ]; then
  heading "Re-issuing TLS certificates for *.${NEW_DOMAIN}"
  bash "${SCRIPT_DIR}/issue-certs.sh" || warn "Certificate issuance returned non-zero (continuing)."
else
  warn "Skipping TLS re-issuance (--skip-ssl). Run scripts/issue-certs.sh when DNS is ready."
fi

# --- 7. Health check ---------------------------------------------------------
heading "Verifying"
bash "${SCRIPT_DIR}/healthcheck.sh" || warn "Some health checks did not pass; review with 'make ps' / 'make logs'."

heading "Domain changed to ${NEW_DOMAIN}"
cat <<EOF

  New access URLs:
    Chat     : https://chat.${NEW_DOMAIN}
    Identity : https://auth.${NEW_DOMAIN}
    Flows    : https://flow.${NEW_DOMAIN}
    Tracing  : https://trace.${NEW_DOMAIN}

  If you skipped SSL or DNS wasn't ready, run:  sudo ./scripts/issue-certs.sh
EOF
success "Done."
