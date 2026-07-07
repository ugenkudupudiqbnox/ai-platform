# ADR 0012 — Audit Flow: 16th subflow implemented, validated-JSON AuditRequest → collect the run's artifacts → synthesize an immutable §13 audit log (append-only AuditRecords) + ExecutionSummary → Audit JSON + WorkflowState (read-only emission, no §1 gate, no transport)

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (adds a genuinely-new 16th row to architecture §4 — the
  first *addition* since `ar_invoice_generation` (ADR-0009))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §8/§9/§11/§13/§14/§15/§16,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [audit flow](../audit-flow.md), [supervisor](../supervisor.md),
  [file intake](../file-intake.md), [calculation](adr-0008-calculation-flow.md),
  [invoice generation](adr-0009-invoice-generation-flow.md),
  [zoho upload](adr-0011-zoho-upload-flow.md),
  [approval flow](adr-0010-approval-flow.md)

## Context

The Cosmic AR Agent had **fifteen** reusable subflows on the supervisor canvas
(ADRs 0003–0011); nine were implemented. None of them was an *audit* flow:
architecture §4 had no audit row (row 8 `ar_reporting` is a reserved **AR-aging /
dashboard extract** sourced from ZohoBooks + Foodics — folding the Audit Flow
onto it would erase a spec'd flow and contradict ADR-0004–0009's reserved
slots). There was no `ar_audit` / audit-log / `ExecutionHistory` concept anywhere
in the codebase; the bundled `AuditRecordComponent` (ar_common) and
`AuditLoggerComponent` (cosmic_common) are **scaffolds** returning
`AR_NOT_IMPLEMENTED`. The bundle the Audit Flow must collect — execution
history, input files, validation reports, calculation results, invoices,
approvals, Zoho upload results, execution time, errors, warnings — did not exist
as a single artifact; it is scattered across per-flow `WorkflowState` snapshots
and envelope `data`. The `AuditRecord` (`audit-record.schema.json`) and
`ExecutionSummary` (`execution-summary.schema.json`) contracts already existed
(both `additionalProperties:false`, draft v1.0.0).

The request (`prompts/P15_audit_flow.md`, verbatim) is:

> Collect / Execution History / Input Files / Validation Reports / Calculation
> Results / Invoices / Approvals / Zoho Upload Results / Execution Time / Errors
> / Warnings / Generate immutable audit log. / Generate execution summary. /
> Return Audit JSON. / Update Workflow State.

So the Audit Flow is a **genuinely new 16th subflow** (`ar_audit`). Per the
**established new-subflow pattern** (ADRs 0004–0009 each did this), it is wired
into the supervisor: `supervisor.py` (`SUBFLOWS` + `TIER` + `INTENT_KEYWORDS`),
`supervisor.json` (a 16th `RunFlow` node), architecture §4 row 16 + heading
amendment, §5 mermaid, and this ADR. **This is the first count bump since
ADR-0009** — architecture §4's heading is amended from "Fifteen" to "Sixteen".

This ADR records the fourteen decisions made when the **Audit Flow** (`ar_audit`)
was implemented as a real LangGraph flow — the new `AuditFlowComponent`
orchestrator, the wired `ar_audit.json` canvas, the read-only / no-§1-gate /
no-transport design, the immutable-audit-log synthesis from a collected bundle,
the `ExecutionSummary` + `WorkflowState` emission, and the offline self-test
with a custom no-interrupt graph walker.

## Decisions

### 1. Implements the 16th subflow `ar_audit` (architecture §4 row 16) — count change Fifteen → Sixteen

`ar_audit` is implemented as a real LangGraph flow (`AuditFlowComponent`).
Architecture §4 gains a **row 16** (`ar_audit`, tier `read-only`, shared
`Envelope, Audit, Checkpoint`, source `Validated JSON audit-request input`); the
heading is amended from "Fifteen reusable LangFlow subflows" to "Sixteen
reusable LangFlow subflows", exactly as ADRs 0004–0009 amended Nine → Ten → … →
Fifteen. A new amendment note records "Row 16 (`ar_audit`) is added by ADR-0012,
further amending it to 'Sixteen'." Historical amendment-note references that say
"amending it to 'Fifteen'" are left intact (they record the past).

