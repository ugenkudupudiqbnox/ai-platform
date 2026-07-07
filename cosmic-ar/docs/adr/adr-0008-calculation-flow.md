# ADR 0008 — Calculation Flow: 14th subflow, validated JSON → Revenue / Discount / VAT / Municipality Tax / Royalty / Collections / Expenses / Net Receivable / Net Payable via the Business Rule Engine (read-only compute + report)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0006](adr-0006-kitchen-revenue-flow.md) / [0007](adr-0007-foodics-processing-flow.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§4/§8/§9/§10/§11/§12/§14/§15/§17/§19/§55,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [calculation](../calculation.md), [supervisor](../supervisor.md),
  [kitchen revenue](../kitchen-revenue.md), [foodics processing](../foodics-processing.md)

## Context

ADR-0007 added the Foodics Processing Flow as the 13th AR subflow. None of the
thirteen performs **general financial calculation driven by externalized
business rules**. The planned AR pipeline is **P10 Validation Flow → P11
Calculation Flow**: P10 (not yet built) validates raw data and emits a
"Validated JSON" payload; **P11 (this ADR) takes that Validated JSON and computes
nine figures — Revenue, Discount, VAT, Municipality Tax, Royalty, Collections,
Expenses, Net Receivable, Net Payable — then returns structured JSON with
logging and checkpoints.**

The binding constraint (from `prompts/P11_calculation_flow.md`, verbatim the
request): **"No hardcoded business rules. All calculations must use Business
Rule Engine."** This is a sharp departure from the existing
`ar_kitchen_revenue` flow, which hardcodes every formula in Python. To satisfy
it, this task **implements the scaffold `BusinessRuleEngineComponent`**
(`cosmic_common` — previously returned `AR_NOT_IMPLEMENTED`, no callers) and
**extends its rule schema with calculation rule kinds** (`sum` / `pct_of` /
`amount` / `formula`). The new `CalculationFlowComponent` delegates all nine
calculations to that engine; the flow component itself contains **zero
formulas**.

This ADR records the thirteen decisions made when the **Calculation Flow**
(`ar_calculation`) was added as the 14th AR subflow (the implemented +
extended `BusinessRuleEngineComponent`, the new
`CalculationFlowComponent` orchestrator, the wired `ar_calculation.json`
canvas, the supervisor wiring, and the two offline self-tests).

## Decisions

### 1. A new 14th subflow, amending architecture §4's "Thirteen reusable subflows"

`ar_calculation` is added to `SUBFLOWS` as the 14th entry, with a `TIER`
entry and an `INTENT_KEYWORDS` entry, and a 14th `RunFlow` node on the
supervisor canvas. Architecture §4's table grows a row 14 and its heading
becomes "Fourteen reusable LangFlow subflows"; §5's diagram grows a
`route → calculation → effect` branch.

- **Deviation:** architecture §4 said "Thirteen reusable LangFlow subflows"
  (after ADR-0007). This adds a 14th. Per the constitution's Authority note, a
  deviation from a binding standard is recorded as a written waiver in the
  flow's README **and a linked ADR** — this is that ADR.
- **Why:** general financial calculation driven by externalized rules is a
  distinct AR activity with its own input (validated aggregated facts +
  parameters), its own deterministic compute (the nine figures via the BRE),
  and its own output (a `reconcile`-type `CalculationResult` + Validation /
  Exception reports). It does not fit any of the existing thirteen subflows.

### 2. Waiver of §55 — figures only, NOT statutory filing

Constitution §55 lists "Tax filing, VAT/Saudi Zakat calculation, and statutory
returns" as out-of-scope. This flow computes **VAT / Municipality Tax / Royalty
as invoice/reconciliation figures** (the receivable billed to the customer and
the payable owed to the authority), **not as a statutory filing**. Per the
constitution's Authority note, this deviation from a binding standard is
recorded as a written waiver in the flow's README **and this linked ADR**.

