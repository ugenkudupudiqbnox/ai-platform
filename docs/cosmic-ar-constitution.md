# Cosmic AR Agent — Project Constitution

The governing engineering-standards document for the **Cosmic AR Agent** — the
accounts-receivable automation agent for Cosmic Vikings Restaurants Management.
Every flow, custom component, and agent in this project must comply with the
sections below. This document governs **agent flows and custom `lfx` Components
only**; platform infrastructure (docker-compose topology, SSO, TLS, backups,
monitoring) stays governed by the existing [docs](architecture.md) set and is
cross-linked where the agent depends on it.

> **Authority.** This is a constitution, not a style guide. Where a section
> prescribes a standard, it is binding. Deviations require a written waiver
> recorded in the flow's README and a linked ADR.

## 1. Product Vision

The Cosmic AR Agent autonomously operates the accounts-receivable (AR) lifecycle
for Cosmic Vikings Restaurants Management: invoice presentment, payment
matching and reconciliation against Foodics POS receipts, dunning of overdue
customers, and GL posting into Zoho Books. The agent is deterministic where the
rules are fixed and LLM-driven where judgement is required, with a
**human-in-the-loop gate before any action that moves money or mutates the
ledger**. The platform it runs on is the self-hosted AI platform in this repo —
LangFlow (web + Celery worker tiers) for orchestration, LibreChat as the
human approval surface, Keycloak for identity, and Langfuse for observability.

> **North star:** no customer money moves, and no ledger entry posts, without
> a recorded, SSO-attributable approval.

## 2. Scope

Current scope is accounts receivable only. Each capability maps to a LangFlow
construct (flow, custom component, or built-in node).

| In-scope capability | LangFlow construct | Source system | Status |
|---------------------|---------------------|---------------|--------|
| AR invoice presentment (Zoho Books) | flow + `ZohoBooksAPTool` read calls | Zoho Books | current |
| Payment receipt matching | flow + `FoodicsAPTool` (POS receipts) | Foodics POS | current |
| Reconciliation (receipt ↔ invoice) | agent node + reconciliation logic | Zoho Books + Foodics | current |
| Dunning (overdue reminders) | flow + templated comms node | Zoho Books | current |
| GL posting of received payments | flow + Zoho Books write call | Zoho Books | current (approval-gated) |
| AR reporting / aging | flow + reporting node | Zoho Books + Foodics | current |
| Human approval capture | LangFlow approval node + LibreChat channel | platform SSO | current |

## 3. Out of Scope

The following are explicitly out of scope for the AR agent and must not be built
into AR flows. Anything in this list requires a §20 extension before it is
touched.

- **Accounts Payable (AP)** — bills, vendor payments, supplier management
  (the existing `ap_tools` bundle is the seed for the future AP agent; see
  §20).
- Payroll and employee compensation.
- Tax filing, VAT/Saudi Zakat calculation, and statutory returns.
- Bank-account creation, bank-to-bank transfers, and treasury.
- Inventory and supply-chain purchasing decisions (Foodics *purchase orders*
  are read for reconciliation, not authored).
- Anything outside the AR lifecycle unless added through §20.

## 4. Design Principles

1. **Idempotency first.** Any action that mutates financial state carries a
   stable idempotency key; a replay produces the same end state, never a
   duplicate.
2. **Least-privilege credentials.** Each tool receives only the credentials it
   needs, sourced from LangFlow Secret Global Variables — never from flow JSON.
3. **Deterministic state.** Given the same inputs and state, an agent run
   produces the same output. Randomness and wall-clock are explicit, injected
   inputs, not hidden side effects.
4. **Fail safe over fail fast.** On uncertainty about a financial action, the
   agent stops and requests human approval rather than guessing.
5. **Human approval before financial mutation.** No money moves and no ledger
   entry posts without a captured, SSO-attributable approval (§19).
6. **Observability is non-optional.** Every run emits structured logs (§12)
   and an audit record (§13); tracing is wired where the platform supports it
   (§11 caveat).
7. **No secret in flow JSON.** Secrets live in Secret Global Variables; only
   the variable *name* is stored in the flow.
