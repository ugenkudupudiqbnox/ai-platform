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
| `LANGFLOW_SSRF_ALLOWED_HOSTS` | SSRF bypass-list — specifically-allowed private/internal hosts that would otherwise be SSRF-blocked (not a restrict-to-only gate; public hosts are allowed by default). The AR vendor transports call `requests` directly and bypass this layer, so the vendor hosts do **not** need to be listed (§16) | `.env` → `langflow` | Existing |
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

These are the vendor sandbox credentials the AR subflows' real transports
(`RealZoho` / `RealFoodics`, in `ar_common`) resolve **by name** at runtime via
`vendor_secrets.read_secret(component, name)` — the subflow component (built by
lfx, so it carries `user_id`) calls lfx's variable store and threads the
plaintext into the transport constructor. No `SecretStrInput` is added to the
subflow components → no flow-JSON template surgery, no `requiresCredentials`
flip. The existing `ap_tools` components (`ZohoBooksAPTool`, `FoodicsAPTool`)
read the **same** variables by name via `SecretStrInput(load_from_db=True)` → a
single source of truth shared by the AP and AR tooling.

Only the variable **name** is referenced; the secret value never lives in the
flow JSON or `.env` (§16). When a required cred is absent, the subflow keeps its
fail-safe behaviour (Zoho → `StubZohoUpload`; Foodics → files path /
`AR_NOT_IMPLEMENTED`), so offline self-tests and the no-creds path stay green.

### Zoho Books (AR `ar_issue_invoice` + AP `ZohoBooksAPTool`)

| Variable | Purpose | Type |
|----------|---------|------|
| `ZOHO_CLIENT_ID` | OAuth app client ID | Credential |
| `ZOHO_CLIENT_SECRET` | OAuth app client secret | Credential |
| `ZOHO_REFRESH_TOKEN` | Long-lived refresh token (access tokens auto-refresh on 401) | Credential |
| `ZOHO_ORG_ID` | Zoho Books organization ID | Generic |
| `ZOHO_BOOKS_API_URL` | Books API base (default `https://www.zohoapis.com/books/v3/`) | Generic |
| `ZOHO_ACCOUNTS_URL` | OAuth/accounts region (default `https://accounts.zoho.com`) | Generic |

### Foodics (AR `ar_foodics_processing` + AP `FoodicsAPTool`)

| Variable | Purpose | Type |
|----------|---------|------|
| `FOODICS_CLIENT_ID` | OAuth client ID | Credential |
| `FOODICS_CLIENT_SECRET` | OAuth client secret | Credential |
| `FOODICS_REFRESH_TOKEN` | 14-day refresh token (Bearer access tokens auto-refresh on 401) | Credential |
| `FOODICS_BUSINESS_ID` | Sent as the `X-Business` header | Generic |
| `FOODICS_API_URL` | API base (default `https://api.foodics.com/v2/`; set the sandbox base) | Generic |
| `FOODICS_TOKEN_URL` | OAuth token endpoint (default `https://api.foodics.com/oauth/token`; verify for sandbox) | Generic |

> Foodics is OAuth 2.0 (client_id/secret → 14-day Bearer + refresh +
> `X-Business`), **not** a static token. The prior `FOODICS_API_TOKEN` variable
> is obsolete — the static-token `FoodicsARTool` scaffold was replaced by
> `RealFoodics` (the cross-bundle import that always returned `None` was
> dropped).

### How to obtain the credentials

