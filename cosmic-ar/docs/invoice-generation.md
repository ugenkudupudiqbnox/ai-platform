# Invoice Generation Flow (`ar_invoice_generation`)

The **Invoice Generation Flow** is the 8th AR subflow (architecture §4 row 8;
[ADR-0009](adr/adr-0009-invoice-generation-flow.md)). It takes a
**validated-JSON invoice request** (`{customer_ref, line_items, totals,
issue_date, currency, …}`) — produced by a caller (the planned P10 Validation
Flow, a manual operator, or an upstream subflow) — assembles a draft
`InvoiceData`, and generates **eight artifacts** — **Invoice JSON, Invoice PDF
(render-spec), Invoice Excel (render-spec), draft Journal Entry, Customer
Statement, Zoho Upload File, Invoice Metadata**, plus **WorkflowState** — then
returns structured JSON. It is the **single stateful orchestrator** for invoice
generation, mirroring the supervisor, the File Intake Flow, the Intercompany
Sales Flow, the Cosmic Kitchen Revenue Flow, the Foodics Processing Flow, and the
Calculation Flow: its responsibilities map to LangGraph nodes inside one `lfx`
component, `InvoiceGenerationFlowComponent`.

**v1 is read-only generate + draft**: it produces the 8 artifacts for review; it
does **not** post, so no money moves and no ledger entry posts this turn (§1
north star preserved). The flow is registered at tier `read-only`, the §19 gate
is **dormant in v1**: there is no `ApprovalGate`, no idempotency key, no
`pending_approval`, and it is **not** in `FINANCIAL_INTENTS` (mirrors the
Calculation Flow, ADR-0008). It is distinct from `ar_issue_invoice` (#1,
`approval` tier, in `FINANCIAL_INTENTS`, keywords "issue/create/present/new
invoice"), which **posts** an invoice to Zoho; this flow only **generates draft
artifacts for review** — issuance is `ar_issue_invoice`'s job.

> **PDF / Excel in v1 are render-ready JSON specs, not binaries.** The
> `langflow` image has no PDF-writing library, the OpenAI adapter is text-only on
> the response side, and there is no app MinIO bucket. So `data.invoice_pdf` /
> `data.invoice_excel` carry the page layout / sections / sheets / columns / rows
> (`render_ready:true`); real `.pdf`/`.xlsx` materialization is a documented
> build-phase step (reportlab + openpyxl writer → MinIO artifact bucket → adapter
> file-delivery — ADR-0009 §5).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§4/§8/§9/§11/§12/§14/§15/§16/§17/§19, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0009](adr/adr-0009-invoice-generation-flow.md),
[ADR-0007](adr/adr-0007-foodics-processing-flow.md),
[ADR-0005](adr/adr-0005-intercompany-sales-flow.md),
[ADR-0002](adr/adr-0002-reusable-component-library.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/invoice_generation.py`](../../docker/langflow-extensions/ar_common/components/ar_common/invoice_generation.py)
  — `InvoiceGenerationFlowComponent` (internal LangGraph
  `StateGraph[InvoiceGenerationState]` + `InMemorySaver`).
- **Flow JSON:** [`flows/ar_invoice_generation.json`](../flows/ar_invoice_generation.json).
- **Self-test:**
  [`invoice_generation_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/invoice_generation_selftest.py)
  (186 stdlib-only pure-function + end-to-end checks) via
  `scripts/invoice-generation.selftest.sh`.

## The eight artifacts

