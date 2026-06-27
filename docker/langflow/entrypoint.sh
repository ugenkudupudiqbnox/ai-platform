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
    # Use LangFlow's own launcher: it applies DB migrations, serves the built
    # frontend (UI) AND the API, and on Linux with --workers>1 runs under
    # gunicorn + uvicorn workers. Launching gunicorn against
    # `langflow.main:create_app()` directly does NOT mount the frontend, so "/"
    # returns {"detail":"Not Found"}.
    log "Starting LangFlow (UI + API) via 'langflow run'..."
    exec langflow run \
      --host 0.0.0.0 \
      --port 7860 \
      --workers "${LANGFLOW_WORKERS:-1}" \
      --worker-timeout "${LANGFLOW_WORKER_TIMEOUT:-300}"
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
