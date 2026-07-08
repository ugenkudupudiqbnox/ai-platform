# Foodics Processing Flow (`ar_foodics_processing`)

The **Foodics Processing Flow** is the 6th AR subflow (architecture §4 row 6;
[ADR-0007](adr/adr-0007-foodics-processing-flow.md)). Cosmic receives Foodics
**Order**, **Order Items**, and **Order Payments** data — either as three
uploaded export files (Excel/CSV) or via the Foodics API — and must turn it into
a **consolidated dataset** (a per-order join of items + payments), a **pivot**
(by item and by payment type), a **payment-type** breakdown, a
**discount-adjusted** invoice set, a **Zoho Books upload format**, a draft
`InvoiceData` per order, and a **Validation Report** + **Exception Report**.
This flow reads the three sources (files now, API via a build-phase seam),
validates rows, builds the consolidated/pivot/sheet3 datasets (all JSON — no
`.xlsx` in v1), determines payment type, applies discount rules, generates the
Zoho upload format + draft `InvoiceData` per order + reports, and returns
structured JSON — with logging (§12), retries (§10), and **checkpoints after
every calculation** (§11 — continuing ADR-0006's stricter pattern). It is the
**single stateful orchestrator** for Foodics order processing, mirroring the
supervisor, the File Intake Flow, the Intercompany Sales Flow, and the Cosmic
Kitchen Revenue Flow: its responsibilities map to LangGraph nodes inside one
`lfx` component, `FoodicsProcessingFlowComponent`.

**v1 is compute + draft only**: it produces the invoice JSON + Zoho upload rows
for review; it does **not** post, so no money moves and no ledger entry posts
this turn (§1 north star preserved). The flow is registered at tier `approval`
(its intent is invoice production), but the §19 gate is **dormant in v1**: there
is no `ApprovalGate`, no idempotency key, no `pending_approval`, and it is
**not** in `FINANCIAL_INTENTS` (mirrors the Intercompany Sales Flow, ADR-0005).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§4/§8/§9/§10/§11/§12/§14/§15/§16/§17/§19, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0007](adr/adr-0007-foodics-processing-flow.md),
[ADR-0006](adr/adr-0006-kitchen-revenue-flow.md),
[ADR-0005](adr/adr-0005-intercompany-sales-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/foodics_processing.py`](../../docker/langflow-extensions/ar_common/components/ar_common/foodics_processing.py)
  — `FoodicsProcessingFlowComponent` (internal LangGraph
  `StateGraph[FoodicsProcessingState]` + `InMemorySaver`).
- **Reused generic parts (`cosmic_common`, §15):**
  [`excel_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/excel_reader.py),
  [`csv_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/csv_reader.py)
  (lazy-imported inside the `read` node's files path).
- **Foodics API seam (scaffold):**
  [`foodics_ar.py`](../../docker/langflow-extensions/ar_tools/components/ar_tools/foodics_ar.py)
  — `FoodicsARTool.fetch_foodics_data` (lazy-imported in the API path;
  build-phase).
- **Flow JSON:** [`flows/ar_foodics_processing.json`](../flows/ar_foodics_processing.json).
- **Self-test:** [`foodics_processing_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/foodics_processing_selftest.py)
  (204 stdlib-only pure-function checks) via `scripts/foodics-processing.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept inputs | `ingest` | Bind `trace_id` (minted), `flow_id="ar_foodics_processing"`, `tenant="cosmic-vikings"`, `created_at`/`updated_at`; carry the `files` refs + `source_mode` in **context** (not state — §8). status="created". |
| Read Order / Order Items / Order Payments (files OR API) | `read` | `source_mode` resolves the source: `auto` = files when uploaded else API; `files`/`api` force it. **Files path:** `_expand_files` → per-file `_normalize_file`+`_resolve_storage_path`+`detect_type` (must be `excel`/`csv`), `_make_reader`+`_read_with_retry` (§10: 3 attempts, exp 1s·2^n ±25% jitter ≤30s) — mirrors the Kitchen Revenue Flow's multi-file loop; each file's rows → `_classify_input(name, rows)` → role bucket in `inputs`. **API path:** `_make_foodics_fetcher` lazy-imports `FoodicsARTool`, calls `fetch_foodics_data` with operations `list_orders`/`list_order_items`/`list_order_payments` inside the §10 retry loop. Scaffold tool returns `AR_NOT_IMPLEMENTED` → flow fails safe (`AR_UPSTREAM`: "Foodics API fetch is build-phase"). Unknown type/no usable file → `AR_UNCERTAIN` (§4). Zero recognized roles → `AR_UNCERTAIN`. status="read". Router `_after_read`: `{failed:respond, read:validate}`. |
| Validate rows | `validate` | Inline hand-rolled per-role validator with role-specific required columns (`order`: order_ref+posted_at; `order_items`: order_ref+item_ref+qty+unit_price; `order_payments`: payment_ref+order_ref+amount+method+posted_at). Per-row checks: amounts parseable & >0, qty>0, unit_price>0, dates ISO, methods in enum (check→`bank_transfer`). A **required column entirely missing for a present role** → hard `AR_VALIDATION`. Else the full `ValidationResult` is built (`contract_name="FoodicsInputs"`). status="validated". Router `_after_validate`: `{failed:respond, validated:classify_exceptions}`. |
| Generate Exception Report | `classify_exceptions` | Split rows into valid vs exception; build the Exception Report = a `ValidationResult` scoped to failures (each error carries a `rule_id` like `fp.qty_positive`/`fp.amount_positive`/`fp.method_enum`/`fp.order_ref_required`). A **missing role** is a validation warning (not a hard fail); downstream nodes emit `0.00`/empty for it. All-rows-fail → `AR_VALIDATION`. status="classified". Router `_after_classify`: `{failed:respond, classified:build_consolidated}`. |
| Populate Consolidated Workbook (JSON) | `build_consolidated` | Join order ↔ order_items by `order_ref`; attach payment rows per order. Emit `data.consolidated = {orders:[{order_ref, customer_ref, posted_at, currency, items:[…], payments:[…], gross_total, payment_total}], count, contract_version}`. Pure compute, 2dp. **Records a checkpoint** (`_audit_ref(trace_id,"consolidated")`). status="consolidated". |
| Refresh Pivot (JSON) | `refresh_pivot` | Aggregate the consolidated dataset by item and by payment type: `data.pivot = {by_item:[{item_ref, qty, amount, count}], by_payment_type:[{payment_type, amount, count}], totals:{gross, collected}, contract_version}`. **Records a checkpoint** (`"pivot"`). status="pivot". |
| Determine Payment Type | `determine_payment_type` | Map each `order_payments` row's raw mode to the `CollectionData.method` enum via `METHOD_SYNONYMS` (cash/card/bank_transfer/online/wallet/other; unknown → `other`); build `data.payment_type_summary = {by_method:[{method, amount, count}], total_collected, contract_version}`. **Records a checkpoint** (`"payment_type"`). status="payment_type". |
| Apply Discount Rules | `apply_discounts` | **Both sources, precedence in-file > baked-in > `0.00`.** Per `order_items` row: in-file `discount_amount` (flat) > `discount_pct` (% of gross) > `discount` (amount, else % of gross) > first matching `DISCOUNT_RULES` baked-in rule (`{matcher:{item_ref|category|payment_type}, kind:"pct"|"amount", value}`) > `0.00`. Discount capped at gross. Compute per-line discount 2dp + running `discounts_total`; stash adjusted line amounts in `adjusted_lines`. **Records a checkpoint** (`"discounts"`). status="discounts". |
| Populate Sheet3 (JSON) | `populate_sheet3` | A third report dataset (per-order net summary: order_ref, gross, discounts, tax=`0.00`, net, payment_type). Emit `data.sheet3 = {rows:[…], count, contract_version}`. **Records a checkpoint** (`"sheet3"`). status="sheet3". |
| Generate Zoho Upload Format | `build_zoho_upload` | Transform the consolidated + discounted data into Zoho Books invoice-import rows: `data.zoho_upload = {format:"zoho-books-invoice-import", rows:[{customer_ref, invoice_number, date, item_details:[{item_ref, qty, rate, amount, discount}], discount_total, total, currency}], count, contract_version}`. `customer_ref` is the Zoho customer id (no PII — §16). **Records a checkpoint** (`"zoho_upload"`). status="zoho". |
| Generate Invoice JSON | `build_invoice` | Build **one `InvoiceData` per `order_ref`** (mirrors intercompany's per-buyer grouping). Each: discount-adjusted line amounts, `subtotal`=Σ gross, `discounts`=order's discount share 2dp, `total`=`subtotal`−`discounts`, `balance_due`=`total`, `issue_date`=order `posted_at`, `due_date`=issue+`NET_TERMS_DAYS`(30), `currency` (column else `SAR`), `status="draft"`, deterministic `invoice_id`/`invoice_number` via `uuid5(NAMESPACE_URL,"foodics:{trace_id}:{order_ref}")` shaped `FP-{order_ref}-{8hex}`. Inline `_validate_invoice` guard. **Records a checkpoint** (`"invoice"`). status="invoice". Router `_after_invoice`: `{failed:respond, invoice:build_state}`. |
| Update Workflow State | `build_state` | Build a `WorkflowState` snapshot: `status="completed"`, `intent="ar_foodics_processing"`, `matched_amount`/`outstanding_balance`/`posted_total="0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (gate dormant), `audit_refs`, `tool_call_ref=f"{trace_id}:ar_foodics_processing:0"`, `contract_version`. Immutable (§8). status="completed". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"foodics_processing")`; reflect `audit_refs`+`checkpoints` into the `WorkflowState` snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). |
| Return structured JSON | `respond` | `_finalize_envelope` builds `data={invoices, consolidated, pivot, payment_type_summary, sheet3, zoho_upload, validation_report, exception_report, workflow_state, audit_refs, checkpoints, discounts_total, document_count, invoice_count, source_mode, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). |
| Logging | `run()` boundary | §12 structured `key=value` via `logging.getLogger("ar.foodics_processing")`: `trace_id`/`flow_id`/`tenant`/`ar_entity="foodics_processing"`/`event`/`outcome`/`code`/`source_mode`; no PII/secrets (customer refs are ids — §16). |
| Retries | `read` wrapper | §10 loop (above) on **both** the file reads and the Foodics API calls; the only retry surface in v1 (no other external lookups). |
| Checkpoints after every calculation | `build_consolidated`/`refresh_pivot`/`determine_payment_type`/`apply_discounts`/`populate_sheet3`/`build_zoho_upload`/`build_invoice` + `checkpoint` | Continues ADR-0006's stricter pattern: each calc/transform node records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (`{consolidated, pivot, payment_type, discounts, sheet3, zoho_upload, invoice}`), persisted by `InMemorySaver` at each super-step (§11 — ADR-0007 §10). |

Graph edges: `START → ingest → read → validate → classify_exceptions →
build_consolidated → refresh_pivot → determine_payment_type → apply_discounts
→ populate_sheet3 → build_zoho_upload → build_invoice → build_state →
checkpoint → respond → END`, with conditional short-circuits to `respond` on
any `failed` status (`_after_read`/`_after_validate`/`_after_classify`/
`_after_invoice` return `state.status` against status-keyed path maps —
ADR-0003 §9).

## Canvas wiring (3 nodes / 3 edges)

`ar_foodics_processing.json` wires (modeled on `ar_kitchen_revenue.json`):

- `ChatInput.message → FoodicsProcessingFlowComponent.user_input`
- `ChatInput.message → FoodicsProcessingFlowComponent.files` (inputTypes
  `["Data","Message"]`, type `source`)
- `FoodicsProcessingFlowComponent.foodics_processing_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Kitchen Revenue
canvas; the orchestrator node's full source is embedded as
`template.code.value` (LangFlow runs the embedded copy — it must stay in sync
with the on-disk `foodics_processing.py`). There is no standalone `File` node —
files ride on the ChatInput `.files` handle into the orchestrator's `files`
HandleInput (ADR-0003 §8).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, optional, `tool_mode` — carries
  intent keywords), `files` (HandleInput, `is_list`, `input_types=["Data",
  "Message"]` — the three Foodics export refs), `source_mode` (DropdownInput,
  options `["auto","files","api"]`, value `"auto"` — forces the input source),
  `model_name` (MessageTextInput, value `"glm-5.2:cloud"` — documented LLM
  hook; deterministic v1 ignores it).