8. **Reuse over rebuild.** Prefer built-in LangFlow components and the existing
   `ap_tools` bundle (§15) before writing a new component.

## 5. Coding Standards

Custom components are Python classes in an extension bundle (see §7). They
subclass `lfx.custom.Component` and use only `lfx.io` / `lfx.schema` imports for
LangFlow integration, mirroring the existing
`docker/langflow-extensions/ap_tools/components/ap_tools/` components.

| Aspect | Standard |
|--------|----------|
| Base class | `lfx.custom.Component` |
| LangFlow imports | `from lfx.custom import Component`; `from lfx.io import ...`; `from lfx.schema import Message` |
| External I/O | `requests` (already used); pin per-bundle in `pyproject.toml` |
| Type hints | Required on all public/output methods |
| `name` attribute | Bare class name (`name = "ZohoBooksAPTool"`) so existing flow nodes match the registry by `data.type` without re-adding from the palette |
| `display_name` / `description` / `icon` | Always set; `description` tells the LLM when to call the tool |
| Credential inputs | `SecretStrInput(..., load_from_db=True)` only — never `MessageTextInput` for a secret |
| Error propagation | Catch expected exceptions at the output-method boundary and return a `Message` per §9/§14; never `raise` out of a tool's output method |
| Logging | `self.log(...)` for in-component events; never `print` |
| Shell (if any) | `set -euo pipefail`, `shellcheck`-clean, source `scripts/common.sh` |

```python
from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output, SecretStrInput
from lfx.schema import Message


class CosmicARTool(Component):
    name = "CosmicARTool"          # bare class name — registry addressable
    display_name = "Cosmic AR Tool"
    description = "When to call this tool, in one sentence for the LLM."
    icon = "BookOpen"

    inputs = [
        SecretStrInput(name="api_token", display_name="API Token",
                       info="Select the XXX_API_TOKEN Secret Global Variable.",
                       required=True, load_from_db=True),
        DropdownInput(name="operation", display_name="Operation",
                      options=["list_invoices", "get_invoice"],
                      value="list_invoices", tool_mode=True),
    ]
    outputs = [Output(name="ar_tool_output", display_name="AR Tool Result",
                      method="run")]
```

## 6. Naming Conventions

| Artifact | Convention | Example |
|----------|-----------|---------|
| Flow ID (display) | `ar_<verb>_<object>` | `ar_file_intake`, `ar_calculation` |
| Custom component class | `PascalCase`, suffix `…Tool` or `…Component` | `ZohoBooksAPTool`, `ReconcileComponent` |
| Bundle directory | lowercase `snake_case` (enforced) | `ap_tools`, `ar_tools` |
| Extension `id` in `extension.json` | kebab-case | `ap-tools`, `ar-tools` |
| Secret Global Variable | `UPPER_SNAKE` | `ZOHO_CLIENT_ID`, `FOODICS_API_TOKEN` |
| Component input `name` | `snake_case` | `zoho_client_id`, `entity_id` |
| Output `name` | `<bundle>_tool_output` | `ap_tool_output` |
| State keys (`AgentState`) | `snake_case` | `pending_approvals`, `matched_receipts` |

> The inline-bundle directory name is validated against the bundle-name
> pattern and **must** be lowercase `snake_case`; hyphens are rejected with
> `inline-bundle-name-invalid`. The `id` in `extension.json` may stay
> kebab-case — only the directory name is constrained.

## 7. Folder Structure

Tooling ships as inline extension bundles under `docker/langflow-extensions/`,
bind-mounted read-only into the `langflow` container at `/app/extensions` with
`LANGFLOW_COMPONENTS_PATH=/app/extensions`. Flow *definitions* are not files —
they live in the LangFlow Postgres database (`LANGFLOW_DB_NAME`).

```text
docker/langflow-extensions/
  <bundle>/                       # snake_case; one bundle per source system / domain
    extension.json                # manifest: id, name, version, bundle, capabilities
    pyproject.toml                # pip metadata + [tool.langflow.extension] manifest ref
    README.md                     # what the bundle exposes + credential list
    components/<bundle>/
      __init__.py
      <tool>.py                   # one Component class per file
```

