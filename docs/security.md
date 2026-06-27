# Security

## Posture summary

| Area | Control |
|------|---------|
| Secrets | All generated at install (`openssl`); no default credentials anywhere |
| Secret storage | `.env` is mode `600`, git-ignored; rendered `realm.json`/`librechat.yaml` git-ignored |
| Transport | TLS 1.2/1.3 only, HSTS (2y, preload), modern cipher suite, OCSP stapling |
| Headers | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` |
| Rate limiting | NGINX zones for general/auth/api + per-IP connection caps (HTTP 429) |
| Identity | Centralized SSO via Keycloak; brute-force protection enabled on the realm |
| Container hardening | `no-new-privileges`, read-only config mounts (`:ro`), non-root images, healthchecks |
| Database | Per-service least-privilege roles; `PUBLIC` grants revoked |
| Redis | Password-protected, dangerous commands renamed/disabled |
| Network | Datastores isolated on the `backend` network; only NGINX publishes host ports |
| Logging | JSON logs with size/rotation limits per service |

## Secrets management

- `scripts/gen-secrets.sh` creates strong values: hex (exact-length crypto
  material), URL-safe base64 (passwords), and UUIDs (client secrets).
- Linked secrets have a single source of truth and are mirrored (e.g. the
  Keycloak client secret → app config; Langfuse init keys → LangFlow tracing).
- Rotate everything with `sudo ./install.sh --force-secrets` (then update the
  matching Keycloak clients if client secrets changed).

### Upgrading to Docker secrets (optional)
For stricter environments, move sensitive values from `.env` to Docker secrets:
1. `docker secret create` (Swarm) or file-based secrets with `secrets:` in
   Compose.
2. Reference them via `*_FILE` env conventions where the upstream images support
   it (Postgres, Keycloak, etc.).
This repo defaults to `.env` for single-host simplicity.

## Network exposure

- Public: `80` (ACME + redirect) and `443` (apps) via NGINX only.
- Everything else (Postgres, Redis, Mongo, ClickHouse, MinIO, Keycloak mgmt,
  LangFlow, Langfuse, Flower, Grafana) is reachable only on Docker networks.
- Restrict the host firewall to 22/80/443:
  ```bash
  ufw allow 22,80,443/tcp && ufw enable
  ```

## Hardening checklist for production

- [ ] Real domain + trusted Let's Encrypt certificates issued.
- [ ] Host firewall limited to 22/80/443; SSH key-only auth.
- [ ] `.env` backed up to a secrets manager; not in version control.
- [ ] Restrict LangFlow access to specific Keycloak groups (oauth2-proxy
      `allowed_groups`).
- [ ] Disable password login where SSO suffices
      (`LANGFUSE_AUTH_DISABLE_PASSWORD_LOGIN=true`, LibreChat email login off).
- [ ] Off-site, encrypted backups + periodic restore drills.
- [ ] Enable the monitoring profile and alert on resource/queue anomalies.
- [ ] Review image tags and run `upgrade.sh` regularly; watch the `security.yml`
      CI (Trivy/gitleaks/Checkov) results.

## Reporting

Treat the generated `.env` and `realm.json` as crown-jewel secrets. If they leak,
rotate with `--force-secrets`, update Keycloak clients, and re-issue API keys.
