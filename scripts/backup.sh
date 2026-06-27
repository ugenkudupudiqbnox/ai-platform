#!/usr/bin/env bash
# =============================================================================
# backup.sh — full platform backup (databases + object store + data volumes).
# Produces a timestamped, compressed snapshot under $BACKUP_DIR and prunes
# snapshots older than $BACKUP_RETENTION_DAYS.
#
# Usage: backup.sh
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT="$(get_env COMPOSE_PROJECT_NAME || echo aiplatform)"
PROJECT="${PROJECT:-aiplatform}"

BACKUP_BASE="$(get_env BACKUP_DIR || echo "${REPO_ROOT}/backups")"
case "${BACKUP_BASE}" in
  /*) ;;                                   # absolute, keep as-is
  *)  BACKUP_BASE="${REPO_ROOT}/${BACKUP_BASE#./}" ;;
esac
RETENTION="$(get_env BACKUP_RETENTION_DAYS || echo 14)"
TS="$(date +%Y%m%d-%H%M%S)"
DEST="${BACKUP_BASE}/${TS}"
mkdir -p "${DEST}"

heading "Backup -> ${DEST}"

# Tar a named Docker volume into the destination directory.
backup_volume() {
  local vol="$1" out="$2"
  log "Archiving volume ${PROJECT}_${vol} -> ${out}"
  docker run --rm \
    -v "${PROJECT}_${vol}:/data:ro" \
    -v "${DEST}:/backup" \
    alpine sh -c "tar czf /backup/${out} -C /data ." \
    || warn "Volume ${vol} not found or empty (skipped)."
}

# --- PostgreSQL logical dumps ------------------------------------------------
PGUSER="$(get_env POSTGRES_SUPER_USER)"
PGPASS="$(get_env POSTGRES_SUPER_PASSWORD)"
for db in \
    "$(get_env POSTGRES_DB)" \
    "$(get_env KEYCLOAK_DB_NAME)" \
    "$(get_env LANGFLOW_DB_NAME)" \
    "$(get_env LANGFUSE_DB_NAME)"; do
  log "Dumping PostgreSQL database '${db}'"
  dc exec -T -e PGPASSWORD="${PGPASS}" postgres \
    pg_dump -U "${PGUSER}" -d "${db}" --no-owner --clean --if-exists \
    | gzip > "${DEST}/postgres-${db}.sql.gz" \
    || warn "Failed to dump database '${db}'."
done

# --- MongoDB (LibreChat) -----------------------------------------------------
MUSER="$(get_env MONGO_INITDB_ROOT_USERNAME)"
MPASS="$(get_env MONGO_INITDB_ROOT_PASSWORD)"
log "Dumping MongoDB"
dc exec -T mongo sh -c \
  "mongodump --username '${MUSER}' --password '${MPASS}' --authenticationDatabase admin --archive" \
  | gzip > "${DEST}/mongo.archive.gz" \
  || warn "Failed to dump MongoDB."

# --- Redis (snapshot then archive volume) ------------------------------------
RPASS="$(get_env REDIS_PASSWORD)"
log "Triggering Redis save"
dc exec -T redis redis-cli -a "${RPASS}" --no-auth-warning SAVE >/dev/null 2>&1 \
  || warn "Redis SAVE failed (continuing)."
backup_volume redis_data redis-data.tar.gz

# --- Data volumes ------------------------------------------------------------
backup_volume clickhouse_data    clickhouse-data.tar.gz
backup_volume minio_data         minio-data.tar.gz
backup_volume langflow_data      langflow-data.tar.gz
backup_volume librechat_images   librechat-images.tar.gz
backup_volume librechat_uploads  librechat-uploads.tar.gz

# --- Manifest ----------------------------------------------------------------
{
  echo "timestamp=${TS}"
  echo "project=${PROJECT}"
  echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "contents:"
  ls -1 "${DEST}"
} > "${DEST}/MANIFEST.txt"

success "Backup complete: ${DEST}"

# --- Retention ---------------------------------------------------------------
log "Pruning backups older than ${RETENTION} days"
find "${BACKUP_BASE}" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime "+${RETENTION}" \
  -exec rm -rf {} + 2>/dev/null || true

success "Done."