```text
# What lives where
Flows (graph JSON)        → LangFlow Postgres DB (LANGFLOW_DB_NAME) — NOT on disk
Tool source (Python)      → docker/langflow-extensions/<bundle>/components/
Secrets                   → LangFlow Secret Global Variables (managed in UI)
LangFlow config/state     → langflow_data named volume (/var/lib/langflow)
```

## 8. State Management Standards

Agent state is the single, typed, resumable representation of an in-flight AR
run. It is owned by the agent graph, not by component closures.

- **Typed schema.** State is a typed `AgentState` (dataclass or typed dict).
  Every field consumed by a downstream node is declared; undeclared fields are
  rejected, not silently passed through.
- **Immutable updates.** Nodes return *new* state fragments; they never mutate
  shared state in place. Financial running totals (`matched_amount`,
  `outstanding_balance`, `posted_total`) are explicit, named fields.
- **No hidden state.** Pure tool Components are stateless — all context comes
  from inputs. The only stateful node is the agent orchestrator; side effects
  live in source systems (Zoho Books, Foodics), not in component attributes.
- **Approval state is first-class.** Pending approvals live in
  `pending_approvals` as structured records (`approval_id`, `action`,
  `amount`, `requested_by`, `requested_at`), not as free text.
- **Resume determinism.** A loaded checkpoint plus the same external state
  must reproduce the same decision; reads are re-fetched, never cached in
  state across runs.

## 9. Error Handling Standards

| Error class | When | Handling |
|-------------|------|----------|
| `CredentialError` | Secret Global Variable missing/empty | Fail fast, return `status=error`, `code=AR_CREDENTIAL_MISSING`; no retry |
| `ValidationError` | Input/entity ID invalid | Return `error`, `code=AR_VALIDATION`; no retry |
| HTTP 401 | Token expired | One re-credential retry (mirror `ZohoBooksAPTool` refresh-on-401), then surface error |
| HTTP 403 | Authorization denied | Return `error`, `code=AR_FORBIDDEN`; alert; no retry |
| HTTP 404 | Entity not found | Return `error`, `code=AR_NOT_FOUND`; not retried as transient |
| HTTP 408 / 429 | Timeout / rate limit | Retry per §10; respect `Retry-After` on 429 |
| HTTP 5xx | Upstream fault | Retry per §10 as transient |
| `ConnectionError` / `Timeout` | Network | Retry per §10 |
| `Exception` (unexpected) | Anything else | Return `error`, `code=AR_UNEXPECTED`; log full traceback server-side; never leak to caller |

- Component output methods **never raise**; they catch at the boundary and
  return a `Message` whose text conforms to the §14 envelope, so a tool error
  cannot crash the graph.
- The agent graph has a **single error node** that maps tool errors to the
  JSON envelope and decides stop-vs-retry per §10.
- No `except: pass`. Every handler either retries, returns a structured error,
  or escalates to human approval.

## 10. Retry Standards

| Transient? | Max attempts | Backoff | Jitter | Give-up action |
|-----------|--------------|---------|--------|----------------|
| Yes (5xx, 408, 429, network) | 3 | exponential, base 1s × 2^n | ±25% full jitter | Return `error`, `code=AR_UPSTREAM`; escalate if financial |
| 401 (token) | 1 re-credential, then 1 replay | immediate | none | Return `error`, `code=AR_AUTH` |
| Non-transient (4xx except 408/429) | 0 | — | — | Return structured error |

- **No retry on 4xx** except 408/429. A 400 is a request bug, not a transient
  fault.
- **Idempotency keys are mandatory** for any POST that mutates financial state
  (GL post, invoice issuance, refund). Retries replay the same key; the
  upstream deduplicates.
- **`Retry-After` is honored** on 429 exactly when present.
- **Backoff is bounded** — total retry window per tool call ≤ 30s to stay
  inside the LangFlow worker timeout (`LANGFLOW_WORKER_TIMEOUT`, default
  300s, but a single tool must not consume it).
- A retry that exhausts attempts on a *financial* action never silently
  fails — it surfaces as `pending_approval` so a human can confirm whether
  the side effect landed.

