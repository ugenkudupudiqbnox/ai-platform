# Human Approval Flow (`ar_approval`)

The **Human Approval Flow** is the 9th AR subflow (architecture §4 row 9;
[ADR-0010](adr/adr-0010-approval-flow.md)). It is the **presentational approval
surface** for the AR Agent: an operator/upstream caller submits a
**validated-JSON review packet** (the Revenue/Expense/Invoice summaries + the
approval proposal), the flow **pauses** (constitution §19 `interrupt()`),
**presents** the packet, **captures** an Approve / Reject / Request-Changes
decision on **resume**, **updates WorkflowState**, and **logs** an `AuditRecord`
(§13) — then returns the `ApprovalResult` in the §14 envelope. It implements
`prompts/P13_approval_flow.md` verbatim: *Pause execution. Present [Revenue
Summary / Expense Summary / Invoice Summary / Validation Report]. Allow [Approve
/ Reject / Request Changes]. Resume execution. Update Workflow State. Log all
approvals.*

It is the **single stateful orchestrator** for human approval, mirroring the
supervisor and the other AR subflows: its responsibilities map to LangGraph
nodes inside one `lfx` component, `HumanApprovalFlowComponent`. It is the **first
*subflow* to use `Command`/`interrupt`** (previously supervisor-only).