- **Deviation:** §55 marks VAT/Zakat calculation out-of-scope. This flow
  computes VAT/Municipality Tax/Royalty **figures** for AR reconciliation and
  invoicing — not statutory returns filing.
- **Why:** the figures are needed on the AR invoice/reconciliation (what to
  bill, what to remit); computing them as figures is a prerequisite for any
  future filing flow, which remains out-of-scope. The figures are deterministic
  outputs of the rules, reviewable in the envelope.
- **Build-phase (still out-of-scope here):** a separate statutory-filing flow
  (Zakat/VAT returns) — this flow does not file anything.

### 3. Tier `read-only` — compute + report only, §19 gate dormant

The flow is registered at tier `read-only` (compute + report — no posting). The
§19 gate is **dormant in v1**: there is no `ApprovalGate`, no `interrupt()`, no
idempotency key, no `pending_approval`, no GL/invoice POST. It returns `AR_OK`
with the `CalculationResult` + reports. It is **not** added to
`FINANCIAL_INTENTS` (no financial POST → no financial-retry escalation). This
mirrors the Kitchen Revenue Flow (ADR-0006).

- **Deviation:** none — `read-only` is a registered §19 tier; the gate is
  simply off. The §1 north star (no money moves, no ledger entry posts) is
  preserved.
- **Why:** produce a reviewable calculation result now with zero posting risk;
  the §19 machinery is only justified once a posting target exists.

### 4. Implement + extend the Business Rule Engine as the single calculator; the flow has ZERO formulas

`BusinessRuleEngineComponent` (`cosmic_common`) is implemented and its rule
schema extended with four **calculation** rule kinds (`sum` / `pct_of` /
`amount` / `formula`) alongside the existing `assert` kind. The
`CalculationFlowComponent` orchestrator delegates **all nine calculations** to
that engine; the flow component itself contains **no formulas** — only payload
parsing, validation, and envelope assembly. This is the binding constraint from
`P11_calculation_flow.md`.

The engine's evaluation logic is factored into a **module-level pure function**
`_evaluate_rules(rules, payload, strict) -> dict` so it is testable without the
`lfx`/LangGraph runtime. The flow's `evaluate_rules` node calls that pure
function **directly** (`from components.cosmic_common.business_rule_engine
import _evaluate_rules`) — a deliberate testability choice that avoids double
`lfx`-stubbing in the self-tests. The `lfx` `BusinessRuleEngineComponent`
wrapper is retained (its `evaluate()` calls the same pure function) so the
engine is still usable as a canvas component / Flow-as-Tool.

- **Deviation:** none for §15 (the BRE is a reused `cosmic_common` component);
  the "flow calls the pure fn directly" choice is an internal testability
  decision recorded here.
- **Why:** the constraint is "no hardcoded business rules". Centralizing the
  formulas in the engine as declarative rules makes them overridable data, not
  code; the pure-fn factorization keeps them unit-testable.

### 5. Kahn topological sort over `outputs.*` references; cycle / duplicate / unknown output → `AR_VALIDATION`; asserts evaluate AFTER calcs