## 11. Checkpoint Standards

Checkpoints make an AR run resumable and auditable. The agent checkpoints at
these boundaries:

- **After every human-approval gate** (so an approved action is never lost).
- **Before any financial POST** (so a crash mid-call can be reconciled, not
  blindly replayed).
- **After each reconciled batch** (so partial progress survives a restart).

Each checkpoint stores: the full `AgentState`, the intended next action, the
idempotency key for any pending mutation, and a tool-call reference
(`trace_id` + tool name + call index) sufficient to re-fetch — never the raw
secret.

> **Langfuse caveat (binding).** LangFlow→Langfuse tracing is currently
> **disabled** in this deployment (`LANGFLOW_DEACTIVATE_TRACING=true`), because
> LangFlow 1.10.1's `LangfuseResourceManager` crashes under the assistant-path
> deepcopy (commit `79beb3b`). Therefore a checkpoint **must record enough
> state to resume without relying on Langfuse spans** — the checkpoint, not the
> trace, is the source of truth for resumption until that override is dropped.
> See [FAQ](faq.md) for the tracing-auto-wire status.

## 12. Logging Standards

Logs are structured `key=value` (or JSON) on stdout, captured and rotated by
Docker via the `x-logging` anchor. Log level follows `LANGFLOW_LOG_LEVEL`.

| Mandatory field | Meaning |
|-----------------|---------|
| `trace_id` | Correlates one AR run across nodes (propagate end-to-end) |
| `flow_id` | LangFlow flow UUID |
| `tenant` | Cosmic Vikings tenant/entity scope |
| `ar_entity` | Invoice/payment/receipt ID the line concerns |
| `event` | What happened (`invoice.fetch`, `payment.match`, `approval.request`) |
| `outcome` | `ok` / `error` / `pending_approval` |

- **No PII, no secrets in logs.** Credentials are `SecretStrInput` / Secret
  Global Variables — their values are never logged; only the variable *name*
  may appear in a debug line if useful.
- **Customer-identifying data** is referenced by ID, not by name/email/body
  content, unless explicitly required for the operation.
- Use `self.log(...)` inside components; `print()` is prohibited.
- Error lines include the §9 `code` and `trace_id`, not raw stack traces to the
  caller (full traceback stays server-side).

## 13. Audit Standards

Every action that affects money or the ledger writes an **immutable audit
record**.

| Audit field | Source |
|-------------|--------|
| `actor` | Keycloak subject (`sub`) from the SSO session — see [OIDC](oidc.md) |
| `action` | What was done (e.g. `gl.post`, `invoice.issue`, `refund.issue`) |
| `before` / `after` | State delta (amounts, status) |
| `timestamp` | When (UTC, ISO-8601) |
| `approval_ref` | Link to the §19 approval that authorized it |
| `trace_id` | Links to logs/traces |
| `idempotency_key` | For replay-safe replay reconciliation |

- **Source of record.** The financial source system (Zoho Books / Foodics) is
  the primary system of record; the agent's audit entry is the *intent and
  attribution* layer that complements it.
- **Langfuse is the intended audit/trace store** (analytics in ClickHouse,
  blobs in MinIO — see [Backup](backup.md)), but is gated by the §11 caveat;
  until tracing is re-enabled, audit attribution lives in the app log plus the
  source system's own history.
- **Retention** follows [Backup](backup.md); audit data is included in the
  Langfuse/postgres backups and must survive a restore.
- Audit records are **append-only**; correction is a new compensating entry,
  never an edit.

## 14. JSON Response Standards

Every component output method and every flow run returns this canonical
envelope. Tool outputs wrap the envelope text in a `Message`.

```json
{
  "status": "ok | error | pending_approval",
  "code": "AR_OK | AR_<CATEGORY>",
  "data": { },
  "error": { "message": "", "detail": "" },
  "trace_id": "",
  "approval_ref": ""
}
```

- `status` is one of `ok`, `error`, `pending_approval`.
- `code` is a stable, documented `AR_*` string (see §9 table); the caller
  branches on `code`, never on free-text parsing.
