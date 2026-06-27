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
#
# The functions below are sourced by scripts/change-domain.selftest.sh; the
# main routine only runs when this file is executed directly.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# Services that embed the public hostname and must be recreated on a change.
CD_SERVICES=(oauth2-proxy librechat langfuse-web langfuse-worker langflow langflow-worker)

# Validate a candidate domain. Returns non-zero (without exiting) on problems so
# callers/tests can react. Args: <new> <old>
cd_validate_domain() {
  local new="$1" old="${2:-}"
  if [ -z "${new}" ]; then
    warn "A new domain is required."
    return 1
  fi
  case "${new}" in
    *.*) : ;;
    *) warn "Domain '${new}' does not look valid (expected e.g. ai.example.com)."; return 1 ;;
  esac
  if [ "${new}" = "${old}" ]; then
    warn "New domain equals the current domain (${old}); nothing to change."
    return 2
  fi
  return 0
}

# Write the new base domain and derived hostnames into .env. Args: <new>
cd_update_env() {
  local new="$1"
  set_env BASE_DOMAIN "${new}"
  set_env CHAT_HOST  "chat.${new}"
  set_env AUTH_HOST  "auth.${new}"
  set_env FLOW_HOST  "flow.${new}"
  set_env TRACE_HOST "trace.${new}"
  chmod 600 "${ENV_FILE}" 2>/dev/null || true
}

# Authenticate kcadm against the master realm using the bootstrap admin.
cd_kcadm_login() {
  local user pass
  user="$(get_env KC_BOOTSTRAP_ADMIN_USERNAME)"
  pass="$(get_env KC_BOOTSTRAP_ADMIN_PASSWORD)"
  dc exec -T keycloak /opt/keycloak/bin/kcadm.sh config credentials \
    --server http://localhost:8080 --realm master \
    --user "${user}" --password "${pass}" >/dev/null
}

# Update one Keycloak client's URLs in place. Args: <clientId> <host> <callback>
cd_update_client() {
  local client_id="$1" host="$2" callback="$3" realm cid
  realm="$(get_env KEYCLOAK_REALM || echo AIPlatform)"
  cid="$(dc exec -T keycloak /opt/keycloak/bin/kcadm.sh get clients -r "${realm}" \
          -q "clientId=${client_id}" --fields id --format csv --noquotes 2>/dev/null \
        | tr -d '\r' | head -n1)"
  if [ -z "${cid}" ]; then
    warn "Client '${client_id}' not found in realm ${realm}; skipping."
    return 0
  fi
  dc exec -T keycloak /opt/keycloak/bin/kcadm.sh update "clients/${cid}" -r "${realm}" \
    -s "rootUrl=https://${host}" \
    -s "baseUrl=https://${host}" \
    -s "redirectUris=[\"https://${host}${callback}\"]" \
    -s "webOrigins=[\"https://${host}\"]" \
    -s "attributes={\"post.logout.redirect.uris\":\"https://${host}/*\"}" >/dev/null
  success "Updated Keycloak client '${client_id}' -> https://${host}"
}

# Update all three OIDC clients for the new domain. Args: <new>
cd_update_all_clients() {
  local new="$1"
  cd_update_client librechat "chat.${new}"  "/oauth/openid/callback"
  cd_update_client langflow  "flow.${new}"  "/oauth2/callback"
  cd_update_client langfuse  "trace.${new}" "/api/auth/callback/keycloak"
}

# Recreate the services that bake in the hostname so they re-read .env.
cd_recreate_services() {
  dc up -d "${CD_SERVICES[@]}"
}

cd_main() {
  require_root
  require_cmd envsubst
  [ -f "${ENV_FILE}" ] || die ".env not found — run ./install.sh first."

  local new_domain="" skip_ssl=0 assume_yes=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) new_domain="$2"; shift 2 ;;
      --skip-ssl) skip_ssl=1; shift ;;
      --yes|-y) assume_yes=1; shift ;;
      -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done

  local old_domain; old_domain="$(get_env BASE_DOMAIN || echo "")"

  if [ -z "${new_domain}" ]; then
    [ -t 0 ] || die "No --domain given and not interactive."
    read -r -p "New base domain (current: ${old_domain:-unset}): " new_domain
  fi

  local vrc=0
  cd_validate_domain "${new_domain}" "${old_domain}" || vrc=$?
  [ "${vrc}" -eq 2 ] && exit 0       # no-op (same domain)
  [ "${vrc}" -ne 0 ] && die "Invalid domain."

  heading "Change domain: ${old_domain:-unset} -> ${new_domain}"
  cat <<EOF
This will:
  - update BASE_DOMAIN and the chat/auth/flow/trace hostnames in .env
  - re-render the Keycloak realm template
  - update the live Keycloak clients (redirect URIs, web origins, root URLs)
  - recreate: ${CD_SERVICES[*]}
  - re-issue TLS certificates for the new subdomains$([ "${skip_ssl}" -eq 1 ] && echo " (SKIPPED)")

New URLs: chat/auth/flow/trace.${new_domain}
Make sure DNS A/AAAA records for those names point at this host.
EOF

  if [ "${assume_yes}" -ne 1 ]; then
    read -r -p "Proceed? Type 'yes' to continue: " confirm
    [ "${confirm}" = "yes" ] || die "Aborted."
  fi

  log "Updating .env..."
  cd_update_env "${new_domain}"

  render_realm

  log "Recreating Keycloak with the new hostname..."
  dc up -d keycloak
  wait_for_service keycloak 360

  log "Authenticating to Keycloak admin API..."
  cd_kcadm_login
  log "Updating Keycloak client redirect URIs..."
  cd_update_all_clients "${new_domain}"

  log "Recreating dependent services with the new domain..."
  cd_recreate_services

  if [ "${skip_ssl}" -eq 0 ]; then
    heading "Re-issuing TLS certificates for *.${new_domain}"
    bash "${REPO_ROOT}/scripts/issue-certs.sh" || warn "Certificate issuance returned non-zero (continuing)."
  else
    warn "Skipping TLS re-issuance (--skip-ssl). Run scripts/issue-certs.sh when DNS is ready."
  fi

  heading "Verifying"
  bash "${REPO_ROOT}/healthcheck.sh" || warn "Some health checks did not pass; review with 'make ps'."

  heading "Domain changed to ${new_domain}"
  cat <<EOF

  New access URLs:
    Chat     : https://chat.${new_domain}
    Identity : https://auth.${new_domain}
    Flows    : https://flow.${new_domain}
    Tracing  : https://trace.${new_domain}

  If you skipped SSL or DNS wasn't ready, run:  sudo ./scripts/issue-certs.sh
EOF
  success "Done."
}

# Only run main when executed directly (not when sourced by the self-test).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  cd_main "$@"
fi