- **Deviation:** none — this is the established count-amendment pattern.
- **Why:** the prompt asks for an Audit Flow; no existing row covered it, so a
  genuinely-new 16th row is added.

### 2. Wired into the supervisor — `supervisor.py` + `supervisor.json` → 19 nodes / 18 edges / 16 RunFlow

`ar_audit` is routable via the supervisor: `supervisor.py` appends `"ar_audit"`
to `SUBFLOWS`, adds `"ar_audit": "read-only"` to `TIER`, and adds the intent tuple
`("ar_audit", ("audit", "audit log", "execution summary", "run summary",
"run history", "audit trail"))` to `INTENT_KEYWORDS` (after the `ar_reporting`
tuple — no collision with `ar_reporting`'s `"summary report"`). `ar_audit` is
**NOT** added to `FINANCIAL_INTENTS` (no money moves). `supervisor.json` gains a
16th `RunFlow` node (`RunFlow-ar16`, `flow_name_selected="ar_audit"`,
`flow_id_selected=null`, `component_as_tool` output) + one edge to
`SupervisorAgentComponent-ar001.tools` → **19 nodes / 18 edges / 16 RunFlow**.
**No `AgentState` change.**

- **Deviation:** none — mirrors the ADR-0004–0009 new-subflow wiring.
- **Why:** the prompt's audit intent ("audit" / "execution summary" / "run
  history") should be routable from the supervisor like every other subflow.

### 3. Input = flow-internal `AuditRequest` wrapper in `user_input`; 5th subflow with NO `files` HandleInput; NO new schema

The flow's primary input is `user_input` (a `MessageTextInput`, `tool_mode`,
required) carrying a validated-JSON **`AuditRequest`** wrapper — a caller-
assembled bundle of the run's collected artifacts: `execution_history`,
`input_files`, `validation_reports`, `calculation_results`, `invoices`,
`approvals`, `zoho_upload_results`, `execution_time`, `errors`, `warnings`
(all artifact lists **optional** — an empty bundle audits an empty/no-op run),
plus `trace_id?` / `tenant?` / `actor` (the Keycloak sub). The wrapper is
**flow-internal** (documented here + in the operational doc, not a new schema
file). This is the 5th AR subflow with **no `files` HandleInput** (mirrors
`ar_calculation` / `ar_invoice_generation` / `ar_approval` / `ar_issue_invoice`).

- **Deviation:** a new flow-internal wrapper (not a contract schema). Recorded
  here per the Authority note.
- **Why:** audit needs the run's scattered artifacts collected into one
  bundle; reusing the existing per-artifact schemas (§15) avoids new contracts.

### 4. Read-only audit emission — tier `read-only`, NOT in `FINANCIAL_INTENTS`, NO §1 gate, NO `approval_ref`, NO idempotency

