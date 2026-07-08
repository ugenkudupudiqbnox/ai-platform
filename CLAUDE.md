# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **production self-hosted AI platform** deployed with Docker Compose on Ubuntu
Server 24.04 LTS. It is **not an application codebase** — there is no
TS/Python/Go product source here. The repo is **infrastructure-as-code**: a
`docker-compose.yml` topology plus the bash lifecycle tooling that provisions,
upgrades, backs up, and re-configures the stack. One command —
`sudo ./install.sh --domain <d> --email <e>` — provisions the whole thing with
generated secrets, four service databases, Keycloak SSO, TLS certs, backups
and monitoring.

The stack (all containers, two networks `edge`/`backend`):
- **Edge**: `nginx` (TLS/HTTP2/rate-limit) + `oauth2-proxy` (guards LangFlow) + `certbot` (on-demand, `tools` profile)
- **Apps**: `librechat` (chat UI), `keycloak` (identity/OIDC), `langflow` (gunicorn web) + `langflow-worker` (Celery) + `flower` (queue dashboard), `langfuse-web` + `langfuse-worker` (observability)
- **Data**: `postgres`, `redis`, `mongo`, `clickhouse`, `minio` + `minio-init` (one-shot bucket creator)
- **Monitoring**: separate compose overlay `monitoring/docker-compose.monitoring.yml` under the `monitoring` profile (Prometheus/Grafana/OTel)

Public subdomains derive from `BASE_DOMAIN`: `chat.`/`auth.`/`flow.`/`trace.`

## Commands

### Validation (no Docker required) — run before every change
```bash
make validate          # compose config -q + shellcheck (if installed)
make test              # all offline self-tests (scripts/*.selftest.sh) with summary
make config            # render the merged compose config for inspection
```
Full CI battery (mirrors `.github/workflows/build.yml`):
```bash
shellcheck -x --source-path=SCRIPTDIR install.sh upgrade.sh uninstall.sh \
  healthcheck.sh scripts/*.sh docker/postgres/init/*.sh docker/langflow/*.sh
docker compose --env-file .env config -q
docker compose -f docker-compose.yml -f monitoring/docker-compose.monitoring.yml config -q
yamllint -d "{extends: relaxed, rules: {line-length: disable}}" docker-compose.yml monitoring/ .github/
envsubst < docker/keycloak/realm.json.tmpl | jq empty   # render realm + validate JSON
jq empty monitoring/grafana/provisioning/dashboards/platform-overview.json
```
No real `.env` for validation? `cp .env.example .env && sed -i 's/__GENERATED__/placeholder/g' .env` — never commit a populated `.env`.

### Running a single self-test
Self-tests are standalone executables that exit non-zero on failure:
```bash
bash scripts/change-domain.selftest.sh     # one suite
```

### Operating a live stack (on a throwaway host — never a box you can't reset)
```bash
sudo ./install.sh --domain ai.example.com --email admin@example.com   # full provision
make ps | make logs S=nginx | make health
make monitoring-up            # Prometheus + Grafana overlay
make scale-workers N=4        # scale LangFlow Celery workers
make upgrade | make uninstall
sudo ./scripts/change-domain.sh --domain new.example.com   # rebrand post-install
```

## Architecture & key conventions

### The `.env` system is the single source of truth
- `.env.example` is the **template** (committed). `scripts/gen-secrets.sh` copies it to `.env` (git-ignored, mode 600) and replaces every `__GENERATED__` placeholder with a random secret. `.env` must never be committed.
- **All** host/domain/secret/image-tag values flow through `.env` → interpolated by `docker compose --env-file .env`. Nothing host- or secret-specific is hard-coded in `docker-compose.yml`. Image tags are env vars (e.g. `KEYCLOAK_IMAGE`), so version pins live in `.env.example`.
- Some secrets are **mirrored** to keep a single source of truth: e.g. `OPENID_CLIENT_SECRET` mirrors `KEYCLOAK_CLIENT_SECRET_LIBRECHAT`, `OAUTH2_PROXY_CLIENT_SECRET` mirrors `KEYCLOAK_CLIENT_SECRET_LANGFLOW`, `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` mirror the Langfuse init project keys, `LANGFLOW_REDIS_QUEUE` embeds `REDIS_PASSWORD`. `gen-secrets.sh` sets these via `set_env ... "$(get_env ...)"`. When changing one, update the mirrors too (see how `change-domain.sh` threads values through).
- Read/write env values only via `scripts/common.sh` helpers `get_env`/`set_env` (idempotent, in-place sed, `|`-delimited). Don't parse `.env` by hand.

