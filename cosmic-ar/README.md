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
- [Architecture](../docs/cosmic-ar-architecture.md) — supervisor + nine
  subflows + shared components + LangGraph state + checkpoint/retry/error
  designs, with mermaid diagrams.

## Scaffold map

| What | Where | Loaded by LangFlow? |
|------|-------|---------------------|
| Runtime bundles (components) | [`../docker/langflow-extensions/ar_common/`](../docker/langflow-extensions/ar_common/), [`../docker/langflow-extensions/ar_tools/`](../docker/langflow-extensions/ar_tools/), [`../docker/langflow-extensions/cosmic_common/`](../docker/langflow-extensions/cosmic_common/) | Yes — `:ro` bind-mount at `/app/extensions` |
| Flow import files | [`flows/`](flows/) | No — import into the LangFlow DB |
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

[`flows/`](flows/) holds 10 LangFlow export skeletons: the **wired** supervisor
flow plus the **nine wired subflows** — the Zoho Upload Flow (`ar_issue_invoice`,
architecture §4 row 1 — see [docs/zoho-upload-flow.md](docs/zoho-upload-flow.md)
and [ADR-0011](docs/adr/adr-0011-zoho-upload-flow.md)), the Human Approval Flow
(`ar_approval`, row 2 — see [docs/approval-flow.md](docs/approval-flow.md) and
[ADR-0010](docs/adr/adr-0010-approval-flow.md)), the File Intake Flow
(`ar_file_intake`, row 3 — see [docs/file-intake.md](docs/file-intake.md) and
[ADR-0004](docs/adr/adr-0004-file-intake-flow.md)), the Intercompany Sales Flow
(`ar_intercompany_sales`, row 4 — see [docs/intercompany-sales.md](docs/intercompany-sales.md)
and [ADR-0005](docs/adr/adr-0005-intercompany-sales-flow.md)), the Cosmic Kitchen
Revenue Flow (`ar_kitchen_revenue`, row 5 — see
[docs/kitchen-revenue.md](docs/kitchen-revenue.md) and
[ADR-0006](docs/adr/adr-0006-kitchen-revenue-flow.md)), the Foodics Processing
Flow (`ar_foodics_processing`, row 6 — see
[docs/foodics-processing.md](docs/foodics-processing.md) and
[ADR-0007](docs/adr/adr-0007-foodics-processing-flow.md)), the Calculation Flow
(`ar_calculation`, row 7 — see [docs/calculation.md](docs/calculation.md) and
[ADR-0008](docs/adr/adr-0008-calculation-flow.md)), the Invoice Generation Flow
(`ar_invoice_generation`, row 8 — see [docs/invoice-generation.md](docs/invoice-generation.md)
and [ADR-0009](docs/adr/adr-0009-invoice-generation-flow.md)), and the Audit Flow
(`ar_audit`, row 9 — see [docs/audit-flow.md](docs/audit-flow.md) and
[ADR-0012](docs/adr/adr-0012-audit-flow.md)). Flow **definitions** live in the
LangFlow Postgres DB (constitution §7), not on disk — these JSONs are import
artifacts, not auto-loaded. (Seven reserved placeholder business subflows —
`ar_fetch_invoices`/`ar_fetch_receipts`/`ar_match_payments`/`ar_reconcile`/
`ar_dunning`/`ar_post_gl`/`ar_reporting` — were never implemented and were
retired by [ADR-0013](docs/adr/adr-0013-retire-placeholder-subflows.md), which
renumbered architecture §4 to the nine implemented flows above.)

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
and [ADR-0006](docs/adr/adr-0006-kitchen-revenue-flow.md). The **Foodics
Processing Flow** is implemented (real LangGraph + wired canvas + deterministic
Foodics Order/Order Items/Order Payments → consolidated/pivot/discounts/Zoho
upload format/draft InvoiceData per order); see
[docs/foodics-processing.md](docs/foodics-processing.md) and
[ADR-0007](docs/adr/adr-0007-foodics-processing-flow.md). The **Calculation
Flow** is implemented (real LangGraph + wired canvas + the Business Rule Engine
computing the 9 Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/
Expenses/Net Receivable/Net Payable figures from validated JSON — zero
hardcoded formulas; §55 waiver, figures only); see
[docs/calculation.md](docs/calculation.md) and
[ADR-0008](docs/adr/adr-0008-calculation-flow.md). The **Invoice Generation
Flow** is implemented (real LangGraph + wired canvas + a validated-JSON invoice
request → draft InvoiceData + 8 artifacts as JSON-in-envelope — Invoice JSON/PDF
render-spec/Excel render-spec/draft Journal Entry/Customer Statement/Zoho Upload
File/Invoice Metadata + WorkflowState; v1 read-only generate + draft, no
posting; PDF/Excel binaries are build-phase); see
[docs/invoice-generation.md](docs/invoice-generation.md) and
[ADR-0009](docs/adr/adr-0009-invoice-generation-flow.md). The **Human Approval
Flow** is implemented (real LangGraph + wired canvas + §19 interrupt
pause/present/capture/resume + 3-way Approve/Reject/Request-Changes +
WorkflowState + audit logging; standalone presentational surface, no supervisor
change); see [docs/approval-flow.md](docs/approval-flow.md) and
[ADR-0010](docs/adr/adr-0010-approval-flow.md). The **Zoho Upload Flow** is
implemented (real LangGraph + wired canvas + a validated-JSON
`ZohoUploadRequest` → §10-retried upload to Zoho Books → all-or-nothing
rollback of created invoices on partial failure → canonical `ZohoUploadResult`
per create + enriched per-invoice view with `rolled_back` + `WorkflowState` +
audit per create/rollback (§13); §1 `approval_ref` required at the boundary, no
in-flow interrupt; deterministic stub transport v1 — real `ZohoBooksARTool`
POST/DELETE build-phase); see [docs/zoho-upload-flow.md](docs/zoho-upload-flow.md)
and [ADR-0011](docs/adr/adr-0011-zoho-upload-flow.md). The **Audit Flow** is
implemented (read-only, no §1 gate — real LangGraph + wired canvas + a
validated-JSON `AuditRequest` collecting the run's execution history/input
files/validation reports/calculation results/invoices/approvals/Zoho upload
results/execution time/errors/warnings → synthesize an immutable §13 audit log
(append-only AuditRecords, one per artifact + a terminal `audit.summary`) +
`ExecutionSummary` + `WorkflowState`; pure compute, no transport — Postgres/
Langfuse persistence build-phase; architecture §4 row 9); see
[docs/audit-flow.md](docs/audit-flow.md) and
[ADR-0012](docs/adr/adr-0012-audit-flow.md). Remaining build-phase
work (not done here):