`ar_audit` is **read-only audit emission** — no money moves, so tier
`read-only`, **NOT** in `FINANCIAL_INTENTS`, **NO §1 approval gate**, **NO
`approval_ref`**, **NO idempotency key**. This mirrors `ar_invoice_generation`
(#15, ADR-0009) and `ar_calculation` (#14, ADR-0008) — read-only compute +
report flows. Constitution §1 ("no money moves without SSO-attributable
approval") does not gate an audit emission; the `actor` (Keycloak sub) is still
recorded on every `AuditRecord` (§13 — *who* emitted the audit), but no
approval proof is required to *read* the run's history. There is **no
`pending_approval` envelope branch** in this flow.

- **Deviation:** none — §1 governs financial mutation, not read-only audit.
- **Why:** audit is observation, not a financial action; gating it would
  block compliance work. §13 still attributes the emission to the actor.

### 5. No transport / pure compute (deterministic aggregation from the input wrapper; persistence build-phase)

The Audit Flow is **pure compute**: it aggregates the collected bundle in
memory and returns the audit log + summary + workflow state in the envelope.
There are **no external calls, no side effects, no `set_transport` seam**
(unlike `ar_issue_invoice`). The "immutable audit log" is *generated* in-memory
and returned in the envelope (offline-testable); persistence to the Postgres
`audit` table and/or Langfuse is **build-phase** (mirrors
`ar_invoice_generation`'s "artifacts in-envelope, materialization build-phase").

- **Deviation:** v1 does not persist the audit log to Postgres/Langfuse.
  Recorded here per the Authority note.
- **Why:** offline testability + no DB dependency in the scaffold; the in-
  envelope audit log is a deterministic function of the input bundle. The
  §13 contract is satisfied (append-only records emitted); *durability* is a
  build-phase concern.
- **Build-phase:** persist `audit_log` to the Postgres `audit` table + emit
  Langfuse events; the flow code is unchanged (an append after `build_audit_log`).

### 6. "Generate immutable audit log" = synthesize a list of `AuditRecord`s from the collected bundle (§13)

The `build_audit_log` node **synthesizes** one append-only `AuditRecord`
(`audit-record.schema.json`, `append_only=true`, `actor`=ctx.actor (Keycloak
sub — §13), `trace_id`, `tenant`, `timestamp`, `contract_version`, a
deterministic `audit_id = _audit_ref(trace_id, label)` uuid5, and
`correlation_id = trace_id`) per collected artifact, in order:

- per `input_file` → `action="file.intake"`, `after={file_ref, doc_type}`
  (and `source_system` when the file's `source` is zoho/foodics — decision 7);
- per `validation_report` → `action="validation.report"`,
  `after={contract_name, valid, n_errors, n_warnings}`;
- per `calculation_result` → `action="calculation.result"`,
  `after={result_type, matched, outstanding, posted}` (totals flattened to
  scalar keys — decision 12);
- per `invoice` → `action="invoice.generated"`, `before={status:"draft"}`,
  `after={invoice_id, status, total, currency}`;
- per `approval` → `action="approval.decision"`, `approval_ref` link,
  `before={status:"pending"}`, `after={decision, decided_by}`;
- per `zoho_upload_result` → `action="invoice.issue"`,
  `source_system="zoho"`, `source_ref=zoho_id`,
  `after={code, zoho_id, duplicate}`;
- a **terminal** `audit.summary` record, `after={n_records, matched,
  outstanding, posted, n_errors, n_warnings, subflows_count, duration_ms}`
  (all scalar — decision 12).

All records are appended to `audit_log` (state) + `audit_refs`. The log is
immutable by construction (`append_only=true` on every record; the node only
appends, never mutates a prior record).

- **Deviation:** none — §13 (append-only audit, actor = Keycloak sub,
  correlation_id).
- **Why:** the prompt's "Generate immutable audit log" maps to the existing
  `AuditRecord` contract; synthesizing one record per artifact gives a faithful,
  attributable trail of the run's actions.

### 7. `source_system` only on zoho/foodics records — omitted on internal actions (NO enum amendment)

`audit-record.schema.json` `source_system` is an enum `["zoho","foodics"]`
(optional). The Audit Flow synthesizes records for internal actions
(validation/calculation/file-intake-with-non-enum-source/audit.summary) that
are **not** zoho or foodics. **Decision: set `source_system` only on
zoho/foodics records** — `invoice.issue` (always `"zoho"`), and `file.intake`
when the file's `source` is `"zoho"` or `"foodics"`; **omit `source_system`**
on all other records (it is optional in the schema). **No enum amendment** is
made (avoids a contract change). The `AuditLoggerComponent`'s inconsistent
`cosmic-ar-agent|librechat|…` dropdown is **not used** — the schema enum
governs.

- **Deviation:** internal-action records omit `source_system` (optional field).
  Recorded here per the Authority note.
- **Why:** the schema enum is `[zoho,foodics]`; emitting an internal-only value
  would break the contract, and amending the enum is out of scope. Omitting the
  optional field is the contract-correct choice.

### 8. "Generate execution summary" = build the `ExecutionSummary` contract

The `build_execution_summary` node builds the `ExecutionSummary`
(`execution-summary.schema.json`, `additionalProperties:false`): `trace_id`,
`flow_id="ar_audit"`, `tenant`, `intent="ar_audit"`, `status="ok"`,
`code="AR_OK"`, `totals{matched, outstanding, posted}` (2dp, from the last
`calculation_result` carrying a `totals` dict, else `"0.00"`), `started_at` /
`ended_at` (from `execution_time` or `created_at` / `updated_at`),
`approvals` (the `approval_ref`s — omitted if empty), `audit_refs`,
`checkpoint_id` (`_audit_ref(trace_id, "ar_audit")`), `subflows_invoked`
(unique `flow_id`s from `execution_history`, in order), and
`contract_version`. status="summarized".

- **Deviation:** none — §14 (the ExecutionSummary contract is reused as-is).
- **Why:** the prompt's "Generate execution summary" maps directly to the
  existing `ExecutionSummary` contract; `intent="ar_audit"` records that this
  summary describes an audit emission.

### 9. "Return Audit JSON" = §14 envelope `data` = `audit_log` + `execution_summary` + collected bundle echoes + `workflow_state` + `audit_refs` + `checkpoints`

The `respond` node (`_finalize_envelope`) returns a §14 envelope whose `data`
carries: `audit_log` (the synthesized `AuditRecord` list — the "Audit JSON"),
`execution_summary`, the collected bundle echoes (`input_files`,
`validation_reports`, `calculation_results`, `invoices`, `approvals`,
`zoho_upload_results`, `execution_history`, `execution_time`, `errors`,
`warnings`), `workflow_state`, `audit_refs`, `checkpoints`, `flow_id`,
`tenant`, `started_at`, `ended_at`, `contract_version`. The ok branch is
`{"status":"ok","code":"AR_OK",…}`; the failed branch is
`{"status":"error","code":<err.code>,"error":<err>,…}` (code defaults
`AR_UNEXPECTED`). **No `pending_approval` branch** (read-only — decision 4).

- **Deviation:** none — §14 (envelope reuse).
- **Why:** the prompt's "Return Audit JSON" is the §14 envelope carrying the
  audit log + summary + the collected bundle + workflow state.

### 10. "Update Workflow State" = `WorkflowState` (`status="completed"`, `intent="ar_audit"`, totals, `pending_approvals=[]`, `idempotency_keys={}`)

The `build_state` node builds the `WorkflowState` (`workflow-state.schema.json`):
`status="completed"`, `intent="ar_audit"`, `matched_amount` / `outstanding_balance`
/ `posted_total` (from the collected totals, else `"0.00"`), `pending_approvals=[]`
(read-only — no gate, nothing pending), `idempotency_keys={}` (read-only — no
idempotent POST), `audit_refs`, `contract_version`. Immutable (§8). status="stated".

- **Deviation:** none — §8/§14 (WorkflowState reuse; completed status).
- **Why:** the prompt's "Update Workflow State" maps to the existing
  `WorkflowState` contract; read-only audit completes synchronously with no
  pending approvals and no idempotency keys.

### 11. Checkpoints after `validate`/`collect`/`audit_log`/`summary`/`state` + aggregate `ar_audit` (§11)

The flow records a labeled `_audit_ref(trace_id, label)` into `audit_refs` and
a `checkpoints{<label>}` map at six boundaries: `validate` records
`"validate"`, `collect` records `"collect"`, `build_audit_log` records
`"audit_log"`, `build_execution_summary` records `"summary"`, `build_state`
records `"state"`, and the final `checkpoint` node records the aggregate
`"ar_audit"` (6 labels on every success path), persisted by `InMemorySaver` at
each super-step. This continues ADR-0006/0007/0008/0009/0010/0011's stricter
"checkpoints after every step" pattern.

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each audit boundary is an auditable, resumable point; the checkpoint
  is the source of truth for resume while Langfuse tracing is off.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (decision
  below).

### 12. NO new contract schemas — `audit-record`/`execution-summary`/`workflow-state`/`envelope` reused as-is (§15); scalar-only `state_delta` flattening

No contract schema file is added or amended. `AuditRecord`,
`ExecutionSummary`, `WorkflowState`, and `Envelope` are reused verbatim (§15).
The `AuditRequest` wrapper + `audit_log` + collected bundle are flow-internal
JSON. **One constraint drove the synthesis:** `audit-record.schema.json`
`$defs/state_delta` is `additionalProperties: {"type": ["string","number",
"boolean","null"]}` — `before`/`after` MUST be **scalar-only** (no nested
objects/arrays). So the `audit.summary` record's `after` flattens totals and
timing into scalar keys (`n_records` int, `matched`/`outstanding`/`posted`
2dp strings, `n_errors`/`n_warnings`/`subflows_count` ints, `duration_ms` int),
and the `calculation.result` record's `after` flattens totals to
`matched`/`outstanding`/`posted` scalars. A `_scalar_after(d, keys)` helper
projects keys from a dict, dropping any non-scalar value, to enforce the
constraint defensively.

- **Deviation:** none — §15 reuse; the scalar-only `state_delta` is honoured by
  flattening (no schema amendment).
- **Why:** the existing contracts already cover audit records, the execution
  summary, and workflow state; no new schema is needed. The scalar-only
  constraint is a contract requirement, not a design choice.

### 13. Supervisor routing — "audit"/"execution summary"/"run summary"/"audit trail" intents → `ar_audit`; caller-assembles the `AuditRequest`

The supervisor routes the intent to `ar_audit` via the keywords in decision 2.
`ar_audit` parses the `AuditRequest` JSON from `user_input` (caller-assembled —
exactly how `ar_calculation`/`ar_invoice_generation`/`ar_issue_invoice` take
validated JSON as `user_input`). The **cross-subflow auto-accumulation** — the
supervisor automatically assembling the `AuditRequest` from multiple subflow
envelopes across a multi-subflow run — is a **v2 multi-subflow-run +
`AgentState`-artifact build-phase feature** (not exercisable in v1, which is
single-subflow-per-run); a manual/operator-assembled `AuditRequest` works now.
No field is added to `AgentState` (the bundle stays in the envelope `data`).

- **Deviation:** v1 requires a caller-assembled `AuditRequest`; the supervisor
  does not auto-accumulate. Recorded here per the Authority note.
- **Why:** auto-accumulation needs `AgentState` artifact fields + a
  `_node_invoke` merge + multi-subflow-run support — all build-phase. v1
  delivers the audit *capability* (the flow) and routes the intent to it.
- **Build-phase:** add `AgentState` artifact fields + a `_node_invoke` merge so
  the supervisor auto-assembles the `AuditRequest` from the run's subflow
  envelopes.

### 14. One stdlib-only offline self-test with a custom no-interrupt graph walker (no transport stub — pure compute)

A single stdlib-only offline self-test ships per the CLAUDE.md self-test
convention: `audit_flow_selftest.py` (180 checks over the flow's pure functions
+ end-to-end graph). It stubs `lfx`/`langgraph` via `types.ModuleType` so it
runs on the host without the in-image venv, and copies the
`zoho_upload_flow_selftest.py` `_StateGraph`/`_Compiled` walker verbatim (the
no-interrupt variant — drives the stub graph on `state.status` via the router
fn + path map, unknown status falls back to `respond`, reconstructs the frozen
dataclass after each node via `asdict` + `type(initial)(**d)`). **No transport
stub is needed** (pure compute — no `set_transport`/`ScenarioStub`/`_SLEEP`);
input is driven via `user_input` JSON. It is picked up by `make test` and CI via
`scripts/audit-flow.selftest.sh`.

- **Deviation:** none — the project self-test convention; the custom walker is
  an internal testability decision recorded here.
- **Why:** the audit-log synthesis + summary + state emission is the core
  behavior; the pure-compute flow makes every path exercisable offline with no
  transport stub.

## Build-phase (not done here)

1. **Persist the audit log** — append `audit_log` to the Postgres `audit` table
   + emit Langfuse events after `build_audit_log`; the flow code is unchanged
   (an append, not a rewrite). Durability turns the in-envelope log into a
   durable compliance record (decision 5).
2. **Cross-subflow auto-accumulation** — add `AgentState` artifact fields +
   a `_node_invoke` merge so the supervisor auto-assembles the `AuditRequest`
   from the run's subflow envelopes across a multi-subflow run (decision 13).
3. **`ValidationEngineComponent` wiring** for the `AuditRequest` wrapper
   (replace the inline hand-rolled `_validate_request`). The canonical schema
   (flow-internal once promoted) would remain the source of truth; the self-test
   keeps the validator in sync.
4. **Durable Postgres checkpointer** — swap `InMemorySaver` for
   `langgraph-checkpoint-postgres` (shared with the supervisor — ADR-0003
   build-phase; this flow follows for free — decision 11).
5. **Import** the sixteen subflows (incl. `ar_audit.json`) + `supervisor.json`;
   open the supervisor flow so `RunFlow(ar_audit)` resolves `flow_id_selected`;
   `docker compose restart langflow`; `docker exec langflow python -m lfx
   extension validate /app/extensions/ar_common`.