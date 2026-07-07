# Cosmic Kitchen Revenue Flow (`ar_kitchen_revenue`)

The **Cosmic Kitchen Revenue Flow** is the 5th AR subflow (architecture §4 row
5; [ADR-0006](adr/adr-0006-kitchen-revenue-flow.md)). Cosmic Kitchen operates
inside a Marriott hotel and produces four daily Excel/CSV sheets — **Menu Sales
Analysis**, **Daily Sales**, **Detailed Check Payment**, and **Marriott
Backup** — that together describe a period's revenue (split by meal period:
**Breakfast**, **Half Board**, …), the payments collected against it, the
kitchen's expenses, and the resulting **Net Receivable** / **Net Payable**
positions. This flow reads the four sheets, classifies each by role (filename
keyword, header fallback), validates their rows, calculates **Revenue**
(Breakfast/Half Board as `RevenueData.by_segment`), **Collections**
(`CollectionData`), **Expenses** (a reported total — not an AP posting), and
the **Net Receivable** / **Net Payable** positions (a `CalculationResult` of
`calculation_type="reconcile"`), generates a **Revenue JSON**, a **Validation
Report**, and an **Exception Report**, updates `WorkflowState`, and returns
structured JSON — with logging (§12), retries (§10), and **checkpoints after
every calculation** (§11 — the user's explicit stricter requirement). It is the
**single stateful orchestrator** for kitchen revenue, mirroring the
supervisor, the File Intake Flow, and the Intercompany Sales Flow: its
responsibilities map to LangGraph nodes inside one `lfx` component,
`KitchenRevenueFlowComponent`.

**v1 is read-only compute + report**: it produces the figures for review; it
does **not** post anything, so no money moves and no ledger entry posts this
turn (§1 north star preserved, like the other read-only report flows). The flow is
registered at tier `read-only` — there is no §19 gate, no idempotency key, no
`pending_approval`. `Net Receivable` / `Net Payable` are **reported** figures,
not ledger mutations. "Expenses" is a reported total from the Marriott Backup
sheet, **not** an `ExpenseData` (AR-adjustments-only, requires
`approval_ref`+`idempotency_key`) and **not** an AP posting (§20 seed-only).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§19/§20, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0006](adr/adr-0006-kitchen-revenue-flow.md),
[ADR-0005](adr/adr-0005-intercompany-sales-flow.md),
[ADR-0004](adr/adr-0004-file-intake-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue.py`](../../docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue.py)
  — `KitchenRevenueFlowComponent` (internal LangGraph
  `StateGraph[KitchenRevenueState]` + `InMemorySaver`).
- **Reused generic parts (`cosmic_common`, §15):**
  [`excel_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/excel_reader.py),
  [`csv_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/csv_reader.py)
  (lazy-imported inside the `read` node; no `pdf_reader` use — the four kitchen
  sheets are Excel/CSV).
- **Flow JSON:** [`flows/ar_kitchen_revenue.json`](../flows/ar_kitchen_revenue.json).
- **Self-test:** [`kitchen_revenue_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue_selftest.py)
  (199 stdlib-only pure-function checks) via `scripts/kitchen-revenue.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept uploaded sheets | `ingest` | Bind `trace_id` (minted), `flow_id="ar_kitchen_revenue"`, `tenant="cosmic-vikings"`, `created_at`/`updated_at`; carry the `files` refs in **context** (not state — §8). status="created". |
| Read + classify the four sheets | `read` | `_expand_files` → per-file `_normalize_file`+`_resolve_storage_path`+`detect_type` (must be `excel`/`csv`), `_make_reader`+`_read_with_retry` (§10: 3 attempts, exp 1s·2^n ±25% jitter ≤30s; hard error → `AR_VALIDATION`, transient exhausted → `AR_UPSTREAM`) — **mirrors the File Intake Flow's multi-file loop**. Each file's rows → `_classify_input(name, rows)` → role bucket in `inputs`. Unknown type/no readable file → `AR_UNCERTAIN` (§4). Zero recognized roles → `AR_UNCERTAIN`. status="read". Router `_after_read`: `{failed:respond, read:validate}`. |
| Validate rows | `validate` | Inline hand-rolled per-role validator with role-specific required columns (`menu_sales`: segment+date; `daily_sales`: date; `check_payment`: payment_id+amount+method+date; `marriott_backup`: amount+date). Per-row checks: amounts parseable (signed 2dp where appropriate), dates ISO, methods in enum (check→`bank_transfer`), segments non-empty. A **required column entirely missing for a present role** → hard `AR_VALIDATION`. Else the full `ValidationResult` is built. status="validated". Router `_after_validate`: `{failed:respond, validated:classify_exceptions}`. |
| Generate Exception Report | `classify_exceptions` | Split rows into valid vs exception; build the Exception Report = a `ValidationResult` scoped to failures (each error carries a `rule_id` like `kr.amount_positive`/`kr.date_iso`/`kr.method_enum`/`kr.segment_required`). A **missing role** is a validation warning (not a hard fail); that calc emits `0.00`. All-rows-fail → `AR_VALIDATION`. status="classified". Router `_after_classify`: `{failed:respond, classified:calc_revenue}`. |
| Calculate Revenue | `calc_revenue` | Build `RevenueData` from the sales roles. **Menu Sales authoritative**; **Daily Sales cross-check** (divergence >0.01 → Exception Report warning, not a hard fail). `by_segment` groups by the meal-period column (Breakfast → `breakfast`, Half Board → `half_board`); always ≥1 entry. `period` = min/max date; `currency` from a column else default `SAR`. **Records a checkpoint** (`_audit_ref(trace_id,"revenue")`). status="revenue". |
| Calculate Collections | `calc_collections` | Build `CollectionData` from `check_payment` rows: `total_collected`=Σ; `payments[]` (payment_id, customer_ref, amount 2dp, method mapped to enum, `posted_at` ISO, `match_status="unmatched"` — v1 has no invoice list, so `matched_amount="0.00"`, `unmatched_amount=total`); `by_method[]`. **Records a checkpoint**. status="collections". |
| Calculate Expenses | `calc_expenses` | From `marriott_backup` rows: `total`=Σ (signed 2dp) + `by_category[]`. **Reported** total — not an `ExpenseData`, not an AP posting (§20 seed-only). Stash for `calc_nets`. **Records a checkpoint**. status="expenses". |
| Calculate Net Receivable / Net Payable | `calc_nets` | Build a `CalculationResult` (`calculation_type="reconcile"`) with `totals={total_revenue, total_collections, total_expenses, net_receivable(=rev−collections), net_payable(=total_expenses)}` (signed 2dp) + `line_items[]` (one per top-level figure + one per expense category). **Records a checkpoint**. status="nets". |
| Update Workflow State | `build_state` | Build a `WorkflowState` snapshot: `status="completed"`, `intent="ar_kitchen_revenue"`, `matched_amount`/`outstanding_balance`/`posted_total="0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (no POST), `audit_refs`. Immutable (§8). status="completed". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"kitchen_revenue")`; reflect `audit_refs`+`checkpoints` into the `WorkflowState` snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). |
| Return structured JSON | `respond` | `_finalize_envelope` builds `data={revenue, collections, nets, validation_report, exception_report, workflow_state, audit_refs, checkpoints, document_count, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). |
| Logging | `run()` boundary | §12 structured `key=value` via `logging.getLogger("ar.kitchen_revenue")`: `trace_id`/`flow_id`/`tenant`/`ar_entity="kitchen_revenue"`/`event`/`outcome`/`code`; no PII/secrets (customer refs are ids — §16). |
| Retries | `read` wrapper | §10 loop (above); the only retry surface in v1 (no external lookups — all from sheet columns). |
| Checkpoints after every calculation | `calc_revenue`/`calc_collections`/`calc_expenses`/`calc_nets` + `checkpoint` | **NEW stricter pattern** (no existing flow does this): each calc node records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (`{revenue, collections, expenses, nets}`), persisted by `InMemorySaver` at each super-step (§11 "after each reconciled batch", strengthened to "after every calculation" — ADR-0006 §9). |

Graph edges: `START → ingest → read → validate → classify_exceptions →
calc_revenue → calc_collections → calc_expenses → calc_nets → build_state →
checkpoint → respond → END`, with conditional short-circuits to `respond` on any
`failed` status (`_after_read`/`_after_validate`/`_after_classify` return
`state.status` against status-keyed path maps — ADR-0003 §9).

## Canvas wiring (3 nodes / 3 edges)

`ar_kitchen_revenue.json` wires (modeled on `ar_intercompany_sales.json`):

- `ChatInput.message → KitchenRevenueFlowComponent.user_input`
- `ChatInput.message → KitchenRevenueFlowComponent.files` (inputTypes
  `["Data","Message"]`, type `source`)
- `KitchenRevenueFlowComponent.kitchen_revenue_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Intercompany canvas;
the orchestrator node's full source is embedded as `template.code.value`. There
is no standalone `File` node — files ride on the ChatInput `.files` handle into
the orchestrator's `files` HandleInput (ADR-0003 §8 explains why a standalone
`File` node is avoided).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, optional, `tool_mode` — carries
  intent keywords), `files` (HandleInput, `is_list`, `input_types=["Data",
  "Message"]` — the four kitchen-sheet refs), `model_name` (MessageTextInput —
  documented LLM hook; deterministic v1 ignores it).
