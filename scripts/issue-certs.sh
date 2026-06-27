#!/usr/bin/env bash
# =============================================================================
# issue-certs.sh — obtain (or renew) a Let's Encrypt certificate covering all
# four platform subdomains via the HTTP-01 webroot challenge, then reload NGINX.
#
# Non-fatal: if issuance fails (e.g. DNS not yet pointed at this host) the
# self-signed bootstrap certificate is kept and a warning is printed, so the
# overall install still succeeds. Re-run this script once DNS is correct.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

BASE_DOMAIN="$(get_env BASE_DOMAIN || echo "")"
ACME_EMAIL="$(get_env ACME_EMAIL || echo "")"
ACME_STAGING="$(get_env ACME_STAGING || echo "0")"

if [ -z "${BASE_DOMAIN}" ] || [ "${BASE_DOMAIN}" = "example.com" ]; then
  warn "BASE_DOMAIN is unset or still 'example.com'; skipping Let's Encrypt issuance."
  warn "The platform will continue with the self-signed bootstrap certificate."
  exit 0
fi

STAGING_FLAG=""
if [ "${ACME_STAGING}" = "1" ]; then
  STAGING_FLAG="--staging"
  warn "ACME_STAGING=1 — using the Let's Encrypt STAGING CA (untrusted certs)."
fi

log "Requesting certificate for chat/auth/flow/trace.${BASE_DOMAIN}..."

if dc run --rm certbot certonly \
      --webroot --webroot-path /var/www/certbot \
      --cert-name aiplatform \
      -d "chat.${BASE_DOMAIN}" \
      -d "auth.${BASE_DOMAIN}" \
      -d "flow.${BASE_DOMAIN}" \
      -d "trace.${BASE_DOMAIN}" \
      --email "${ACME_EMAIL}" \
      --agree-tos --no-eff-email --non-interactive \
      --keep-until-expiring \
      ${STAGING_FLAG}; then
  success "Certificate issued/renewed."
  if dc exec -T nginx nginx -s reload 2>/dev/null; then
    success "NGINX reloaded with the new certificate."
  else
    warn "Could not reload NGINX automatically; run 'make reload-nginx'."
  fi
else
  warn "Certificate issuance failed (check DNS A/AAAA records and port 80 reachability)."
  warn "Keeping the self-signed certificate. Re-run: sudo ./scripts/issue-certs.sh"
fi
