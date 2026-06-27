# Upgrade

## Routine upgrade

```bash
sudo ./upgrade.sh
```

This will:

1. Take a pre-upgrade backup (skip with `SKIP_BACKUP=1`).
2. Re-render `librechat.yaml` from its template.
3. Pull updated images and rebuild local images (Keycloak, LangFlow).
4. Start datastores and wait for Postgres.
5. Recreate all services (`docker compose up -d --remove-orphans`). Keycloak,
   Langfuse and LangFlow apply their own migrations on start.
6. Smoke-test Keycloak, Langfuse and NGINX health.
7. Prune dangling images and run a health check.

## Pinning / changing versions

Image tags live in `.env` (copied from `.env.example`). To upgrade a single
component, edit its tag and run the upgrade:

```bash
sed -i 's#^LANGFUSE_WEB_IMAGE=.*#LANGFUSE_WEB_IMAGE=langfuse/langfuse:3.20.0#' .env
sed -i 's#^LANGFUSE_WORKER_IMAGE=.*#LANGFUSE_WORKER_IMAGE=langfuse/langfuse-worker:3.20.0#' .env
sudo ./upgrade.sh
```

> Always upgrade `langfuse` and `langfuse-worker` to the **same** version.

## Major version notes

- **Langfuse**: review the Langfuse release notes for ClickHouse migration steps
  before crossing a major version. Migrations run automatically on container
  start, but a backup first is strongly advised.
- **Keycloak**: the image is rebuilt with `kc.sh build` on every upgrade so the
  optimized server matches the new version.
- **PostgreSQL major upgrades** (e.g. 16→17) require a dump/restore, not just a
  tag bump — see [backup.md](backup.md) and [restore.md](restore.md).

## Rollback

1. Restore the pre-upgrade backup (see [restore.md](restore.md)).
2. Reset the image tags in `.env` to the previous versions.
3. `sudo ./upgrade.sh`.
