# ADR 0006 — Cosmic Kitchen Revenue Flow: 12th subflow, four sheets → Revenue/Collections/Expenses/Net Receivable/Net Payable (read-only compute + report)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0005](adr-0005-intercompany-sales-flow.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§19/§20,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [kitchen revenue](../kitchen-revenue.md), [supervisor](../supervisor.md),
  [file intake](../file-intake.md), [intercompany sales](../intercompany-sales.md)

## Context

ADR-0005 added the Intercompany Sales Flow as the 11th AR subflow, amending
architecture §4's "Eleven reusable LangFlow subflows". None of those eleven
covers **Cosmic Kitchen revenue reporting**. Cosmic Kitchen operates inside a
Marriott hotel and produces four daily Excel/CSV sheets — **Menu Sales
Analysis**, **Daily Sales**, **Detailed Check Payment**, and **Marriott
Backup** — that together describe a period's revenue (split by meal period:
**Breakfast**, **Half Board**, …), the payments collected against it, the
kitchen's expenses, and the resulting **Net Receivable** / **Net Payable**
positions. Today the supervisor has no path that turns these four sheets into a
single revenue report: an upload with no matching keyword fails safe with
`AR_UNCERTAIN` and is never read. (`ar_reporting` is a `read-only`-tier name
registered in the supervisor but **not implemented** — no component file
exists — so it cannot do this work.)

This ADR records the eleven decisions made when the **Cosmic Kitchen Revenue
Flow** (`ar_kitchen_revenue`) was added as the 12th AR subflow (the new
`KitchenRevenueFlowComponent` orchestrator, the wired
`ar_kitchen_revenue.json` canvas, and the supervisor wiring).

## Decisions

### 1. A new 12th subflow, amending architecture §4's "Eleven reusable subflows"

`ar_kitchen_revenue` is added to `SUBFLOWS` as the 12th entry, with a `TIER`
entry and an `INTENT_KEYWORDS` entry, and a 12th `RunFlow` node on the
supervisor canvas. Architecture §4's table grows a row 12 and its heading
becomes "Twelve reusable LangFlow subflows"; §5's diagram grows a
`route → kitchen_revenue → effect` branch.

- **Deviation:** architecture §4 said "Eleven reusable LangFlow subflows"
  (after ADR-0005). This adds a 12th. Per the constitution's Authority note, a
  deviation from a binding standard is recorded as a written waiver in the
  flow's README **and a linked ADR** — this is that ADR.
- **Why:** kitchen revenue is a distinct AR activity (period revenue reporting
  for an in-hotel kitchen) with its own inputs (the four daily sheets), its own
  deterministic compute (Revenue by meal period, Collections, Expenses, Net
  Receivable/Net Payable), and its own output (a Revenue JSON + Validation/
  Exception reports). It does not fit any of the existing eleven subflows.

### 2. Tier `read-only` — compute + report only, no posting

The flow is registered at tier `read-only`. v1 computes and reports Revenue,
Collections, Expenses, Net Receivable, and Net Payable but **posts nothing**:
there is no §19 gate, no `interrupt()`, no idempotency key, no
`pending_approval`, no ledger POST. It returns `AR_OK` with the figures +
Validation/Exception reports. It is **not** added to `FINANCIAL_INTENTS` (no
financial POST → no financial-retry escalation). Net Receivable / Net Payable
are **reported** figures, not ledger mutations (mirrors the
`ar_reporting` read-only slot).

- **Deviation:** none — `read-only` is the §19 tier that proceeds unattended;
  the §1 north star (no money moves, no ledger entry posts) is preserved.
- **Why:** produce a reviewable revenue report now with zero posting risk; the
  §19 machinery (gate + idempotency + checkpoint-before-POST +
  audit-with-`approval_ref`) is only justified once a posting target exists.
- **Build-phase upgrade to posting:** if/when kitchen revenue figures feed a
  GL post or an AP voucher, add the gate + idempotency key +
  checkpoint-before-POST + audit-with-`approval_ref` and add
  `ar_kitchen_revenue` (or a sibling posting flow) to `FINANCIAL_INTENTS`.

### 3. Four inputs mapped to roles by filename keyword (header fallback) — deterministic, in-file

Each uploaded sheet is classified into one of four roles — `menu_sales`,
`daily_sales`, `check_payment`, `marriott_backup` — by **filename keyword**
(`menu`/`daily`/`check`|`payment`/`marriott`|`backup`), with a **header-content
fallback** (payment_id/method → check_payment; meal_period/service_type/
package → menu_sales; expense_category/expense_type → marriott_backup). All
facts (amounts, segments, methods, categories, dates) are **columns in the
sheets**. The flow does no `ar_tools` call, no `ConfigurationLoader` lookup, no
menu/rate/expense config table. The only §10 retry is around the file **reads**
(transient I/O); the validate/calculate path is pure and in-file.

