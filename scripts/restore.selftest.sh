#!/usr/bin/env bash
# =============================================================================
# restore.selftest.sh — offline self-test for restore.sh.
#
# Sources restore.sh (functions only; main is guarded), mocks `dc`/`docker` and
# helpers, builds a fake snapshot directory, then asserts: source-path
# resolution (relative/absolute/missing/empty), per-database psql restore,
# MongoDB restore (present + skipped), and volume restores (present + skipped).
# Requires no Docker.
#
# Run with: ./scripts/restore.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

export ENV_FILE="${WORK}/.env"
cat > "${ENV_FILE}" <<'EOF'
COMPOSE_PROJECT_NAME=testproj
POSTGRES_SUPER_USER=postgres
POSTGRES_SUPER_PASSWORD=pgpw
MONGO_INITDB_ROOT_USERNAME=librechat
MONGO_INITDB_ROOT_PASSWORD=mongopw
REDIS_PASSWORD=redispw
EOF

# shellcheck source=restore.sh
source "${SELFTEST_DIR}/restore.sh"
set +e   # the harness controls flow, not `set -e` inherited from restore.sh

REPO_ROOT="${WORK}"
DC_LOG="${WORK}/dc.log"
DOCKER_LOG="${WORK}/docker.log"
: > "${DC_LOG}"; : > "${DOCKER_LOG}"

# Mock `dc`: log the call; consume piped stdin (the gunzip output) when present.
dc() {
  echo "dc $*" >> "${DC_LOG}"
  if [ ! -t 0 ]; then cat >/dev/null 2>&1; fi
  return 0
}
# Mock `docker`: log the call only.
docker() { echo "docker $*" >> "${DOCKER_LOG}"; return 0; }
# Neutralize side-effecting helpers.
wait_for_service() { :; }
require_root() { :; }

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_eq()   { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$1', got '$2')"; fi; }
assert_rc()   { if [ "$3" = "$1" ]; then ok "$2 (rc=$3)"; else bad "$2 (expected rc $1, got $3)"; fi; }
assert_grep() { if grep -qF -- "$1" "$2"; then ok "$3"; else bad "$3 (missing '$1' in $2)"; fi; }
assert_nogrep(){ if grep -qF -- "$1" "$2"; then bad "$3 (unexpected '$1' in $2)"; else ok "$3"; fi; }

# Build a fake snapshot directory.
SNAP="${WORK}/backups/20260101-000000"
mkdir -p "${SNAP}"
echo "-- keycloak dump" | gzip > "${SNAP}/postgres-keycloak.sql.gz"
echo "-- langfuse dump" | gzip > "${SNAP}/postgres-langfuse.sql.gz"
echo "-- mongo dump"    | gzip > "${SNAP}/mongo.archive.gz"
echo "redis"            | gzip > "${SNAP}/redis-data.tar.gz"
echo "clickhouse"       | gzip > "${SNAP}/clickhouse-data.tar.gz"
# Intentionally NOT present: minio-data / langflow-data / librechat-* archives.

echo "== restore.sh self-test =="

# --- 1. Source-path resolution -----------------------------------------------
echo "[1] source path resolution"
rc=0; rs_resolve_src "backups/20260101-000000" >/dev/null 2>&1 || rc=$?
assert_rc 0 "relative path resolves" "${rc}"
assert_eq "${SNAP}" "${SRC}" "relative path resolved under REPO_ROOT"
rc=0; rs_resolve_src "${SNAP}" >/dev/null 2>&1 || rc=$?
assert_rc 0 "absolute path accepted" "${rc}"
assert_eq "${SNAP}" "${SRC}" "absolute path kept as-is"
rc=0; rs_resolve_src "${WORK}/does-not-exist" >/dev/null 2>&1 || rc=$?
assert_rc 1 "missing directory rejected" "${rc}"
rc=0; rs_resolve_src "" >/dev/null 2>&1 || rc=$?
assert_rc 2 "empty argument rejected" "${rc}"

# Lock SRC to the snapshot and init project for the remaining tests.
rs_resolve_src "${SNAP}" >/dev/null 2>&1
rs_init
assert_eq "testproj" "${PROJECT}" "project name read from env"

# --- 2. PostgreSQL restore ---------------------------------------------------
echo "[2] postgres restore"
: > "${DC_LOG}"
rs_restore_postgres >/dev/null 2>&1
assert_grep "psql -U postgres -d keycloak" "${DC_LOG}" "psql restores 'keycloak'"
assert_grep "psql -U postgres -d langfuse" "${DC_LOG}" "psql restores 'langfuse'"
assert_grep "PGPASSWORD=pgpw" "${DC_LOG}" "PGPASSWORD passed to psql container"
assert_nogrep "psql -U postgres -d langflow" "${DC_LOG}" "absent dump (langflow) is not restored"

# --- 3. MongoDB restore ------------------------------------------------------
echo "[3] mongo restore"
: > "${DC_LOG}"
rs_restore_mongo >/dev/null 2>&1
assert_grep "mongorestore --username 'librechat'" "${DC_LOG}" "mongorestore invoked with credentials"
assert_grep "--drop --archive" "${DC_LOG}" "mongorestore drops then restores from archive"
# Skips cleanly when the archive is absent.
: > "${DC_LOG}"
SRC_SAVE="${SRC}"; SRC="${WORK}/empty"; mkdir -p "${SRC}"
rs_restore_mongo >/dev/null 2>&1
assert_nogrep "mongorestore" "${DC_LOG}" "mongo restore skipped when archive absent"
SRC="${SRC_SAVE}"

# --- 4. Volume restores ------------------------------------------------------
echo "[4] volume restores"
: > "${DOCKER_LOG}"
rs_restore_volumes >/dev/null 2>&1
# Present archives are restored into project-prefixed (writable) volumes.
assert_grep "testproj_redis_data:/data" "${DOCKER_LOG}" "redis volume restored"
assert_grep "tar xzf /backup/redis-data.tar.gz" "${DOCKER_LOG}" "redis archive extracted"
assert_grep "testproj_clickhouse_data:/data" "${DOCKER_LOG}" "clickhouse volume restored"
assert_grep "rm -rf /data/*" "${DOCKER_LOG}" "target volume cleared before extract"
# Absent archives are skipped (no docker call).
assert_nogrep "testproj_minio_data" "${DOCKER_LOG}" "absent minio archive skipped"
assert_nogrep "testproj_langflow_data" "${DOCKER_LOG}" "absent langflow archive skipped"
assert_nogrep "testproj_librechat_images" "${DOCKER_LOG}" "absent librechat-images archive skipped"

# Single missing-archive helper returns success (non-fatal skip).
: > "${DOCKER_LOG}"
rc=0; rs_restore_volume minio_data minio-data.tar.gz >/dev/null 2>&1 || rc=$?
assert_rc 0 "missing-archive restore is a non-fatal skip" "${rc}"
assert_nogrep "docker" "${DOCKER_LOG}" "no docker run for a missing archive"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
