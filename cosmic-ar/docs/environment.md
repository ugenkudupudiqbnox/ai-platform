# Cosmic AR Agent — Environment variables

Single reference for every variable the agent touches. Three kinds: `.env`
(platform template, interpolated by `docker compose --env-file .env`), LangFlow
**Secret Global Variables** (managed in the LangFlow UI, never in `.env`), and
**build-phase** vars (not yet wired into `docker-compose.yml`/`gen-secrets.sh`).
See constitution §16/§17/§18.

## `.env` (defined in [`../../.env.example`](../../.env.example))

| Variable | Purpose | Source | Status |
|----------|---------|--------|--------|
| `LANGFLOW_ADAPTER_FLOW_IDS` | UUID of the supervisor flow — the OpenAI adapter maps LibreChat's `model` to it (`docker/langflow-adapter/adapter.py`) | `.env` → `langflow-openai-adapter` (`LANGFLOW_FLOW_IDS`) | Existing (referenced in `docker-compose.yml`; set after the supervisor flow is imported) |
| `LANGFLOW_DEACTIVATE_TRACING` | `true` — Langfuse tracing off (§11 caveat); checkpoints must be self-sufficient for resume | hard-coded in `docker-compose.yml` | Existing |
| `LANGFLOW_SSRF_ALLOWED_HOSTS` | Egress allow-list — must include Zoho Books + Foodics hosts (§16) | `.env` → `langflow` | Existing (set to allow the AR source hosts) |
| `LANGFLOW_WORKERS` / `LANGFLOW_WORKER_TIMEOUT` / `LANGFLOW_WORKER_AUTOSCALE` / `LANGFLOW_WORKER_REPLICAS` | LangFlow web + Celery worker sizing | `.env` → `docker-compose.yml` | Existing (see [Scaling](../../docs/scaling.md)) |
| `LANGFLOW_LOG_LEVEL` | App log level (§12) | `.env` → `docker-compose.yml` | Existing |

## `.env` — Cosmic AR Agent section (added by this scaffold)

| Variable | Purpose | Default / convention | Status |
|----------|---------|----------------------|--------|
| `AR_AGENT_DB_NAME` | Checkpoint database name | `ar_agent` | Build-phase (needs `docker-compose.yml` postgres env wiring) |
| `AR_AGENT_DB_USER` | Least-privilege role for the checkpoint DB | `ar_agent` | Build-phase |
| `AR_AGENT_DB_PASSWORD` | Role password | `__GENERATED__` — **extend `scripts/gen-secrets.sh` at build phase** | Build-phase |
| `AR_APPROVAL_AUTO_MATCH_CEILING` | Amount at/above which an auto-match action escalates from `auto` to `approval` (§19) — a build-phase payment-matching knob (no matching subflow is implemented in v1) | (unset = no auto-match; tune at build phase) | Build-phase |
| `AR_APPROVAL_DUAL_CONTROL_CEILING` | Amount at/above which refunds/write-offs require `dual-control` (§19) | (unset; tune at build phase) | Build-phase |

## LangFlow Secret Global Variables (managed in the LangFlow UI — **not** `.env`)

| Variable | Used by | Notes |
|----------|---------|-------|
| `ZOHO_CLIENT_ID` | `ZohoBooksARTool` | Zoho OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | `ZohoBooksARTool` | Zoho OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | `ZohoBooksARTool` | Long-lived refresh token (auto-refreshes the access token) |
| `ZOHO_ORG_ID` | `ZohoBooksARTool` | Zoho Books organization ID |
| `FOODICS_API_TOKEN` | `FoodicsARTool` | FOODICS API bearer token |

> These are referenced from `SecretStrInput(..., load_from_db=True)` so only the
> variable *name* is stored in the flow JSON — never the secret value (§16).

## LangFlow Global Variables (build-phase, non-secret — §17)

The Calculation Flow (`ar_calculation`) computes its nine figures from a
declarative ruleset whose rates are **tunables** (§17). v1 reads concrete rate
values from the payload's `parameters` block; the rules reference them as
`parameters.<rate>`. The forward path is to resolve those rates from **plain
(non-secret) LangFlow Global Variables** at build time via the engine's
`$GV:NAME` token (ADR-0008 §9). The repo only evidences
`SecretStrInput(load_from_db=True)` for secrets today — there is no plain-number
Global Variable input type wired — so these are documented build-phase vars,
not yet wired.

| Variable | Used by | Notes |
|----------|---------|-------|
| `VAT_RATE` | `ar_calculation` (via the rules `parameters.vat_rate`) | VAT rate as a decimal (`0.15` = 15%) — non-secret, plain |
| `MUNICIPALITY_TAX_RATE` | `ar_calculation` (via `parameters.municipality_rate`) | Municipality tax rate, decimal — non-secret, plain |
| `ROYALTY_RATE` | `ar_calculation` (via `parameters.royalty_rate`) | Royalty rate, decimal — non-secret, plain |
| `DISCOUNT_RATE` | `ar_calculation` (via `parameters.discount_rate`) | Discount rate, decimal — non-secret, plain |

> These are **not** secrets (§16) and so do **not** use `SecretStrInput`; they
> are §17 tunables. A missing rate in the payload defaults to `"0.00"` + an
> `AR_VALIDATION_MISSING_RATE` warning (not a hard fail), so a partial payload
> still produces reviewable zeroed figures. Wiring the `$GV:` injection is
> build-phase (see [calculation.md](calculation.md)).

## Build-phase wiring summary

1. Add `AR_AGENT_DB_*` to the `postgres` service environment in `docker-compose.yml`.
2. Add `rand_hex` generation of `AR_AGENT_DB_PASSWORD` to `scripts/gen-secrets.sh`.
3. Pass `AR_AGENT_DB_*` + a SQLAlchemy URL onto the `langflow` service in
   `docker-compose.yml` so `CheckpointComponent` can reach the `ar_agent` DB.
4. Set `LANGFLOW_ADAPTER_FLOW_IDS` to the supervisor flow UUID after import.
5. Allow Zoho Books / Foodics egress in `LANGFLOW_SSRF_ALLOWED_HOSTS`.

> The scaffold does none of the above automatically — it keeps `make validate`/CI
> green and documents these as build-phase steps (see
> [`../README.md#build-phase-platform-integration`](../README.md)).