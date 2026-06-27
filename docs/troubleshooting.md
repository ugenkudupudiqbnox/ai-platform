# Troubleshooting

## General

```bash
make ps                 # what's up / health state
make health             # full snapshot (resources, queue, DB metrics)
make logs S=<service>   # tail a single service
docker compose --env-file .env logs --tail=200 <service>
```

## TLS / certificates

**Symptom:** browser shows a self-signed/untrusted certificate.
- DNS for `chat/auth/flow/trace.<domain>` must resolve to the server and ports
  80/443 must be reachable. Then:
  ```bash
  sudo ./scripts/issue-certs.sh
  ```
- Test issuance without hitting rate limits using staging first:
  ```bash
  sed -i 's/^ACME_STAGING=.*/ACME_STAGING=1/' .env
  sudo ./scripts/issue-certs.sh
  ```
  Switch back to `ACME_STAGING=0` and re-issue for a trusted cert.

**Symptom:** NGINX won't start (no certificate).
- Ensure the bootstrap cert exists:
  ```bash
  sudo ./scripts/bootstrap-certs.sh
  docker compose --env-file .env up -d nginx
  ```

**Renewals:** handled by `aiplatform-certbot-renew.timer`. Force a check:
```bash
sudo systemctl start aiplatform-certbot-renew.service
journalctl -u aiplatform-certbot-renew.service --no-pager
```

## HTTP/3 (QUIC)

The stock `nginx:1.27-alpine` image is **not** compiled with QUIC, so HTTP/3 is
shipped as commented config. To enable it:
1. Use a QUIC-enabled NGINX image (e.g. build from `nginxinc/nginx-quic`) and set
   `NGINX_IMAGE` to it.
2. Uncomment `- "443:443/udp"` in `docker-compose.yml`.
3. Add `listen 443 quic reuseport;` and `add_header Alt-Svc 'h3=":443"';` to the
   vhosts.

## Keycloak

**Realm not imported / need to re-import:**
Import only runs on first boot. To re-import:
```bash
docker compose --env-file .env stop keycloak
docker compose --env-file .env rm -f keycloak
# (optional) wipe the realm via the admin console first, then:
docker compose --env-file .env up -d keycloak
```

**Login redirect mismatch:** confirm the public hostnames in `.env`
(`AUTH_HOST` etc.) match your DNS and the client redirect URIs in the realm.

## Langfuse

**`langfuse-web` unhealthy:** it depends on Postgres, ClickHouse, Redis and the
MinIO bucket. Check ordering:
```bash
make logs S=clickhouse
make logs S=minio-init   # must complete successfully (bucket creation)
make logs S=langfuse-web
```

**ClickHouse connection errors:** ensure `clickhouse` is healthy and
`CLICKHOUSE_USER/PASSWORD` match in `.env`.

**Redis db note:** Langfuse uses a single `REDIS_CONNECTION_STRING` (db 3). If
you point Langfuse at an external Redis, give it a dedicated instance/db.

## LangFlow workers

**Workers crash-loop with "no Celery application":** your pinned LangFlow version
doesn't expose the task queue. The web tier still works; queued execution is
unavailable. Pin a version with the feature or reduce `LANGFLOW_WORKER_REPLICAS`
to 0:
```bash
docker compose --env-file .env up -d --scale langflow-worker=0
```

**Queue stuck:** inspect with Flower or:
```bash
make health   # shows celery queue length
```

## LibreChat

**Mongo auth errors:** verify `MONGO_INITDB_ROOT_*` in `.env` match the running
`mongo` container (recreate the volume if you changed them after first boot).

**OIDC button missing:** ensure `ALLOW_SOCIAL_LOGIN=true` and the `OPENID_*`
variables are set; check `make logs S=librechat`.

## Resetting a single service's data

```bash
docker compose --env-file .env stop <service>
docker volume rm aiplatform_<volume>     # e.g. aiplatform_langflow_data
docker compose --env-file .env up -d <service>
```

## Full reset

```bash
sudo ./uninstall.sh --volumes   # removes containers + data (irreversible)
sudo ./install.sh --domain … --email …
```