- On `ok`, `data` holds the payload and `error` is omitted/empty.
- On `error`, `error.message` is human-readable, `error.detail` is opaque
  diagnostic context; **raw exceptions are never leaked**.
- On `pending_approval`, `approval_ref` identifies the approval to fulfill
  (§19) and `data` describes the proposed action.
- Secrets are **masked** anywhere they could appear (never present in
  `data`/`error`).

## 15. Component Reuse Guidelines

- **Reuse before authoring.** Before writing a new component, check (1) the
  LangFlow built-in component index, (2) the existing `ap_tools` bundle
  (`ZohoBooksAPTool`, `FoodicsAPTool`), and (3) any sibling AR bundle. Add a
  new tool only when none covers the need.
- **Shared logic lives in a bundle's package**, not duplicated across flows.
  If two flows need the same Zoho Books read, they both call the bundled
  `ZohoBooksAPTool`; the Python is in exactly one place.
- **A new tool requires the full bundle set:** `extension.json` manifest,
  `pyproject.toml` with the
  `[project.entry-points."langflow.extensions"]` entry-point and the
  `[tool.langflow.extension] manifest` reference, a component file, and a
  `README.md` listing exposed tools and required Secret Global Variables.
- **Validate offline before deploy:**

```bash
docker exec langflow python -m lfx extension validate /app/extensions/<bundle>
```

- Edits to a bundle take effect on container **recreate** (the mount is
  read-only `:ro`); never write into `/app/extensions` at runtime.

## 16. Security Guidelines

- **Credentials only via Secret Global Variables** — `SecretStrInput(...,
  load_from_db=True)`. Never hard-code a secret, never paste one into a flow
  field, never store one in `MessageTextInput`. See [Security](security.md).
- **Flows run behind `oauth2-proxy` + Keycloak SSO** — no AR flow is publicly
  runnable without an authenticated, attributed session. See [OIDC](oidc.md).
- **Egress is constrained** by `LANGFLOW_SSRF_ALLOWED_HOSTS` (the AR agent may
  only reach Zoho Books, Foodics, and platform-internal services).
- **No customer PII in prompts/traces** beyond what the operation requires;
  prefer IDs over names, redact payment instrument numbers.
- **Least-privilege service roles** — the agent's tooling uses the per-service
  least-privilege roles already provisioned by the platform; do not introduce
  shared superuser credentials.
- **Non-root, read-only mounts** — components run in the hardened LangFlow
  container (`no-new-privileges`, non-root `USER 1000`, `:ro` extension
  mount). Do not weaken these for a bundle.
- Never commit a populated `.env`, rendered `realm.json`, or `librechat.yaml`
  (all git-ignored — keep them so).

## 17. Configuration Standards

- **`.env` is the single source of truth** for host/domain/secret/endpoint
  values, interpolated by `docker compose --env-file .env`. Nothing
  host- or secret-specific is hard-coded in `docker-compose.yml` or in a flow.
  See the repo's [Configuration](../CLAUDE.md) convention.
- **Read/write env values only via `scripts/common.sh` helpers**
  (`get_env`/`set_env`); do not parse `.env` by hand in any lifecycle script.
- **Rendered artifacts are git-ignored** (`realm.json`, `librechat.yaml`); a
  flow must not depend on a rendered file living on disk.
- **Per-flow tunables** (thresholds, dunning cadence, approval ceilings) live
  in LangFlow Global Variables or the flow's own inputs — never baked into a
  component or committed as a magic number.
- **Version pins live in `.env.example`** (image tags as env vars); a bundle
  declares its own Python deps in its `pyproject.toml`.

## 18. Environment Variables

Agent-relevant variables wired in `docker-compose.yml`. Secret Global
Variables are managed in the LangFlow UI, **not** in `.env`.