**Standalone presentational flow (ADR-0010 §2).** The supervisor already has an
*internal* `_node_gate` that calls `interrupt()`/`Command(resume=...)` for
approval-tier intents *mid-supervisor-run*. This subflow is a **separate,
direct-invocation surface** (its own `flow_id` `ar_approval`) — it does **not**
edit `supervisor.py` / `supervisor.json` (which already pre-wire `ar_approval`).
The supervisor resume-path interaction is a documented build-phase integration
item (ADR-0010 build-phase). v1 is **single-approver** (dual-control
second-approver enforcement is build-phase) and uses `InMemorySaver` (non-durable
— the §11 fallback; durable Postgres is build-phase).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§8/§9/§10/§11/§13/§14/§16/§19, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0010](adr/adr-0010-approval-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/approval_flow.py`](../../docker/langflow-extensions/ar_common/components/ar_common/approval_flow.py)
  — `HumanApprovalFlowComponent` (internal LangGraph
  `StateGraph[ApprovalFlowState]` + `InMemorySaver`).
- **Reused contract schemas (§15, no schema change except the `decision` enum
  amendment):** `approval-request.schema.json`, `approval-result.schema.json`
  (`decision` enum += `request_changes` — ADR-0010 §4), `audit-record.schema.json`,
  `workflow-state.schema.json`.
- **Flow JSON:** [`flows/ar_approval.json`](../flows/ar_approval.json).
- **Self-test:**
  [`approval_flow_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/approval_flow_selftest.py)
  (158 stdlib-only pure-function + end-to-end pause/resume checks) via
  `scripts/approval-flow.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept the review packet | `ingest` | Parse the review-packet JSON from `user_input`; bind `trace_id` (packet.`trace_id` else `mint_id()`), `flow_id="ar_approval"`, `tenant`, `created_at`/`updated_at`; carry `tier`-override + `model_name` in **context** (not state — §8). Malformed JSON / non-object / missing `action` or `proposal` → `AR_VALIDATION`. status="created". Router `_after_ingest`: `{failed:respond, created:assemble_packet}`. |
| Build the approval request + presentation packet | `assemble_packet` | Build the `ApprovalRequest` (contract): `approval_id` (`mint_id()`), `approval_ref = ar-approval-{approval_id}`, `action`, `amount` 2dp (default `"0.00"`), `currency` (`^[A-Z]{3}$`, default `SAR`), `tier` (packet > ctx override > `"approval"`), `requested_by` (packet > ctx actor > `"unknown"`), `requested_at` (`utc_now()`), `proposal`, `idempotency_key` (optional), `contract_version`. Build the presentation `packet = {approval_ref, action, tier, amount, currency, proposal, summaries:{revenue_summary, expense_summary, invoice_summary, validation_report}}` (each summary optional — present whatever the caller supplied). **Records a checkpoint** `"packet"`. status="assembled". |
| Pause + present + capture | `request_approval` | **§19 `interrupt(payload)`** where `payload = {approval_ref, action, tier, trace_id, packet, options:["approve","reject","request_changes"]}`. On first run the graph suspends here → `run()` emits `pending_approval`. On resume `interrupt()` returns the decision (a dict `{decision, decided_by, reason}` or a reply string). `_normalize_decision` maps `approve`→`approved`, `reject`→`rejected`, `request changes`/`changes`/`revise`→`request_changes`. Missing/invalid decision → `AR_FORBIDDEN`. Else status="decided" + `decision`/`decided_by`/`decided_at`/`reason`. **Records a checkpoint** `"decision"`. Router `_after_request_approval`: `{failed:respond, decided:update_state}`. |
| Update Workflow State | `update_state` | Build the `ApprovalResult` (contract): `approval_id, approval_ref, decision, decided_by, decided_at, trace_id, tier, idempotency_key` (echo), `consumed=false` (the authorized POST is a separate flow's job — §19 non-reusable), `reason`, `contract_version`. Build the `WorkflowState` snapshot: `status="completed"` (regardless of decision — capturing a decision is terminal for the request), `intent="ar_approval"`, totals `"0.00"` (no money moved), `pending_approvals=[]`, `idempotency_keys={}` (no POST), `audit_refs`, `contract_version`. Immutable (§8). **Records a checkpoint** `"state"`. status="state_updated". |
| Log all approvals (§13) | `audit` | Build one append-only `AuditRecord` per decision (approved/rejected/request_changes all logged): `audit_id` (`uuid5` from `trace_id`+"audit"), `trace_id`, `tenant`, `actor=decided_by` (Keycloak sub), `action="approval.decision:{action}"`, `timestamp=decided_at`, `append_only=true`, `approval_ref` (§19 link), `idempotency_key` (echo), `before={"status":"pending"}`, `after={"decision","reason"}`, `contract_version`. Append to `audit_records` + `audit_refs`. **Records a checkpoint** `"audit"`. status="audited". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"ar_approval")`; reflect `audit_refs`+`checkpoints` into the `WorkflowState` snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). status="completed". |
| Return structured JSON | `respond` | `_finalize_envelope` reads `graph.get_state(config).values` (plain dict). If `snapshot.next` truthy (paused at `request_approval`) → `pending_approval` + `approval_ref` + `data={action, tier, packet, options, checkpoint_id}`. Else if `status=="failed"` → `error` + `error`. Else → `ok` + `data={approval_result, workflow_state, packet, audit_records, audit_refs, checkpoints, decision, flow_id, tenant, started_at, ended_at, contract_version}`. |
| Implement logging | `run()` boundary | §12 structured `key=value` via `self.log`: `event=approval.run outcome=… trace_id=… flow_id=… ar_entity=approval decision=… code=…`; failure boundary `code=AR_UNEXPECTED`. No PII/secrets (§16). |
| Never raises | `run()` boundary | §5/§9 — `run()` catches at the boundary and returns an `AR_UNEXPECTED` envelope. |
| Checkpoints after every gate | `assemble_packet`/`request_approval`/`update_state`/`audit` + `checkpoint` | Five labeled `_audit_ref` entries into `audit_refs` + a `checkpoints{<label>}` map (`{packet, decision, state, audit, ar_approval}`), persisted by `InMemorySaver` at each super-step (§11 "after every human-approval gate"; the `interrupt` itself persists via the saver — ADR-0010 §10). |

Graph edges: `START → ingest → assemble_packet → request_approval → update_state
→ audit → checkpoint → respond → END`, with conditional short-circuits to
`respond` on a `failed` status (`_after_ingest` / `_after_request_approval`
return `state.status` against status-keyed path maps — ADR-0003 §9). The
`request_approval` node is the **pause point** (`interrupt()`).