- **Deviation:** none — this matches the File Intake / Intercompany in-file
  determination (ADR-0004/ADR-0005) and keeps v1 dependency-light beyond
  `openpyxl` (already in the image per ADR-0004).
- **Why:** the four sheets already carry the revenue facts; an external lookup
  would add a secret/dependency surface (§16) and a §10 retry surface for no
  benefit in v1.

### 4. Revenue source priority: Menu Sales authoritative, Daily Sales cross-check

`calculate_revenue` builds `RevenueData` from the sales roles with **source
priority**: Menu Sales Analysis (line items carrying the meal-period column) is
**authoritative**; Daily Sales is a **cross-check** (its grand total must
reconcile with the Menu Sales total within `0.01`; a larger divergence is
appended to the Exception Report as a **warning**, not a hard fail). If Menu
Sales is absent, the flow falls back to Daily Sales (segment =
`"daily_summary"`). `by_segment` groups by the meal-period column (Breakfast →
`breakfast`, Half Board → `half_board`); `by_segment` always has ≥1 entry
(`RevenueData` `minItems: 1`).

- **Deviation:** none.
- **Why:** treating both sheets as authoritative would double-count; treating
  Menu Sales alone would miss a divergence the kitchen needs to see. The
  cross-check surfaces the divergence as a warning (not a hard fail) so a
  reporting flow still produces its figures while flagging the discrepancy —
  the same "warn, don't block" posture as the Intercompany cross-check.

### 5. Reuse `CalculationResult` (reconcile) for nets — no new contract

The Net Receivable / Net Payable positions ride in a `CalculationResult` with
`calculation_type="reconcile"` and `totals={total_revenue, total_collections,
total_expenses, net_receivable, net_payable}` (all signed 2dp). `line_items[]`
carries one entry per top-level figure plus one per expense category (each with
`source_refs`). `Net Receivable = Revenue − Collections`; `Net Payable = total
Expenses`. No new contract is authored and the `CalculationResult.
calculation_type` enum is unchanged (§15 reuse).

- **Deviation:** none. `CalculationResult` already supports arbitrary
  `totals` keys (`additionalProperties: ^-?\d+\.\d{2}$`) and the `reconcile`
  type — exactly the shape a net-position report needs.

### 6. `ExpenseData` is NOT used — "Expenses" is a reported total, not an adjustment

"Expenses" is a **reported** total computed from the Marriott Backup sheet
(`total` + `by_category`, signed 2dp), stashed on state as working data and
carried into the nets `CalculationResult.line_items`. It is **not** an
`ExpenseData` — `ExpenseData` is AR-adjustments-only (refunds/write-offs/credit
notes) and every adjustment requires `approval_ref` + `idempotency_key`, which
does not fit a read-only expense *calculation* from an uploaded sheet. "Net
Payable" is a **reported** figure, **not an AP posting** (§20 seed-only — AP
is a future extension; this flow does not create vendor invoices or match
them).

- **Deviation:** none — §15 mandates reuse before authoring; `ExpenseData`
  does not fit a read-only calculation, and authoring a 16th contract for the
  kitchen expense breakdown would violate that. The nets `CalculationResult`
  already carries the breakdown in `line_items`.
- **Why:** keeps the contract surface unchanged and the flow read-only; an
  `ExpenseData` would drag in the §19 approval/idempotency requirements that a
  *reported* figure must not trigger.

### 7. Supervisor merge: revenue/collections/nets are not recognized `data.totals` keys

