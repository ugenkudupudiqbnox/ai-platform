# ADR 0009 — Invoice Generation Flow: 15th subflow, validated-JSON invoice request → Invoice JSON / PDF render-spec / Excel render-spec / draft Journal Entry / Customer Statement / Zoho Upload File / Invoice Metadata + WorkflowState (read-only generate + draft)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0005](adr-0005-intercompany-sales-flow.md) / [0007](adr-0007-foodics-processing-flow.md) / [0008](adr-0008-calculation-flow.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§4/§8/§9/§11/§12/§14/§15/§16/§17/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [invoice generation](../invoice-generation.md), [supervisor](../supervisor.md),
  [calculation](../calculation.md), [intercompany sales](../intercompany-sales.md),
  [foodics processing](../foodics-processing.md)

## Context

ADR-0008 added the Calculation Flow as the 14th AR subflow. None of the fourteen
subflows **generates the full invoice artifact set**. The request
(`prompts/P12_invoice_generatoin_flow.md`, verbatim): generate **Invoice PDF /
Invoice Excel / Journal Entry / Customer Statement / Zoho Upload File / Invoice
Metadata / Invoice JSON**, update Workflow State, and return structured JSON.

This ADR records the thirteen decisions made when the **Invoice Generation Flow**
(`ar_invoice_generation`) was added as the 15th AR subflow: a new
`InvoiceGenerationFlowComponent` orchestrator that takes a **validated-JSON
invoice request** (customer_ref, line_items, totals, issue_date, currency, …),
assembles a draft `InvoiceData` (§15 reuse), and derives **eight artifacts** as
structured JSON in the §14 envelope.

**v1 is read-only generate + draft** (mirrors `ar_calculation` /
`ar_kitchen_revenue`): no posting, no idempotency key, no `pending_approval`,
**not** in `FINANCIAL_INTENTS`, §19 gate dormant — §1 north star preserved. It is
distinct from `ar_issue_invoice` (#7, `approval` tier, in `FINANCIAL_INTENTS`,
keywords "issue/create/present/new invoice"), which **posts** an invoice to Zoho;
this flow only **generates draft artifacts for review**.

