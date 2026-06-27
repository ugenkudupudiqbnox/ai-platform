#!/usr/bin/env bash
# =============================================================================
# langfuse-keys.selftest.sh — offline self-test for langfuse-keys.sh.
#
# Runs the real script against a throwaway .env with a controllable `docker`
# mock. Asserts: the deterministic Langfuse keys are mirrored into the LangFlow
# tracing vars, LangFlow is recreated, a missing init key fails with a helpful
# message, and a failing Langfuse health probe is non-fatal. Requires no Docker.
#
# Run with: ./scripts/langfuse-keys.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SELFTEST_DIR}/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

ENVF="${WORK}/.env"
OUT="${WORK}/out"
LF_MOCK_LOG="${WORK}/docker.log"
export LF_MOCK_LOG

MOCKBIN="${WORK}/bin"; mkdir -p "${MOCKBIN}"
cat > "${MOCKBIN}/docker" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${LF_MOCK_LOG:-/dev/null}"
case "$1" in
  inspect) echo "healthy"; exit 0 ;;
esac
case "$*" in
  *" ps -q "*) echo "cid-mock" ;;
  *"exec -T langfuse-web node"*) [ -n "${LF_HEALTH_BAD:-}" ] && exit 1 || exit 0 ;;
esac
exit 0
MOCK
printf '#!/usr/bin/env bash\nexit 0\n' > "${MOCKBIN}/sleep"
chmod +x "${MOCKBIN}/docker" "${MOCKBIN}/sleep"

write_env_full() {
  cat > "${ENVF}" <<'EOF'
COMPOSE_PROJECT_NAME=testproj
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-test-public
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-test-secret
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://langfuse-web:3000
EOF
}

run_lf() { # <env-assignments...>
  : > "${LF_MOCK_LOG}"
  env "$@" ENV_FILE="${ENVF}" PATH="${MOCKBIN}:${PATH}" \
    bash "${REPO}/scripts/langfuse-keys.sh" >"${OUT}" 2>&1
}

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_rc()   { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
env_grep()    { if grep -qE -- "$1" "${ENVF}"; then ok "$2"; else bad "$2 (missing /$1/ in .env)"; fi; }
log_grep()    { if grep -qF -- "$1" "${LF_MOCK_LOG}"; then ok "$2"; else bad "$2 (missing '$1' in docker log)"; fi; }
out_grep()    { if grep -qF -- "$1" "${OUT}"; then ok "$2"; else bad "$2 (missing '$1' in output)"; fi; }

echo "== langfuse-keys.sh self-test =="

# --- 1. Happy path -----------------------------------------------------------
echo "[1] keys mirrored + LangFlow recreated"
write_env_full
run_lf ; rc=$?
assert_rc 0 "exits 0" "${rc}"
env_grep '^LANGFUSE_PUBLIC_KEY=pk-lf-test-public$' "public key mirrored into tracing var"
env_grep '^LANGFUSE_SECRET_KEY=sk-lf-test-secret$' "secret key mirrored into tracing var"
log_grep "up -d langflow langflow-worker" "LangFlow services recreated"
log_grep "exec -T langfuse-web node" "Langfuse health probe attempted"
out_grep "LangFlow tracing -> Langfuse configured." "reports success"
out_grep "pk-lf-test-public" "prints the project public key"

# --- 2. Missing init key -----------------------------------------------------
echo "[2] missing init key"
cat > "${ENVF}" <<'EOF'
COMPOSE_PROJECT_NAME=testproj
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-test-secret
LANGFUSE_HOST=http://langfuse-web:3000
EOF
run_lf ; rc=$?
if [ "${rc}" -ne 0 ]; then ok "fails when the public key is absent (rc=${rc})"; else bad "did not fail on missing key"; fi
out_grep "run gen-secrets.sh" "gives a helpful remediation message"

# --- 3. Health probe failure is non-fatal ------------------------------------
echo "[3] health probe failure is non-fatal"
write_env_full
run_lf LF_HEALTH_BAD=1 ; rc=$?
assert_rc 0 "exits 0 even when the health probe fails" "${rc}"
env_grep '^LANGFUSE_PUBLIC_KEY=pk-lf-test-public$' "keys still synced after probe failure"
out_grep "health check did not pass" "warns about the failed probe"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