The supervisor's `_node_invoke` only merges `data.totals{matched,outstanding,
posted}` (2dp strings) and `data.audit_refs`/`data.audit_ref` into `AgentState`.
`RevenueData.total`, `CollectionData.total_collected`, and the nets
`CalculationResult.tots` are **not** under `data.totals{matched,outstanding,
posted}` (they are `data.revenue.total`, `data.collections.total_collected`,
`data.nets.totals`), so the supervisor does not recognise them as financial
totals. The flow surfaces to the supervisor only via `subflows_invoked` +
`audit_refs`; the revenue/collections/nets/reports stay in the envelope `data`.

- **Deviation:** none — **no `AgentState` schema change**. No
  `revenue`/`net_receivable`/`net_payable` field is added to `AgentState`. v1
  emits no `data.totals{matched,outstanding,posted}` (those stay `"0.00"` inside
  `data.workflow_state`), so the supervisor's totals are unaffected by a
  kitchen-revenue run (same as ADR-0005 §7).

### 8. Missing role → that calc emits `0.00` + a validation warning (not a hard fail); zero recognized roles → `AR_UNCERTAIN`

A **missing role** (e.g. no Detailed Check Payment uploaded) is recorded as a
validation **warning** in the Exception Report (`rule_id` `kr.<role>_missing`)
and that calc emits `0.00` (Collections = `0.00`, `payments=[]`, etc.). Only a
**present role missing a required column** is a hard `AR_VALIDATION` (the flow
cannot produce that role's figure). **Zero recognized roles** (no sheet matched
any of the four) is a hard `AR_UNCERTAIN` (§4 fail-safe, at the `read` node).
All-rows-fail (rows present but none valid) is a hard `AR_VALIDATION`.

- **Deviation:** none — §4 fail-safe governs the zero-recognized and
  all-rows-fail cases; the missing-role warning mirrors the File Intake /
  Intercompany "warn, don't block" posture for partial inputs.

### 9. Checkpoints after every calculation — a new, stricter pattern than File Intake / Intercompany

Each calc node (`calc_revenue`/`calc_collections`/`calc_expenses`/`calc_nets`)
records a **labeled** `_audit_ref` (`uuid5(NAMESPACE_URL,
"kitchen-revenue-audit:{trace_id}:{label}")`) into `audit_refs` and a
`checkpoints{<label>}` map (`{revenue, collections, expenses, nets}`), persisted
by `InMemorySaver` at each super-step. A final aggregate checkpoint
(`label="kitchen_revenue"`) is recorded at the `checkpoint` node and reflected
into the `WorkflowState` snapshot. This is a **new, stricter pattern** than the
File Intake / Intercompany single-end-checkpoint (§11 says "after each
reconciled batch"; this flow strengthens it to "after every calculation" per
the user's explicit requirement).

- **Deviation:** §11's checkpoint cadence is "after each reconciled batch"; the
  existing flows (File Intake, Intercompany) record a single end checkpoint.
  This flow records one per calc. The deviation is recorded here (and in the
  flow's README) per the constitution's Authority note.
- **Why:** a revenue report's audit trail is more useful when each figure's
  checkpoint is individually addressable (revenue vs collections vs expenses vs
  nets), so a reviewer can trace a single net-receivable figure back to its
  exact calc step. The cost is four checkpoint writes per run instead of one —
  acceptable for a read-only reporting flow.

### 10. `InMemorySaver` v1; durable Postgres is build-phase

The flow compiles its graph with `InMemorySaver()` keyed by `session_id`, the
same §11 fallback as the supervisor (ADR-0003 §2), the File Intake Flow
(ADR-0004 §7), and the Intercompany Sales Flow (ADR-0005 §8). Non-durable (lost
on worker recreate); the durable `langgraph-checkpoint-postgres` upgrade is a
documented build-phase step shared across all four orchestrators.

- **Why:** keeps `make validate`/CI green now (no `docker-compose.yml`/
  `Dockerfile`/`.env`/`gen-secrets.sh` edits in this task) while the §11 caveat
  (Langfuse tracing OFF, checkpoint is the source of truth for resume) is
  satisfied for the dev/preview path.

### 11. Inline hand-rolled validators (mirroring File Intake / Intercompany)

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract (`RevenueData`, `CollectionData`, `CalculationResult`,
`WorkflowState`) returns `AR_NOT_IMPLEMENTED`. So the orchestrator uses
**inline hand-rolled validators** for the per-role rows (`_validate_role_row`/
`_validate_role_rows`) and the report builders (`_build_validation_report`,
`_classify_exceptions`) — mirroring the File Intake / Intercompany inline
validators (ADR-0004/ADR-0005). Wiring `ValidationEngineComponent` for those
contracts is a documented build-phase step (ADR-0002 §15 waiver territory).

## Consequences

- Positive: an upload of the four kitchen sheets now produces a reviewable
  Revenue JSON (Breakfast/Half Board segments), Collections, Expenses, Net
  Receivable, and Net Payable through a deterministic, dependency-light path,
  with a per-calculation checkpoint audit trail and the §19 gate deliberately
  absent so no money moves; the supervisor classifies "kitchen revenue"/"menu
  sales"/"marriott backup" etc. and routes here at confidence ≥
  `MIN_CONFIDENCE`; `make test`/`make validate`/CI stay green.
- Negative: in-memory checkpoints are non-durable until the build-phase
  Postgres upgrade; the per-calc checkpoint pattern writes four checkpoints
  per run (vs one for File Intake/Intercompany) — acceptable for read-only
  reporting, but noted here so future flows choose deliberately between the two
  patterns; v1 cannot post the figures it reports (a human takes the report and
  acts out-of-band, or the build-phase posting upgrade lands).
- Build-phase follow-ups: (a) posting upgrade — if kitchen revenue figures feed
  a GL post or AP voucher, add the §19 gate + idempotency + checkpoint-before-POST
  + audit-with-`approval_ref` + add the posting flow to `FINANCIAL_INTENTS`;
  (b) wire `ValidationEngineComponent` for `RevenueData`/`CollectionData`/
  `CalculationResult`/`WorkflowState` (replace the inline validators); (c)
  durable Postgres checkpointer (shared with supervisor/File Intake/Intercompany);
  (d) import the 12 subflows + `supervisor.json`, open the supervisor flow so
  the 12th `RunFlow` resolves `flow_id_selected`.