## Canvas wiring (3 nodes / 2 edges)

`ar_approval.json` wires (modeled on `ar_calculation.json` — the no-`files`
pattern):

- `ChatInput.message → HumanApprovalFlowComponent.user_input`
- `HumanApprovalFlowComponent.approval_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Calculation canvas; the
orchestrator node's full source is embedded as `template.code.value` (LangFlow
runs the embedded copy — it must stay in sync with the on-disk
`approval_flow.py`). There is **no `files` edge** (the 3rd subflow without one —
the review packet arrives as JSON).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, required, `tool_mode` — PRIMARY,
  the review-packet JSON; on the resume turn carries the `approval_ref` + a
  leading verb), `tier` (DropdownInput, options
  `["read-only","auto","approval","dual-control"]`, value `"approval"`,
  `tool_mode` — override; packet.`tier` wins, mirrors `ApprovalGateComponent`),
  `model_name` (MessageTextInput, value `"glm-5.2:cloud"` — documented LLM hook;
  deterministic v1 ignores it). **No `files` HandleInput.**
- **Output:** `approval_output` (Message) — the §14 envelope JSON.

## The review-packet input contract

The validated-JSON review packet the flow consumes (the four summaries are the
caller's responsibility — the flow presents, it does not compute):

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings",
  "action": "ar_post_gl",
  "amount": "1250.00",
  "currency": "SAR",
  "tier": "approval",
  "requested_by": "auth0|keycloak-sub-cv-finance-lead-002",
  "proposal": {
    "operation": "post", "target": "GL:4000",
    "amount": "1250.00", "currency": "SAR",
    "details": {"narration": "Post intercompany receivable"}
  },
  "idempotency_key": "ar-idem:gl_post:inv-123:7f3a1d2e",
  "summaries": {
    "revenue_summary": {"total": "10000.00"},
    "expense_summary": {"total": "4000.00"},
    "invoice_summary": {"invoice_id": "INV-001", "total": "1250.00"},
    "validation_report": {"valid": true, "errors": []}
  }
}
```

A missing `action` (non-empty string) or `proposal` (object) is a hard
`AR_VALIDATION`. Each summary is optional — the flow presents whatever the
caller supplied.

## The pause / present / capture / resume design

1. **First turn** — the caller submits the review packet. The graph runs
   `ingest → assemble_packet → request_approval`; `request_approval` calls
   `interrupt(payload)`. LangGraph suspends; the checkpoint records the pending
   approval. `run()` returns a `pending_approval` envelope with `approval_ref`
   and `data={action, tier, packet, options:["approve","reject","request_changes"],
   checkpoint_id}`.
2. **Resume turn** — the human replies in the same session carrying the
   `approval_ref` + a leading verb: `"approve <ref>"`, `"reject <ref> …"`, or
   `"request changes <ref> …"`. `run()` detects the ref + the pending checkpoint,
   parses the reply into `{decision, decided_by, reason}` via
   `_parse_decision_reply`, and invokes `Command(resume={…})`. The graph
   continues `request_approval → update_state → audit → checkpoint → respond`,
   returning `AR_OK` with the `ApprovalResult` + 1 audit record + the
   `WorkflowState` snapshot.
3. **Garbage resume** — a reply carrying the ref but no recognized verb →
   `AR_FORBIDDEN` (the gate fails safe; §19).

One `approval_ref` authorizes exactly one idempotent action (§19, non-reusable);
`ApprovalResult.consumed=false` here (the authorized POST is a separate flow's
job).

## The 3-way decision + `request_changes` semantics

`_normalize_decision` maps the captured verb to a canonical decision:
`approve`/`approved`/`accept`/`ok`/`yes` → `approved`;
`reject`/`rejected`/`deny`/`decline`/`no` → `rejected`;
`request changes`/`request_changes`/`changes`/`change`/`revise`/`request` →
`request_changes`. Anything else → `None` → `AR_FORBIDDEN`.

