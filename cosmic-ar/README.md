# Cosmic AR Agent

Accounts-receivable automation for Cosmic Vikings Restaurants Management. This
directory is the **project scaffold** for the agent: non-runtime artifacts
(flow import files, dependency list, documentation) plus pointers to the
runtime bundles. **No business logic is implemented here yet** — see
[Status](#status).

## Governing documents

- [Project Constitution](../docs/cosmic-ar-constitution.md) — the binding
  engineering standards (state §8, error handling §9, retry §10, checkpoint §11,
  envelope §14, security §16, human approval §19, AP extension §20).
- [Architecture](../docs/cosmic-ar-architecture.md) — supervisor + twelve
  subflows + shared components + LangGraph state + checkpoint/retry/error
  designs, with mermaid diagrams.

## Scaffold map

| What | Where | Loaded by LangFlow? |
|------|-------|---------------------|
| Runtime bundles (components) | [`../docker/langflow-extensions/ar_common/`](../docker/langflow-extensions/ar_common/), [`../docker/langflow-extensions/ar_tools/`](../docker/langflow-extensions/ar_tools/), [`../docker/langflow-extensions/cosmic_common/`](../docker/langflow-extensions/cosmic_common/) | Yes — `:ro` bind-mount at `/app/extensions` |
| Flow import files (placeholders) | [`flows/`](flows/) | No — import into the LangFlow DB |
| Dependency list | [`requirements.txt`](requirements.txt) | No — documentation only |
| JSON contracts | [`contracts/`](contracts/) + [`docs/contracts.md`](docs/contracts.md) | No — design artifacts |
| Environment variables | [`docs/environment.md`](docs/environment.md) | n/a |
| ADRs | [`docs/adr/`](docs/adr/) | n/a |
| Runbooks (placeholders) | [`docs/runbooks/`](docs/runbooks/) | n/a |

### Runtime bundles

- **`ar_common`** — the six cross-cutting components (`SupervisorAgentComponent`,
  `JsonEnvelopeComponent`, `ApprovalGateComponent`, `IdempotencyKeyComponent`,
  `CheckpointComponent`, `AuditRecordComponent`) and the typed `AgentState`
  schema. No credentials.
- **`ar_tools`** — `ZohoBooksARTool` (invoices, customers, customer payments)
  and `FoodicsARTool` (POS receipts, sales). Credentials via LangFlow Secret
  Global Variables.
- **`cosmic_common`** — 15 generic reusable components (Excel/CSV/PDF readers,
  document classifier, Excel normalizer, business-rule/validation/calculation
  engines, invoice builder, Zoho connector, audit logger, notification,
  checkpoint manager, state manager, configuration loader). The generic
  reusable layer that `ar_common`/`ar_tools` compose; also the reuse base for
  the AP extension (§20). The readers, classifier, and `DocumentManifest`
  validator are **implemented** (used by the File Intake Flow — see
  [ADR-0004](docs/adr/adr-0004-file-intake-flow.md)); the rest are scaffold. See
  [`docs/components.md`](docs/components.md) and
  [ADR-0002](docs/adr/adr-0002-reusable-component-library.md).

Every component is a valid, importable `lfx` skeleton whose output method
returns a placeholder `Message`, so the bundles load cleanly without business
logic.

### Flow import files

[`flows/`](flows/) holds 13 LangFlow export skeletons: the **wired** supervisor
flow, the **wired** File Intake Flow (`ar_file_intake`, the 10th subflow — see
[docs/file-intake.md](docs/file-intake.md) and
[ADR-0004](docs/adr/adr-0004-file-intake-flow.md)), the **wired** Intercompany
Sales Flow (`ar_intercompany_sales`, the 11th subflow — see
[docs/intercompany-sales.md](docs/intercompany-sales.md) and
[ADR-0005](docs/adr/adr-0005-intercompany-sales-flow.md)), the **wired** Cosmic
Kitchen Revenue Flow (`ar_kitchen_revenue`, the 12th subflow — see
[docs/kitchen-revenue.md](docs/kitchen-revenue.md) and
[ADR-0006](docs/adr/adr-0006-kitchen-revenue-flow.md)), and nine placeholder
business subflows. Flow **definitions** live in the LangFlow Postgres DB
(constitution §7), not on disk — these JSONs are import artifacts, not
auto-loaded.

## Status

The **supervisor is implemented** (real LangGraph + wired canvas + adapter
file/approval forwarding); see [docs/supervisor.md](docs/supervisor.md) and
[ADR-0003](docs/adr/adr-0003-supervisor-runflow-and-adapter.md). The **File
Intake Flow** is implemented (real LangGraph + wired canvas + real readers/
classifier/validator); see [docs/file-intake.md](docs/file-intake.md) and
[ADR-0004](docs/adr/adr-0004-file-intake-flow.md). The **Intercompany Sales
Flow** is implemented (real LangGraph + wired canvas + deterministic KOT →
draft invoice); see [docs/intercompany-sales.md](docs/intercompany-sales.md) and
[ADR-0005](docs/adr/adr-0005-intercompany-sales-flow.md). The **Cosmic Kitchen
Revenue Flow** is implemented (real LangGraph + wired canvas + deterministic
four-sheet → revenue report); see [docs/kitchen-revenue.md](docs/kitchen-revenue.md)
and [ADR-0006](docs/adr/adr-0006-kitchen-revenue-flow.md). Remaining build-phase
work (not done here):

1. ~~Implement the LangGraph `StateGraph[AgentState]` + checkpointer in
   `SupervisorAgentComponent`.~~ ✓ Done (in-image `InMemorySaver`; durable
   Postgres checkpointer still build-phase — step 6).
2. Implement HTTP + OAuth refresh-on-401 in `ZohoBooksARTool` (mirror
   `ZohoBooksAPTool`).
3. Implement the FOODICS calls in `FoodicsARTool`.
4. Implement the nine business subflows' logic (the supervisor delegates to
   them; their internals are separate tasks). The File Intake Flow (10th
   subflow), the Intercompany Sales Flow (11th subflow), and the Cosmic Kitchen
   Revenue Flow (12th subflow) are done.