- **Output:** `foodics_processing_output` (Message) — the §14 envelope JSON.

## The dual-source read (files + API seam)

The `read` node is source-agnostic. `source_mode` resolves the source: `auto`
uses files when uploaded else API; `files` requires uploaded exports; `api`
fetches via `FoodicsARTool`.

**Files path (v1):** the three Foodics exports are read via the `cosmic_common`
Excel/CSV readers and classified by role — filename keyword first, header-
content fallback. This is deterministic, in-file, and needs no credentials (§16).

**API path (now wired, gated on credentials):** `_make_foodics_fetcher()`
returns the real transport — `ar_common.foodics_transport.RealFoodics` (OAuth 2.0
client-id/secret/refresh → 14-day Bearer + `X-Business`, `list_orders` /
`list_order_items` / `list_order_payments`, Laravel pagination, transient raises
so the §10 loop owns retry) — when the Foodics Secret Global Variables are
present (resolved by name via `vendor_secrets.read_secret`, threaded through the
`set_foodics_creds` seam set by `FoodicsProcessingFlowComponent.run`). The prior
broken cross-bundle `from components.ar_tools.foodics_ar import FoodicsARTool`
import (never on `sys.path` → `None` → `AR_NOT_IMPLEMENTED`) was dropped. Absent
creds → `_make_foodics_fetcher()` returns `None` → the API path **fails safe**
(`AR_UPSTREAM` / `AR_NOT_IMPLEMENTED`) and `auto` mode falls back to files. See
[`environment.md`](environment.md) for the credential setup (Foodics is OAuth, not
the obsolete static `FOODICS_API_TOKEN`), and `contracts.md` `V1-STUB` (resolved
for Foodics).

## The three-role classification

Each uploaded export → one role by filename keyword (header fallback):

| Export (example filename) | Role | Required columns | Feeds |
|---|---|---|---|
| Orders (`order…`, not item/payment) | `order` | order_ref + posted_at | Consolidated (order header) |
| Order Items (`item…`) | `order_items` | order_ref + item_ref + qty + unit_price | Consolidated items, discounts, invoice lines |
| Order Payments (`payment…`) | `order_payments` | payment_ref + order_ref + amount + method + posted_at | Payment type, pivot, consolidated payments |

A sheet that matches no keyword and no header signature is skipped (not a
failure). **Zero recognized roles** → `AR_UNCERTAIN` (§4). A **missing role**
is a warning, not a hard fail — downstream nodes emit `0.00`/empty for it.

## The JSON-dataset workbook / pivot / Sheet3 design

v1 is read-Excel-in, JSON-out (no flow has ever written an `.xlsx`).
`build_consolidated` / `refresh_pivot` / `populate_sheet3` are **compute nodes**
emitting structured JSON sections (`data.consolidated`, `data.pivot`,
`data.sheet3`) in the §14 envelope. openpyxl is in the image but only used
read-only; writing a real `.xlsx` + pivot is a documented build-phase step
(ADR-0007 §4).

## The discount-rules precedence

