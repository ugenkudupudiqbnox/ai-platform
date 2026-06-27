#!/usr/bin/env bash
# =============================================================================
# backup.sh — full platform backup (databases + object store + data volumes).
# Produces a timestamped, compressed snapshot under $BACKUP_DIR and prunes
# snapshots older than $BACKUP_RETENTION_DAYS.
#
#   backup.sh
#
# The functions below are sourced by scripts/backup.selftest.sh; the main routine
# only runs when this file is executed directly.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# Resolve project name, backup base directory, retention and the destination
# snapshot directory. BACKUP_TS may be overridden (used by the self-test) for a
# deterministic, non-time-dependent destination.
bk_init() {
  PROJECT="$(get_env COMPOSE_PROJECT_NAME || echo aiplatform)"
  PROJECT="${PROJECT:-aiplatform}"

  BACKUP_BASE="$(get_env BACKUP_DIR || echo "${REPO_ROOT}/backups")"
  case "${BACKUP_BASE}" in
    /*) ;;                                   # absolute, keep as-is
    *)  BACKUP_BASE="${REPO_ROOT}/${BACKUP_BASE#./}" ;;
  esac

  RETENTION="$(get_env BACKUP_RETENTION_DAYS || echo 14)"
  TS="${BACKUP_TS:-$(date +%Y%m%d-%H%M%S)}"
  DEST="${BACKUP_BASE}/${TS}"
  mkdir -p "${DEST}"
}

# Tar a named Docker volume into the destination directory. Args: <vol> <out>
bk_backup_volume() {
  local vol="$1" out="$2"
  log "Archiving volume ${PROJECT}_${vol} -> ${out}"
  docker run --rm \
    -v "${PROJECT}_${vol}:/data:ro" \
    -v "${DEST}:/backup" \
    alpine sh -c "tar czf /backup/${out} -C /data ." \
    || warn "Volume ${vol} not found or empty (skipped)."
}

# PostgreSQL logical dumps (one gzipped file per database).
bk_dump_postgres() {
  local pguser pgpass db
  pguser="$(get_env POSTGRES_SUPER_USER)"
  pgpass="$(get_env POSTGRES_SUPER_PASSWORD)"
  for db in \
      "$(get_env POSTGRES_DB)" \
      "$(get_env KEYCLOAK_DB_NAME)" \
      "$(get_env LANGFLOW_DB_NAME)" \
      "$(get_env LANGFUSE_DB_NAME)"; do
    log "Dumping PostgreSQL database '${db}'"
    dc exec -T -e PGPASSWORD="${pgpass}" postgres \
      pg_dump -U "${pguser}" -d "${db}" --no-owner --clean --if-exists \
      | gzip > "${DEST}/postgres-${db}.sql.gz" \
      || warn "Failed to dump database '${db}'."
  done
}

# MongoDB (LibreChat) logical dump.
bk_dump_mongo() {
  local muser mpass
  muser="$(get_env MONGO_INITDB_ROOT_USERNAME)"
  mpass="$(get_env MONGO_INITDB_ROOT_PASSWORD)"
  log "Dumping MongoDB"
  dc exec -T mongo sh -c \
    "mongodump --username '${muser}' --password '${mpass}' --authenticationDatabase admin --archive" \
    | gzip > "${DEST}/mongo.archive.gz" \
    || warn "Failed to dump MongoDB."
}

# Redis snapshot (SAVE) followed by a volume archive.
bk_dump_redis() {
  local rpass
  rpass="$(get_env REDIS_PASSWORD)"
  log "Triggering Redis save"
  dc exec -T redis redis-cli -a "${rpass}" --no-auth-warning SAVE >/dev/null 2>&1 \
    || warn "Redis SAVE failed (continuing)."
  bk_backup_volume redis_data redis-data.tar.gz
}

# Remaining data volumes.
bk_dump_volumes() {
  bk_backup_volume clickhouse_data    clickhouse-data.tar.gz
  bk_backup_volume minio_data         minio-data.tar.gz
  bk_backup_volume langflow_data      langflow-data.tar.gz
  bk_backup_volume librechat_images   librechat-images.tar.gz
  bk_backup_volume librechat_uploads  librechat-uploads.tar.gz
}

# Write an inventory manifest for the snapshot.
bk_write_manifest() {
  {
    echo "timestamp=${TS}"
    echo "project=${PROJECT}"
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "${TS}")"
    echo "contents:"
    ls -1 "${DEST}"
  } > "${DEST}/MANIFEST.txt"
}

# Prune snapshot directories older than the retention window. Args: <days>
bk_prune() {
  local retention="$1"
  log "Pruning backups older than ${retention} days"
  find "${BACKUP_BASE}" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime "+${retention}" \
    -exec rm -rf {} + 2>/dev/null || true
}

bk_main() {
  bk_init
  heading "Backup -> ${DEST}"
  bk_dump_postgres
  bk_dump_mongo
  bk_dump_redis
  bk_dump_volumes
  bk_write_manifest
  success "Backup complete: ${DEST}"
  bk_prune "${RETENTION}"
  success "Done."
}

# Only run main when executed directly (not when sourced by the self-test).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  bk_main "$@"
fi