Calculation rules reference each other's outputs (`pct_of` on `outputs.revenue`,
`formula` over `outputs.*`). The engine topologically sorts the calc rules with
Kahn's algorithm over `outputs.<name>` references. **Cycle**, **duplicate
`output`**, and **unknown-output reference** all → `AR_VALIDATION` (never
silent). `assert` rules evaluate **after** all calcs so they can assert on
computed outputs. A strict run with a failed assert → `AR_RULE_FAILED`; a
non-strict run (the flow's v1 default) records the failed asserts in `data`
and still returns `AR_OK`.

- **Deviation:** none — §9 maps validation failures to `AR_VALIDATION`.
- **Why:** a mis-ordered, cyclic, or dangling ruleset must fail loud, not
  silently compute wrong figures.

### 6. `formula` = restricted recursive-descent parser (`+ - * ( )` + unary `-`); NO `/`, NO `eval`

The `formula` kind parses `expr` with a hand-written recursive-descent parser
over `+`, `-`, `*`, parentheses, unary `-`, decimal literals, and named
operands (resolved outputs → facts → parameters). It **rejects `/`** and
**never calls `eval`**. An unknown identifier → `AR_VALIDATION`. This keeps the
formula surface small, deterministic, and safe (no arbitrary code execution).

- **Deviation:** none — §9/§16; the parser is a deliberate scope guard.
- **Why:** `net_receivable` / `net_payable` are additive/subtractive over the
  computed outputs; division is not needed for the v1 figures and would invite
  rounding/ordering ambiguity. Rejecting `eval` removes a code-injection
  vector.

### 7. Rate resolution: literal decimal | dotted path | `$GV:NAME`

A rate field resolves in order: a `$GV:NAME` token →
`payload["_global_variables"][NAME]`; a known path (`facts.`/`parameters.`/
`outputs.`/`_global_variables.` prefix) → that path; otherwise a literal
decimal (`"0.15"` = 15%). A **missing** rate (path absent or `$GV` unset)
→ `AR_VALIDATION` (never silent `0.00`) at the engine level. The flow's
`resolve_parameters` node, by contrast, defaults a missing rate to `"0.00"` +
a validation **warning** (not a hard fail) — see decision 9 — so a run with
partial parameters still produces reviewable (zeroed) figures.

- **Deviation:** none.
- **Why:** unresolvable rates must surface explicitly; the `$GV:` token is
  forward-compatible with LangFlow Global Variables (decision 9) without
  requiring them today.

### 8. Seed ruleset ships as the flow's `rules` input default (declarative, overridable)

A default seed ruleset (the nine rules computing the nine figures) ships as the
`CalculationFlowComponent`'s `rules` input default (a `MultilineInput` JSON
string). It is **declarative data the operator can override** without a code
change — e.g. tuning the `net_receivable` / `net_payable` formulas
(`municipality_tax` appears on both sides: billed to the customer → receivable;
owed to the municipality → payable). A malformed `rules` JSON at runtime falls
back to the seed ruleset (and logs a warning) rather than crashing.

- **Deviation:** none — §17 (tunables as flow inputs).
- **Why:** the rules are the configurable heart of the flow; shipping them as
  an overridable input keeps the component code formula-free (decision 4) and
  lets operators adjust formulas without a redeploy.

### 9. Rates are LangFlow Global Variables (§17); v1 via `payload.parameters`, `$GV:` injection build-phase

Per §17, tunables (rates) belong in LangFlow Global Variables, not baked into
the component. The rules reference rates by `parameters.<rate>`; v1 reads
concrete rate values from the payload's `parameters` block (the validated JSON
carries them). `$GV:NAME` injection — resolving `parameters.vat_rate` from the
LangFlow Global Variables `VAT_RATE` / `MUNICIPALITY_TAX_RATE` / `ROYALTY_RATE`
/ `DISCOUNT_RATE` at build time — is a **documented build-phase seam**: the
repo only evidences `SecretStrInput(load_from_db=True)` for secrets today;
there is no plain-number Global Variable input type wired. A missing rate in
`parameters` defaults to `"0.00"` + an `AR_VALIDATION_MISSING_RATE` warning
(not a hard fail), so a partial payload still produces reviewable zeroed
figures. `environment.md` records the four build-phase Global Variables.

- **Deviation:** none — §17 satisfied; v1 uses payload parameters as the
  carrier, `$GV:` is the forward path.
- **Build-phase:** wire the four plain Global Variables and inject them into
  the payload `parameters` at build time so rates flow from LangFlow GV → rules
  without a per-call payload edit.

### 10. Reuse `calculation-result.schema.json` (§15) — NO schema change; the 9 figs are NOT in `AgentState`