`apply_discounts` reads in-file discount columns **and** a baked-in
`DISCOUNT_RULES` config. **Precedence:** an explicit in-file discount column
wins; else the first matching baked-in rule; else `0.00`. The per-line discount
is capped at the line gross; the per-order share rolls into
`InvoiceData.discounts` (2dp) and reduces line totals.

| Source | Column(s) | Effect |
|---|---|---|
| In-file (highest) | `discount_amount` | flat 2dp amount off gross |
| In-file | `discount_pct` | percentage of gross |
| In-file | `discount` | amount (or % of gross if > gross) |
| Baked-in `DISCOUNT_RULES` | `{matcher, kind, value}` | `pct` (% of gross) or `amount` (flat) |
| None (lowest) | — | `0.00` |

## The one-invoice-per-order design

`build_invoice` emits **one `InvoiceData` per `order_ref`** (mirrors
intercompany's per-buyer grouping). One POS order is the natural unit for Zoho
import and for downstream approval/posting. Deterministic `uuid5` ids mean the
same trace + order always yields the same invoice ids (§4.3).

## The Zoho upload format

`build_zoho_upload` emits a flow-specific JSON representation of the Zoho Books
invoice-import format (`data.zoho_upload`). `customer_ref` is the Zoho customer
id (no PII — §16). The canonical Zoho import template (target-org custom
fields) is build-phase; this flow's JSON is the source of truth that a
build-phase mapper renders to the exact template (ADR-0007 §7).

## The supervisor merge (no `AgentState` change)

Invoices / consolidated / pivot / sheet3 / zoho_upload / payment_type_summary
are not under `data.totals{matched,outstanding,posted}`, so the supervisor's
`_node_invoke` does not recognise them as financial totals. The flow surfaces to
the supervisor only via `subflows_invoked` + `audit_refs`; the datasets stay in
the envelope `data`. **No field is added to `AgentState`** (ADR-0007 §8). v1
emits no `data.totals`, so the supervisor's financial totals are unaffected by a
Foodics run.

## Contracts emitted

- [`InvoiceData`](contracts.md) — `data.invoices` (one per order); line items
  carry discount-adjusted `amount`; `discounts` 2dp; `status="draft"`.
- [`ValidationResult`](contracts.md) — emitted twice: the full report
  (`data.validation_report`, `contract_name="FoodicsInputs"`) and the
  exception-scoped report (`data.exception_report`). No `ExceptionReport`
  schema (§15 reuse).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; totals `"0.00"` (no
  money moved); `intent="ar_foodics_processing"`.
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`.
- **Flow-specific JSON** (no schema — ADR-0007 §7): `data.consolidated`,
  `data.pivot`, `data.sheet3`, `data.zoho_upload`, `data.payment_type_summary`.

## Deterministic rules (v1)

- **Role classification:** filename keyword (`item`→order_items,
  `payment`→order_payments, `order`→order) > header-content sniff > `unknown`
  (skipped). Filename keyword wins over the header fallback.
- **Required columns:** per role (above). A present role missing any → hard
  `AR_VALIDATION`. A missing role → warning + that node `0.00`/empty.
- **Discounts:** in-file column > baked-in rule > `0.00`; capped at gross;
  `discounts_total` 2dp; `Decimal`/`ROUND_HALF_UP`.
- **Invoice ids:** `uuid5(NAMESPACE_URL, "foodics:{trace_id}:{order_ref}")`;
  `invoice_number` shaped `FP-{order_ref}-{8hex}`.
- **Dates:** `issue_date` = order `posted_at`; `due_date` = issue + 30
  (`NET_TERMS_DAYS`); UTC arithmetic (no wall-clock side effects — §4.3).
- **No credentials, no external calls** in the files path (§16) — the three
  exports carry the facts. The API path fails safe until `FoodicsARTool` +
  credentials are wired (ADR-0007 §3).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract returns `AR_NOT_IMPLEMENTED`. So the orchestrator uses **inline
hand-rolled validators** (`_validate_role_rows` per role + `_validate_invoice`
for `InvoiceData`), mirroring the File Intake / Intercompany / Kitchen flows.
Wiring these into `ValidationEngineComponent` is build-phase (ADR-0007 §12).

## The compute + draft v1 / build-phase checklist

v1 is **compute + draft only** — no §19 gate, no idempotency key, no
`pending_approval`, not in `FINANCIAL_INTENTS`, no Zoho POST. Build-phase (not
done here):

1. **Posting upgrade** — add the §19 gate + idempotency key +
   checkpoint-before-POST + audit-with-`approval_ref` and add the posting flow
   to `FINANCIAL_INTENTS`.
2. **Foodics API** — implement `FoodicsARTool`'s `list_orders`/
   `list_order_items`/`list_order_payments` HTTP + bearer token + pagination
   (mirror `FoodicsAPTool`); wire the `FOODICS_API_TOKEN` Secret Global Variable
   (§16); the API path then activates.
3. **Zoho import template** — author the canonical Zoho Books invoice-import
   template + a mapper from `data.zoho_upload` to it.
4. **`.xlsx` workbook + pivot** — add an openpyxl writer + pivot construction
   node that materializes `data.consolidated`/`pivot`/`sheet3` into an `.xlsx`
   (no Dockerfile change — openpyxl is installed).
5. **Import the nine subflows first** (incl. `ar_foodics_processing.json`),
   then `supervisor.json`; open the supervisor flow so the 6th `RunFlow`
   resolves `flow_id_selected`; `docker compose restart langflow`.
6. **Wire `ValidationEngineComponent`** for `InvoiceData` + the flow-specific
   datasets (replace the inline validators).
7. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; this flow follows for free).
8. **Move `DISCOUNT_RULES`** to a Global Variable (§17) /
   `ConfigurationLoaderComponent` so operators can tune it without a code change.