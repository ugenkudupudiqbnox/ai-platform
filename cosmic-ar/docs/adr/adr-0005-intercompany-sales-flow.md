# ADR 0005 — Intercompany Sales Flow: 11th subflow, KOT Excel → draft InvoiceData per buyer (compute + draft only)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0004](adr-0004-file-intake-flow.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [intercompany sales](../intercompany-sales.md), [supervisor](../supervisor.md)

## Context

ADR-0004 added the File Intake Flow as the 10th AR subflow, amending
architecture §4's "Nine reusable LangFlow subflows" to "Ten". None of those ten
covers **intercompany sales**: Cosmic sells to intercompany customer-restaurants
inside a Marriott hotel (**HYP** and **Upyard**) and receives those sales as
**KOT (Kitchen Order Ticket) Excel** sheets — one row per ordered menu item
carrying its quantity and the intercompany **agreed rate** (the transfer price
Cosmic charges the buyer). The supervisor had no path for "produce an
intercompany invoice from a KOT": an uploaded KOT with no text keyword failed
safe with `AR_UNCERTAIN` and was never read. This ADR records the nine decisions
made when the **Intercompany Sales Flow** (`ar_intercompany_sales`) was added as
the 11th AR subflow (the new `IntercompanySalesFlowComponent` orchestrator, the
wired `ar_intercompany_sales.json` canvas, and the supervisor wiring).

## Decisions

### 1. A new 11th subflow, amending architecture §4's "Ten reusable subflows"

`ar_intercompany_sales` is added to `SUBFLOWS` as the 11th entry, with a `TIER`
entry and an `INTENT_KEYWORDS` entry, and an 11th `RunFlow` node on the
supervisor canvas. Architecture §4's table grows a row 11 and its heading
becomes "Eleven reusable LangFlow subflows"; §5's diagram grows a
`route → intercompany_sales → effect` branch.

- **Deviation:** architecture §4 said "Ten reusable LangFlow subflows" (after
  ADR-0004). This adds an 11th. Per the constitution's Authority note, a
  deviation from a binding standard is recorded as a written waiver in the
  flow's README **and a linked ADR** — this is that ADR.
- **Why:** intercompany sales are a distinct AR activity (transfer pricing to
  sister properties) with its own input (KOT Excel), its own deterministic
  compute (qty × agreed rate), and its own output (one draft invoice per
  buyer). It does not fit any of the existing ten subflows.

### 2. Tier `approval`, but v1 is compute + draft only — gate dormant

The flow is registered at tier `approval` (its *intent* is invoice
production, a financial-tier activity). But v1 stops at a **draft**
`InvoiceData` JSON: there is no `ApprovalGate`, no `interrupt()`, no
idempotency key, no `pending_approval`, no Zoho POST. It returns `AR_OK` with
the draft + Validation/Exception reports. It is **not** added to
`FINANCIAL_INTENTS` (no financial POST → no financial-retry escalation).

- **Deviation:** §19 routes approval-tier mutations through the gate. v1
  deliberately leaves the gate dormant because nothing is posted this turn —
  the §1 north star (no money moves, no ledger entry posts) is preserved.
- **Why:** produce a reviewable draft now with zero posting risk; the §19
  machinery is heavy (gate + idempotency + checkpoint-before-POST +
  audit-with-`approval_ref`) and is only justified once issuance is real.
- **Build-phase upgrade to issuance:** add the gate + idempotency key +
  checkpoint-before-POST + audit-with-`approval_ref` + add `ar_intercompany_sales`
  to `FINANCIAL_INTENTS` (mirrors `ar_issue_invoice`, architecture §4 row 7).
  That upgrade is the point at which the tier's gate becomes live.

### 3. KOT menu / quantity / agreed-rate are columns already in the sheet — deterministic, no lookups

The menu item (`item_ref`), quantity (`qty`), and agreed rate (`agreed_rate`)
are **columns in the KOT Excel**. The flow does no `ar_tools` call, no
`ConfigurationLoader` lookup, no menu/rate config table. The only §10 retry is
around the Excel **read** (transient I/O); the validate/calculate/build path is
pure and in-file.

- **Deviation:** none — this matches the File Intake Flow's in-file
  determination (ADR-0004) and keeps v1 dependency-free beyond `openpyxl`
  (already in the image per ADR-0004).
- **Why:** the KOT already carries the transfer-pricing facts; an external
  lookup would add a secret/dependency surface (§16) and a §10 retry surface
  for no benefit in v1.

### 4. One `InvoiceData` per buyer, emitted as `data.invoices` (a list)

The flow groups valid rows by `customer_ref` and emits **one `InvoiceData` per
buyer** (intercompany = one invoice per seller→buyer pair). HYP + Upyard → two
invoices; a single-buyer sheet → a one-element list. Each invoice has
`status="draft"`, one `line_item` per KOT row (`unit_price` = agreed rate,
`amount` = qty × rate), `issue_date` = the buyer's earliest `posted_at`,
`due_date` = issue + `NET_TERMS_DAYS` (deterministic 30, §4.3).

- **Why:** "generate invoice JSON" is per-buyer by definition of an
  intercompany transfer; a single consolidated invoice would mix buyers and
  break the one-customer-per-invoice `InvoiceData` contract.
- `invoice_id`/`invoice_number` are **deterministic** —
  `uuid.uuid5(NAMESPACE_URL, "intercompany:{trace_id}:{customer_ref}")` — so the
  same trace + buyer always yields the same ids (§4.3, no `Math.random`/
  `uuid4`). The invoice number is shaped `IC-<customer>-<8hex>`.

### 5. Reuse `ValidationResult` for both reports (no 16th schema — §15)

The Validation Report is a full `ValidationResult` over all KOT rows; the
Exception Report is the subset of `ValidationResult.errors` for the rows that
failed business rules, emitted as a **second** `ValidationResult` scoped to
failures (each error carries a `rule_id` like `kot.qty_positive`/
`kot.rate_positive`/`kot.date_iso`/`kot.customer_ref_required`). No
`ExceptionReport` schema is authored — §15 reuse.

- **Deviation:** none. §15 mandates reuse before authoring; `ValidationResult`
  already has `valid`/`errors[]`/`code` pattern `^AR_VALIDATION(_[A-Z_]+)?$`/
  `rule_id?` — exactly the shape an exception report needs.
- **The envelope `data` carries `{invoices, revenue, validation_report,
  exception_report, workflow_state, audit_refs, ...}`.**

### 6. Reuse `RevenueData` for revenue — do NOT extend `CalculationResult`

Revenue is computed as a `RevenueData` (`total`, `currency`, `by_segment`
grouped by `customer_ref`, `by_customer_ref`, `by_invoice` backfilled from the
  built invoices, `period`). The `CalculationResult.calculation_type` enum is
`["match","reconcile","aging","rounding"]` — there is no `"revenue"` value and
this ADR does **not** add one (no contract change).

- **Deviation:** none. Using `RevenueData` (already a contract) instead of
  extending `CalculationResult` keeps the contract surface unchanged (§15).

### 7. Supervisor merge: revenue is not a recognized `data.totals` key

The supervisor's `_node_invoke` only merges `data.totals{matched,outstanding,
posted}` (2dp strings) and `data.audit_refs`/`data.audit_ref` into `AgentState`.
`RevenueData.total` is **not** under `data.totals` (it is `data.revenue.total`),
so the supervisor does not recognise it as a financial total. The flow surfaces
to the supervisor only via `subflows_invoked` + `audit_refs`; the
revenue/invoices/reports stay in the envelope `data`.

- **Deviation:** none — **no `AgentState` schema change**. No `revenue` field is
  added to `AgentState`. v1 emits no `data.totals{matched,outstanding,posted}`
  (those stay `"0.00"` inside `data.workflow_state`), so the supervisor's totals
  are unaffected by an intercompany-sales run.

### 8. `InMemorySaver` v1; durable Postgres is build-phase

The flow compiles its graph with `InMemorySaver()` keyed by `session_id`, the
same §11 fallback as the supervisor (ADR-0003 §2) and the File Intake Flow
(ADR-0004 §7). Non-durable (lost on worker recreate); the durable
`langgraph-checkpoint-postgres` upgrade is a documented build-phase step shared
across all three orchestrators.

- **Why:** keeps `make validate`/CI green now (no `docker-compose.yml`/
  `Dockerfile`/`.env`/`gen-secrets.sh` edits in this task) while the §11 caveat
  (Langfuse tracing OFF, checkpoint is the source of truth for resume) is
  satisfied for the dev/preview path.

### 9. Inline hand-rolled validators (mirroring File Intake)

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract (`InvoiceData`, `RevenueData`, `WorkflowState`) returns
`AR_NOT_IMPLEMENTED`. So the orchestrator uses **inline hand-rolled validators**
for KOT rows (`_validate_kot_row`/`_validate_kot_rows`), `InvoiceData`
(`_validate_invoice`), and the report builders (`_build_validation_report`,
`_classify_exceptions`) — mirroring the File Intake Flow's inline validators.
Wiring `ValidationEngineComponent` for those contracts is a documented
build-phase step (ADR-0002 §15 waiver territory).

## Consequences

- Positive: an uploaded KOT now produces a reviewable draft `InvoiceData` per
  buyer + a Validation Report + an Exception Report through a deterministic,
  dependency-light path, with the §19 gate deliberately dormant so no money
  moves; the supervisor classifies "intercompany sales"/"KOT" and routes here
  at confidence ≥ `MIN_CONFIDENCE`; `make test`/`make validate`/CI stay green.
- Negative: in-memory checkpoints are non-durable until the build-phase
  Postgres upgrade; the gate is dormant so v1 cannot actually issue the
  invoices it drafts (a human must take the draft and post it out-of-band, or
  the build-phase issuance upgrade must land).
- Build-phase follow-ups: (a) issuance upgrade — add the §19 gate + idempotency
  + checkpoint-before-POST + audit-with-`approval_ref` + add to
  `FINANCIAL_INTENTS` (mirrors `ar_issue_invoice` row 7); (b) wire
  `ValidationEngineComponent` for `InvoiceData`/`RevenueData`/`WorkflowState`
  (replace the inline validators); (c) durable Postgres checkpointer (shared
  with supervisor/File Intake); (d) import the 11 subflows + `supervisor.json`,
  open the supervisor flow so the 11th `RunFlow` resolves `flow_id_selected`.