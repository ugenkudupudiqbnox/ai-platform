# Architecture

## Overview

The platform is a set of Docker Compose services on two networks:

- **edge** (`aiplatform-edge`): NGINX, oauth2-proxy and the public-facing app
  containers. NGINX is the only service that publishes host ports (80/443).
- **backend** (`aiplatform-backend`): datastores and internal app traffic.

Application containers join both networks; datastores stay on `backend` only.

## Request flow

```
Internet → NGINX (TLS termination, HSTS, rate limiting, compression)
  ├── chat.<domain>  → librechat:3080
  ├── auth.<domain>  → keycloak:8080
  ├── flow.<domain>  → oauth2-proxy:4180 → langflow:7860
  └── trace.<domain> → langfuse-web:3000
```

NGINX uses regex `server_name` matching (`~^chat\.`, …) so the configuration is
domain-agnostic — the same files work for any `BASE_DOMAIN`. A single TLS
certificate (cert name `aiplatform`) carries all four subdomains as SANs.

## Components

### NGINX (edge)
Reverse proxy with HTTP/2, gzip, security headers, three rate-limit zones
(`general`, `auth`, `api`), connection limits, WebSocket upgrade support and
200 MB upload limits. Serves ACME HTTP-01 challenges and a `/healthz` probe.
An internal `:8081/stub_status` endpoint feeds the Prometheus exporter.

### Keycloak (identity)
Built as an optimized image (`kc.sh build --db=postgres`). On first boot it
imports the rendered `realm.json`, creating the `AIPlatform` realm with three
OIDC clients (librechat, langflow, langfuse), four realm roles (Admin,
Developer, User, Guest), three groups, and three seed users. Runs behind NGINX
with `KC_PROXY_HEADERS=xforwarded`.

### LibreChat (chat)
Node application using **MongoDB** as its primary datastore and Redis for
caching/sessions. Authenticates users via Keycloak OIDC. Model providers
(OpenAI, Anthropic, Gemini, Azure, Ollama) are configured through
`librechat.yaml` and environment variables.

### LangFlow (flows)
Production deployment split into tiers:
- **web** — gunicorn with `uvicorn` workers (`LANGFLOW_WORKERS`), serving the
  LangFlow ASGI app; auto-applies DB migrations on start.
- **worker** — Celery workers consuming a Redis-backed queue for background /
  queued execution; horizontally scalable.
- **flower** — Celery queue dashboard.

Postgres is the metadata store; Redis is the cache + Celery broker/backend.
Access is gated by oauth2-proxy (Keycloak) at the edge — see
[oidc.md](oidc.md).

### Langfuse (observability)
Langfuse **v3**, which requires more than Postgres:
- **PostgreSQL** — transactional metadata
- **ClickHouse** — high-volume trace/observation analytics
- **Redis** — queue/cache
- **MinIO (S3)** — event and media blob storage

`langfuse-web` and `langfuse-worker` run as separate containers. Migrations run
automatically; an admin user, org, project and **deterministic API keys** are
created on first boot via `LANGFUSE_INIT_*`.

### LangFlow → Langfuse tracing
Because Langfuse keys are deterministic (set in `.env`), they are injected into
the LangFlow web and worker containers as `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` with no manual step. LangFlow then
emits traces, prompts, generations, token/latency/cost data to Langfuse.

## Data stores and ports (internal)

| Service     | Internal port(s) | Backing store |
|-------------|------------------|---------------|
| postgres    | 5432             | `postgres_data` |
| redis       | 6379             | `redis_data` (AOF) |
| mongo       | 27017            | `mongo_data` |
| clickhouse  | 8123 / 9000      | `clickhouse_data` |
| minio       | 9000 / 9001      | `minio_data` |
| keycloak    | 8080 / 9000      | Postgres |
| langflow    | 7860 / 9090      | Postgres + `langflow_data` |
| langfuse-web| 3000             | PG + ClickHouse + MinIO |
| librechat   | 3080             | Mongo + volumes |

### Redis logical databases
| DB | Consumer |
|----|----------|
| 0  | LibreChat cache |
| 1  | LangFlow Celery broker |
| 2  | LangFlow result backend |
| 3  | Langfuse queue/cache |

## Deviations from the literal spec

- **Langfuse v3** pulls in ClickHouse + MinIO (the spec listed Postgres only;
  only the legacy v2 line is Postgres-only).
- **LangFlow** is gated by oauth2-proxy because open-source LangFlow has no
  native OIDC.
- **MongoDB** is added because LibreChat requires it.
- **HTTP/3 (QUIC)** is included as commented config — the stock `nginx:alpine`
  image is not built with QUIC. See [troubleshooting.md](troubleshooting.md).