1. ~~Implement the LangGraph `StateGraph[AgentState]` + checkpointer in
   `SupervisorAgentComponent`.~~ ✓ Done (in-image `InMemorySaver`; durable
   Postgres checkpointer still build-phase — step 6).
2. Implement HTTP + OAuth refresh-on-401 in `ZohoBooksARTool` (mirror
   `ZohoBooksAPTool`).
3. Implement the FOODICS calls in `FoodicsARTool`.
4. ~~Implement the nine business subflows' logic (the supervisor delegates to
   them; their internals are separate tasks).~~ ✓ Done — all nine subflows are
   implemented: the Zoho Upload Flow (row 1), the Human Approval Flow (row 2),
   the File Intake Flow (row 3), the Intercompany Sales Flow (row 4), the
   Cosmic Kitchen Revenue Flow (row 5), the Foodics Processing Flow (row 6),
   the Calculation Flow (row 7), the Invoice Generation Flow (row 8), and the
   Audit Flow (row 9). The seven reserved placeholder slots were retired by
   [ADR-0013](docs/adr/adr-0013-retire-placeholder-subflows.md).
5. ~~Wire the subflows in the LangFlow UI and import their real flow JSONs.~~
   ✓ Done — all nine subflow JSONs and `supervisor.json` are wired; import the
   nine subflows first, then the supervisor, per
   [flows/README.md](flows/README.md).
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
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_foodics_processing.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_calculation.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_invoice_generation.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_approval.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_issue_invoice.json'))"
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_audit.json'))"
bash scripts/file-intake.selftest.sh   # 86 pure-function checks (file intake)
bash scripts/intercompany-sales.selftest.sh   # 135 pure-function checks (intercompany sales)
bash scripts/kitchen-revenue.selftest.sh   # 199 pure-function checks (kitchen revenue)
bash scripts/foodics-processing.selftest.sh   # 204 pure-function checks (foodics processing)
bash scripts/business-rule-engine.selftest.sh   # 79 pure-function checks (business rule engine)
bash scripts/calculation.selftest.sh   # 112 pure-function checks (calculation flow)
bash scripts/invoice-generation.selftest.sh   # 186 pure-function + end-to-end checks (invoice generation flow)
bash scripts/approval-flow.selftest.sh   # 158 pure-function + end-to-end pause/resume checks (human approval flow)
bash scripts/zoho-upload-flow.selftest.sh   # 162 pure-function + end-to-end upload/retry/rollback checks (zoho upload flow)
bash scripts/audit-flow.selftest.sh   # 180 pure-function + end-to-end audit checks (audit flow)
shellcheck -x docker/postgres/init/02-ar-agent-db.sh scripts/adapter.selftest.sh scripts/file-intake.selftest.sh scripts/intercompany-sales.selftest.sh scripts/kitchen-revenue.selftest.sh scripts/foodics-processing.selftest.sh scripts/business-rule-engine.selftest.sh scripts/calculation.selftest.sh scripts/invoice-generation.selftest.sh scripts/approval-flow.selftest.sh scripts/zoho-upload-flow.selftest.sh scripts/audit-flow.selftest.sh
make validate
make test        # adapter.selftest.sh (43) + file-intake.selftest.sh (86) + intercompany-sales.selftest.sh (135) + kitchen-revenue.selftest.sh (199) + foodics-processing.selftest.sh (204) + business-rule-engine.selftest.sh (79) + calculation.selftest.sh (112) + invoice-generation.selftest.sh (186) + approval-flow.selftest.sh (158) + zoho-upload-flow.selftest.sh (162) + audit-flow.selftest.sh (180)
# Post-deploy (running stack):
docker exec langflow python -m lfx extension validate /app/extensions/ar_common
docker exec langflow python -m lfx extension validate /app/extensions/ar_tools
docker exec langflow python -m lfx extension validate /app/extensions/cosmic_common
# Excel/PDF intake needs a rebuilt image (openpyxl/pdfplumber in the Dockerfile,
# ADR-0004 §3): docker compose build langflow langflow-worker
```