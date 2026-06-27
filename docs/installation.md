# Installation

## Prerequisites

- Ubuntu Server 24.04 LTS, x86_64, with root/sudo.
- DNS A/AAAA records pointing the four subdomains at the server's public IP:
  - `chat.<your-domain>`
  - `auth.<your-domain>`
  - `flow.<your-domain>`
  - `trace.<your-domain>`
- Ports 80 and 443 reachable from the internet (for ACME + access).
- Recommended sizing: 8 vCPU, 16 GB RAM, 60 GB disk.

> NGINX and Certbot run **inside containers**. Do not install host NGINX/Certbot
> — they would conflict on ports 80/443.

## One-command install

```bash
git clone <this-repo> ai-platform
cd ai-platform
sudo ./install.sh --domain ai.example.com --email admin@example.com
```

### Flags

| Flag | Purpose |
|------|---------|
| `--domain <d>` | Base domain (subdomains are derived) |
| `--email <e>` | Let's Encrypt registration email |
| `--staging` | Use the Let's Encrypt **staging** CA (testing) |
| `--skip-ssl` | Keep self-signed certs (no ACME) |
| `--skip-deps` | Assume Docker is already installed |
| `--force-secrets` | Regenerate every secret |

If you omit `--domain`/`--email`, the installer prompts for them (and falls back
to `example.com` + self-signed if left blank).

## What the installer does

1. **Detect OS** and install Docker Engine + Compose plugin + CLI tools.
2. **Generate secrets** → writes `.env` (mode `600`) from `.env.example`.
3. **Derive hostnames** and render `realm.json` and `librechat.yaml`.
4. **Build/pull images** (Keycloak + LangFlow are built locally).
5. **Start datastores** (Postgres, Redis, Mongo, ClickHouse, MinIO) and wait for
   health; create the MinIO bucket.
6. **Start Keycloak** — applies DB migrations and imports the realm.
7. **Start Langfuse** — runs migrations and bootstraps the admin/org/project/keys.
8. **Sync tracing keys** into LangFlow.
9. **Start LangFlow, workers, Flower, LibreChat, oauth2-proxy.**
10. **Bootstrap TLS** (self-signed) → start NGINX → **issue Let's Encrypt certs.**
11. **Install systemd timers** for renewal (daily 03:30) and backups (daily 02:00).
12. **Run health checks** and print URLs + credentials.

## After install

Credentials are printed at the end and stored in `.env`. Retrieve them anytime:

```bash
grep -E 'KC_BOOTSTRAP_ADMIN_PASSWORD|LANGFUSE_INIT_USER_PASSWORD|LANGFLOW_SUPERUSER_PASSWORD|FLOWER_PASSWORD' .env
```

Verify everything is healthy:

```bash
make health
```

## Re-running / idempotency

`install.sh` is safe to re-run. Existing secrets in `.env` are preserved (use
`--force-secrets` to rotate). Realm import only happens on Keycloak's first boot;
to re-import, see [troubleshooting.md](troubleshooting.md).

## Changing the domain after install

You can move the platform to a different base domain at any time without
reinstalling:

```bash
sudo ./scripts/change-domain.sh --domain new.example.com
# or
make change-domain D=new.example.com
```

This script:

1. Updates `BASE_DOMAIN` and the `chat./auth./flow./trace.` hostnames in `.env`.
2. Re-renders the Keycloak realm template.
3. **Updates the live Keycloak clients in place** (redirect URIs, web origins,
   root URLs) via `kcadm` — no realm wipe, so users and data are preserved.
4. **Migrates LibreChat OIDC accounts** to the new issuer URL (LibreChat binds
   each SSO account to the issuer and would otherwise reject logins with
   "Authentication failed" after the domain changes).
5. Recreates the services that embed the hostname (Keycloak, oauth2-proxy,
   LibreChat, Langfuse web/worker, LangFlow web/worker).
6. Re-issues Let's Encrypt certificates for the new subdomains and restarts
   LibreChat so its OIDC discovery uses the new certificate.

NGINX itself needs no change — its `server_name` matching is domain-agnostic.

> Point DNS A/AAAA records for the new `chat./auth./flow./trace.` subdomains at
> the host **before** running it (or pass `--skip-ssl` and issue certs later with
> `sudo ./scripts/issue-certs.sh`). Use `--yes` to skip the confirmation prompt.

## Configuring model providers

Edit `.env` and add your keys, then `make up` (or `make upgrade`):

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

Empty keys simply disable that provider.