**Zoho Books (sandbox / test org):**
1. Sign in to the Zoho API Console (<https://api-console.zoho.com>) in the data
   center that matches your Books org (US → `accounts.zoho.com` /
   `www.zohoapis.com`; EU → `.eu`; IN → `.in`; …). Create a **Self Client** (or
   Server-based) OAuth app → note `Client ID` + `Client Secret`.
2. Generate a **refresh token**: with the Self Client, enter the scopes
   `ZohoBooks.invoices.CREATE,ZohoBooks.invoices.DELETE,ZohoBooks.invoices.READ`
   (add `ZohoBooks.contacts.READ` for customer lookups), then *View Refresh
   Token*. Copy the **refresh token** — it is long-lived and does not expire.
3. In Zoho Books (the sandbox/test org), open **Settings → Organizations** →
   copy the **Organization ID**.
4. Set `ZOHO_BOOKS_API_URL` / `ZOHO_ACCOUNTS_URL` to the matching region (omit to
   use the US defaults).

**Foodics (sandbox):**
1. Sign in to the Foodics developer portal (<https://apidocs.foodics.com>) / the
   sandbox console (`console-sandbox.foodics.com`). Create/identify your OAuth
   client → note `Client ID` + `Client Secret`.
2. Complete the OAuth authorization-code flow once against the sandbox to obtain
   your first **access token** + **refresh token** (14-day lifetime; the
   transport auto-refreshes the access token on 401 and keeps the rotated
   refresh token in memory for the run).
3. Note the **Business ID** (sent as the `X-Business` header) from the sandbox
   console.
4. Set `FOODICS_API_URL` + `FOODICS_TOKEN_URL` to the **sandbox** endpoints
   (verify the exact sandbox hosts against apidocs.foodics.com — the defaults
   point at the production API).

**Infrasys:** **not integrated** — there is no Infrasys flow or transport in the
repo (zero references). Infrasys is partner-gated through **Shiji** (developer
endorsement required): request access at `developer.hero-cloud.com` and email
`hk-infrasys-api-enquiry.list@shijigroup.com` for endorsement. This is long-lead
— start the email in parallel; no Infrasys Secret Global Variables exist yet.

### How to create them in the LangFlow UI

1. Open LangFlow (`flow.<domain>`) → **Settings** (sidebar gear).
2. **Global Variables → New Variable**.
3. For each variable above:
   - **Name**: the exact variable name (e.g. `ZOHO_CLIENT_ID`).
   - **Type**: `Credential` for secrets (`*_CLIENT_ID`, `*_CLIENT_SECRET`,
     `*_REFRESH_TOKEN`); `Generic` for non-secret config (`*_ORG_ID`,
     `*_BUSINESS_ID`, `*_API_URL`, `*_ACCOUNTS_URL`, `*_TOKEN_URL`).
   - **Value**: paste the plaintext from the vendor console.
4. Save. Credential-type values are stored encrypted, keyed to your LangFlow
   user; the AR subflows + AP tools resolve them by name at runtime.

> **Resolution order** (`vendor_secrets.read_secret`): 1. LangFlow Secret Global
> Variable (encrypted DB, by `user_id` + name) → 2. `os.getenv(name)` (offline
> dev / no-`user_id` contexts) → 3. `default`. A `None` for any required cred
> keeps the fail-safe path.

### Verify (live, after the variables exist)

Real REST run through the supervisor (ground truth, not in-process — pattern P1):

```bash
docker exec aiplatform-langflow-1 curl -s --compressed -X POST \
  http://localhost:7860/api/v1/run/a6fa4f88-1b52-40a6-b4ed-1338d25f582a \
  -H 'Content-Type: application/json' \
  -d '{"input_value":"<NL + JSON request>","input_type":"chat","output_type":"chat","session_id":"verify-1"}'
```

- `ar_issue_invoice` with a valid `approval_ref` → `AR_OK` with a **real
  `zoho_id`** created in the Zoho test org (confirm in the Zoho Books UI); an
  idempotent re-run with the same `invoice_number` → `duplicate=true`, no second
  invoice.
- `ar_foodics_processing` (after approval) with `source_mode=api` → `AR_OK` with
  real order / order_items / order_payments rows from the Foodics sandbox.
- Regression sweep: `ar_calculation` / `ar_audit` still `AR_OK`;
  `ar_issue_invoice` without `approval_ref` still `AR_FORBIDDEN` (§19 intact).

Egress: the langflow container reaches the vendor hosts over TCP:443 (verified
for `accounts.zoho.com`, `www.zohoapis.com`, `api.foodics.com`,
`console-sandbox.foodics.com`). The transports call `requests` directly, so
lfx's SSRF layer does not gate them; `LANGFLOW_SSRF_ALLOWED_HOSTS` (a bypass-list
for specifically-allowed private hosts, not a restrict-to-only gate) does not
need the public vendor hosts.

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
5. Create the Zoho Books + Foodics **Secret Global Variables** in the LangFlow
   UI (see [the setup guide above](#langflow-secret-global-variables-managed-in-the-langflow-ui--not-env)).
   The transports egress over `requests` directly (SSRF does not gate them), so
   no `LANGFLOW_SSRF_ALLOWED_HOSTS` change is needed for the public vendor hosts.

> The scaffold does none of the above automatically — it keeps `make validate`/CI
> green and documents these as build-phase steps (see
> [`../README.md#build-phase-platform-integration`](../README.md)).