### Config rendering (`scripts/common.sh`)
- `render_realm()` runs `envsubst` over an **allow-list** of variables only, reading `docker/keycloak/realm.json.tmpl` → `docker/keycloak/realm.json` (git-ignored). The allow-list keeps unrelated JSON untouched.
- `render_librechat()` just copies `librechat.yaml.tmpl` → `librechat.yaml` (LibreChat resolves `${ENV}` itself at runtime).
- Both rendered artifacts are git-ignored. After editing a `.tmpl`, re-run the renderer (or `install.sh`).

### Shared shell helpers (`scripts/common.sh`) — sourced by every script
`REPO_ROOT`, `ENV_FILE`, `dc()` (always runs `docker compose --env-file .env` from repo root), `require_root`, `require_cmd`, `get_env`/`set_env`, `rand_hex`/`rand_b64url`/`rand_b64`/`rand_uuid`, `render_realm`/`render_librechat`, `wait_for_service`, and the colored loggers (`log`/`info`/`success`/`warn`/`error`/`die`/`heading`). New scripts should `source scripts/common.sh`, use `set -euo pipefail`, and keep `shellcheck`-clean.

### `docker-compose.yml` anchors
Reuse `x-logging`, `x-restart`, `x-security` (`<<: [*default-restart, *hardening]`). Every service needs a healthcheck, `restart` policy, and `logging` config — except where a distroless/no-HTTP image makes one impossible (oauth2-proxy, langfuse-worker); those gate readiness via `depends_on` and a comment explaining the omission.

### Startup ordering & ordering pitfalls (read before touching deps)
- `depends_on: ... condition: service_healthy` chains the boot order; `wait_for_service <svc> <timeout>` in `install.sh` blocks until healthy.
- **oauth2-proxy startup deadlock**: it does OIDC discovery against the **internal** Keycloak (`http://keycloak:8080`) via `OAUTH2_PROXY_SKIP_OIDC_DISCOVERY: true` so it doesn't depend on public DNS/TLS/NGINX being up. The browser-facing login URL stays public; token redeem + JWKS go over the backend network. The issuer claim is still validated against the public URL. Don't "simplify" this back to public discovery.
- **LibreChat OIDC discovery** runs once at startup against `https://auth.<domain>`. It boots before the real Let's Encrypt cert exists (self-signed bootstrap), so `install.sh` and `change-domain.sh` **restart LibreChat after cert issuance** so discovery re-runs against the trusted cert. Keep this restart when changing the TLS/domain flow.
- **LangFlow multi-worker requires Redis**: `LANGFLOW_JOB_QUEUE_TYPE=redis` + `LANGFLOW_REDIS_QUEUE_URL` are mandatory — LangFlow refuses `--workers>1` with the default in-memory build queue. The web tier runs `langflow run` (not raw gunicorn) because that's what mounts the frontend UI; raw gunicorn against `langflow.main:create_app()` returns `{"detail":"Not Found"}` at `/`.
- **LangFuse v3** needs Postgres + ClickHouse + Redis + MinIO; `minio-init` creates the bucket and its completion is the MinIO readiness gate.
- Postgres per-service DBs are provisioned **once** by `docker/postgres/init/01-databases.sh` (least-privilege role+db per service, public schema locked to owner). Runs only on first init of the data volume.

