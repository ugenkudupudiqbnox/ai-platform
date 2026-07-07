# Calculation Flow (`ar_calculation`)

The **Calculation Flow** is the 14th AR subflow (architecture §4 row 14;
[ADR-0008](adr/adr-0008-calculation-flow.md)). It takes a **Validated JSON**
payload (aggregated facts + parameters — the output of the planned P10
Validation Flow, or any caller that produces the payload contract) and computes
nine figures — **Revenue, Discount, VAT, Municipality Tax, Royalty,
Collections, Expenses, Net Receivable, Net Payable** — then returns structured
JSON. The binding constraint is **"No hardcoded business rules. All
calculations must use Business Rule Engine."** Every figure is computed by the
`BusinessRuleEngineComponent` from a declarative ruleset; the flow component
itself contains **zero formulas**. It returns a `reconcile`-type
`CalculationResult` + a Validation Report + an Exception Report — with logging
(§12) and **checkpoints after every calculation** (§11 — continuing
ADR-0006/0007's stricter pattern). It is the **single stateful orchestrator**
for AR calculation, mirroring the supervisor, the File Intake Flow, the
Intercompany Sales Flow, the Cosmic Kitchen Revenue Flow, and the Foodics
Processing Flow: its responsibilities map to LangGraph nodes inside one `lfx`
component, `CalculationFlowComponent`.

**v1 is read-only compute + report**: it produces the `CalculationResult` for
review; it does **not** post, so no money moves and no ledger entry posts this
turn (§1 north star preserved). The flow is registered at tier `read-only`, the
§19 gate is **dormant in v1**: there is no `ApprovalGate`, no idempotency key,
no `pending_approval`, and it is **not** in `FINANCIAL_INTENTS` (mirrors the
Kitchen Revenue Flow, ADR-0006).

> **§55 waiver:** the constitution marks "Tax filing, VAT/Saudi Zakat
> calculation, and statutory returns" out-of-scope. This flow computes
> VAT/Municipality Tax/Royalty as **invoice/reconciliation figures** (what to
> bill, what to remit), **not** a statutory filing. The waiver is recorded in
> the flow README and [ADR-0008](adr/adr-0008-calculation-flow.md) §2.

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§4/§8/§9/§11/§12/§14/§15/§17/§19/§55, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0008](adr/adr-0008-calculation-flow.md),
[ADR-0006](adr/adr-0006-kitchen-revenue-flow.md),
[ADR-0002](adr/adr-0002-reusable-component-library.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/calculation.py`](../../docker/langflow-extensions/ar_common/components/ar_common/calculation.py)
  — `CalculationFlowComponent` (internal LangGraph
  `StateGraph[CalculationState]` + `InMemorySaver`).
- **Calculator (reused `cosmic_common`, §15):**
  [`business_rule_engine.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/business_rule_engine.py)
  — `BusinessRuleEngineComponent` (implemented + extended; its pure
  `_evaluate_rules` is called directly by the flow's `evaluate_rules` node —
  ADR-0008 §4).
