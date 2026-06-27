# Enterprise AI Platform

[![Repo](https://img.shields.io/badge/GitHub-ugenkudupudiqbnox%2Fai--platform-181717?logo=github)](https://github.com/ugenkudupudiqbnox/ai-platform)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

A production-ready, self-hosted AI platform deployed with Docker Compose on
**Ubuntu Server 24.04 LTS**. One command — `sudo ./install.sh` — provisions the
entire stack with generated secrets, automatic database creation, Keycloak SSO,
TLS certificates, backups and monitoring.

| Layer        | Component                                   |
|--------------|---------------------------------------------|
| Edge         | NGINX (TLS, HTTP/2, rate limiting), oauth2-proxy |
| Chat         | LibreChat (OpenAI / Anthropic / Gemini / Azure / Ollama) |
| Identity     | Keycloak (realm, clients, roles, groups, users) |
| Flows        | LangFlow (gunicorn web tier + Celery workers + Flower) |
| Observability| Langfuse v3 (traces, prompts, cost, latency, OTel) |
| Data         | PostgreSQL 16, Redis 7, MongoDB 7, ClickHouse, MinIO |
| Ops          | Certbot, backups, Prometheus/Grafana, OpenTelemetry |

## Architecture

```
                          Internet
                             │
                          ┌──▼───┐   TLS / HTTP2 / HSTS / rate-limit
                          │ NGINX│   chat. auth. flow. trace.
                          └──┬───┘
        ┌───────────┬────────┼─────────────┬───────────────┐
        ▼           ▼        ▼             ▼               ▼
   LibreChat    Keycloak  oauth2-proxy  Langfuse        (Flower)
     (chat)      (auth)       │          (trace)
        │           │         ▼            │
        │           │      LangFlow ───────┤  tracing (auto-wired keys)
        │           │      (gunicorn)      │
        │           │         │            │
        │           │     LangFlow         │
        │           │     Celery workers   │
        ▼           ▼         ▼            ▼
   MongoDB     PostgreSQL   Redis     ClickHouse + MinIO
                   │          │
                   └────── shared data tier ──────┘

   Model providers: OpenAI · Anthropic · Google Gemini · Azure OpenAI · Ollama
```

See [docs/architecture.md](docs/architecture.md) for the full design.

## Quick start

On a fresh Ubuntu 24.04 server with DNS records for `chat`, `auth`, `flow`,
`trace` subdomains pointing at the host:

```bash
git clone <this-repo> ai-platform
cd ai-platform
sudo ./install.sh --domain ai.example.com --email admin@example.com
```

The installer will:

1. Detect the OS and install Docker + the Compose plugin.
2. Generate every password/secret and write a locked-down `.env`.
3. Create the four service databases with least-privilege users.
4. Import the Keycloak `AIPlatform` realm (clients, roles, groups, users).
5. Run all migrations, bootstrap Langfuse keys, and auto-wire LangFlow tracing.
6. Issue Let's Encrypt certificates and start NGINX.
7. Install systemd timers for certificate renewal and daily backups.
8. Run health checks and print the access URLs and credentials.

No manual configuration is required. If DNS isn't ready, the platform comes up
on self-signed certificates — fix DNS and run `sudo ./scripts/issue-certs.sh`.

## Day-2 operations

```bash
make ps              # service status
make logs S=nginx    # tail one service
make health          # full health snapshot
make backup          # on-demand backup
make scale-workers N=4   # scale LangFlow workers
make monitoring-up   # start Prometheus + Grafana + exporters
make upgrade         # pull/rebuild and recreate
```

## Documentation

| Topic | File |
|-------|------|
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Installation | [docs/installation.md](docs/installation.md) |
| Upgrade | [docs/upgrade.md](docs/upgrade.md) |
| Backup | [docs/backup.md](docs/backup.md) |
| Restore | [docs/restore.md](docs/restore.md) |
| OIDC / SSO | [docs/oidc.md](docs/oidc.md) |
| Worker scaling | [docs/scaling.md](docs/scaling.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| FAQ | [docs/faq.md](docs/faq.md) |
| Security | [docs/security.md](docs/security.md) |

## Requirements

- Ubuntu Server 24.04 LTS (x86_64), root/sudo access
- ≥ 8 vCPU, ≥ 16 GB RAM, ≥ 60 GB disk (recommended for the full stack)
- Public DNS records for the four subdomains (for trusted TLS)
- Outbound internet access (image pulls, ACME, model providers)

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for the
contributor quick-start, repository layout, local validation steps and coding
standards.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
