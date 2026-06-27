# Backup

## What is backed up

`scripts/backup.sh` produces a timestamped directory under `$BACKUP_DIR`
(default `./backups/<YYYYmmdd-HHMMSS>/`) containing:

| Artifact | Source | Method |
|----------|--------|--------|
| `postgres-<db>.sql.gz` | Postgres (postgres, keycloak, langflow, langfuse) | `pg_dump` (logical) |
| `mongo.archive.gz` | MongoDB (LibreChat) | `mongodump --archive` |
| `redis-data.tar.gz` | Redis | `SAVE` + volume tar |
| `clickhouse-data.tar.gz` | ClickHouse (Langfuse analytics) | volume tar |
| `minio-data.tar.gz` | MinIO (Langfuse blobs) | volume tar |
| `langflow-data.tar.gz` | LangFlow config/state | volume tar |
| `librechat-images.tar.gz`, `librechat-uploads.tar.gz` | LibreChat files | volume tar |
| `MANIFEST.txt` | — | inventory + timestamp |

## Running a backup

```bash
sudo ./scripts/backup.sh      # or: make backup
```

## Scheduling

`install.sh` installs a systemd timer that runs a full backup **daily at 02:00**
(`aiplatform-backup.timer`). Check it:

```bash
systemctl list-timers | grep aiplatform
systemctl status aiplatform-backup.timer
journalctl -u aiplatform-backup.service --no-pager
```

## Retention

Backups older than `BACKUP_RETENTION_DAYS` (default 14) are pruned automatically
at the end of each run. Adjust in `.env`:

```
BACKUP_RETENTION_DAYS=30
```

## Off-host copies

Volume tars and DB dumps are self-contained; sync the `backups/` directory to
off-site storage, e.g.:

```bash
rclone sync ./backups remote:ai-platform-backups
# or
aws s3 sync ./backups s3://my-bucket/ai-platform-backups
```

## Consistency notes

- Postgres and Mongo dumps are logical and transaction-consistent.
- ClickHouse and MinIO are captured as volume archives; for very high write
  volumes consider pausing `langfuse-worker` during the backup window for a fully
  quiescent snapshot.
- `.env` (your secrets) is **not** included in backups — store it securely and
  separately.
