#!/usr/bin/env bash
# =============================================================================
# restore.sh — restore a platform snapshot produced by backup.sh.
# DESTRUCTIVE: overwrites current databases and data volumes.
#
# Usage: restore.sh <backup-directory>
#        restore.sh backups/20260101-030000
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

[ $# -ge 1 ] || die "Usage: $0 <backup-directory>"
SRC="$1"
case "${SRC}" in /*) ;; *) SRC="${REPO_ROOT}/${SRC#./}" ;; esac
[ -d "${SRC}" ] || die "Backup directory not found: ${SRC}"

PROJECT="$(get_env COMPOSE_PROJECT_NAME || echo aiplatform)"
PROJECT="${PROJECT:-aiplatform}"

heading "Restore from ${SRC}"
warn "This will OVERWRITE current databases and volumes."
read -r -p "Type 'yes' to continue: " confirm
[ "${confirm}" = "yes" ] || die "Aborted."

# Restore a tar archive into a named volume (clears it first).
restore_volume() {
  local vol="$1" archive="$2"
  [ -f "${SRC}/${archive}" ] || { warn "Missing ${archive} (skipped)."; return 0; }
  log "Restoring volume ${PROJECT}_${vol} from ${archive}"
  docker run --rm \
    -v "${PROJECT}_${vol}:/data" \
    -v "${SRC}:/backup:ro" \
    alpine sh -c "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/${archive} -C /data"
}

# Bring up only the datastores needed for logical restores.
log "Starting datastores..."
dc up -d postgres mongo redis clickhouse minio
wait_for_service postgres 180
wait_for_service mongo 180

# --- PostgreSQL --------------------------------------------------------------
PGUSER="$(get_env POSTGRES_SUPER_USER)"
PGPASS="$(get_env POSTGRES_SUPER_PASSWORD)"
for dump in "${SRC}"/postgres-*.sql.gz; do
  [ -e "${dump}" ] || continue
  db="$(basename "${dump}" .sql.gz)"; db="${db#postgres-}"
  log "Restoring PostgreSQL database '${db}'"
  gunzip -c "${dump}" | dc exec -T -e PGPASSWORD="${PGPASS}" postgres \
    psql -U "${PGUSER}" -d "${db}" \
    || warn "Restore of '${db}' reported errors."
done

# --- MongoDB -----------------------------------------------------------------
if [ -f "${SRC}/mongo.archive.gz" ]; then
  MUSER="$(get_env MONGO_INITDB_ROOT_USERNAME)"
  MPASS="$(get_env MONGO_INITDB_ROOT_PASSWORD)"
  log "Restoring MongoDB"
  gunzip -c "${SRC}/mongo.archive.gz" | dc exec -T mongo sh -c \
    "mongorestore --username '${MUSER}' --password '${MPASS}' --authenticationDatabase admin --drop --archive" \
    || warn "MongoDB restore reported errors."
fi

# --- Volumes (stop consumers first) ------------------------------------------
log "Stopping services that hold volume locks..."
dc stop redis clickhouse minio langflow langflow-worker librechat >/dev/null 2>&1 || true

restore_volume redis_data        redis-data.tar.gz
restore_volume clickhouse_data   clickhouse-data.tar.gz
restore_volume minio_data        minio-data.tar.gz
restore_volume langflow_data     langflow-data.tar.gz
restore_volume librechat_images  librechat-images.tar.gz
restore_volume librechat_uploads librechat-uploads.tar.gz

log "Restarting the full stack..."
dc up -d

success "Restore complete. Review service logs with 'make logs'."