### NGINX config layout
`docker/nginx/conf.d/` is a **directory bind-mount** (edits visible to the running container; `nginx -s reload` applies them — see `make reload-nginx`). `nginx.conf` itself is a single-file mount pinned to its inode at container start, so structural changes there need a recreate. Per-subdomain vhosts: `chat.conf`, `auth.conf`/`default.conf`, `flow.conf`, `trace.conf`, plus shared `ssl.conf`, `proxy.conf`, `ratelimit.conf`, `security-headers.conf`, `metrics.conf`, `acme.conf`, `upstreams.conf`. `upstreams.conf` `include`s `figlinks.conf` (a separate external project's vhost) — keep that include in exactly one place to avoid duplicate server blocks.

### Day-2 / lifecycle scripts (all `sudo`, all source `common.sh`)
`install.sh`, `upgrade.sh`, `uninstall.sh`, `healthcheck.sh` at repo root; `scripts/` holds `common.sh`, `gen-secrets.sh`, `langfuse-keys.sh` (auto-wires LangFlow→Langfuse tracing keys), `bootstrap-certs.sh`/`issue-certs.sh`, `backup.sh`/`restore.sh`, `change-domain.sh`, `wait-for.sh`. `install.sh` installs systemd timers for daily cert renewal (03:30) + daily backup (02:00).

## Self-test convention (follow for any non-trivial script logic)

Logic that can run without the live stack gets an offline self-test (`scripts/<name>.selftest.sh`, run by `make test` and CI). Pattern:
1. **Factor logic into functions** in the target script, and guard `main` with `[ "${BASH_SOURCE[0]}" = "${0}" ]` so the script can be **sourced** (functions callable) without executing main.
2. The self-test sources/mocks: builds a throwaway `.env` + `REPO_ROOT`, installs a mock `docker`/`systemctl`/`id` on `PATH` (see `scripts/selftest-lib.sh`'s `st_make_sandbox` for the integration-test variant that copies the whole repo), then asserts pure logic. `set -uo pipefail` (not `-e`) so assertions can collect; end with `[ "${FAIL}" -eq 0 ]` and print `== results: N passed, M failed ==`.
3. Functions prefixed per-script (e.g. `cd_*` in `change-domain.sh`) so they're addressable from the test.

When adding non-trivial script behavior: factor into functions, guard main, add a `.selftest.sh`.

## Coding standards (from CONTRIBUTING.md)
- **Shell**: `bash`, `set -euo pipefail`, source `scripts/common.sh`, `shellcheck`-clean (justify any `disable` with a comment).
- **Compose/YAML**: 2-space indent; reuse the `x-*` anchors; every service needs healthcheck + restart + logging.
- **Config**: host/secret-specific values belong in `.env` or a `.tmpl` rendered at install — never hard-coded.
- **Docs**: update the relevant `docs/<topic>.md` and any affected tables when behavior changes.
- **Commits**: imperative, Conventional Commits (`feat:`/`fix:`/`docs:`/`ci:`/`refactor:`…).
- **Security**: never commit secrets, populated `.env`, or rendered `realm.json`/`librechat.yaml` (all git-ignored — keep them so). New services should run non-root, drop capabilities, mount config read-only, avoid default creds. See `docs/security.md`.

## Cosmic AR supervisor (live deploy & verification)

The Cosmic AR Agent is a LangFlow-resident supervisor (`SupervisorAgentComponent`,
embedded in `cosmic-ar/flows/supervisor.json`, UUID in `.env`
`LANGFLOW_ADAPTER_FLOW_IDS`) that routes to 9 AR subflows by intent and returns a
§14 envelope. Source lives under `docker/langflow-extensions/ar_common/` (bind
mounted read-only at `/app/extensions:ro` — host `.py` edits are live, no image
rebuild). The adapter (`docker/langflow-adapter/`) forwards envelopes as
OpenAI-schema `message.content`. See `cosmic-ar/docs/contracts.md` for the V1-*
caveat log and `cosmic-ar/docs/session-notes-*.md` for run history.

**Deploy mechanism (no UUID churn):** edit the repo `.py` → re-embed into
`supervisor.json` `nodes[].data.node.template.code.value` **byte-identical**
(`json.dump(indent=2, ensure_ascii=False)`) → in-place `PATCH
/api/v1/flows/<UUID>` (UUID unchanged → **no adapter repoint/recreate**).
DELETE/re-POST would mint a fresh supervisor UUID and force a
`set_env LANGFLOW_ADAPTER_FLOW_IDS` + adapter recreate — avoid it; use PATCH.

**Restart rule (critical, easy to get wrong):**
- **Embedded** component code (`supervisor.py`'s embedded copy in
  `supervisor.json`) is recompiled by lfx **per run → no container restart**.
- **Imported** modules (`agent_state.py`, `envelope.py`, `idempotency.py`,
  other `ar_common` modules) are cached in the long-running LangFlow
  process's `sys.modules` → **require `docker restart aiplatform-langflow-1`**
  after PATCH (then wait ~15s for healthy). Skipping this surfaces as
  `'AgentState' object has no attribute '<new field>'`.

**Verification is the real REST path, not in-process:** run
`docker exec aiplatform-langflow-1 curl -s --compressed -X POST
http://localhost:7860/api/v1/run/<UUID> …` with a **real NL+JSON** message.
This exercises `_finalize_envelope`. In-process `tool.ainvoke` tests bypass it
and can mask defects (a prior "AR_OK" check fed pure `json.dumps(payload)`
straight to the tool and missed the NL-prefix parse bug). Regression-sweep after
any supervisor change: `ar_calculation` (AR_OK + figures under
`data.result`), `ar_audit` (AR_OK), `ar_issue_invoice` (`pending_approval`,
§19 gate intact, `data={action,tier}`), `ar_kitchen_revenue` (error, no
parse-error regression).

**Envelope shape:** on `AR_OK` the supervisor surfaces the routed subflow's §14
result under `data.result` (nested — not flat — so the deferred
`data.execution_summary` per `execution-summary.schema.json` composes later
without restructuring). `pending_approval`/`awaiting_approval` keep
`data={action,tier}` (§19). Full §14 conformance + the LangGraph `Command(resume=)`
§19 resume path are deferred to v2/build-phase.

### Cosmic AR vendor transports (Zoho Books + Foodics, build-phase)

The two vendor-touching subflows (`ar_issue_invoice`, `ar_foodics_processing`)
perform **real HTTP** against vendor sandboxes via pure-Python transports in
`ar_common` (`zoho_transport.RealZoho`, `foodics_transport.RealFoodics`),
**gated on credentials** (absent creds → fail-safe). Key decisions/patterns
(durable — apply to any future vendor transport):

- **Credentials by name from LangFlow Secret Global Variables, not `.env`/flow
  inputs.** The subflow component (built by lfx → carries `user_id`, the
  encrypted-DB lookup key) resolves secrets via `ar_common/vendor_secrets.py`
  `read_secret(self, name)` → `self.variables(name, name)` (lfx sync wrapper;
  unwrap `pydantic.SecretStr` via `get_secret_value()`) → `os.getenv` fallback →
  `default`; the plaintext is threaded into the transport constructor. **No
  `SecretStrInput` is added to the subflow components** → no flow-JSON template
  surgery, no `requiresCredentials` flip (the `ap_tools` components read the
  same names via `SecretStrInput(load_from_db=True)` → single source). Absent
  creds keep the fail-safe (Zoho → `StubZohoUpload`; Foodics → files /
  `AR_NOT_IMPLEMENTED`) so `make test` stays green offline.
- **Transports call `requests` directly → lfx SSRF does NOT gate them.**
  `LANGFLOW_SSRF_ALLOWED_HOSTS` (`lfx/utils/ssrf_protection.py` `is_host_allowed`)
  is a **bypass-list** (allowlisted private hosts skip the blocked-range check),
  NOT a restrict-to-only gate — public vendor hosts are allowed regardless. Do
  not "fix" the allowlist to require the vendor hosts; verify container egress
  (TCP:443) instead.
- **Foodics is OAuth 2.0** (client-id/secret/refresh → 14-day Bearer +
  `X-Business`), NOT a static token — `FOODICS_API_TOKEN` is obsolete; the
  `ar_tools.foodics_ar.FoodicsARTool` scaffold is unused by AR (the real
  transport is `ar_common.foodics_transport.RealFoodics`).
- **Wiring seams (module globals; serial sync `graph.invoke` makes them safe):**
  Zoho via `set_transport(RealZoho(creds))` in `ZohoUploadFlowComponent.run` (no
  reset on no-creds → preserves self-test stubs). Foodics via the
  `set_foodics_creds(creds)` seam set by `FoodicsProcessingFlowComponent.run`
  (resets to `None` on no-creds so a prior run's creds never leak);
  `_make_foodics_fetcher()` returns `RealFoodics(creds)` else `None` (drops the
  broken `from components.ar_tools.foodics_ar import FoodicsARTool` cross-bundle
  import). Transport contract: return the `StubZohoUpload` dict shape and
  **don't raise** for ordinary API errors — the flow's §10 loop owns retry
  (transient = transport-flagged/408/429/5xx; hard 4xx no-retry).
- **Deploy = re-embed + in-place PATCH + restart.** Re-embed the edited `.py`
  byte-identical into `cosmic-ar/flows/<flow>.json`
  `nodes[].data.node.template.code.value` (`json.dump(indent=2,
  ensure_ascii=False)`) → in-place `PATCH /api/v1/flows/<UUID>` (UUID unchanged
  → no adapter repoint) → `docker restart aiplatform-langflow-1` (the new
  transports are imported modules cached in `sys.modules`). Live subflow UUIDs:
  `ar_issue_invoice`=b5b49e24…, `ar_foodics_processing`=87d38266….
- **Infrasys is NOT integrated** (no flow/transport; zero repo references) —
  partner-gated via Shiji (`developer.hero-cloud.com`,
  `hk-infrasys-api-enquiry.list@shijigroup.com`). Long-lead; start the email in
  parallel, don't look for Infrasys code.
- **Live real-vendor verification is gated on the operator** creating the Secret
  Global Variables in the LangFlow UI (Zoho: `ZOHO_CLIENT_ID/CLIENT_SECRET/
  REFRESH_TOKEN/ORG_ID` + `ZOHO_BOOKS_API_URL/ACCOUNTS_URL`; Foodics:
  `FOODICS_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN/BUSINESS_ID` +
  `FOODICS_API_URL/TOKEN_URL`). Setup guide + per-vendor obtain steps:
  `cosmic-ar/docs/environment.md`. Notes: `cosmic-ar/docs/session-notes-
  2026-07-08-vendor-transports.md`; caveat close-out: `cosmic-ar/docs/contracts.md`
  `V1-STUB`.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
