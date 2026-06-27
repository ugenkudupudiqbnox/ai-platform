#!/usr/bin/env bash
# =============================================================================
# LangFlow dispatching entrypoint.
#   web    -> gunicorn (uvicorn workers) serving the LangFlow ASGI app
#   worker -> Celery worker (delegates to worker-entrypoint.sh)
# Database migrations are applied automatically on web start.
# =============================================================================
set -euo pipefail

ROLE="${1:-web}"

log() { echo "[langflow-entrypoint] $*"; }

# Ensure the config/state directory exists and is writable.
mkdir -p "${LANGFLOW_CONFIG_DIR:-/var/lib/langflow}" || true

# Wait until Postgres is reachable before starting (parsed from LANGFLOW_DATABASE_URL).
wait_for_db() {
  local url="${LANGFLOW_DATABASE_URL:-}"
  [ -z "${url}" ] && return 0
  log "Waiting for the database to accept connections..."
  python - "$url" <<'PY'
import sys, time, socket
from urllib.parse import urlparse
url = sys.argv[1]
p = urlparse(url)
host = p.hostname or "postgres"
port = p.port or 5432
deadline = time.time() + 180
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"database reachable at {host}:{port}")
            sys.exit(0)
    except OSError:
        time.sleep(2)
print("timed out waiting for database", file=sys.stderr)
sys.exit(1)
PY
}

case "${ROLE}" in
  web)
    wait_for_db
    log "Applying database migrations..."
    # LangFlow ships an idempotent migration command; --fix applies pending heads.
    langflow migration --fix || log "migration step reported a non-zero status (continuing)"

    log "Starting LangFlow web tier via gunicorn..."
    exec gunicorn "langflow.main:create_app()" --config "${GUNICORN_CONF}"
    ;;

  worker)
    wait_for_db
    exec /opt/aiplatform/worker-entrypoint.sh
    ;;

  *)
    # Pass through any other command (e.g. `langflow ...`, a shell, etc.).
    log "Executing custom command: $*"
    exec "$@"
    ;;
esac