| Artifact | Envelope key | Shape | §15 reuse? |
|---|---|---|---|
| Invoice JSON | `data.invoice` | a draft `InvoiceData` (`status="draft"`) | yes — `InvoiceData` schema |
| Invoice PDF | `data.invoice_pdf` | render-ready spec (`render_ready:true`, page/sections/layout) | flow-specific JSON (build-phase binary) |
| Invoice Excel | `data.invoice_excel` | render-ready spec (`render_ready:true`, sheets/columns/rows) | flow-specific JSON (build-phase binary) |
| Journal Entry | `data.journal_entry` | balanced double-entry draft, `status="draft"` | flow-specific JSON (no schema) |
| Customer Statement | `data.customer_statement` | opening/closing balance, invoices, payments, aging | flow-specific JSON (no schema) |
| Zoho Upload File | `data.zoho_upload` | `zoho-books-invoice-import` row JSON (mirrors foodics) | flow-specific JSON (no schema) |
| Invoice Metadata | `data.invoice_metadata` | ids, content_hash, source_refs, line_item_count | flow-specific JSON (no schema) |
| WorkflowState | `data.workflow_state` | snapshot, totals `"0.00"`, `intent="ar_invoice_generation"` | yes — `WorkflowState` schema |

`data.artifact_count = 8`. Plus `data.validation_report` / `data.exception_report`
(`ValidationResult`, §15 reuse — no `ExceptionReport` schema).

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept inputs | `ingest` | Parse the validated-JSON invoice request from `user_input`; bind `trace_id` (minted), `flow_id="ar_invoice_generation"`, `tenant="cosmic-vikings"`, `created_at`/`updated_at`; carry `layout` + `model_name` in **context** (not state — §8). Malformed JSON / non-object → `AR_VALIDATION`. status="created". Router `_after_ingest`: `{failed:respond, created:validate_payload}`. |
| Validate payload | `validate_payload` | Inline hand-rolled validator for the invoice-request contract: `customer_ref` present; `line_items` non-empty, each `{item_ref, description, qty>0, unit_price>0}`; `totals` parseable 2dp & consistent (`total = subtotal + tax - discounts`, `balance_due = total`); `issue_date` ISO; `currency ^[A-Z]{3}$`. No `customer_ref` / no `line_items` → hard `AR_VALIDATION`. Non-parseable amount / bad date / bad currency → warning (not hard fail). Builds the full `ValidationResult` (`contract_name="InvoiceGenerationInputs"`). status="validated". Router `_after_validate`: `{failed:respond, validated:classify_exceptions}`. |
| Generate Exception Report | `classify_exceptions` | Exception Report = a `ValidationResult` scoped to the failures (each warning carries a `rule_id` like `ig.line_item_qty`/`ig.totals_consistency`/`ig.currency`). status="classified". Router `_after_classify`: `{failed:respond, classified:build_invoice}`. |
| Assemble InvoiceData (Invoice JSON) | `build_invoice` | Deterministic `invoice_id`/`invoice_number` via `uuid5(NAMESPACE_URL, "invoice-gen:{trace_id}:{customer_ref}:{issue_date}")` shaped `IG-{customer_ref}-{8hex}`; `line_id` per line via `uuid5`; `subtotal = Σ(qty×unit_price)`, `tax`/`discounts`, `total = subtotal+tax-discounts`, `balance_due = total` (2dp); `status="draft"`; `due_date = issue_date + NET_TERMS_DAYS (30)`; `currency` (payload else `SAR`); `po_number`/`salesperson_ref`/`notes`/`source_ref` passed through (no PII — §16). Inline `_validate_invoice` guard (can fail → `AR_VALIDATION`). **Record checkpoint** `"invoice"`. status="invoiced". Router `_after_invoice`: `{failed:respond, invoiced:build_journal_entry}`. |
| Build Journal Entry | `build_journal_entry` | Draft GL **Journal Entry** (flow-specific JSON): balanced double-entry — debit AR `total`, credit Revenue `subtotal`, credit Tax Payable `tax`, debit Discounts `discounts`; `total_debit == total_credit` asserted; `entry_id = uuid5("invoice-gen-je:{trace_id}:{invoice_id}")`; `je_date = issue_date`; `status="draft"` (no POST — §1). **Record checkpoint** `"journal_entry"`. status="journaled". |
| Build Customer Statement | `build_customer_statement` | **Customer Statement** (flow-specific JSON): `customer_ref`, `period {start,end}`, `opening_balance="0.00"` (v1 — no prior AR history fetched), `invoices:[<the InvoiceData summary>]`, `payments:[]` (none), `closing_balance = total`, `aging:{current:total, overdue:"0.00"}`. **Record checkpoint** `"customer_statement"`. status="stated". |
| Build Zoho Upload File | `build_zoho_upload` | **Zoho Upload File** (mirrors foodics `data.zoho_upload`): `{format:"zoho-books-invoice-import", rows:[{customer_ref, invoice_number, date:issue_date, item_details:[{item_ref, qty, rate:unit_price, amount, discount}], discount_total, total, currency}], count:1, trace_id, contract_version, generated_at}`. `customer_ref` is the Zoho customer id (no PII — §16). **Record checkpoint** `"zoho_upload"`. status="zoho". |
| Build Invoice Metadata | `build_metadata` | **Invoice Metadata** (flow-specific JSON): `invoice_id`, `invoice_number`, `customer_ref`, `tenant`, `trace_id`, `flow_id`, `issue_date`, `due_date`, `currency`, `status`, `line_item_count`, `content_hash` (sha256 over canonical InvoiceData JSON), `source_refs:["build_invoice"]`, `generated_at` (UTC), `contract_version`. **Record checkpoint** `"invoice_metadata"`. status="metadata". |
| Build PDF render-spec | `build_pdf_spec` | **Invoice PDF** render-ready spec: `{format:"invoice-pdf", render_ready:true, page:{size:"A4", margins:{…}}, sections:[header, bill_to, line_items_table, totals, footer], data_ref:invoice_id, layout, contract_version}`. **Not a binary** — materialization build-phase. **Record checkpoint** `"invoice_pdf"`. status="pdf_spec". |
| Build Excel render-spec | `build_excel_spec` | **Invoice Excel** render-ready spec: `{format:"invoice-excel", render_ready:true, sheets:[{name:"Invoice", columns:[…], rows:[…]}, {name:"Line Items", columns:[…], rows:[…]}], data_ref:invoice_id, contract_version}`. **Not a binary** — materialization build-phase (openpyxl writer). **Record checkpoint** `"invoice_excel"`. status="excel_spec". |
| Update Workflow State | `build_state` | `WorkflowState` snapshot: `status="completed"`, `intent="ar_invoice_generation"`, `matched_amount`/`outstanding_balance`/`posted_total="0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (gate dormant), `audit_refs`, `tool_call_ref=f"{trace_id}:ar_invoice_generation:0"`, `contract_version`. Immutable (§8). status="completed". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"ar_invoice_generation")`; reflect `audit_refs`+`checkpoints` into the WorkflowState snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). |
| Return structured JSON | `respond` | `_finalize_envelope` builds `data={invoice, journal_entry, customer_statement, zoho_upload, invoice_metadata, invoice_pdf, invoice_excel, validation_report, exception_report, workflow_state, audit_refs, checkpoints, artifact_count, line_item_count, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). |
| Logging | `run()` boundary | §12 structured `key=value` via `self.log`: `event=invoice_generation.run outcome=… trace_id=… flow_id=… ar_entity=invoice_generation code=…`; failure boundary `code=AR_UNEXPECTED`. No PII/secrets (§16 — `customer_ref` is an id). |
| Never raises | `run()` boundary | §5/§9 — `run()` catches at the boundary and returns an `AR_UNEXPECTED` envelope; a malformed `layout` JSON falls back to the default spec. |
| Checkpoints after every generation | each `build_*` + `checkpoint` | Continues ADR-0006/0007/0008's stricter pattern: each generation boundary records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (8 labels: `invoice`, `journal_entry`, `customer_statement`, `zoho_upload`, `invoice_metadata`, `invoice_pdf`, `invoice_excel`, `ar_invoice_generation`), persisted by `InMemorySaver` at each super-step (§11 — ADR-0009 §11). |

Graph edges: `START → ingest → validate_payload → classify_exceptions →
build_invoice → build_journal_entry → build_customer_statement →
build_zoho_upload → build_metadata → build_pdf_spec → build_excel_spec →
build_state → checkpoint → respond → END`, with conditional short-circuits to
`respond` on any `failed` status (`_after_ingest`/`_after_validate`/
`_after_classify`/`_after_invoice` return `state.status` against status-keyed
path maps — ADR-0003 §9). Only ingest/validate/classify/invoice can produce
`AR_VALIDATION`; the downstream build nodes are pure compute → static edges,
unexpected errors caught at the `run()` boundary.

## The invoice-request payload contract

The validated-JSON invoice request the flow consumes (the PRIMARY input via
`user_input`):

```json
{
  "customer_ref": "CUST-42",
  "issue_date": "2026-07-07",
  "currency": "SAR",
  "po_number": "PO-99",
  "salesperson_ref": "SP-1",
  "notes": "v1 draft",
  "line_items": [
    {"item_ref": "ITEM-A", "description": "Catering — breakfast", "qty": "10", "unit_price": "150.00"},
    {"item_ref": "ITEM-B", "description": "Catering — lunch", "qty": "2", "unit_price": "500.00"}
  ],
  "tax": "75.00",
  "discounts": "100.00"
}
```

- `customer_ref` (required) — the Zoho customer id (no PII — §16).
- `line_items` (required, non-empty) — each `{item_ref, description, qty>0,
  unit_price>0}`; amounts are 2dp.
- `issue_date` (required, ISO `YYYY-MM-DD`); `due_date` is derived
  (`issue_date + 30`).
- `currency` (optional, `^[A-Z]{3}$`, default `SAR`).
- `tax` / `discounts` (optional, 2dp; default `"0.00"`). `total = subtotal +
  tax - discounts` (computed, not trusted — `totals` inconsistency is a warning,
  not a hard fail).
- `po_number` / `salesperson_ref` / `notes` / `source_ref` (optional, passed
  through to the InvoiceData — no PII).

A **missing** `customer_ref` / `line_items` / `issue_date` → hard `AR_VALIDATION`;
a bad amount / date / currency / totals inconsistency → a warning (the run still
produces reviewable artifacts).

## The Journal Entry double-entry design

The draft JE is balanced by construction:

| Account | Debit | Credit |
|---|---|---|
| AR (receivable) | `total` | |
| Revenue | | `subtotal` |
| Tax Payable | | `tax` |
| Discounts | `discounts` | |
| **Total** | `total + discounts` | `subtotal + tax` |

Since `total = subtotal + tax - discounts`, the debit total (`total + discounts`)
equals the credit total (`subtotal + tax`); `total_debit == total_credit` is
asserted. `status="draft"` (no POST — §1). Posting this JE to the GL is a
build-phase financial step (with the §19 gate), not part of this read-only flow.

## The Customer Statement design (v1)

v1 does **not** fetch prior AR history: `opening_balance="0.00"`, `payments:[]`.
The statement covers the invoice's period, lists the one generated invoice as
its `invoices` summary, and sets `closing_balance = total` with
`aging:{current:total, overdue:"0.00"}`. Wiring `ZohoBooksARTool` to pull prior
invoices/payments is a documented build-phase step.

## Canvas wiring (3 nodes / 2 edges)

`ar_invoice_generation.json` wires (modeled on `ar_calculation.json`):

- `ChatInput.message → InvoiceGenerationFlowComponent.user_input`
- `InvoiceGenerationFlowComponent.invoice_generation_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Calculation canvas; the
orchestrator node's full source is embedded as `template.code.value` (LangFlow
runs the embedded copy — it must stay in sync with the on-disk
`invoice_generation.py`). There is **no `files` edge** — the second subflow
without one (after `ar_calculation`, ADR-0009 §4).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, required, `tool_mode` — carries
  the invoice-request JSON, the PRIMARY input), `layout` (MultilineInput,
  default = `LAYOUT_JSON`, overridable — §17), `model_name` (MessageTextInput,
  value `"glm-5.2:cloud"` — documented LLM hook; deterministic v1 ignores it).
  **No `files` HandleInput.**
