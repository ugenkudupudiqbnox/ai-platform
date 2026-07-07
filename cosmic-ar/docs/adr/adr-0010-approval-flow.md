# ADR 0010 — Human Approval Flow: 9th subflow implemented, validated-JSON review packet → §19 interrupt pause/present/capture/resume + Approve/Reject/Request-Changes + WorkflowState + audit (standalone presentational surface)

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (implements the long-standing `ar_approval` row of
  architecture §4 / [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§8/§9/§10/§11/§13/§14/§16/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [approval flow](../approval-flow.md), [supervisor](../supervisor.md),
  [invoice generation](../invoice-generation.md)

## Context

`ar_approval` has been a row in architecture §4 (row 9) and a `RunFlow` node on
the supervisor canvas since [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md),
but its flow JSON was an **empty-graph placeholder** (`cosmic-ar/flows/ar_approval.json`
had `nodes: []`) and the bundled `ApprovalGateComponent` was an unused no-op
scaffold. The supervisor already has an **internal** `_node_gate` that calls
`interrupt()`/`Command(resume=approval_ref)` for approval-tier intents
*mid-supervisor-run*. What was missing was a **real, presentational approval
surface** — a flow an operator/upstream caller invokes directly to pause,
present a review packet, capture a decision, and log it.

The request (`prompts/P13_approval_flow.md`, verbatim) is:

> Pause execution. Present [Revenue Summary / Expense Summary / Invoice Summary
> / Validation Report]. Allow [Approve / Reject / Request Changes]. Resume
> execution. Update Workflow State. Log all approvals.

This ADR records the fourteen decisions made when the **Human Approval Flow**
(`ar_approval`) was implemented as a real LangGraph flow — the new
`HumanApprovalFlowComponent` orchestrator, the wired `ar_approval.json` canvas,
the `approval-result.schema.json` enum amendment, and the offline self-test with
a custom interrupt/resume walker. **No count change** — architecture §4 stays
"Fifteen reusable LangFlow subflows"; row 9 simply changes from a placeholder to
an implemented flow.

## Decisions

### 1. Implements the 9th subflow `ar_approval` (architecture §4 row 9) — NO count change

`ar_approval` is implemented as a real LangGraph flow (`HumanApprovalFlowComponent`).
Architecture §4 row 9's purpose, source-tool, and amendment note are updated; the
heading stays "Fifteen reusable LangFlow subflows" — `ar_approval` was already
counted (it has been a row + a `RunFlow` node since ADR-0003). This ADR records
the implementation, not an addition.

- **Deviation:** none — row 9 already existed; this fills in its body.
- **Why:** the prompt asks for a Human Approval Flow; the placeholder was the
  last unimplemented subflow that the prompt targets.

### 2. Standalone presentational approval flow — NO `supervisor.py` / `supervisor.json` edit

`ar_approval` is a **standalone, direct-invocation surface** (its own `flow_id`
`ar_approval`): an operator/upstream caller submits a review packet → pause →
present → capture → resume → log. The supervisor's **internal** `_node_gate`
continues to handle mid-run financial gating (approval-tier intents pause inside
the supervisor run) **unchanged**. **No `supervisor.py` / `supervisor.json`
edit this task** — `ar_approval` is already pre-wired into the supervisor
constants (`SUBFLOWS`, `TIER["ar_approval"]="approval"`, `INTENT_KEYWORDS`
`("approve","approval")`) + a `RunFlow(ar_approval)` node on the canvas
(supervisor totals stay 18 nodes / 17 edges / 15 `RunFlow`).

- **Deviation:** the supervisor already has an internal gate; this adds a
  *separate* presentational surface rather than reusing the internal one. The
  supervisor resume-path interaction (routing the resume through this subflow vs
  the internal gate; a potential double-gate) is a **documented build-phase
  integration item** needing live LangGraph `Flow-as-Tool` + `interrupt`
  propagation testing (not exercisable offline).
- **Why:** the prompt's "Present summaries → Approve/Reject/Request Changes" is
  a presentational round-trip distinct from the supervisor's mid-run gate; a
  standalone flow is invokable directly and testable in isolation. Coupling it
  into the supervisor's resume path now would risk a double-gate that needs live
  LangGraph testing to characterize.

### 3. Input = validated-JSON review packet in `user_input`; 3rd subflow with NO `files` HandleInput

The flow's primary input is `user_input` (a `MessageTextInput`, `tool_mode`)
carrying a validated-JSON **review packet**: `{trace_id?, tenant?, action,
amount?, currency?, tier?, requested_by?, proposal:{operation, target, amount?,
currency?, details?}, idempotency_key?, summaries:{revenue_summary?,
expense_summary?, invoice_summary?, validation_report?}}`. The flow **presents**
the four summaries + proposal and **gates**; it does **not** compute the
summaries (the upstream flows — Revenue/Expense/Invoice/Validation — produce
them). This is the 3rd AR subflow with **no `files` HandleInput** (mirrors
`ar_calculation` / `ar_invoice_generation`).

- **Deviation:** the flow does not compute the summaries it presents; it trusts
  the validated-JSON packet. Recorded here per the Authority note.
- **Why:** approval is a presentation + capture concern, not a computation; the
  summaries are the caller's responsibility (separation of concerns).

### 4. Contract amendment: `approval-result.schema.json` `decision` enum += `request_changes`

The `decision` enum is amended from `["approved","rejected","expired"]` to
`["approved","rejected","request_changes","expired"]`, with a description
sentence: *"`request_changes` returns the request to the requester for revision
(terminal for that approval_ref — §19 non-reusable: the requester must submit a
new packet → new trace → new ref)."* The schema is `x-status: draft`; no runtime
validator consumes this enum today (`ValidationEngineComponent` only implements
`DocumentManifest`). `contracts.md`'s ApprovalResult table is updated to match.

- **Deviation:** the contract's `decision` enum is amended (a draft-schema
  change). Recorded here per the Authority note.
- **Why:** the prompt explicitly requires "Request Changes" as a third option;
  the existing `approved`/`rejected`/`expired` enum has no slot for it.

### 5. Pause via `interrupt()` + resume via `Command(resume={decision,decided_by,reason})` — first *subflow* to use `Command`/`interrupt`

The `request_approval` node calls `interrupt(payload)` (§19), where `payload`
carries the `approval_ref`, `action`, `tier`, the presentation `packet`, and
`options:["approve","reject","request_changes"]`. On first run the graph
suspends there → `run()` emits `pending_approval`. On resume `interrupt()`
returns the resume value and the node completes. The flow resumes with
`Command(resume={decision, decided_by, reason})` (a dict) — the resume path
parses the leading verb + remainder reason from the reply text
(`"approve <ref>"` / `"reject <ref> …"` / `"request changes <ref> …"`) into that
dict. This is the **first *subflow* to use `Command`/`interrupt`** (previously
supervisor-only); it mirrors the supervisor's `_node_gate` round-trip.

- **Deviation:** none — §19 mandates `interrupt()`/`Command(resume=…)` for
  approval gating.
- **Why:** `interrupt()` is LangGraph's pause/resume primitive; the dict resume
  value carries the decision + actor + reason atomically.

### 6. `ApprovalResult.consumed=false` here — the authorized POST is a separate flow's job

The emitted `ApprovalResult` has `consumed=false`. The flow **captures** a
decision; it does **not** post. Per §19 non-reusable, one `approval_ref`
authorizes exactly one idempotent action; `consumed` flips to `true` on the
authorized POST (a separate flow's job — e.g. `ar_post_gl`/`ar_issue_invoice`).
Replay with the same ref after `consumed=true` is rejected.

- **Deviation:** none — §19.
- **Why:** capture and POST are separate concerns; the approval flow must not
  move money (§1 north star).

### 7. `request_changes` is terminal for that `approval_ref` — the requester submits a new packet

A `request_changes` decision is **terminal for that `approval_ref`** — the
requester must revise and submit a **new** review packet (new `trace_id` → new
`approval_ref`). The same ref is not reused for the revised request (§19
non-reusable). The flow's `WorkflowState.status` is `completed` regardless of
which of the three decisions is captured (capturing *any* decision is terminal
for the request).

- **Deviation:** none — §19 non-reusable.
- **Why:** reusing a ref for a revised request would break the one-ref-one-action
  invariant; a new packet → new ref keeps the audit trail unambiguous.

### 8. `WorkflowState.status="completed"` regardless of decision; totals `"0.00"`; `pending_approvals=[]`

The `WorkflowState` snapshot has `status="completed"` regardless of the captured
decision (approved/rejected/request_changes — capturing a decision is terminal
for the request), `intent="ar_approval"`, `matched_amount`/`outstanding_balance`/
`posted_total="0.00"` (no money moves — the flow captures a decision, it does
not post), `pending_approvals=[]` (the approval is captured, not pending), and
`idempotency_keys={}` (no POST here). Immutable (§8).

- **Deviation:** none — §8/§19.
- **Why:** the flow's job is capture, not effect; the workflow completes once a
  decision is on record.

### 9. Logs all approvals — one `AuditRecord` per decision (§13)

The `audit` node builds one append-only `AuditRecord` per decision
(approved/rejected/request_changes are all logged — "Log all approvals"). The
record carries `actor=decided_by` (the Keycloak sub — §13), `action=
"approval.decision:<action>"`, `timestamp=decided_at`, `append_only=true` (§13),
`approval_ref` (the §19 link), `idempotency_key` (echo), and a `before`/`after`
delta (`before={"status":"pending"}`, `after={"decision","reason"}`). It is
appended to `audit_records` + `audit_refs`.

- **Deviation:** none — §13 (append-only audit, actor = Keycloak sub).
- **Why:** §1 north star — no money moves without SSO-attributable approval; the
  audit record is the attributable record of who decided what.

### 10. Checkpoints after `packet`/`decision`/`state`/`audit` + aggregate `ar_approval` (§11)

The flow records a labeled `_audit_ref(trace_id, label)` into `audit_refs` and a
`checkpoints{<label>}` map at five boundaries: `assemble_packet` records
`"packet"`, `request_approval` records `"decision"`, `update_state` records
`"state"`, `audit` records `"audit"`, and the final `checkpoint` node records
the aggregate `"ar_approval"` (5 checkpoints total), persisted by `InMemorySaver`
at each super-step (§11 "after every human-approval gate"; the `interrupt` itself
persists via the saver). This continues ADR-0006/0007/0008/0009's stricter
"checkpoints after every calculation/step" pattern.

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each approval boundary is an auditable, resumable point; the
  checkpoint is the source of truth for resume while Langfuse tracing is off.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (decision
  below).

### 11. Deterministic `approval_ref = ar-approval-{mint_id()}` (uuid4 mint — non-reusable per §19)

`approval_id = mint_id()` (uuid4) and `approval_ref = f"ar-approval-{approval_id}"`,
mirroring the supervisor's `_node_gate` (which mints the ref the same way). The
ref is unique per request (non-reusable per §19). The self-test checks the shape
(`ar-approval-<uuid>`) via the shared `APPROVAL_REF_RE`, not a fixed value.

- **Deviation:** none — matches the contracts' `^ar-approval-<uuid>$` pattern.
- **Why:** a uuid4 mint guarantees a fresh, non-reusable ref per request without
  a registry.

### 12. `InMemorySaver` v1 / durable Postgres build-phase

Checkpointing uses the in-image `InMemorySaver` keyed by `session_id` — the §11
fallback (non-durable, lost on worker recreate). Durable Postgres checkpointing
(`langgraph-checkpoint-postgres` into the `ar_agent` DB) remains a documented
build-phase step (shared with the supervisor / other subflows — ADR-0003
build-phase; this flow follows for free).

- **Deviation:** none — documented §11 fallback.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer.

### 13. `ApprovalGateComponent` scaffold left as-is; the flow inlines `interrupt()` directly

The bundled `ApprovalGateComponent` (`ar_common`) is an unused importable
scaffold; it is **left unchanged**. The flow inlines `interrupt()` directly in
its `request_approval` node, mirroring the supervisor's `_node_gate` (which also
inlines `interrupt()` rather than delegating to a gate component). The scaffold
remains a valid importable skeleton for future canvas use.

- **Deviation:** none — the scaffold is retained; the flow chooses to inline.
- **Why:** inlining keeps the pause/capture logic in one testable node; the
  scaffold's no-op wrapper would add an indirection without value.

### 14. One stdlib-only offline self-test with a custom interrupt/resume walker

A single stdlib-only offline self-test ships per the CLAUDE.md self-test
convention: `approval_flow_selftest.py` (158 checks over the flow's pure
functions + end-to-end pause/resume graph). It stubs `lfx`/`langgraph` so it
runs on the host without the in-image venv. **The key difference from the base
walker:** the self-test's `_Compiled` walker models `interrupt()` pause/resume
via a module-level `_INTERRUPT` box (`pause` mode raises a `_Pause` exception;
`resume` mode returns the stored value) — the base walker stubs `interrupt` →
`None`, which would fall through to `AR_FORBIDDEN`. It is picked up by `make
test` and CI via `scripts/approval-flow.selftest.sh`.

- **Deviation:** none — the project self-test convention; the custom walker is
  an internal testability decision recorded here.
- **Why:** the pause/resume round-trip is the core behavior; it cannot be tested
  with the base `interrupt`→`None` stub.

## Build-phase (not done here)

1. **Supervisor resume-path integration** — live-test the supervisor resume-path
   ↔ `ar_approval` subflow interaction (routing the resume through the subflow vs
   the internal gate; potential double-gate) with live LangGraph `Flow-as-Tool`
   + `interrupt` propagation. Not exercisable offline.
2. **Dual-control second-approver enforcement** — v1 is single-approver
   (`second_approver_ref` left `None`); dual-control (two distinct approvers) is
   a documented build-phase step (§19).
3. **`ValidationEngineComponent` wiring** for `ApprovalRequest` / `ApprovalResult` /
   `AuditRecord` (replace the inline hand-rolled validators). The canonical
   schema files remain the source of truth; the self-test keeps the validators in
   sync.
4. **Adapter 3-option rendering** — the envelope carries `data.options` +
   `data.packet` so a future LibreChat plugin can render all three buttons
   (Approve/Reject/Request-Changes); the adapter's current `render_approval`
   shows approve/reject only — left unchanged this task.
5. **Durable Postgres checkpointer** (decision 12).
6. **Import** the fifteen subflows (incl. the now-wired `ar_approval.json`) +
   `supervisor.json`; open the supervisor flow so `RunFlow(ar_approval)` resolves
   `flow_id_selected`; `docker compose restart langflow`;
   `docker exec langflow python -m lfx extension validate /app/extensions/ar_common`.