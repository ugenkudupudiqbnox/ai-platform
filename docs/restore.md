# Restore

> **Destructive.** Restoring overwrites current databases and data volumes.
> Take a fresh backup first if the current state has any value.

## Restore a snapshot

```bash
sudo ./scripts/restore.sh backups/20260101-030000
# or
make restore SRC=backups/20260101-030000
```

You will be prompted to type `yes` to confirm.

## What the restore does

1. Starts the datastores (Postgres, Mongo, Redis, ClickHouse, MinIO).
2. Restores each `postgres-<db>.sql.gz` with `psql` (dumps were taken with
   `--clean --if-exists`).
3. Restores MongoDB with `mongorestore --drop`.
4. Stops volume consumers, then extracts the Redis/ClickHouse/MinIO/LangFlow/
   LibreChat volume archives back into their named volumes.
5. Brings the full stack back up.

## After restore

```bash
make health
make logs S=langfuse-web
```

- If Keycloak/Langfuse schema versions differ from the backup, their automatic
  migrations reconcile on start.
- If you restored onto a new host, re-run TLS issuance once DNS is correct:
  `sudo ./scripts/issue-certs.sh`.

## Partial restore

To restore only one database, extract the relevant dump manually:

```bash
gunzip -c backups/<ts>/postgres-langfuse.sql.gz \
  | docker compose exec -T -e PGPASSWORD="$(grep ^POSTGRES_SUPER_PASSWORD .env | cut -d= -f2-)" \
      postgres psql -U postgres -d langfuse
```

## Disaster recovery (new host)

1. Install the platform: `sudo ./install.sh --domain … --email …` (this creates
   `.env` and the volumes/networks).
2. Copy your backup directory onto the host.
3. Run `sudo ./scripts/restore.sh backups/<ts>`.
4. Restore your original `.env` if you need the **same** secrets/keys as before
   (otherwise the freshly generated ones are used).