- **Output:** `kitchen_revenue_output` (Message) — the §14 envelope JSON.

## The four-input role classification

Each uploaded sheet → one role by filename keyword (header fallback):

| Sheet (example filename) | Role | Required columns | Feeds |
|---|---|---|---|
| Menu Sales Analysis (`menu…`) | `menu_sales` | segment (meal period) + date | Revenue (authoritative) |
| Daily Sales (`daily…`) | `daily_sales` | date | Revenue (cross-check / fallback) |
| Detailed Check Payment (`check…`/`payment…`) | `check_payment` | payment_id + amount + method + date | Collections |
| Marriott Backup (`marriott…`/`backup…`) | `marriott_backup` | amount + date | Expenses |

A sheet that matches no keyword and no header signature is skipped (not a
failure). **Zero recognized roles** → `AR_UNCERTAIN` (§4).

## The Menu-Sales-authoritative / Daily-Sales-cross-check design

Revenue is built from the sales roles with **source priority**: Menu Sales
Analysis (line items carrying the meal-period column) is authoritative; Daily
Sales is a cross-check. Its grand total must reconcile with the Menu Sales
total within `0.01`; a larger divergence is appended to the Exception Report as
a **warning** (`rule_id="kr.revenue_cross_check"`), not a hard fail. This
avoids silent double-counting (both sheets would over-state revenue) while
still producing the report when the two sheets disagree. If Menu Sales is
absent, the flow falls back to Daily Sales (segment = `"daily_summary"`).

