#!/usr/bin/env bash
# =============================================================================
# cert-helpers.selftest.sh — offline self-tests for the TLS helper scripts
# bootstrap-certs.sh and issue-certs.sh.
#
# Both are thin wrappers around `dc run certbot ...`; this runs them for real
# with a `docker` mock that records the constructed command and can simulate a
# certbot failure. Asserts the self-signed bootstrap invocation, and issue-certs'
# skip / production / staging / graceful-failure behaviour. Requires no Docker.
#
# Run with: ./scripts/cert-helpers.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SELFTEST_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

ENVF="${WORK}/.env"
OUT="${WORK}/out"
CERT_MOCK_LOG="${WORK}/docker.log"
export CERT_MOCK_LOG

# docker mock: record the call; fail `certonly` when ISSUE_FAIL is set.
MOCKBIN="${WORK}/bin"; mkdir -p "${MOCKBIN}"
cat > "${MOCKBIN}/docker" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${CERT_MOCK_LOG:-/dev/null}"
case "$*" in
  *certonly*) [ -n "${ISSUE_FAIL:-}" ] && exit 1 || exit 0 ;;
esac
exit 0
MOCK
chmod +x "${MOCKBIN}/docker"

write_env() { # <base_domain> <acme_email> <acme_staging>
  cat > "${ENVF}" <<EOF
COMPOSE_PROJECT_NAME=testproj
BASE_DOMAIN=$1
ACME_EMAIL=$2
ACME_STAGING=$3
EOF
}

run() { # <extra-env...> -- <script> [args]
  : > "${CERT_MOCK_LOG}"
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  env "${envs[@]}" ENV_FILE="${ENVF}" PATH="${MOCKBIN}:${PATH}" bash "$@" >"${OUT}" 2>&1
}

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_rc()    { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
log_grep()     { if grep -qF -- "$1" "${CERT_MOCK_LOG}"; then ok "$2"; else bad "$2 (missing '$1' in docker log)"; fi; }
log_nogrep()   { if grep -qF -- "$1" "${CERT_MOCK_LOG}"; then bad "$2 (unexpected '$1' in docker log)"; else ok "$2"; fi; }
out_grep()     { if grep -qF -- "$1" "${OUT}"; then ok "$2"; else bad "$2 (missing '$1' in output)"; fi; }

echo "== cert-helpers self-test =="

# --- 1. bootstrap-certs.sh ---------------------------------------------------
echo "[1] bootstrap-certs.sh"
write_env "test.example.com" "admin@test.example.com" "0"
run -- "${REPO}/scripts/bootstrap-certs.sh"; rc=$?
assert_rc 0 "bootstrap-certs exits 0" "${rc}"
log_grep "run --rm --no-deps" "runs a one-off certbot container"
log_grep "BOOT_DOMAIN=test.example.com" "passes the base domain to the container"
log_grep "--entrypoint sh certbot" "overrides entrypoint to a shell on the certbot image"
# The literal ${BOOT_DOMAIN} is expected here — it is expanded inside the
# container at runtime, not by the host, so it appears verbatim in the command.
# shellcheck disable=SC2016
log_grep 'subjectAltName=DNS:chat.${BOOT_DOMAIN},DNS:auth.${BOOT_DOMAIN},DNS:flow.${BOOT_DOMAIN},DNS:trace.${BOOT_DOMAIN}' \
  "self-signed cert covers all four subdomains"
out_grep "Bootstrap certificate ready." "reports completion"

# --- 2. issue-certs.sh: skipped for placeholder domain -----------------------
echo "[2] issue-certs.sh — skip on example.com"
write_env "example.com" "admin@example.com" "0"
run -- "${REPO}/scripts/issue-certs.sh"; rc=$?
assert_rc 0 "exits 0 without issuing" "${rc}"
log_nogrep "certonly" "does not call certbot for the placeholder domain"
out_grep "skipping Let's Encrypt issuance" "explains the skip"

# --- 3. issue-certs.sh: production issuance + nginx reload --------------------
echo "[3] issue-certs.sh — production"
write_env "test.example.com" "admin@test.example.com" "0"
run -- "${REPO}/scripts/issue-certs.sh"; rc=$?
assert_rc 0 "exits 0 on success" "${rc}"
log_grep "certonly --webroot --webroot-path /var/www/certbot" "uses the webroot challenge"
log_grep "--cert-name aiplatform" "uses the fixed cert name"
log_grep "-d chat.test.example.com" "requests chat subdomain"
log_grep "-d auth.test.example.com" "requests auth subdomain"
log_grep "-d flow.test.example.com" "requests flow subdomain"
log_grep "-d trace.test.example.com" "requests trace subdomain"
log_grep "--email admin@test.example.com" "passes the ACME email"
log_grep "--keep-until-expiring" "is idempotent across renewals"
log_nogrep "--staging" "uses the production CA when ACME_STAGING=0"
log_grep "nginx -s reload" "reloads NGINX after issuance"
out_grep "Certificate issued/renewed." "reports success"

# --- 4. issue-certs.sh: staging ----------------------------------------------
echo "[4] issue-certs.sh — staging"
write_env "test.example.com" "admin@test.example.com" "1"
run -- "${REPO}/scripts/issue-certs.sh"; rc=$?
assert_rc 0 "exits 0 in staging mode" "${rc}"
log_grep "--staging" "uses the staging CA when ACME_STAGING=1"

# --- 5. issue-certs.sh: graceful failure -------------------------------------
echo "[5] issue-certs.sh — certbot failure is non-fatal"
write_env "test.example.com" "admin@test.example.com" "0"
run ISSUE_FAIL=1 -- "${REPO}/scripts/issue-certs.sh"; rc=$?
assert_rc 0 "exits 0 even when issuance fails (keeps self-signed)" "${rc}"
log_grep "certonly" "certbot was attempted"
log_nogrep "nginx -s reload" "does not reload NGINX on failure"
out_grep "Certificate issuance failed" "reports the failure"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