| Variable | Purpose | Source |
|----------|---------|--------|
| `LANGFLOW_COMPONENTS_PATH` | Bundle discovery root (`/app/extensions`) | `docker-compose.yml` |
| `LANGFLOW_JOB_QUEUE_TYPE` | `redis` — required for multi-worker | `docker-compose.yml` |
| `LANGFLOW_REDIS_QUEUE_URL` | Redis job-queue URL (password-honored) | `docker-compose.yml` |
| `LANGFLOW_WORKERS` | web-tier worker processes | `.env` / `docker-compose.yml` |
| `LANGFLOW_WORKER_TIMEOUT` | per-request hard timeout (s) | `.env` / `docker-compose.yml` |
| `LANGFLOW_WORKER_AUTOSCALE` | Celery `max,min` autoscale | `.env` / `docker-compose.yml` |
| `LANGFLOW_WORKER_REPLICAS` | worker container count | `.env` / `docker-compose.yml` |
| `LANGFLOW_SSRF_ALLOWED_HOSTS` | Egress allow-list for tool calls | `.env` → `docker-compose.yml` |
| `LANGFLOW_LOG_LEVEL` | App log level | `.env` / `docker-compose.yml` |
| `LANGFLOW_DEACTIVATE_TRACING` | Langfuse tracing toggle (currently `true`, see §11) | `docker-compose.yml` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | Tracing keys/host (auto-wired) | `scripts/langfuse-keys.sh` → `docker-compose.yml` |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID` | Zoho Books credentials | LangFlow Secret Global Variables |
| `FOODICS_API_TOKEN` | Foodics API bearer token | LangFlow Secret Global Variable |

> Worker/web sizing and the Redis-queue requirement are documented in
> [Scaling](scaling.md); tracing auto-wiring in [FAQ](faq.md).

## 19. Human Approval Guidelines

Every action is classified into one of four tiers. The tier determines whether
the agent may proceed unattended.

| Tier | Examples | Behavior |
|------|----------|----------|
| `read-only` | List invoices, fetch a bill, read a receipt | Auto, no approval |
| `auto` | Match a receipt to an invoice *below* the auto-match threshold; send a routine dunning reminder | Auto, logged + audited |
| `approval` | GL posting, invoice issuance, refund below dual-control ceiling, write-off | Block on `pending_approval`; proceed only after a captured approval |
| `dual-control` | Refund above the dual-control ceiling, write-off above ceiling, any manual ledger override | Two distinct approvers required |

- **Any financial mutation requires at least `approval`.** There is no
  `auto` tier for moving money or posting to the ledger.
- **Approval state is captured in `AgentState`** (§8), **checkpointed** (§11),
  **audited with `approval_ref`** (§13), and returned as `pending_approval`
  in the envelope (§14).
- **The approval surface is SSO-gated:** the LibreChat→LangFlow chat channel
  (via the OpenAI-compatible adapter) or the LangFlow UI, both behind
  Keycloak. The approver's Keycloak `sub` is the `actor` of record.
- **Approval is non-reusable** — one `approval_ref` authorizes exactly one
  idempotent action; replay with the same ref is rejected.

## 20. Future Extension Guidelines for AP

Accounts Payable is the first planned extension beyond AR. The existing
`ap_tools` bundle (`ZohoBooksAPTool`, `FoodicsAPTool`) is the seed — it already
demonstrates the bundle pattern, the Secret Global Variable credential model,
and the Zoho OAuth refresh-on-401 retry behavior.

- **Ship AP as additional bundles** under `docker/langflow-extensions/`,
  following §6 (naming), §7 (structure), and §15 (reuse). One bundle per
  source system or sub-domain; do not grow `ap_tools` into a monolith.
- **Reuse the credential pattern:** `SecretStrInput(..., load_from_db=True)`
  resolving from Secret Global Variables; reuse `ZOHO_*` / `FOODICS_*` where
  the same source system is already wired.
- **Priority AP extensions:** 3-way match (purchase order ↔ goods receipt ↔
  vendor invoice) and AP approval workflows (vendor invoice approval, payment
  authorization).
- **All cross-cutting standards apply unchanged:** idempotency + retry (§10),
  checkpointing (§11), logging (§12), audit (§13), the JSON envelope (§14),
  security (§16), and **human approval before any AP financial mutation**
  (§19). No AP mutation runs unattended.
- **AP gets its own constitution addendum** when it moves from future to
  current scope — this section only authorizes the extension shape, not the
  behaviors, which inherit from the sections above.