- **Flow JSON:** [`flows/ar_calculation.json`](../flows/ar_calculation.json).
- **Self-tests:**
  [`business_rule_engine_selftest.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/business_rule_engine_selftest.py)
  (79 stdlib-only pure-function checks) via
  `scripts/business-rule-engine.selftest.sh`, and
  [`calculation_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/calculation_selftest.py)
  (112 stdlib-only pure-function + end-to-end checks) via
  `scripts/calculation.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept inputs | `ingest` | Parse the validated-JSON payload from `user_input`; bind `trace_id` (minted), `flow_id="ar_calculation"`, `tenant="cosmic-vikings"`, `created_at`/`updated_at`; carry `rules` + `model_name` in **context** (not state — §8). Malformed JSON / non-object → `AR_VALIDATION`. status="created". Router `_after_ingest`: `{failed:respond, created:resolve_parameters}`. |
| Resolve parameters | `resolve_parameters` | Populate `parameters` from `payload.parameters` (v1; `$GV:` Global-Variable injection is build-phase). A **missing rate** → `"0.00"` + an `AR_VALIDATION_MISSING_RATE` warning (not a hard fail). status="resolved". Router `_after_resolve`: `{failed:respond, resolved:validate_payload}`. |
| Validate payload | `validate_payload` | Inline hand-rolled validator for the input contract: facts present (a `facts` dict is required — no facts → hard `AR_VALIDATION`), each fact parses as 2dp monetary (non-parseable → warning, not hard fail), `period.start`/`period.end` ISO dates, `currency` `^[A-Z]{3}$`. Builds the full `ValidationResult` (`contract_name="CalculationInputs"`). status="validated". Router `_after_validate`: `{failed:respond, validated:classify_exceptions}`. |
| Generate Exception Report | `classify_exceptions` | Build the Exception Report = a `ValidationResult` scoped to the failing facts (each warning carries a `rule_id` like `calc.fact_amount`/`calc.period_iso`/`calc.currency`). status="classified". Router `_after_classify`: `{failed:respond, classified:evaluate_rules}`. |
| **Evaluate rules** (CORE — zero formulas in the flow) | `evaluate_rules` | Build the engine payload `{facts, parameters, outputs:{}, _global_variables:{}}`; call the BRE pure `_evaluate_rules(rules, payload, strict=False)` directly. On `AR_VALIDATION` / `AR_RULE_FAILED` → `failed`. Extract `data.calculations` (the 9 figures). **Record checkpoint** `"rules"`. status="evaluated". Router `_after_evaluate`: `{failed:respond, evaluated:build_calculation_result}`. |
| Assemble CalculationResult | `build_calculation_result` | Build the `reconcile`-type `CalculationResult`: `totals` = the 9 signed-2dp keys, `line_items` = one per figure with `source_refs=[<rule_id>]`, `inputs_ref=trace_id`, `currency` (payload else `SAR`), `computed_at` (UTC), `contract_version`. **Record checkpoint** `"calculation_result"`. status="built". |
| Update Workflow State | `build_state` | `WorkflowState` snapshot: `status="completed"`, `intent="ar_calculation"`, `matched_amount`/`outstanding_balance`/`posted_total="0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (gate dormant), `audit_refs`, `tool_call_ref=f"{trace_id}:ar_calculation:0"`, `contract_version`. Immutable (§8). status="completed". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"ar_calculation")`; reflect `audit_refs`+`checkpoints` into the `WorkflowState` snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). |
| Return structured JSON | `respond` | `_finalize_envelope` builds `data={calculation_result, calculations, validation_report, exception_report, workflow_state, audit_refs, checkpoints, rule_count, fact_count, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). |
| Logging | `run()` boundary | §12 structured `key=value` via `self.log`: `event=calculation.run outcome=… trace_id=… flow_id=… ar_entity=calculation code=…`; failure boundary `code=AR_UNEXPECTED`. No PII/secrets (§16). |
| Never raises | `run()` boundary | §5/§9 — `run()` catches at the boundary and returns an `AR_UNEXPECTED` envelope; `evaluate()` (the BRE wrapper) likewise never raises. |
| Checkpoints after every calculation | `evaluate_rules` + `build_calculation_result` + `checkpoint` | Continues ADR-0006/0007's stricter pattern: each calc boundary records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (`{rules, calculation_result, ar_calculation}`), persisted by `InMemorySaver` at each super-step (§11 — ADR-0008 §12). |

Graph edges: `START → ingest → resolve_parameters → validate_payload →
classify_exceptions → evaluate_rules → build_calculation_result → build_state →
checkpoint → respond → END`, with conditional short-circuits to `respond` on
any `failed` status (`_after_ingest`/`_after_resolve`/`_after_validate`/
`_after_classify`/`_after_evaluate` return `state.status` against status-keyed
path maps — ADR-0003 §9).

## The Business Rule Engine rule schema

The `BusinessRuleEngineComponent` evaluates a declarative ruleset. Each rule has
a `rule_id`, a `kind`, and an `output` (for calcs) or `field`/`op`/`value` (for
asserts). Kinds:

| Kind | Shape | Behavior |
|---|---|---|
| `assert` | `{rule_id, kind:"assert", field, op, value}` | Boolean check; ops `== != < <= > >= in not_in`. Evaluated **after** all calcs (can assert on `outputs.*`). → `{rule_id, passed, message}`. |
| `sum` | `{rule_id, kind:"sum", inputs:[<paths>], output}` | Σ of the referenced facts; a missing fact → `0.00`. 2dp `ROUND_HALF_UP`. |
| `pct_of` | `{rule_id, kind:"pct_of", base:<path>, rate:<literal\|path\|$GV:NAME>, output}` | `(base * rate)` 2dp `ROUND_HALF_UP`. Rates are decimals (`"0.15"` = 15%). |
| `amount` | `{rule_id, kind:"amount", source:<path>, output}` | Copies a fact to an output (2dp). |
| `formula` | `{rule_id, kind:"formula", expr:"...", output}` | Restricted recursive-descent parser over `+ - * ( )` + unary `-` + decimal literals + named operands (outputs → facts → parameters). **NO `/`, NO `eval`.** Unknown identifier → `AR_VALIDATION`. |

**Paths:** `facts.<n>`, `parameters.<n>`, `outputs.<n>`, or a top-level key.
`$GV:NAME` resolves via `payload["_global_variables"][NAME]` (forward-compatible
— populated at build-phase from LangFlow Global Variables).

**Dependency ordering:** Kahn topological sort over `outputs.*` references;
**cycle / duplicate `output` / unknown-output reference → `AR_VALIDATION`**
(never silent). `assert` rules evaluate after all calcs.

**Rate resolution:** literal decimal | dotted path | `$GV:NAME`. A missing rate
at the engine level → `AR_VALIDATION` (never silent `0.00`); the flow's
`resolve_parameters` node defaults a missing payload rate to `"0.00"` + a
warning instead (so a partial payload still produces reviewable zeroed
figures).

**Engine envelope:** ok → `{"status":"ok","code":"AR_OK","data":{"results":[…],
"calculations":{<output>:<signed 2dp>}}}`; strict + failed assert →
`{"status":"error","code":"AR_RULE_FAILED",…,"error":{"failed_rule_ids":[…]}}`;
malformed rules / cycle / bad expr / unresolvable rate →
`{"status":"error","code":"AR_VALIDATION",…}`.

## The seed ruleset

The flow ships a default seed ruleset as its `rules` input (declarative JSON,
overridable). It computes the nine figures:

```json
[
  {"rule_id":"R_REVENUE","kind":"sum","inputs":["facts.gross_sales","facts.returns","facts.allowances"],"output":"revenue"},
  {"rule_id":"R_DISCOUNT","kind":"pct_of","base":"facts.gross_sales","rate":"parameters.discount_rate","output":"discount"},
  {"rule_id":"R_VAT","kind":"pct_of","base":"outputs.revenue","rate":"parameters.vat_rate","output":"vat"},
  {"rule_id":"R_MUNICIPALITY","kind":"pct_of","base":"outputs.revenue","rate":"parameters.municipality_rate","output":"municipality_tax"},
  {"rule_id":"R_ROYALTY","kind":"pct_of","base":"outputs.revenue","rate":"parameters.royalty_rate","output":"royalty"},
  {"rule_id":"R_COLLECTIONS","kind":"sum","inputs":["facts.cash_collected","facts.card_collected","facts.bank_collected","facts.online_collected","facts.wallet_collected"],"output":"collections"},
  {"rule_id":"R_EXPENSES","kind":"sum","inputs":["facts.expense_food","facts.expense_labor","facts.expense_overhead"],"output":"expenses"},
  {"rule_id":"R_NET_RECEIVABLE","kind":"formula","expr":"revenue - discount + vat + municipality_tax - collections","output":"net_receivable"},
  {"rule_id":"R_NET_PAYABLE","kind":"formula","expr":"expenses + royalty + municipality_tax","output":"net_payable"}
]
```

> `municipality_tax` appears on both sides: billed to the customer → receivable;
> owed to the municipality → payable. The operator can adjust these formulas
> via the `rules` input without a code change (§17).

## Canvas wiring (3 nodes / 2 edges)

`ar_calculation.json` wires (modeled on `ar_kitchen_revenue.json`):

- `ChatInput.message → CalculationFlowComponent.user_input`
- `CalculationFlowComponent.calculation_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Kitchen Revenue canvas;
the orchestrator node's full source is embedded as `template.code.value`
(LangFlow runs the embedded copy — it must stay in sync with the on-disk
`calculation.py`). There is **no `files` edge** — the first subflow without one
(ADR-0008 §11).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, required, `tool_mode` — carries
  the validated JSON, the PRIMARY input), `rules` (MultilineInput, default =
  the seed ruleset JSON, overridable — §17), `model_name` (MessageTextInput,
  value `"glm-5.2:cloud"` — documented LLM hook; deterministic v1 ignores it).
  **No `files` HandleInput.**
