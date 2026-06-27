#!/usr/bin/env bash
# =============================================================================
# restore.sh — restore a platform snapshot produced by backup.sh.
# DESTRUCTIVE: overwrites current databases and data volumes.
#
#   restore.sh <backup-directory> [--yes]
#   restore.sh backups/20260101-030000
#
# The functions below are sourced by scripts/restore.selftest.sh; the main
# routine only runs when this file is executed directly.
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# Resolve the backup source directory into the global SRC. Returns:
#   0 ok   1 directory not found   2 no argument given
rs_resolve_src() {
  local arg="${1:-}"
  if [ -z "${arg}" ]; then
    warn "Usage: restore.sh <backup-directory> [--yes]"
    return 2
  fi
  case "${arg}" in
    /*) SRC="${arg}" ;;
    *)  SRC="${REPO_ROOT}/${arg#./}" ;;
  esac
  if [ ! -d "${SRC}" ]; then
    warn "Backup directory not found: ${SRC}"
    return 1
  fi
  return 0
}

rs_init() {
  PROJECT="$(get_env COMPOSE_PROJECT_NAME || echo aiplatform)"
  PROJECT="${PROJECT:-aiplatform}"
}

# Bring up the datastores needed for logical restores.
rs_start_datastores() {
  log "Starting datastores..."
  dc up -d postgres mongo redis clickhouse minio
  wait_for_service postgres 180
  wait_for_service mongo 180
}

# Restore every postgres-<db>.sql.gz dump found in SRC.
rs_restore_postgres() {
  local pguser pgpass dump db
  pguser="$(get_env POSTGRES_SUPER_USER)"
  pgpass="$(get_env POSTGRES_SUPER_PASSWORD)"
  for dump in "${SRC}"/postgres-*.sql.gz; do
    [ -e "${dump}" ] || continue
    db="$(basename "${dump}" .sql.gz)"; db="${db#postgres-}"
    log "Restoring PostgreSQL database '${db}'"
    gunzip -c "${dump}" | dc exec -T -e PGPASSWORD="${pgpass}" postgres \
      psql -U "${pguser}" -d "${db}" \
      || warn "Restore of '${db}' reported errors."
  done
}

# Restore MongoDB from its archive, if present.
rs_restore_mongo() {
  [ -f "${SRC}/mongo.archive.gz" ] || { log "No MongoDB archive in snapshot (skipped)."; return 0; }
  local muser mpass
  muser="$(get_env MONGO_INITDB_ROOT_USERNAME)"
  mpass="$(get_env MONGO_INITDB_ROOT_PASSWORD)"
  log "Restoring MongoDB"
  gunzip -c "${SRC}/mongo.archive.gz" | dc exec -T mongo sh -c \
    "mongorestore --username '${muser}' --password '${mpass}' --authenticationDatabase admin --drop --archive" \
    || warn "MongoDB restore reported errors."
}

# Restore a tar archive into a named volume (clears it first). Args: <vol> <archive>
rs_restore_volume() {
  local vol="$1" archive="$2"
  [ -f "${SRC}/${archive}" ] || { warn "Missing ${archive} (skipped)."; return 0; }
  log "Restoring volume ${PROJECT}_${vol} from ${archive}"
  docker run --rm \
    -v "${PROJECT}_${vol}:/data" \
    -v "${SRC}:/backup:ro" \
    alpine sh -c "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/${archive} -C /data"
}

# Stop services that hold volume locks before restoring volumes.
rs_stop_consumers() {
  log "Stopping services that hold volume locks..."
  dc stop redis clickhouse minio langflow langflow-worker librechat >/dev/null 2>&1 || true
}

rs_restore_volumes() {
  rs_restore_volume redis_data        redis-data.tar.gz
  rs_restore_volume clickhouse_data   clickhouse-data.tar.gz
  rs_restore_volume minio_data        minio-data.tar.gz
  rs_restore_volume langflow_data     langflow-data.tar.gz
  rs_restore_volume librechat_images  librechat-images.tar.gz
  rs_restore_volume librechat_uploads librechat-uploads.tar.gz
}

rs_main() {
  require_root

  local src_arg="" assume_yes=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes|-y) assume_yes=1; shift ;;
      -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
      -*) die "Unknown argument: $1" ;;
      *) src_arg="$1"; shift ;;
    esac
  done

  local rc=0
  rs_resolve_src "${src_arg}" || rc=$?
  [ "${rc}" -ne 0 ] && exit "${rc}"
  rs_init

  heading "Restore from ${SRC}"
  if [ "${assume_yes}" -ne 1 ]; then
    warn "This will OVERWRITE current databases and volumes."
    read -r -p "Type 'yes' to continue: " confirm
    [ "${confirm}" = "yes" ] || die "Aborted."
  fi

  rs_start_datastores
  rs_restore_postgres
  rs_restore_mongo
  rs_stop_consumers
  rs_restore_volumes

  log "Restarting the full stack..."
  dc up -d
  success "Restore complete. Review service logs with 'make logs'."
}

# Only run main when executed directly (not when sourced by the self-test).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  rs_main "$@"
fi