**No §55/§3 waiver** — invoice generation is in-scope (constitution §2 "AR
invoice presentment (Zoho Books)"; §19 `approval` tier names "invoice issuance").
This flow generates *draft* artifacts without the issuance POST, so there is no
statutory-filing/VAT concern (unlike ADR-0008's VAT-figures waiver).

## Decisions

### 1. A new 15th subflow, amending architecture §4's "Fourteen reusable subflows"

`ar_invoice_generation` is added to `SUBFLOWS` as the 15th entry, with a `TIER`
entry and an `INTENT_KEYWORDS` entry, and a 15th `RunFlow` node on the supervisor
canvas. Architecture §4's table grows a row 15 and its heading becomes "Fifteen
reusable LangFlow subflows"; §5's diagram grows a
`route → invoice_generation → effect` branch.

- **Deviation:** architecture §4 said "Fourteen reusable LangFlow subflows"
  (after ADR-0008). This adds a 15th. Per the constitution's Authority note, a
  deviation from a binding standard is recorded as a written waiver in the
  flow's README **and a linked ADR** — this is that ADR.
- **Why:** generating the full draft invoice artifact set from a validated
  invoice request is a distinct AR activity with its own input (a JSON invoice
  request), its own deterministic compute (assemble `InvoiceData` then derive 8
  artifacts), and its own output (8 JSON artifacts + reports + WorkflowState).
  It does not fit any of the existing fourteen subflows.

### 2. Tier `read-only` — generate + draft only, §19 gate dormant; NOT in `FINANCIAL_INTENTS`

The flow is registered at tier `read-only` (generate + draft — no posting). The
§19 gate is **dormant in v1**: there is no `ApprovalGate`, no `interrupt()`, no
idempotency key, no `pending_approval`, no Zoho POST. It returns `AR_OK` with the
8 artifacts + reports + WorkflowState. It is **not** added to
`FINANCIAL_INTENTS` (no financial POST → no financial-retry escalation). This
mirrors the Calculation Flow (ADR-0008 §3) and the Kitchen Revenue Flow
(ADR-0006).

- **Deviation:** none — `read-only` is a registered §19 tier; the gate is
  simply off. The §1 north star (no money moves, no ledger entry posts) is
  preserved.
- **Why:** produce a reviewable draft invoice artifact set now with zero posting
  risk; the §19 machinery is only justified once a posting target exists.
  **Issuance is `ar_issue_invoice` #7's job** (it posts, at tier `approval`, in
  `FINANCIAL_INTENTS`); this flow is the generate/draft half of that lifecycle.

### 3. Intent-keyword placement BEFORE `ar_fetch_invoices` (classifier specificity)

`classify_intent` uses strict `>` with first-match-wins on ties, and
`ar_fetch_invoices` is first in `INTENT_KEYWORDS` with a bare `"invoice"` keyword
(len 7 > 4 → score 1.0). Any text containing the substring "invoice" therefore
ties at 1.0 with `ar_fetch_invoices` winning — which would shadow every
more-specific invoice intent. The `ar_invoice_generation` tuple
(`"generate invoice"`, `"invoice generation"`, `"draft invoice"`,
`"build invoice"`, `"compose invoice"`, `"invoice pdf"`, `"invoice excel"`,
`"journal entry"`, `"customer statement"`) is placed **before**
`ar_fetch_invoices` so these multi-word 1.0-score keywords win first-match,
while a bare `"invoice"` / `"fetch invoice"` / `"list invoice"` /
`"outstanding invoice"` still falls through to `ar_fetch_invoices` (none of this
flow's keywords is the bare token `"invoice"`).

- **Deviation:** none — keyword ordering is an internal classifier decision;
  `INTENT_KEYWORDS` is not a binding standard.
- **Why:** without the reorder, `"generate invoice"` / `"draft invoice"` /
  `"invoice pdf"` would route to `ar_fetch_invoices`, defeating the new flow's
  discovery surface.
- **Note (out of scope):** `ar_issue_invoice`'s `"issue/create/present/new
  invoice"` keywords are likewise shadowed by `ar_fetch_invoices` today. That is
  a **pre-existing** limitation left unchanged here — `ar_issue_invoice` is a
  `FINANCIAL_INTENTS` flow and retargeting its routing is out of scope for the
  invoice-generation task. No supervisor self-test pins routing today.

### 4. Output = 8 artifacts as JSON-in-envelope; §15 reuse, NO new schema, NO `AgentState` change

The flow emits eight artifacts, all as structured JSON in the §14 envelope `data`:
`invoice` (the Invoice JSON — a draft `InvoiceData`), `journal_entry`,
`customer_statement`, `zoho_upload`, `invoice_metadata`, `invoice_pdf`,
`invoice_excel`, plus `workflow_state`. §15 reuse of the existing
`InvoiceData` / `ValidationResult` / `WorkflowState` / `Envelope` schemas —
**no new contract schema, no schema amendment**. The Journal Entry, Customer
Statement, Zoho Upload File, Invoice Metadata, PDF render-spec, and Excel
render-spec are **flow-specific JSON** (no schema), mirroring foodics'
`data.zoho_upload` / `data.consolidated` / `data.pivot` / `data.sheet3`
(ADR-0007 §7). None of these are recognized `data.totals{matched,outstanding,
posted}` keys → the supervisor's `_node_invoke` does not merge them into
`AgentState`; they stay in the envelope `data`. **No `AgentState` schema change**
(mirrors ADR-0006 §7 / ADR-0007 §8 / ADR-0008 §10).

- **Deviation:** none — §15 reuse; no new contract.
- **Why:** avoid authoring/amending contracts for review-only JSON that fits the
  envelope; keep the supervisor's totals-merge contract stable.

### 5. PDF / Excel as render-ready JSON specs in v1; binary materialization build-phase

The `langflow` image has **no PDF-writing library** (`pdfplumber` is read-only;
no `reportlab`/`fpdf`/`weasyprint`), there is **no file-delivery path** back to
LibreChat (the OpenAI adapter is text-only on the response side), and **no app
MinIO bucket** (MinIO is Langfuse-only). So v1 emits the Invoice PDF and Invoice
Excel as **render-ready JSON specs** in the envelope (`data.invoice_pdf` /
`data.invoice_excel`, `render_ready:true`) — the spec carries the page layout,
sections, sheets, columns, and rows; real `.pdf`/`.xlsx` materialization is a
**documented build-phase step**. This is exactly the ADR-0007 §4 precedent
(foodics emits `data.consolidated`/`pivot`/`sheet3` as JSON; the real `.xlsx` is
build-phase).

- **Deviation:** none for §14 (the specs are envelope `data`); the "no binary
  in v1" constraint is an environment fact recorded here.
- **Build-phase:** add `reportlab` (+ an `openpyxl` writer) to
  `docker/langflow/Dockerfile`, wire a MinIO artifact bucket + `MINIO_*` env onto
  the `langflow` service, add adapter file-delivery, then materialize
  `data.invoice_pdf`/`data.invoice_excel` to real binaries. **No
  `docker-compose.yml` / Dockerfile / `.env` / adapter edit this task** →
  `make validate`/CI stays green.

### 6. Journal Entry & Customer Statement as flow-specific JSON, NO new schema (§15)

The draft Journal Entry and the Customer Statement are emitted as flow-specific
JSON in the envelope (`data.journal_entry`, `data.customer_statement`) — **no
new `JournalEntry` / `CustomerStatement` schema**. This mirrors ADR-0007 §7
(foodics' `data.zoho_upload` is flow-specific JSON, no schema) and ADR-0008 §10
(reuse before authoring).

- **Deviation:** none — §15 reuse of the envelope; the JE/Statement shapes are
  flow-specific JSON, not contracts.
- **Why:** these are review-only draft artifacts; a contract is only warranted
  once a downstream consumer (GL posting, statement mailing) is built, which is
  build-phase.

### 7. Zoho Upload File = `zoho-books-invoice-import` row JSON (mirrors foodics)

The Zoho Upload File artifact (`data.zoho_upload`) is the same shape foodics
emits (ADR-0007): `{format:"zoho-books-invoice-import", rows:[{customer_ref,
invoice_number, date, item_details:[{item_ref, qty, rate, amount, discount}],
discount_total, total, currency}], count, trace_id, contract_version,
generated_at}`. `customer_ref` is the Zoho customer id (no PII — §16). No new
schema (flow-specific JSON, decision 4).

- **Deviation:** none — §15 reuse of the foodics precedent shape.
- **Why:** one consistent Zoho import-row format across the invoice-producing
  flows; the operator imports the same JSON shape whether it came from a
  Foodics order or a manual invoice request.

### 8. Invoice Metadata = deterministic content hash + `source_refs`

The Invoice Metadata artifact (`data.invoice_metadata`) carries `invoice_id`,
`invoice_number`, `customer_ref`, `tenant`, `trace_id`, `flow_id`, `issue_date`,
`due_date`, `currency`, `status`, `line_item_count`, a deterministic
`content_hash` (sha256 over the canonical InvoiceData JSON, hex-digested), and
`source_refs:["build_invoice"]`, `generated_at`, `contract_version`. The hash
lets a reviewer verify the Invoice JSON was not altered after generation.

- **Deviation:** none.
- **Why:** traceability + tamper-evidence for the draft artifact set without a
  new contract.

### 9. Deterministic invoice ids (`uuid5`), `status="draft"`, `due_date = issue + 30`

`invoice_id` is `uuid5(NAMESPACE_URL, "invoice-gen:{trace_id}:{customer_ref}:{issue_date}")`
and `invoice_number` is shaped `IG-{customer_ref}-{8hex upper}` — mirroring
foodics' `FP-{ref}-{8hex}` (ADR-0007) and intercompany's `IC-{customer}-{8hex}`
(ADR-0005). The same trace + customer + issue date always yields the same ids
(§4.3 — no `Math.random`/`uuid4`). `status="draft"` (no POST — §1);
`due_date = issue_date + NET_TERMS_DAYS (30)`; `currency` from the payload else
`SAR` (`^[A-Z]{3}$`). `line_id`s are `uuid5` per line. An inline
`_validate_invoice` guard checks the InvoiceData shape (can fail →
`AR_VALIDATION`).

- **Deviation:** none — §4.3 determinism.
- **Why:** deterministic ids make the draft reproducible and idempotent across
  re-runs of the same request; `draft` status makes the no-post intent explicit.

### 10. Draft Journal Entry is balanced double-entry, `status="draft"` (no POST — §1)

The Journal Entry is balanced double-entry: **debit AR = total**, **credit
Revenue = subtotal**, **credit Tax Payable = tax**, **debit Discounts =
discounts**. Since `total = subtotal + tax - discounts`,
`total_debit (= total + discounts) == total_credit (= subtotal + tax)` is
asserted. `entry_id = uuid5("invoice-gen-je:{trace_id}:{invoice_id}")`,
`je_date = issue_date`, `status="draft"` (no POST — §1). It is a **review-only
draft**; posting it to the GL is `ar_post_gl` #6's job (with the §19 gate).

- **Deviation:** none — §1 (no ledger entry posts this turn).
- **Why:** give the reviewer the balanced JE that *would* be posted, without
  posting it; the assertion guards the arithmetic.

### 11. Checkpoints after every generation step (8 labels) — continues ADR-0006/0007/0008's stricter pattern

The flow records a labeled `_audit_ref(trace_id, label)` into `audit_refs` and a
`checkpoints{<label>}` map at **eight** boundaries: `build_invoice` records
`"invoice"`, `build_journal_entry` records `"journal_entry"`,
`build_customer_statement` records `"customer_statement"`, `build_zoho_upload`
records `"zoho_upload"`, `build_metadata` records `"invoice_metadata"`,
`build_pdf_spec` records `"invoice_pdf"`, `build_excel_spec` records
`"invoice_excel"`, and the final `checkpoint` node records the aggregate
`"ar_invoice_generation"` (8 checkpoints total), persisted by `InMemorySaver` at
each super-step (§11). This continues ADR-0006 §9 / ADR-0007 §10 / ADR-0008 §12's
stricter "checkpoints after every generation" pattern (beyond §11's "after each
reconciled batch").

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each artifact boundary is an auditable, resumable point; the
  checkpoint is the source of truth for resume while Langfuse tracing is off.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (decision
  13).

### 12. `layout` as an overridable flow input default (§17); Global-Variable move build-phase

Per §17, tunables belong in flow inputs / LangFlow Global Variables, not baked
into the component. The PDF/Excel render-spec layout ships as the
`InvoiceGenerationFlowComponent`'s `layout` input (a `MultilineInput` JSON
string, default = a declarative `LAYOUT_JSON` spec — page size, margins,
sections, columns). It is **declarative data the operator can override** without
a code change. A malformed `layout` JSON at runtime falls back to the default
(and the run still succeeds — §5/§9) rather than crashing. Moving the layout to
a LangFlow Global Variable is a **documented build-phase seam** (the repo only
evidences `SecretStrInput(load_from_db=True)` for secrets today; there is no
plain-JSON Global Variable input type wired). **No `environment.md` edit this
task** (no new Global Variables).

- **Deviation:** none — §17 satisfied; v1 uses the flow input as the carrier,
  GV is the forward path.
- **Build-phase:** wire a plain-JSON Global Variable for the layout and inject
  it into the `layout` input at build time.

### 13. `InMemorySaver` v1 / durable Postgres build-phase; one stdlib-only offline self-test

Checkpointing uses the in-image `InMemorySaver` keyed by `session_id` — the §11
fallback (non-durable, lost on worker recreate). Durable Postgres
checkpointing (`langgraph-checkpoint-postgres` into the `ar_agent` DB) remains a
documented build-phase step (same §11 fallback as the supervisor / File Intake /
Intercompany / Kitchen / Foodics / Calculation flows).

One **stdlib-only offline self-test** ships per the CLAUDE.md self-test
convention: `invoice_generation_selftest.py` (186 checks over the flow's pure
functions + end-to-end graph — payload parse, validation, exception
classification, InvoiceData assembly, Journal Entry balance, Customer Statement,
Zoho Upload File, Metadata hash, PDF/Excel render-specs, WorkflowState,
checkpoints, envelope, `run()` never raises). It stubs `lfx`/`langgraph` so it
runs on the host without the in-image venv; it is picked up by `make test` and
CI via `scripts/invoice-generation.selftest.sh`.

- **Deviation:** none — documented §11 fallback + the project self-test
  convention.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (shared
  with the supervisor — ADR-0003 build-phase; this flow follows for free).