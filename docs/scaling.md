# Worker scaling & performance tuning

## LangFlow tiers

LangFlow runs as two tiers plus a dashboard:

| Container | Role | Scale knob |
|-----------|------|-----------|
| `langflow` | gunicorn web tier (API/UI) | `LANGFLOW_WORKERS` (processes per container) |
| `langflow-worker` | Celery workers (queued/background execution) | replicas + `LANGFLOW_WORKER_CONCURRENCY` / `LANGFLOW_WORKER_AUTOSCALE` |
| `flower` | Celery queue dashboard | n/a |

### Web tier (gunicorn)

`docker/langflow/gunicorn.conf.py` is driven by environment variables:

| Variable | Meaning | Default |
|----------|---------|---------|
| `LANGFLOW_WORKERS` | gunicorn worker processes | `8` |
| `LANGFLOW_WORKER_TIMEOUT` | per-request hard timeout (s) | `300` |
| `LANGFLOW_PRELOAD` | preload app before fork (shared mem, faster boot) | `true` |
| `LANGFLOW_LOG_LEVEL` | gunicorn/app log level | `INFO` |

Rule of thumb: `workers ≈ (2 × CPU cores) + 1`, bounded by available RAM (each
worker loads the app). Long flow runs should go through the **worker tier**, not
block a web worker.

### Worker tier (Celery + Redis)

| Variable | Meaning | Default |
|----------|---------|---------|
| `LANGFLOW_WORKER_REPLICAS` | number of worker containers | `2` |
| `LANGFLOW_WORKER_CONCURRENCY` | child processes per worker | `4` |
| `LANGFLOW_WORKER_AUTOSCALE` | `max,min` child autoscale | `8,2` |
| `LANGFLOW_REDIS_QUEUE` | Celery broker URL (Redis db 1) | derived |

Scale horizontally at runtime:

```bash
make scale-workers N=6
# equivalent to:
docker compose up -d --scale langflow-worker=6
```

Capabilities provided by the worker tier:
- background + queued execution via Celery on a Redis broker (db 1) with results
  in db 2;
- graceful shutdown (`stop_grace_period: 60s`, warm Celery shutdown drains
  in-flight tasks);
- automatic restart (`restart: unless-stopped`);
- per-worker autoscaling (`--autoscale`);
- structured logs to stdout (captured + rotated by Docker);
- queue depth + worker metrics via Flower and the health check.

> **Version note:** the worker entrypoint auto-detects LangFlow's Celery app
> across releases. If background execution is unavailable in your pinned LangFlow
> version, the worker logs a clear message and exits; the web tier still works.
> Pin a LangFlow version that ships the task-queue feature to use queued runs.

### Queue dashboard (Flower)

Flower monitors the Redis broker and connected workers. It listens on `:5555`
with basic auth (`FLOWER_USER` / `FLOWER_PASSWORD`). It is on the internal
networks; reach it via an SSH tunnel:

```bash
ssh -L 5555:localhost:5555 user@server
docker compose exec flower true   # confirm it's running
# then browse http://localhost:5555 after also exposing the container port,
# or add a vhost in NGINX if you want it published.
```

## Datastore tuning

- **Redis** — bump `REDIS_MAXMEMORY` for heavier queue/cache load; AOF is on.
- **Postgres** — for large deployments mount a tuned `postgresql.conf`
  (shared_buffers, work_mem, max_connections).
- **ClickHouse** — scales with trace volume; give it fast disk and RAM.
- **Langfuse** — scale `langfuse-worker` horizontally for high ingestion rates.

## Monitoring the load

```bash
make health            # queue depth, worker count, Redis/PG metrics
make monitoring-up     # Prometheus + Grafana dashboards
```