- **Output:** `calculation_output` (Message) — the §14 envelope JSON.

## The aggregated-facts + parameters payload contract

The validated-JSON payload the flow consumes:

```json
{
  "trace_id": "…",
  "tenant": "cosmic-vikings",
  "period": {"start": "2026-01-01", "end": "2026-01-31"},
  "currency": "SAR",
  "facts": {
    "gross_sales": "10000.00", "returns": "-200.00", "allowances": "-100.00",
    "cash_collected": "3000.00", "card_collected": "2000.00",
    "bank_collected": "0", "online_collected": "0", "wallet_collected": "0",
    "expense_food": "1500.00", "expense_labor": "2000.00", "expense_overhead": "500.00"
  },
  "parameters": {
    "discount_rate": "0.05", "vat_rate": "0.15",
    "municipality_rate": "0.14", "royalty_rate": "0.02"
  }
}
```

A **missing** `facts` dict is a hard `AR_VALIDATION`; a missing rate in
`parameters` defaults to `"0.00"` + a warning. This is the contract the planned
P10 Validation Flow will emit.

## The supervisor merge (no `AgentState` change)

The nine figures are not under `data.totals{matched,outstanding,posted}`, so the
supervisor's `_node_invoke` does not recognise them as financial totals. The
flow surfaces to the supervisor only via `subflows_invoked` + `audit_refs`; the
`CalculationResult` stays in the envelope `data`. **No field is added to
`AgentState`** (ADR-0008 §10, mirrors ADR-0006 §7 / ADR-0007 §8). v1 emits no
`data.totals`, so the supervisor's financial totals are unaffected by a
calculation run.