5. Wire the nine business subflows in the LangFlow UI and import their real
   flow JSONs (the nine skeletons here are still placeholders; `supervisor.json`,
   `ar_file_intake.json`, `ar_intercompany_sales.json` and
   `ar_kitchen_revenue.json` are wired — import the twelve subflows first, then
   the supervisor, per [flows/README.md](flows/README.md)).
6. Provision the `ar_agent` Postgres DB and swap `InMemorySaver` →
   `langgraph-checkpoint-postgres` (durable resume — see build-phase
   integration below). The File Intake Flow's `InMemorySaver` swaps for free
   (ADR-0004 §7).
7. Rebuild the `langflow` image so `openpyxl`/`pdfplumber` are available for
   Excel/PDF intake (`docker compose build langflow langflow-worker`; CSV works
   without a rebuild — ADR-0004 §3).

## Build-phase platform integration

The scaffold deliberately **does not** edit `docker-compose.yml` (to keep
`make validate`/CI green). `scripts/gen-secrets.sh` now generates
`AR_AGENT_DB_PASSWORD` (so `.env` is placeholder-free and `make test` stays
green), but the var is not yet wired onto a service. Wire the agent in at build
phase:

### 1. Provision the `ar_agent` database

A shellcheck-clean placeholder exists at
[`../docker/postgres/init/02-ar-agent-db.sh`](../docker/postgres/init/02-ar-agent-db.sh).
It is **inert until the build phase** wires the `AR_AGENT_DB_*` env vars onto
the `postgres` service in `docker-compose.yml`:

```yaml
  postgres:
    environment:
      # ...existing keys...
      AR_AGENT_DB_NAME: ${AR_AGENT_DB_NAME}
      AR_AGENT_DB_USER: ${AR_AGENT_DB_USER}
      AR_AGENT_DB_PASSWORD: ${AR_AGENT_DB_PASSWORD}
```

These vars are defined in [`../.env.example`](../.env.example) (AR section),
and `AR_AGENT_DB_PASSWORD` is generated by `scripts/gen-secrets.sh`.

### 2. Wire the checkpoint DB onto the supervisor

Pass `AR_AGENT_DB_*` (and a SQLAlchemy URL) onto the `langflow` service in
`docker-compose.yml`, bake `langgraph-checkpoint-postgres` into
`docker/langflow/Dockerfile`, and swap the supervisor's `InMemorySaver()` for
the Postgres saver so checkpoints survive `langflow`/`langflow-worker` recreates
(§11 durable resume).

### 3. Point the adapter at the supervisor flow

Set `LANGFLOW_ADAPTER_FLOW_IDS` (in `.env`) to the supervisor flow's UUID once
the flow is imported into LangFlow, so LibreChat's `model` dropdown routes to it
(via `docker/langflow-adapter/adapter.py`, which forwards uploaded files and
surfaces `pending_approval`/`approval_ref`).

## Validate

```bash
# Offline (no live stack):
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/*.py \
                     docker/langflow-extensions/ar_tools/components/ar_tools/*.py \
                     docker/langflow-extensions/cosmic_common/components/cosmic_common/*.py \
                     docker/langflow-adapter/adapter.py \
                     docker/langflow-adapter/adapter_selftest.py
python3 -c "import json; json.load(open('cosmic-ar/flows/supervisor.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_file_intake.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_intercompany_sales.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_kitchen_revenue.json'))"
bash scripts/file-intake.selftest.sh   # 86 pure-function checks (file intake)
bash scripts/intercompany-sales.selftest.sh   # 135 pure-function checks (intercompany sales)
bash scripts/kitchen-revenue.selftest.sh   # 199 pure-function checks (kitchen revenue)
shellcheck -x docker/postgres/init/02-ar-agent-db.sh scripts/adapter.selftest.sh scripts/file-intake.selftest.sh scripts/intercompany-sales.selftest.sh scripts/kitchen-revenue.selftest.sh
make validate
make test        # adapter.selftest.sh (43) + file-intake.selftest.sh (86) + intercompany-sales.selftest.sh (135) + kitchen-revenue.selftest.sh (199)
# Post-deploy (running stack):
docker exec langflow python -m lfx extension validate /app/extensions/ar_common
docker exec langflow python -m lfx extension validate /app/extensions/ar_tools
docker exec langflow python -m lfx extension validate /app/extensions/cosmic_common
# Excel/PDF intake needs a rebuilt image (openpyxl/pdfplumber in the Dockerfile,
# ADR-0004 §3): docker compose build langflow langflow-worker
```