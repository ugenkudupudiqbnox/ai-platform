# ADR 0007 — Foodics Processing Flow: 13th subflow, Foodics Order + Order Items + Order Payments → consolidated/pivot/discounts/Zoho upload/draft InvoiceData (compute + draft only)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0006](adr-0006-kitchen-revenue-flow.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§17/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [foodics processing](../foodics-processing.md), [supervisor](../supervisor.md),
  [intercompany sales](../intercompany-sales.md), [kitchen revenue](../kitchen-revenue.md)

## Context

ADR-0006 added the Cosmic Kitchen Revenue Flow as the 12th AR subflow, amending
architecture §4's "Twelve reusable LangFlow subflows". None of those twelve
covers **Foodics POS order processing**. Cosmic receives Foodics **Order**,
**Order Items**, and **Order Payments** data (either as three export files or
via the Foodics API) and must turn it into a consolidated dataset, a pivot, a
payment-type breakdown, a discount-adjusted invoice set, a **Zoho Books upload
format**, a draft `InvoiceData` per order, and a Validation/Exception report.
Today an upload with no matching keyword fails safe with `AR_UNCERTAIN` and is
never read.

This ADR records the twelve decisions made when the **Foodics Processing Flow**
(`ar_foodics_processing`) was added as the 13th AR subflow (the new
`FoodicsProcessingFlowComponent` orchestrator, the wired
`ar_foodics_processing.json` canvas, and the supervisor wiring).

## Decisions

### 1. A new 13th subflow, amending architecture §4's "Twelve reusable subflows"

`ar_foodics_processing` is added to `SUBFLOWS` as the 13th entry, with a `TIER`
entry and an `INTENT_KEYWORDS` entry, and a 13th `RunFlow` node on the supervisor
canvas. Architecture §4's table grows a row 13 and its heading becomes
"Thirteen reusable LangFlow subflows"; §5's diagram grows a
`route → foodics_processing → effect` branch.

- **Deviation:** architecture §4 said "Twelve reusable LangFlow subflows"
  (after ADR-0006). This adds a 13th. Per the constitution's Authority note, a
  deviation from a binding standard is recorded as a written waiver in the
  flow's README **and a linked ADR** — this is that ADR.
- **Why:** Foodics order processing is a distinct AR activity (turning POS
  order data into a draft invoice + Zoho upload) with its own inputs (Order /
  Order Items / Order Payments), its own deterministic compute (consolidated
  join, pivot, payment-type, discounts), and its own output (Zoho upload format
  + draft `InvoiceData` per order + Validation/Exception reports). It does not
  fit any of the existing twelve subflows.

### 2. Tier `approval` — compute + draft only, §19 gate dormant in v1

The flow is registered at tier `approval` (its intent is invoice production),
but the §19 gate is **dormant in v1**: there is no `ApprovalGate`, no
`interrupt()`, no idempotency key, no `pending_approval`, no Zoho POST. It
returns `AR_OK` with the draft invoice set + Zoho upload rows + reports. It is
**not** added to `FINANCIAL_INTENTS` (no financial POST → no financial-retry
escalation). This mirrors the Intercompany Sales Flow (ADR-0005).

- **Deviation:** none — `approval` is a registered §19 tier; the gate is simply
  off in v1. The §1 north star (no money moves, no ledger entry posts) is
  preserved.
- **Why:** produce a reviewable draft + Zoho upload format now with zero
  posting risk; the §19 machinery is only justified once a posting target
  exists.
- **Build-phase upgrade to posting:** add the gate + idempotency key +
  checkpoint-before-POST + audit-with-`approval_ref` and add
  `ar_foodics_processing` (or a sibling posting flow) to `FINANCIAL_INTENTS`.

### 3. Dual-source read — uploaded export files (now) + Foodics API fetch (build-phase seam)

The `read` node is source-agnostic. A `source_mode` input (`auto` | `files` |
`api`, default `auto`) resolves the source: `auto` = files when uploaded else
API; `files`/`api` force it. **Files path:** the three Foodics exports are read
via the `cosmic_common` readers and classified by role (`order` /
`order_items` / `order_payments`) — filename keyword first, header-content
fallback (mirrors the Kitchen Revenue Flow's multi-file loop). **API path:**
`FoodicsARTool` is lazy-imported + instantiated and `fetch_foodics_data` is
called with operations `list_orders` / `list_order_items` /
`list_order_payments` inside the §10 retry loop. `FoodicsARTool` is a scaffold
today (returns `AR_NOT_IMPLEMENTED`), so the API path **fails safe**
(`AR_UPSTREAM` / `AR_NOT_IMPLEMENTED`: "Foodics API fetch is build-phase —
provide export files or wire FoodicsARTool"). §10 retry covers both paths.

- **Deviation:** none for files; the API path is a documented build-phase seam.
- **Why:** Cosmic receives Foodics data both ways; v1 delivers value from the
  deterministic file path now, with the API path as a drop-in for build-phase.
  Wiring `FoodicsARTool` HTTP + bearer token + pagination + credentials (§16
  Secret Global Variable) is build-phase.
- **Build-phase:** implement `FoodicsARTool`'s order endpoints (the AP-side
  `FoodicsAPTool` already does request/retry for receipts/sales), wire the
  `FOODICS_API_TOKEN` Secret Global Variable (§16), and the API path activates.

### 4. JSON-dataset workbook / pivot / Sheet3 — no `.xlsx` in v1

v1 is read-Excel-in, JSON-out (no flow has ever written an `.xlsx`).
`build_consolidated` / `refresh_pivot` / `populate_sheet3` are **compute nodes**
emitting structured JSON sections (`data.consolidated`, `data.pivot`,
`data.sheet3`). openpyxl is in the image but only used read-only; writing a real
`.xlsx` + pivot is a net-new code path and a documented build-phase step, not v1.

- **Deviation:** none — JSON-in-envelope is the §14 pattern; no schema is
  authored for these flow-specific datasets.
- **Why:** deliver the consolidated/pivot/Sheet3 figures as reviewable JSON
  now; a real workbook + pivot is only needed when a downstream consumer
  requires the binary artifact.
- **Build-phase:** add an openpyxl writer + pivot construction node that
  materializes `data.consolidated`/`pivot`/`sheet3` into an `.xlsx` (no
  Dockerfile change — openpyxl is installed).

### 5. Discount rules — both in-file columns and a baked-in config

`apply_discounts` reads in-file discount columns on the Order Items sheet
(`discount_amount` / `discount_pct` / `discount`) **and** applies a
`DISCOUNT_RULES` config baked into the component (a list of
`{matcher, kind:"pct"|"amount", value}` rules over `item_ref` / `category` /
`payment_type`). **Precedence:** an explicit in-file discount column wins; else
the first matching baked-in rule; else `0.00`. The per-line discount rolls into
a running `discounts_total` (2dp) and reduces line totals; the per-order share
rolls into `InvoiceData.discounts` (2dp).

- **Deviation:** none — `model_name`-style tunables belong in Global Variables
  (§17); `DISCOUNT_RULES` is the v1 seed, documented here.
- **Why:** Foodics exports sometimes carry discount columns and sometimes do
  not; supporting both (with a deterministic precedence) keeps the flow
  robust without an external rules service.
- **Build-phase:** move `DISCOUNT_RULES` to a Global Variable (§17) /
  `ConfigurationLoaderComponent` so operators can tune it without a code change.

### 6. One `InvoiceData` per `order_ref` — deterministic ids

`build_invoice` emits **one `InvoiceData` per `order_ref`** (mirrors the
Intercompany Sales Flow's per-buyer grouping). Each invoice carries
discount-adjusted line items, `subtotal`/`discounts`/`total`/`balance_due` (2dp),
`issue_date` = the order's `posted_at`, `due_date` = issue + `NET_TERMS_DAYS`
(30), `currency` (column else `SAR`), `status="draft"`, and deterministic
`invoice_id`/`invoice_number` via `uuid5(NAMESPACE_URL,
"foodics:{trace_id}:{order_ref}")` shaped `FP-{order_ref}-{8hex}`. An inline
`_validate_invoice` guard checks the `InvoiceData` schema.

- **Deviation:** none — §15 reuse of `InvoiceData`; `customer_ref` is a Zoho
  customer id (no PII — §16).
- **Why:** one invoice per POS order is the natural unit for Zoho import and
  for downstream approval/posting.

### 7. Zoho upload format — flow-specific JSON, no new schema

`build_zoho_upload` transforms the consolidated + discounted data into Zoho
Books invoice-import rows: `data.zoho_upload = {format:"zoho-books-invoice-import",
rows:[{customer_ref, invoice_number, date, item_details:[{item_ref, qty, rate,
amount, discount}], discount_total, total, currency}], count,
contract_version}`. No `ZohoUpload` / `ConsolidatedWorkbook` / `Pivot` / `Sheet3`
contract schema is authored (§15 reuse only of `InvoiceData` /
`ValidationResult` / `WorkflowState` / `Envelope`); these datasets are
flow-specific JSON in the envelope `data`, documented here.

- **Deviation:** none — §14 envelope; `customer_ref` is the Zoho customer id
  (no PII — §16).
- **Why:** the canonical Zoho import template is build-phase (it depends on
  the target Zoho org's custom fields); the flow's JSON is the source of truth
  that a build-phase mapper renders to the exact template.
- **Build-phase:** author the canonical Zoho import template + a mapper from
  `data.zoho_upload` to it.

### 8. Supervisor merge — no `AgentState` schema change

The supervisor's `_node_invoke` merges only `data.totals{matched,outstanding,
posted}` and `data.audit_refs` into `AgentState`. Invoices / consolidated /
pivot / sheet3 / zoho_upload / payment_type_summary are NOT recognized
`data.totals` keys → they stay in the envelope `data`. The flow surfaces to the
supervisor only via `subflows_invoked` + `audit_refs` (same as ADR-0005 §7 /
ADR-0006).

- **Deviation:** none — no `AgentState` schema change.
- **Why:** keep the supervisor's totals-merge contract stable; the flow's
  outputs are reviewable in the envelope without supervisor-level aggregation.

### 9. Missing role → that node emits `0.00`/empty + Validation warning (not hard fail)

A **missing role** (e.g. no `order_payments` uploaded) is a validation
**warning**, not a hard fail; downstream nodes emit `0.00`/empty for it (e.g.
`determine_payment_type` → `total_collected="0.00"`). Only **zero recognized
roles** → `AR_UNCERTAIN` (§4 fail-safe). A **required column entirely missing
for a present role** is a hard `AR_VALIDATION` (the flow cannot build that
role's data). All-rows-fail → `AR_VALIDATION`.

- **Deviation:** none — §4 fail-safe preserved.
- **Why:** a partial upload (orders + items, payments later) should still
  produce the consolidated dataset + invoices, with a warning surfacing the
  gap.

### 10. Checkpoints after every calculation — continues ADR-0006's stricter pattern

Each calc/transform node (`build_consolidated` / `refresh_pivot` /
`determine_payment_type` / `apply_discounts` / `populate_sheet3` /
`build_zoho_upload` / `build_invoice`) records a labeled `_audit_ref(trace_id,
label)` into `audit_refs` and a `checkpoints{<label>}` map, persisted by
`InMemorySaver` at each super-step (§11). A final aggregate
`_audit_ref(trace_id, "foodics_processing")` is recorded at the `checkpoint`
node. This continues ADR-0006's stricter "checkpoints after every
calculation" pattern (beyond §11's "after each reconciled batch").

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each calc/transform is an auditable, resumable boundary; the
  checkpoint is the source of truth for resume while Langfuse tracing is off.

### 11. `InMemorySaver` v1 / durable Postgres build-phase

Checkpointing uses the in-image `InMemorySaver` keyed by `session_id`. This is
the §11 **fallback**: non-durable (lost on worker recreate). Durable Postgres
checkpointing (`langgraph-checkpoint-postgres` into the `ar_agent` DB) remains
a documented build-phase step (same §11 fallback as the supervisor / File
Intake / Intercompany / Kitchen flows).

- **Deviation:** none — documented §11 fallback.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer.

### 12. Inline hand-rolled validators — `ValidationEngineComponent` only implements `DocumentManifest` today

The flow uses inline hand-rolled per-role validators + an inline
`_validate_invoice` guard (mirrors the File Intake / Intercompany / Kitchen
flows), because `ValidationEngineComponent` only implements `DocumentManifest`
today. Wiring these into `ValidationEngineComponent` for `InvoiceData` (and the
flow-specific datasets) is build-phase.

- **Deviation:** none — same §15 reuse note as ADR-0004/0005/0006.
- **Build-phase:** extend `ValidationEngineComponent` to validate `InvoiceData`
  and route the flow's outputs through it.