## Contracts emitted

- [`CalculationResult`](contracts.md) — `data.calculation_result`,
  `calculation_type="reconcile"`, the 9 signed-2dp `totals` keys, one
  `line_item` per figure with `source_refs=[<rule_id>]`. **No schema change**
  (§15 reuse — the `totals` `additionalProperties` already allows any signed-2dp
  key).
- [`ValidationResult`](contracts.md) — emitted twice: the full report
  (`data.validation_report`, `contract_name="CalculationInputs"`) and the
  exception-scoped report (`data.exception_report`). No `ExceptionReport`
  schema (§15 reuse).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; totals `"0.00"` (no
  money moved); `intent="ar_calculation"`.
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`.

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today. So the
orchestrator uses an **inline hand-rolled validator** for the
`CalculationInputs` contract (facts present + 2dp parseable, period ISO,
currency `^[A-Z]{3}$`), mirroring the File Intake / Intercompany / Kitchen /
Foodics flows. Wiring this into `ValidationEngineComponent` is build-phase
(ADR-0008 §13).

## The read-only v1 / build-phase checklist

v1 is **read-only compute + report only** — no §19 gate, no idempotency key, no
`pending_approval`, not in `FINANCIAL_INTENTS`, no posting. Build-phase (not
done here):

1. **Global-Variable `$GV:` rate injection** — wire the four plain LangFlow
   Global Variables `VAT_RATE`, `MUNICIPALITY_TAX_RATE`, `ROYALTY_RATE`,
   `DISCOUNT_RATE` (§17, non-secret — see [environment.md](environment.md)) and
   inject them into the payload `parameters` at build time so rates flow from
   GV → rules without a per-call payload edit.
2. **P10 Validation Flow upstream** — build the P10 Validation Flow that emits
   this payload contract; this flow defines the contract but does not build
   P10.
3. **Wire `ValidationEngineComponent`** for `CalculationInputs` (replace the
   inline validator).
4. **Posting upgrade** — if a posting target exists, add the §19 gate +
   idempotency key + checkpoint-before-POST + audit-with-`approval_ref` and add
   the posting flow to `FINANCIAL_INTENTS` (this flow stays `read-only`).
5. **Statutory filing** — a separate VAT/Zakat returns flow (still §55
   out-of-scope here).
6. **Import the fifteen subflows first** (incl. `ar_calculation.json`), then
   `supervisor.json`; open the supervisor flow so the 14th `RunFlow` resolves
   `flow_id_selected`; `docker compose restart langflow`.
7. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; this flow follows for free).