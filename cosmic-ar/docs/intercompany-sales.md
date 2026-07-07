# Intercompany Sales Flow (`ar_intercompany_sales`)

The **Intercompany Sales Flow** is the 11th AR subflow (architecture §4 row 11;
[ADR-0005](adr/adr-0005-intercompany-sales-flow.md)). Cosmic sells to
intercompany customer-restaurants inside a Marriott hotel (**HYP** and
**Upyard**) and receives those sales as **KOT (Kitchen Order Ticket) Excel**
sheets — one row per ordered menu item carrying its quantity and the intercompany
**agreed rate** (the transfer price Cosmic charges the buyer). This flow reads
the KOT, validates its rows, looks up menu/qty/agreed-rate **from the sheet
columns** (deterministic, no external calls), calculates intercompany revenue,
generates a **draft** `InvoiceData` JSON per buyer, a Validation Report, and an
Exception Report, updates `WorkflowState`, and returns structured JSON — with
logging (§12), retries (§10), and checkpoints (§11). It is the **single stateful
orchestrator** for intercompany sales, mirroring the supervisor and the File
Intake Flow: its responsibilities map to LangGraph nodes inside one `lfx`
component, `IntercompanySalesFlowComponent`.

**v1 is compute + draft only**: it produces the invoice JSON for review; it does
**not** post/issue the invoice, so no money moves and no ledger entry posts this
turn (§1 north star preserved). The flow is registered at tier `approval` (its
intent is invoice production), but the §19 gate is **dormant in v1** — no
`ApprovalGate`, no idempotency key, no `pending_approval`. Upgrading to actual
issuance (posting the intercompany invoice in Zoho) is a documented build-phase
step (ADR-0005 §2).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§19, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0005](adr/adr-0005-intercompany-sales-flow.md),
[ADR-0004](adr/adr-0004-file-intake-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales.py`](../../docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales.py)
  — `IntercompanySalesFlowComponent` (internal LangGraph
  `StateGraph[IntercompanySalesState]` + `InMemorySaver`).
- **Reused generic parts (`cosmic_common`, §15):**
  [`excel_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/excel_reader.py),
  [`csv_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/csv_reader.py)
  (lazy-imported inside the `read` node; no `pdf_reader` use — a KOT is Excel/CSV).
- **Flow JSON:** [`flows/ar_intercompany_sales.json`](../flows/ar_intercompany_sales.json).
- **Self-test:** [`intercompany_sales_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales_selftest.py)
  (135 stdlib-only pure-function checks) via `scripts/intercompany-sales.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept uploaded KOT | `ingest` | Bind `trace_id` (minted), `flow_id="ar_intercompany_sales"`, `tenant`, `created_at`/`updated_at`; carry the `files` refs in **context** (not state — §8). status="created". |
| Read KOT Excel | `read` | `_expand_files` + `detect_type` (must be `excel`/`csv`); lazy-import `ExcelReaderComponent`/`CSVReaderComponent`, call its output method inside the §10 retry/backoff loop, parse its §14 envelope → `raw_rows` (list[dict]). Unknown type/no-file → `AR_UNCERTAIN` (§4); reader hard error → `AR_VALIDATION`; transient exhausted → `AR_UPSTREAM`. |
| Validate rows | `validate` | Inline hand-rolled KOT-row validator. Required columns: `customer_ref, item_ref, qty, agreed_rate, posted_at` (case-insensitive aliases). Per-row: `qty>0`, `agreed_rate>0`, `posted_at` ISO date, `customer_ref` non-empty. A **missing required column** → hard `AR_VALIDATION` (cannot proceed). Else the full `ValidationResult` is built. |
| Generate Exception Report | `classify_exceptions` | Split rows into `valid_rows`/`exception_rows` (rows with any error). Exception Report = a `ValidationResult` scoped to failures (each error carries a `rule_id`: `kot.customer_ref_required`/`kot.qty_positive`/`kot.rate_positive`/`kot.date_iso`). All-rows-fail → `AR_VALIDATION` (§4). |
| Calculate Revenue | `calculate_revenue` | For each valid row `amount = qty × agreed_rate` (2dp, `Decimal` — §4.3). Build `RevenueData`: `total`, `currency` (default `SAR`), `by_segment` (= `customer_ref`), `by_customer_ref`, `period` (min/max `posted_at`); `by_invoice` is filled later. |
| Generate Invoice JSON | `build_invoice` | Group valid rows by `customer_ref`; emit **one `InvoiceData` per buyer** (HYP + Upyard → two invoices). Each: `status="draft"`, one `line_item` per row (`unit_price`=agreed rate, `amount`=qty×rate), `subtotal`/`total`/`balance_due` 2dp, `issue_date`=earliest `posted_at`, `due_date`=issue + 30, deterministic `invoice_id`/`invoice_number` (`uuid5` from trace+buyer — §4.3). Backfill `RevenueData.by_invoice`. Inline `_validate_invoice` guard. |
| Update Workflow State | `build_state` | Build a `WorkflowState` snapshot: `status="completed"`, `matched_amount`/`outstanding_balance`/`posted_total="0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (no POST), `audit_refs`, `intent="ar_intercompany_sales"`. Immutable (§8). |
| Checkpoint | `checkpoint` | Append the audit record id (`uuid5` from `trace_id`) to `audit_refs`; reflect it into the `WorkflowState` snapshot. `InMemorySaver` persists state (non-durable v1 — ADR-0005 §8). |
| Return structured JSON | `respond` | Terminal; `_finalize_envelope` builds `data={invoices, revenue, validation_report, exception_report, workflow_state, audit_refs, document_count, invoice_count, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",...}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). |
| Implement logging | `run()` boundary | §12 structured `key=value`: `trace_id`/`flow_id`/`tenant`/`ar_entity=intercompany_sales`/`event`/`outcome`/`code`; no PII/secrets (customer refs are ids — HYP/Upyard, §16). |
| Implement retries | `read` | §10 loop (3 attempts, exp backoff `1s·2^n` ±25% jitter ≤30s; non-transient = corrupt/missing → `AR_VALIDATION`, no retry). The only retry surface in v1 (no external lookups — ADR-0005 §3). |
| Implement checkpoints | `checkpoint` node + `InMemorySaver` | `InMemorySaver` keyed by `session_id`; non-durable v1 (build-phase: durable Postgres, ADR-0005 §8). |

Graph edges: `START → ingest → read → validate → classify_exceptions →
calculate_revenue → build_invoice → build_state → checkpoint → respond → END`,
with conditional short-circuits to `respond` on any `failed` status (`_after_read`
/ `_after_validate` / `_after_classify` return `state.status` against status-keyed
path maps — ADR-0003 §9).

## Canvas wiring (3 nodes / 3 edges)

`ar_intercompany_sales.json` wires (modeled on `ar_file_intake.json`):

- `ChatInput.message → IntercompanySalesFlowComponent.user_input`
- `ChatInput.message → IntercompanySalesFlowComponent.files` (inputTypes
  `["Data","Message"]`, type `source`)
- `IntercompanySalesFlowComponent.intercompany_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the File Intake canvas; the
orchestrator node's full source is embedded as `template.code.value`. There is no
standalone `File` node — files ride on the ChatInput `.files` handle into the
orchestrator's `files` HandleInput (ADR-0003 §8 explains why a standalone `File`
node is avoided).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, optional, `tool_mode` — carries
  intent keywords), `files` (HandleInput, `is_list`, `input_types=["Data",
  "Message"]` — the KOT Excel/CSV refs), `model_name` (MessageTextInput —
  documented LLM hook; deterministic v1 ignores it).
