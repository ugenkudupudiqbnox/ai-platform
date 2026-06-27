#!/usr/bin/env bash
# =============================================================================
# bootstrap-certs.sh — create a temporary self-signed certificate so NGINX can
# start before Let's Encrypt has issued a real one. Idempotent: if a certificate
# already exists at the target path it is left untouched.
#
# The cert is written into the shared `certbot_conf` Docker volume at
# /etc/letsencrypt/live/aiplatform/ (the fixed path referenced by ssl.conf).
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

BASE_DOMAIN="$(get_env BASE_DOMAIN || echo "example.com")"

log "Ensuring a bootstrap TLS certificate exists for *.${BASE_DOMAIN}..."

# The body runs INSIDE the certbot container; $BOOT_DOMAIN/$CERT_DIR must expand
# there, not on the host, so single quotes are intentional.
# shellcheck disable=SC2016
dc run --rm --no-deps -e BOOT_DOMAIN="${BASE_DOMAIN}" --entrypoint sh certbot -c '
  set -e
  CERT_DIR=/etc/letsencrypt/live/aiplatform
  if [ -f "$CERT_DIR/fullchain.pem" ] && [ -f "$CERT_DIR/privkey.pem" ]; then
    echo "Certificate already present at $CERT_DIR; nothing to do."
    exit 0
  fi
  command -v openssl >/dev/null 2>&1 || apk add --no-cache openssl >/dev/null 2>&1 || true
  mkdir -p "$CERT_DIR"
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out "$CERT_DIR/fullchain.pem" \
    -subj "/CN=${BOOT_DOMAIN}" \
    -addext "subjectAltName=DNS:chat.${BOOT_DOMAIN},DNS:auth.${BOOT_DOMAIN},DNS:flow.${BOOT_DOMAIN},DNS:trace.${BOOT_DOMAIN}"
  echo "Self-signed bootstrap certificate created at $CERT_DIR."
'

success "Bootstrap certificate ready."
