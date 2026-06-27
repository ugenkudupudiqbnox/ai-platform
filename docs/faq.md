# FAQ

**Q: Can I run this without a public domain?**
Yes. Install with the defaults (or `--skip-ssl`) and the platform uses
self-signed certificates. You'll get browser warnings, and OIDC redirects use
whatever hostnames you set in `.env`. For real use, point DNS at the host and run
`sudo ./scripts/issue-certs.sh`.

**Q: Where are my passwords?**
In `.env` (mode `600`). The installer also prints the important ones at the end.
```bash
grep -E '_PASSWORD=' .env
```

**Q: Why is Langfuse pulling in ClickHouse and MinIO?**
Langfuse v3 (the current line) stores analytics in ClickHouse and blobs in S3.
This deployment uses MinIO as the S3 backend. The Postgres-only setup only exists
on the legacy v2 line.

**Q: Why is LangFlow behind oauth2-proxy instead of "real" OIDC?**
Open-source LangFlow has no built-in OIDC. oauth2-proxy enforces Keycloak login
at the edge and proxies authenticated users to LangFlow. See
[oidc.md](oidc.md).

**Q: Do I need MongoDB? It wasn't in the original list.**
Yes — LibreChat requires MongoDB as its primary datastore.

**Q: How do I add model providers?**
Set the relevant keys in `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GOOGLE_API_KEY`, the `AZURE_OPENAI_*` group, `OLLAMA_BASE_URL`) and run
`make up`. Empty keys disable that provider.

**Q: Is LangFlow → Langfuse tracing automatic?**
Yes. Langfuse is bootstrapped with deterministic API keys, which are injected
into LangFlow (`LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST`). No manual wiring.

**Q: How do I scale for more load?**
Increase `LANGFLOW_WORKERS` (web) and `langflow-worker` replicas, scale
`langfuse-worker`, and size Redis/Postgres/ClickHouse accordingly. See
[scaling.md](scaling.md).

**Q: How do I expose Grafana or Flower publicly?**
They're internal by default. Either tunnel over SSH or add an NGINX vhost
(copy `trace.conf` and point it at `grafana:3000` / `flower:5555`, ideally behind
oauth2-proxy).

**Q: Can I change the subdomains (e.g. `chatbot.` instead of `chat.`)?**
The NGINX vhosts match on the `chat./auth./flow./trace.` prefixes. Changing them
means editing the regex `server_name` in `docker/nginx/conf.d/*.conf`, the
derived `*_HOST` values, and the Keycloak redirect URIs.

**Q: Is it safe to re-run `install.sh`?**
Yes. It preserves existing secrets and only re-applies idempotent steps. Use
`--force-secrets` to rotate everything.

**Q: How do I update?**
`sudo ./upgrade.sh` (takes a backup, pulls/rebuilds, recreates, health-checks).

**Q: How do I completely remove it?**
`sudo ./uninstall.sh` (keeps data) or `sudo ./uninstall.sh --volumes` (deletes
data) or `--purge` (also removes rendered config and timers).