- **Output:** `intercompany_output` (Message) — the §14 envelope JSON.

## Run / resume behavior

`run()` (the only `lfx` entry point; **never raises** — §5/§9, catches at the
boundary → `AR_UNEXPECTED` envelope) builds the `IntercompanySalesContext`
(`tenant="cosmic-vikings"`, `flow_id="ar_intercompany_sales"` — AR-bundle
constants, not host config), compiles + caches the graph once, invokes it with
`config={"configurable":{"thread_id":session_id}}`, reads the final state, and
emits the envelope. `graph.get_state(config).values` is a plain dict (LangGraph
1.2.6 — ADR-0003 §10), so `_finalize_envelope` reads fields by key. Resume is
keyed by `session_id` (the adapter forwards LibreChat's `conversationId`). v1 is
synchronous with no approval pause (the gate is dormant), so the §11
durable-resume value is low for intercompany sales specifically — the durable
Postgres upgrade lands with the supervisor (ADR-0005 §8).

## The one-invoice-per-buyer design

Intercompany = one invoice per seller→buyer pair, so the flow emits **one
`InvoiceData` per `customer_ref`** as `data.invoices` (a list). HYP + Upyard →
two invoices; a single-buyer sheet → a one-element list. A consolidated
single invoice would mix buyers and break the one-customer-per-invoice
`InvoiceData` contract. `invoice_id`/`invoice_number` are deterministic
(`uuid5(NAMESPACE_URL, "intercompany:{trace_id}:{customer_ref}")`) so the same
trace + buyer always yields the same ids (§4.3 — no `Math.random`/`uuid4`);
the invoice number is shaped `IC-<customer>-<8hex>`.

## The supervisor merge (no `AgentState` change)

`RevenueData.total` is not under `data.totals{matched,outstanding,posted}`, so
the supervisor's `_node_invoke` does not recognise it as a financial total. The
flow surfaces to the supervisor only via `subflows_invoked` + `audit_refs`; the
revenue/invoices/reports stay in the envelope `data`. **No `revenue` field is
added to `AgentState`** (ADR-0005 §7). v1 emits no `data.totals`, so the
supervisor's financial totals are unaffected by an intercompany-sales run.

## Contracts emitted

- [`InvoiceData`](contracts.md) — one per buyer, `status="draft"`, in
  `data.invoices` (list).
- [`RevenueData`](contracts.md) — `data.revenue`; `by_invoice` backfilled from
  the built invoices.
- [`ValidationResult`](contracts.md) — emitted twice: the full report
  (`data.validation_report`) and the exception-scoped report
  (`data.exception_report`). No `ExceptionReport` schema (§15 reuse — ADR-0005
  §5).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; totals `"0.00"` (no
  money moved).
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`.

## Deterministic rules (v1)

- **Required columns:** `customer_ref, item_ref, qty, agreed_rate, posted_at`
  (case-insensitive aliases, e.g. `quantity`→`qty`, `unit_price`→`agreed_rate`,
  `order_date`→`posted_at`). A sheet missing any → hard `AR_VALIDATION`.
- **Per-row rules:** `qty` parseable > 0; `agreed_rate` parseable > 0;
  `posted_at` ISO date (`YYYY-MM-DD` / `YYYY/MM/DD`); `customer_ref` non-empty.
- **Revenue:** `amount = qty × agreed_rate` per row, 2dp (`Decimal`,
  `ROUND_HALF_UP`); `total` = Σ; `by_segment`/`by_customer_ref` grouped by
  `customer_ref`; `period` = min/max `posted_at`.
- **Invoice:** `issue_date` = buyer's earliest `posted_at`; `due_date` = issue
  + `NET_TERMS_DAYS` (30); `currency` from a `currency` column if present else
  `SAR` (must match `^[A-Z]{3}$`).
- **No credentials, no external calls** (§16) — the KOT already carries the
  transfer-pricing facts (ADR-0005 §3).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract returns `AR_NOT_IMPLEMENTED`. So the orchestrator uses **inline
hand-rolled validators** for KOT rows (`_validate_kot_row`), `InvoiceData`
(`_validate_invoice`), and the report builders (`_build_validation_report`,
`_classify_exceptions`) — mirroring the File Intake Flow's inline validators
(ADR-0004). Wiring `ValidationEngineComponent` for `InvoiceData`/`RevenueData`/
`WorkflowState` is a documented build-phase step (ADR-0005 §9). The canonical
schema files remain the source of truth and the self-test keeps the validators
in sync (hand-rolled stdlib, no `jsonschema` dep).

## Build-phase checklist (not done here)

1. **Issuance upgrade** — add the §19 gate + idempotency key +
   checkpoint-before-POST + audit-with-`approval_ref`; add
   `ar_intercompany_sales` to `FINANCIAL_INTENTS` (mirrors `ar_issue_invoice`,
   architecture §4 row 7). This is the point at which the tier's gate goes live.
2. **Rebuild the `langflow` image** so `openpyxl` is available for `.xlsx` KOTs
   (`docker compose build langflow langflow-worker`) — already required by the
   File Intake Flow (ADR-0004 §3); CSV works without a rebuild.
3. **Import the fourteen subflows first** (incl. `ar_intercompany_sales.json`),
   then `supervisor.json`; open the supervisor flow so the 11th `RunFlow`
   resolves `flow_id_selected`.
4. **Wire `ValidationEngineComponent`** for `InvoiceData`/`RevenueData`/
   `WorkflowState` (replace the inline validators).
5. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; intercompany sales follows for free).

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales.py \
                     docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales_selftest.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_intercompany_sales.json'))"
bash scripts/intercompany-sales.selftest.sh     # 135 pure-function checks
make validate                                   # compose config unaffected
```