`request_changes` is **terminal for that `approval_ref`** — the requester must
revise and submit a **new** review packet (new `trace_id` → new `approval_ref`).
The same ref is not reused (§19 non-reusable). The `ApprovalResult.decision`
enum was amended to add `request_changes` (ADR-0010 §4).

## The audit-logging design (§13)

The `audit` node logs **all** approvals — one `AuditRecord` per decision
(approved/rejected/request_changes). `actor = decided_by` (the Keycloak sub —
§1/§13: no money moves without SSO-attributable approval), `append_only=true`
(§13), `approval_ref` links the §19 approval, `action="approval.decision:<action>"`,
and a `before`/`after` delta (`before={"status":"pending"}`,
`after={"decision","reason"}`). The record is appended to `audit_records` +
`audit_refs`.

## The supervisor merge (no `AgentState` change)

The decision surfaces to the supervisor only via the envelope
(`data.approval_result` + `data.audit_refs`); **no `AgentState` schema change**
(mirrors ADR-0006/0007/0008/0009). v1 emits no `data.totals{matched,outstanding,
posted}`, so the supervisor's financial totals are unaffected by an
approval-flow run.

## Contracts emitted

- [`ApprovalRequest`](contracts.md) — `data` (internal); built by
  `assemble_packet` (the contract the gate frames).
- [`ApprovalResult`](contracts.md) — `data.approval_result`; `consumed=false`
  (the POST is a separate flow's job); `decision` enum now includes
  `request_changes`.
- [`AuditRecord`](contracts.md) — `data.audit_records` (one per decision);
  `append_only=true`, `actor=decided_by`, `approval_ref` link.
- [`WorkflowState`](contracts.md) — `data.workflow_state`; `status="completed"`
  regardless of decision; totals `"0.00"` (no money moved); `intent="ar_approval"`.
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`; the
  paused envelope is `pending_approval` + `approval_ref`.

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today; every
other contract returns `AR_NOT_IMPLEMENTED`. So the orchestrator uses **inline
hand-rolled validators** for the review packet (`_parse_packet`) and the
contract builders (`_build_approval_request`/`_build_approval_result`/
`_build_audit_record`/`build_workflow_state`) — mirroring the other AR flows.
Wiring `ValidationEngineComponent` for `ApprovalRequest`/`ApprovalResult`/
`AuditRecord` is a documented build-phase step (ADR-0010 build-phase). The
canonical schema files remain the source of truth and the self-test keeps the
validators in sync (hand-rolled stdlib, no `jsonschema` dep).

## Build-phase checklist (not done here)

1. **Supervisor resume-path integration** — live-test the supervisor resume-path
   ↔ this subflow interaction (routing the resume through the subflow vs the
   internal gate; potential double-gate) with live LangGraph `Flow-as-Tool` +
   `interrupt` propagation (ADR-0010 §2).
2. **Dual-control second-approver enforcement** — v1 is single-approver
   (`second_approver_ref=None`); dual-control (two distinct approvers) is
   build-phase (§19).
3. **Wire `ValidationEngineComponent`** for `ApprovalRequest`/`ApprovalResult`/
   `AuditRecord` (replace the inline validators).
4. **Adapter 3-option rendering** — the envelope carries `data.options` +
   `data.packet` so a future LibreChat plugin can render all three buttons
   (Approve/Reject/Request-Changes); the adapter's current `render_approval`
   shows approve/reject only.
5. **Swap `InMemorySaver` → Postgres saver** (shared with the supervisor —
   ADR-0003 build-phase; this flow follows for free).
6. **Import the fifteen subflows first** (incl. the now-wired `ar_approval.json`),
   then `supervisor.json`; open the supervisor flow so `RunFlow(ar_approval)`
   resolves `flow_id_selected`; `docker compose restart langflow`.

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/approval_flow.py \
                     docker/langflow-extensions/ar_common/components/ar_common/approval_flow_selftest.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_approval.json'))"
bash scripts/approval-flow.selftest.sh     # 158 pure-function + end-to-end pause/resume checks
make validate                              # compose config unaffected
```