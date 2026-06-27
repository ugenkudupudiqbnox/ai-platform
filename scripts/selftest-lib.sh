#!/usr/bin/env bash
# =============================================================================
# selftest-lib.sh — shared helpers for the integration self-tests
# (install.selftest.sh / upgrade.selftest.sh).
#
# Not a *.selftest.sh itself, so the test runner never executes it directly; it
# is sourced by the integration tests. Provides st_make_sandbox, which copies the
# repo into a throwaway directory and installs mock `docker`/`systemctl`/`id`
# binaries on PATH so the orchestrators can run end-to-end without Docker/root.
# =============================================================================

# st_make_sandbox <repo_root> <work_dir>
# Sets globals: SANDBOX (repo copy), MOCKBIN, DOCKER_MOCK_LOG, SYSTEMCTL_MOCK_LOG.
# Prepends MOCKBIN to PATH (exported).
st_make_sandbox() {
  local repo_root="$1" work="$2"

  SANDBOX="${work}/repo"
  mkdir -p "${SANDBOX}"
  # Copy the repo, excluding VCS, generated/secret files and prior backups.
  tar -C "${repo_root}" \
      --exclude=./.git \
      --exclude=./backups \
      --exclude=./.env \
      --exclude=./docker/keycloak/realm.json \
      --exclude=./docker/librechat/librechat.yaml \
      -cf - . | tar -C "${SANDBOX}" -xf -
  mkdir -p "${SANDBOX}/backups"

  MOCKBIN="${work}/bin"
  mkdir -p "${MOCKBIN}"
  DOCKER_MOCK_LOG="${work}/docker.log";     : > "${DOCKER_MOCK_LOG}"
  SYSTEMCTL_MOCK_LOG="${work}/systemctl.log"; : > "${SYSTEMCTL_MOCK_LOG}"
  export DOCKER_MOCK_LOG SYSTEMCTL_MOCK_LOG

  # Mock docker: log the call; emit canned output for `inspect` and `ps -q`.
  cat > "${MOCKBIN}/docker" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${DOCKER_MOCK_LOG:-/dev/null}"
case "$1" in
  inspect)         echo "healthy" ;;
  --version|version) echo "Docker version mock" ;;
esac
case "$*" in
  *" ps "*) echo "mockcid000000" ;;
esac
exit 0
MOCK

  # Mock systemctl: log and succeed (so daemon-reload/enable are no-ops).
  cat > "${MOCKBIN}/systemctl" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${SYSTEMCTL_MOCK_LOG:-/dev/null}"
exit 0
MOCK

  # Mock id: report uid 0 so require_root passes without real root.
  cat > "${MOCKBIN}/id" <<'MOCK'
#!/usr/bin/env bash
echo 0
MOCK

  chmod +x "${MOCKBIN}"/docker "${MOCKBIN}"/systemctl "${MOCKBIN}"/id
  export PATH="${MOCKBIN}:${PATH}"
}
