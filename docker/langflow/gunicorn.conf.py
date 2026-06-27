"""
Gunicorn configuration for the LangFlow web tier.

Worker count, timeout and preload are driven by environment variables so the
deployment can be tuned without rebuilding the image (see .env / docs/scaling.md).
"""
import os

# --- Socket ------------------------------------------------------------------
bind = "0.0.0.0:7860"

# --- Worker processes --------------------------------------------------------
# LANGFLOW_WORKERS controls the number of OS worker processes (gunicorn masters
# a pool of UvicornWorker async workers).
workers = int(os.environ.get("LANGFLOW_WORKERS", "8"))
worker_class = "uvicorn.workers.UvicornWorker"

# Per-request hard timeout (seconds). Long-running flow executions should be
# dispatched to the Celery worker tier; this guards the synchronous API.
timeout = int(os.environ.get("LANGFLOW_WORKER_TIMEOUT", "300"))
graceful_timeout = 60
keepalive = 15

# Recycle workers periodically to bound memory growth.
max_requests = 1000
max_requests_jitter = 100

# --- Preloading --------------------------------------------------------------
# Preload the application in the master before forking workers (faster boot,
# shared memory). Disable if you need per-worker code reloads.
preload_app = os.environ.get("LANGFLOW_PRELOAD", "true").lower() in ("1", "true", "yes")

# --- Logging (structured to stdout/stderr; Docker captures + rotates) --------
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LANGFLOW_LOG_LEVEL", "info").lower()
access_log_format = (
    '{"remote":"%(h)s","method":"%(m)s","path":"%(U)s","status":"%(s)s",'
    '"bytes":"%(b)s","referer":"%(f)s","agent":"%(a)s","duration_ms":"%(M)s"}'
)

# Use a memory-backed temp dir for worker heartbeat to avoid disk stalls.
worker_tmp_dir = "/dev/shm"