The flow emits a `reconcile`-type `CalculationResult` (§15 reuse of the existing
`calculation-result.schema.json` — **no schema/enum change**). The schema's
`totals` is `additionalProperties:{pattern:^-?\d+\.\d{2}$}`, so the nine new
signed-2dp totals keys (`revenue`, `discount`, `vat`, `municipality_tax`,
`royalty`, `collections`, `expenses`, `net_receivable`, `net_payable`) are
valid without an enum amendment. Each `line_item` carries
`source_refs=[<rule_id>]` so every figure is traceable to its rule. The nine
figures are **not** recognized `data.totals{matched,outstanding,posted}` keys
→ the supervisor's `_node_invoke` does not merge them into `AgentState`; they
stay in the envelope `data`. **No `AgentState` schema change** (mirrors
ADR-0006 §7 / ADR-0007 §8).

- **Deviation:** none — §15 reuse; no new contract.
- **Why:** avoid authoring/amending a contract for figures that fit the
  existing `reconcile`-type shape; keep the supervisor's totals-merge contract
  stable.

### 11. Input = validated-JSON aggregated payload; first subflow with NO `files` HandleInput

The flow's primary input is `user_input` (a `MessageTextInput`, tool_mode)
carrying the validated-JSON payload: `{trace_id, tenant, period:{start,end},
currency, facts:{<named 2dp monetary fields>}, parameters:{<named rate
fields>}}`. This is the first AR subflow with **no `files` HandleInput** — the
facts arrive pre-aggregated as JSON (the upstream P10 Validation Flow, or a
caller, produces the payload). The deviation is recorded here.

- **Deviation:** every prior subflow accepts uploaded files; this one does not.
  Recorded here per the Authority note.
- **Why:** calculation operates on validated aggregated facts, not raw files;
  the file-reading concern belongs to the upstream Validation Flow (P10).

### 12. Checkpoints after every calculation — continues ADR-0006/0007's stricter pattern

The flow records a labeled `_audit_ref(trace_id, label)` into `audit_refs` and a
`checkpoints{<label>}` map at three boundaries: `evaluate_rules` records
`"rules"`, `build_calculation_result` records `"calculation_result"`, and the
final `checkpoint` node records the aggregate `"ar_calculation"` (3 checkpoints
total), persisted by `InMemorySaver` at each super-step (§11). This continues
ADR-0006 §9 / ADR-0007 §10's stricter "checkpoints after every calculation"
pattern (beyond §11's "after each reconciled batch").

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each calculation boundary is an auditable, resumable point; the
  checkpoint is the source of truth for resume while Langfuse tracing is off.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (decision
  below).

### 13. `InMemorySaver` v1 / durable Postgres build-phase; two stdlib-only offline self-tests

Checkpointing uses the in-image `InMemorySaver` keyed by `session_id` — the §11
fallback (non-durable, lost on worker recreate). Durable Postgres
checkpointing (`langgraph-checkpoint-postgres` into the `ar_agent` DB) remains
a documented build-phase step (same §11 fallback as the supervisor / File
Intake / Intercompany / Kitchen / Foodics flows).

Two **stdlib-only offline self-tests** ship per the CLAUDE.md self-test
convention: `business_rule_engine_selftest.py` (79 checks over the pure
`_evaluate_rules` — rule kinds, topo/cycle/dup/unknown, formula parser, rate
resolution, strict, seed ruleset) and `calculation_selftest.py` (112 checks
over the flow's pure functions + end-to-end graph — payload parse, parameter
resolution, validation, exception classification, CalculationResult assembly,
WorkflowState, checkpoints, envelope, `run()` never raises). Both stub
`lfx`/`langgraph` so they run on the host without the in-image venv; both are
picked up by `make test` and CI via `scripts/business-rule-engine.selftest.sh`
and `scripts/calculation.selftest.sh`.

- **Deviation:** none — documented §11 fallback + the project self-test
  convention.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer.