#!/usr/bin/env bash
# =============================================================================
# backup.selftest.sh — offline self-test for backup.sh.
#
# Sources backup.sh (functions only; main is guarded), points it at a throwaway
# REPO_ROOT/.env, mocks `dc` (docker compose) and `docker`, then asserts:
# backup-dir resolution (relative + absolute), PostgreSQL/Mongo dump invocation
# and gzip output, volume-archive commands, the Redis path, the manifest, and
# retention pruning. Requires no Docker.
#
# Run with: ./scripts/backup.selftest.sh  (also via `make test` and CI).
# =============================================================================
set -uo pipefail
SELFTEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

export ENV_FILE="${WORK}/.env"
cat > "${ENV_FILE}" <<'EOF'
COMPOSE_PROJECT_NAME=testproj
BACKUP_DIR=./backups
BACKUP_RETENTION_DAYS=14
POSTGRES_SUPER_USER=postgres
POSTGRES_SUPER_PASSWORD=pgpw
POSTGRES_DB=postgres
KEYCLOAK_DB_NAME=keycloak
LANGFLOW_DB_NAME=langflow
LANGFUSE_DB_NAME=langfuse
MONGO_INITDB_ROOT_USERNAME=librechat
MONGO_INITDB_ROOT_PASSWORD=mongopw
REDIS_PASSWORD=redispw
EOF

# Source the unit-under-test (functions only; main is guarded out).
# shellcheck source=backup.sh
source "${SELFTEST_DIR}/backup.sh"
set +e   # the harness controls flow, not `set -e` inherited from backup.sh

# Sandbox + determinism.
REPO_ROOT="${WORK}"
export BACKUP_TS="20260101-000000"
DC_LOG="${WORK}/dc.log"
DOCKER_LOG="${WORK}/docker.log"
: > "${DC_LOG}"; : > "${DOCKER_LOG}"

# Mock `dc`: log the call, emit dump bytes on stdout (so gzip writes a file).
dc() { echo "dc $*" >> "${DC_LOG}"; echo "-- mock output for: $*"; return 0; }
# Mock `docker`: log the call only (no real tar).
docker() { echo "docker $*" >> "${DOCKER_LOG}"; return 0; }

# --- assertion harness -------------------------------------------------------
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
assert_eq()   { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (expected '$1', got '$2')"; fi; }
assert_file() { if [ -f "$1" ]; then ok "$2"; else bad "$2 (missing file: $1)"; fi; }
assert_nofile() { if [ ! -e "$1" ]; then ok "$2"; else bad "$2 (unexpected: $1)"; fi; }
assert_dir()  { if [ -d "$1" ]; then ok "$2"; else bad "$2 (missing dir: $1)"; fi; }
assert_grep() { if grep -qF -- "$1" "$2"; then ok "$3"; else bad "$3 (missing '$1' in $2)"; fi; }
assert_gzip() { if gzip -t "$1" >/dev/null 2>&1; then ok "$2"; else bad "$2 (not a valid gzip: $1)"; fi; }

echo "== backup.sh self-test =="

# --- 1. Directory resolution -------------------------------------------------
echo "[1] backup directory resolution"
bk_init >/dev/null 2>&1
assert_eq "${WORK}/backups/20260101-000000" "${DEST}" "relative BACKUP_DIR resolves under REPO_ROOT"
assert_dir "${DEST}" "destination snapshot directory is created"
assert_eq "testproj" "${PROJECT}" "project name read from env"

set_env BACKUP_DIR "${WORK}/absbackups" >/dev/null 2>&1
bk_init >/dev/null 2>&1
assert_eq "${WORK}/absbackups/20260101-000000" "${DEST}" "absolute BACKUP_DIR is honoured"
# Restore relative dir for the remaining tests.
set_env BACKUP_DIR "./backups" >/dev/null 2>&1
bk_init >/dev/null 2>&1

# --- 2. PostgreSQL dumps -----------------------------------------------------
echo "[2] postgres dumps"
: > "${DC_LOG}"
bk_dump_postgres >/dev/null 2>&1
for db in postgres keycloak langflow langfuse; do
  assert_file "${DEST}/postgres-${db}.sql.gz" "dump file for '${db}' created"
  assert_gzip "${DEST}/postgres-${db}.sql.gz" "dump for '${db}' is valid gzip"
  assert_grep "pg_dump -U postgres -d ${db}" "${DC_LOG}" "pg_dump invoked for '${db}'"
done
assert_grep "PGPASSWORD=pgpw" "${DC_LOG}" "PGPASSWORD passed to the dump container"

# --- 3. MongoDB dump ---------------------------------------------------------
echo "[3] mongo dump"
: > "${DC_LOG}"
bk_dump_mongo >/dev/null 2>&1
assert_file "${DEST}/mongo.archive.gz" "mongo archive created"
assert_gzip "${DEST}/mongo.archive.gz" "mongo archive is valid gzip"
assert_grep "mongodump --username 'librechat'" "${DC_LOG}" "mongodump invoked with credentials"
assert_grep "authenticationDatabase admin" "${DC_LOG}" "mongodump uses admin auth db"

# --- 4. Volume archives ------------------------------------------------------
echo "[4] volume archives"
: > "${DOCKER_LOG}"
bk_backup_volume clickhouse_data clickhouse-data.tar.gz >/dev/null 2>&1
assert_grep "testproj_clickhouse_data:/data:ro" "${DOCKER_LOG}" "volume mounted by project-prefixed name (read-only)"
assert_grep "tar czf /backup/clickhouse-data.tar.gz" "${DOCKER_LOG}" "tar writes the expected archive name"
: > "${DOCKER_LOG}"
bk_dump_volumes >/dev/null 2>&1
for v in minio_data langflow_data librechat_images librechat_uploads; do
  assert_grep "testproj_${v}:/data:ro" "${DOCKER_LOG}" "archives volume '${v}'"
done

# --- 5. Redis path -----------------------------------------------------------
echo "[5] redis snapshot"
: > "${DC_LOG}"; : > "${DOCKER_LOG}"
bk_dump_redis >/dev/null 2>&1
assert_grep "redis-cli -a redispw --no-auth-warning SAVE" "${DC_LOG}" "Redis SAVE triggered with auth"
assert_grep "testproj_redis_data:/data:ro" "${DOCKER_LOG}" "Redis data volume archived"

# --- 6. Manifest -------------------------------------------------------------
echo "[6] manifest"
bk_write_manifest >/dev/null 2>&1
assert_file "${DEST}/MANIFEST.txt" "MANIFEST.txt written"
assert_grep "project=testproj" "${DEST}/MANIFEST.txt" "manifest records project"
assert_grep "timestamp=20260101-000000" "${DEST}/MANIFEST.txt" "manifest records timestamp"
assert_grep "postgres-keycloak.sql.gz" "${DEST}/MANIFEST.txt" "manifest lists snapshot contents"

# --- 7. Retention pruning ----------------------------------------------------
echo "[7] retention pruning"
OLD="${BACKUP_BASE}/20200101-010101"
NEW="${BACKUP_BASE}/20991231-235959"
mkdir -p "${OLD}" "${NEW}"
touch -d "40 days ago" "${OLD}" 2>/dev/null || touch -t 202001010101 "${OLD}"
bk_prune 14 >/dev/null 2>&1
assert_nofile "${OLD}" "snapshot older than retention is pruned"
assert_dir "${NEW}" "recent snapshot is kept"
assert_dir "${DEST}" "current snapshot is kept"

# --- Summary -----------------------------------------------------------------
echo
echo "== results: ${PASS} passed, ${FAIL} failed =="
[ "${FAIL}" -eq 0 ]
