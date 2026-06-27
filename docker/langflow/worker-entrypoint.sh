#!/usr/bin/env bash
# =============================================================================
# LangFlow Celery worker entrypoint.
#
# LangFlow's background/queued execution runs on Celery with a Redis broker.
# The exact celery application module has changed across LangFlow releases, so we
# auto-detect the importable app and start the worker against it. This keeps the
# image working across versions instead of hard-coding a fragile module path.
#
# Capabilities provided:
#   - background + queued execution (Celery + Redis)
#   - graceful shutdown (SIGTERM -> warm shutdown, drains in-flight tasks)
#   - horizontal scaling (run N replicas; broker fans tasks out)
#   - autoscaling within a worker (--autoscale max,min)
#   - structured logging to stdout (captured + rotated by Docker)
# =============================================================================
set -euo pipefail

log() { echo "[langflow-worker] $*"; }

CONCURRENCY="${LANGFLOW_WORKER_CONCURRENCY:-4}"
AUTOSCALE="${LANGFLOW_WORKER_AUTOSCALE:-8,2}"
LOGLEVEL="${LANGFLOW_LOG_LEVEL:-INFO}"

# Candidate celery app import paths, newest first.
CANDIDATES=(
  "langflow.core.celery_app:celery_app"
  "langflow.worker:celery_app"
  "langflow.services.task.backends.celery.celery_app:celery_app"
  "langflow.core.celery_app"
)

CELERY_APP=""
for app in "${CANDIDATES[@]}"; do
  module="${app%%:*}"
  if python -c "import importlib,sys; importlib.import_module('${module}')" >/dev/null 2>&1; then
    CELERY_APP="${app}"
    log "Detected Celery app: ${CELERY_APP}"
    break
  fi
done

if [ -z "${CELERY_APP}" ]; then
  log "ERROR: no LangFlow Celery application could be imported in this image."
  log "       Background/queued execution is unavailable for this LangFlow version."
  log "       See docs/scaling.md for supported versions and alternatives."
  exit 1
fi

# Warm shutdown on SIGTERM/SIGINT so in-flight tasks drain (graceful shutdown).
exec celery -A "${CELERY_APP}" worker \
  --loglevel="${LOGLEVEL}" \
  --concurrency="${CONCURRENCY}" \
  --autoscale="${AUTOSCALE}" \
  --max-tasks-per-child=100 \
  --without-gossip \
  --without-mingle \
  -Ofair