## The read-only v1 / build-phase posting checklist

v1 is **read-only compute + report** — no §19 gate, no idempotency key, no
`pending_approval`, not in `FINANCIAL_INTENTS`, no posting. `Net Receivable` /
`Net Payable` are *reported* figures. Build-phase (not done here):

1. **Posting upgrade** — if/when kitchen revenue figures feed a GL post or an
   AP voucher, add the §19 gate + idempotency key + checkpoint-before-POST +
   audit-with-`approval_ref` and add the posting flow to `FINANCIAL_INTENTS`.
2. **Rebuild the `langflow` image** so `openpyxl` is available for `.xlsx`
   sheets (`docker compose build langflow langflow-worker`) — already required
   by the File Intake Flow (ADR-0004 §3); CSV works without a rebuild.
3. **Import the nine subflows first** (incl. `ar_kitchen_revenue.json`),
   then `supervisor.json`; open the supervisor flow so the 5th `RunFlow`
   resolves `flow_id_selected`.
4. **Wire `ValidationEngineComponent`** for `RevenueData`/`CollectionData`/
   `CalculationResult`/`WorkflowState` (replace the inline validators).
5. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; kitchen revenue follows for free).

## The supervisor merge (no `AgentState` change)

`RevenueData.total`, `CollectionData.total_collected`, and the nets
`CalculationResult.totals` are not under `data.totals{matched,outstanding,
posted}`, so the supervisor's `_node_invoke` does not recognise them as
financial totals. The flow surfaces to the supervisor only via
`subflows_invoked` + `audit_refs`; the revenue/collections/nets/reports stay in
the envelope `data`. **No `revenue`/`net_receivable`/`net_payable` field is
added to `AgentState`** (ADR-0006 §7). v1 emits no `data.totals`, so the
supervisor's financial totals are unaffected by a kitchen-revenue run.