- **Output:** `invoice_generation_output` (Message) — the §14 envelope JSON.

## The supervisor merge (no `AgentState` change)

The 8 artifacts are not under `data.totals{matched,outstanding,posted}`, so the
supervisor's `_node_invoke` does not recognise them as financial totals. The
flow surfaces to the supervisor only via `subflows_invoked` + `audit_refs`; the
artifacts stay in the envelope `data`. **No field is added to `AgentState`**
(ADR-0009 §4, mirrors ADR-0006 §7 / ADR-0007 §8 / ADR-0008 §10). v1 emits no
`data.totals`, so the supervisor's financial totals are unaffected by an
invoice-generation run.

## Contracts emitted

- [`InvoiceData`](contracts.md) — `data.invoice`, `status="draft"`,
  deterministic `invoice_id`/`invoice_number` (`IG-{customer_ref}-{8hex}`),
  `due_date = issue + 30`. **No schema change** (§15 reuse).
- [`ValidationResult`](contracts.md) — emitted twice: the full report
  (`data.validation_report`, `contract_name="InvoiceGenerationInputs"`) and the
  exception-scoped report (`data.exception_report`). No `ExceptionReport`
  schema (§15 reuse).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; totals `"0.00"` (no
  money moved); `intent="ar_invoice_generation"`.
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`.
- **Flow-specific JSON (no schema):** `data.journal_entry`,
  `data.customer_statement`, `data.zoho_upload`, `data.invoice_metadata`,
  `data.invoice_pdf`, `data.invoice_excel` (ADR-0009 §4/§5/§6/§7/§8).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today. So the
orchestrator uses **inline hand-rolled validators** for the invoice-request
contract (`_validate_payload`), `InvoiceData` (`_validate_invoice`), and the
report builders (`_build_validation_report`, `_classify_exceptions`) — mirroring
the File Intake / Intercompany / Kitchen / Foodics / Calculation flows. Wiring
`ValidationEngineComponent` for `InvoiceData`/`WorkflowState` is a documented
build-phase step. The canonical schema files remain the source of truth and the
self-test keeps the validators in sync (hand-rolled stdlib, no `jsonschema`
dep).

## The read-only v1 / build-phase checklist

v1 is **read-only generate + draft only** — no §19 gate, no idempotency key, no
`pending_approval`, not in `FINANCIAL_INTENTS`, no posting. Build-phase (not
done here):

1. **PDF/Excel binary materialization** — add `reportlab` (and an `openpyxl`
   writer) to `docker/langflow/Dockerfile`, wire a MinIO artifact bucket +
   `MINIO_*` env onto the `langflow` service, add adapter file-delivery, then
   materialize `data.invoice_pdf`/`data.invoice_excel` to real `.pdf`/`.xlsx`
   (ADR-0009 §5).
2. **P10 Validation Flow upstream** — build the P10 Validation Flow that emits
   (or validates) this invoice-request payload contract; this flow defines the
   contract but does not build P10.
3. **Customer Statement prior history** — wire `ZohoBooksARTool` to pull prior
   invoices/payments so `opening_balance`/`payments`/`aging` reflect real AR
   history (v1 opens at `"0.00"`).
4. **Wire `ValidationEngineComponent`** for `InvoiceData`/`WorkflowState`
   (replace the inline validators).
5. **Posting upgrade** — issuance is `ar_issue_invoice` #1's job (it posts at
   tier `approval`); this flow stays `read-only` and feeds it the draft
   `InvoiceData`/`zoho_upload`.
6. **Layout Global Variable** — move the `layout` spec from a flow input to a
   plain-JSON LangFlow Global Variable (§17 — ADR-0009 §12).
7. **Import the nine subflows first** (incl. `ar_invoice_generation.json`),
   then `supervisor.json`; open the supervisor flow so the 8th `RunFlow`
   resolves `flow_id_selected`; `docker compose restart langflow`.
8. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; this flow follows for free).

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/invoice_generation.py \
                     docker/langflow-extensions/ar_common/components/ar_common/invoice_generation_selftest.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_invoice_generation.json'))"
bash scripts/invoice-generation.selftest.sh     # 186 pure-function + end-to-end checks
make validate                                   # compose config unaffected
```