## Contracts emitted

- [`RevenueData`](contracts.md) — `data.revenue`; `by_segment` groups by meal
  period (Breakfast/Half Board); `by_invoice` is `[]` (no invoices in this
  flow).
- [`CollectionData`](contracts.md) — `data.collections`; every payment
  `match_status="unmatched"` (v1 has no invoice list).
- [`CalculationResult`](contracts.md) — `data.nets`; `calculation_type="reconcile"`,
  `totals` carry the five net keys, `line_items[]` carry the per-figure +
  per-category breakdown.
- [`ValidationResult`](contracts.md) — emitted twice: the full report
  (`data.validation_report`) and the exception-scoped report
  (`data.exception_report`). No `ExceptionReport` schema (§15 reuse — ADR-0006
  §5).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; totals `"0.00"` (no
  money moved).
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`.

## Deterministic rules (v1)

- **Role classification:** filename keyword (`menu`/`daily`/`check`|`payment`/
  `marriott`|`backup`) > header-content sniff > `unknown` (skipped).
- **Required columns:** per role (above). A present role missing any → hard
  `AR_VALIDATION`. A missing role → warning + that calc `0.00`.
- **Revenue:** `amount` per row = explicit amount column > qty × rate, 2dp
  (`Decimal`, `ROUND_HALF_UP`); `total` = Σ; `by_segment` grouped by the
  meal-period column; `period` = min/max date. Menu Sales authoritative, Daily
  Sales cross-check (divergence >0.01 → warning).
- **Collections:** `total_collected` = Σ; `method` mapped to the enum
  (check/cheque → `bank_transfer`); `posted_at` ISO-8601 UTC (date-only →
  midnight UTC); `match_status="unmatched"`.
- **Expenses:** signed 2dp amounts; `by_category` via `_norm_token`.
- **Nets:** `net_receivable = revenue − collections`; `net_payable = total
  expenses`; all signed 2dp.
- **No credentials, no external calls** (§16) — the four sheets carry the
  facts (ADR-0006 §3).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract returns `AR_NOT_IMPLEMENTED`. So the orchestrator uses **inline
hand-rolled validators** for the per-role rows (`_validate_role_row`), and the
report builders (`_build_validation_report`, `_classify_exceptions`) —
mirroring the File Intake / Intercompany inline validators (ADR-0004/ADR-0005).
Wiring `ValidationEngineComponent` for `RevenueData`/`CollectionData`/
`CalculationResult`/`WorkflowState` is a documented build-phase step
(ADR-0006 §11). The canonical schema files remain the source of truth and the
self-test keeps the validators in sync (hand-rolled stdlib, no `jsonschema`
dep).

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue.py \
                     docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue_selftest.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_kitchen_revenue.json'))"
bash scripts/kitchen-revenue.selftest.sh     # 199 pure-function checks
make validate                                # compose config unaffected
```