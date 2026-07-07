# Audit Flow (`ar_audit`)

The **Audit Flow** is the 16th AR subflow (architecture §4 row 16;
[ADR-0012](adr/adr-0012-audit-flow.md)). It collects a run's **execution
history, input files, validation reports, calculation results, invoices,
approvals, Zoho upload results, execution time, errors, and warnings** from a
caller-assembled **`AuditRequest`** wrapper, **validates** the wrapper,
**collects/normalizes** the artifacts, **synthesizes an immutable §13 audit
log** (a list of append-only `AuditRecord`s — one per artifact + a terminal
`audit.summary`), **generates an execution summary** (the `ExecutionSummary`
contract), **updates `WorkflowState`**, and returns the **Audit JSON** in the
§14 envelope. It is the **single stateful orchestrator** for run-level audit
emission, mirroring the supervisor, the File Intake Flow, the Intercompany
Sales Flow, the Cosmic Kitchen Revenue Flow, the Foodics Processing Flow, the
Calculation Flow, the Invoice Generation Flow, the Human Approval Flow, and the
Zoho Upload Flow: its responsibilities map to LangGraph nodes inside one `lfx`
component, `AuditFlowComponent`.

It is a **read-only audit emission** — tier `read-only`, **not in
`FINANCIAL_INTENTS`**, **no §1 approval gate**, no `approval_ref`, no
idempotency key (mirrors `ar_invoice_generation` / `ar_calculation`). No money
moves here; constitution §1 governs financial mutation, not read-only audit.
The `actor` (Keycloak sub) is still recorded on every `AuditRecord` (§13 —
*who* emitted the audit).

> **v1 generates the audit log in-memory and returns it in the envelope.** The
> flow is **pure compute** — no external calls, no side effects, no transport
> seam. Persistence of the audit log to the Postgres `audit` table and/or
> Langfuse is a **build-phase** step (ADR-0012 §5); the flow code is unchanged
> (an append after `build_audit_log`).

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§8/§9/§11/§13/§14/§15/§16, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0012](adr/adr-0012-audit-flow.md),
[ADR-0011](adr/adr-0011-zoho-upload-flow.md),
[ADR-0009](adr/adr-0009-invoice-generation-flow.md),
[ADR-0008](adr/adr-0008-calculation-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[ADR-0002](adr/adr-0002-reusable-component-library.md),
[supervisor](supervisor.md).

## The read-only / no-§1-gate / no-transport design

- **Read-only emission:** `ar_audit` is tier `read-only`, not in
  `FINANCIAL_INTENTS`. No §1 gate, no `approval_ref`, no idempotency key. §1
  ("no money moves without SSO-attributable approval") governs financial
  *mutation*, not audit observation; the `actor` (Keycloak sub) is recorded on
  every `AuditRecord` (§13) so the emission is attributable, but no approval
  proof is required to *read* the run's history.
- **Pure compute, no transport:** the flow aggregates the collected bundle in
  memory and returns the audit log + summary + workflow state in the envelope.
  There is no `set_transport` seam (unlike `ar_issue_invoice`). The "immutable
  audit log" is *generated* (a deterministic function of the input bundle);
  persistence is build-phase (ADR-0012 §5).
- **Caller-assembles the `AuditRequest`:** the wrapper is the run's collected
  artifacts assembled by the caller. Cross-subflow *auto*-accumulation (the
  supervisor assembling the `AuditRequest` from multiple subflow envelopes
  across a multi-subflow run) is a v2 build-phase feature; a manual/
  operator-assembled `AuditRequest` works now (ADR-0012 §13).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/audit_flow.py`](../../docker/langflow-extensions/ar_common/components/ar_common/audit_flow.py)
  — `AuditFlowComponent` (internal LangGraph `StateGraph[AuditFlowState]` +
  `InMemorySaver`).
- **Flow JSON:** [`flows/ar_audit.json`](../flows/ar_audit.json).
- **Self-test:**
  [`audit_flow_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/audit_flow_selftest.py)
  (180 stdlib-only pure-function + end-to-end checks) via
  `scripts/audit-flow.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept the audit request | `ingest` | Parse the `AuditRequest` JSON from `user_input`; bind `trace_id` (request.`trace_id` else minted), `flow_id="ar_audit"`, `tenant` (request.`tenant` else `cosmic-vikings`), `actor` (request.`actor` — Keycloak sub), `created_at`/`updated_at`; carry `model_name` in **context** (not state — §8). Malformed JSON / non-object → `AR_VALIDATION`. status="created". Router `_after_ingest`: `{failed:respond, created:validate}`. |
| Validate the wrapper | `validate` | Inline hand-rolled validator (stdlib): the wrapper is an object; each list field (`execution_history`/`input_files`/`validation_reports`/`calculation_results`/`invoices`/`approvals`/`zoho_upload_results`/`errors`/`warnings`) if present must be a list; `execution_time` if present must be an object with ISO `started_at`/`ended_at`. All artifact lists are **optional** (an empty bundle audits an empty/no-op run — still valid). Malformed → `AR_VALIDATION` with the structured error map. status="validated". **Record checkpoint** `"validate"`. Router `_after_validate`: `{failed:respond, validated:collect}`. |
| Collect / normalize artifacts | `collect` | Normalize the artifacts into state fields + compute summary counts (`n_invoices`/`n_approvals`/`n_zoho_uploads`/`n_calc_results`/`n_val_reports`/`n_input_files`/`n_errors`/`n_warnings`/`n_subflows`). Derive `subflows_invoked` (unique `flow_id`s from `execution_history`, in order). Derive `totals` from the **last** `calculation_result` carrying a `totals` dict (2dp-coerced), else `0.00`. status="collected". **Record checkpoint** `"collect"`. |
| Generate immutable audit log (§13) | `build_audit_log` | Synthesize one append-only `AuditRecord` per artifact + a terminal `audit.summary` record (decision/ADR §6): `input_files` → `file.intake`; `validation_reports` → `validation.report`; `calculation_results` → `calculation.result` (totals flattened to scalar `matched`/`outstanding`/`posted`); `invoices` → `invoice.generated` (`before={status:"draft"}`); `approvals` → `approval.decision` (`approval_ref` link, `before={status:"pending"}`); `zoho_upload_results` → `invoice.issue` (`source_system="zoho"`, `source_ref=zoho_id`); terminal → `audit.summary` (scalar-only `after`). Each record: `audit_id = _audit_ref(trace_id, label)` uuid5, `correlation_id = trace_id`, `actor`, `append_only=true`, `contract_version`. `source_system` only on zoho/foodics records (ADR §7). Append to `audit_log` + `audit_refs`. status="audited". **Record checkpoint** `"audit_log"`. |
| Generate execution summary | `build_execution_summary` | Build the `ExecutionSummary` (`execution-summary.schema.json`): `intent="ar_audit"`, `status="ok"`, `code="AR_OK"`, `totals{matched,outstanding,posted}` (2dp), `started_at`/`ended_at` (from `execution_time` or `created_at`/`updated_at`), `approvals` (the `approval_ref`s — omitted if empty), `audit_refs`, `checkpoint_id = _audit_ref(trace_id,"ar_audit")`, `subflows_invoked`, `contract_version`. status="summarized". **Record checkpoint** `"summary"`. |
| Update Workflow State | `build_state` | `WorkflowState` (`workflow-state.schema.json`): `status="completed"`, `intent="ar_audit"`, `matched_amount`/`outstanding_balance`/`posted_total` (from collected totals or `"0.00"`), `pending_approvals=[]`, `idempotency_keys={}` (read-only — no gate), `audit_refs`, `contract_version`. Immutable (§8). **Record checkpoint** `"state"`. status="stated". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"ar_audit")`; reflect `audit_refs`+`checkpoints` into the WorkflowState snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). status="completed". |
| Return Audit JSON | `respond` | `_finalize_envelope` builds `data={audit_log, execution_summary, input_files, validation_reports, calculation_results, invoices, approvals, zoho_upload_results, execution_history, execution_time, errors, warnings, workflow_state, audit_refs, checkpoints, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`; code defaults `AR_UNEXPECTED`). **No pending branch.** |
| Logging | `run()` boundary | §12 structured `key=value` via `self.log`: `event=audit.run outcome=… trace_id=… flow_id=… ar_entity=audit n_records=… code=…`; failure boundary `code=AR_UNEXPECTED`. No PII/secrets (§16). |
| Never raises | `run()` boundary | §5/§9 — `run()` catches at the boundary and returns an `AR_UNEXPECTED` envelope; bad input → `AR_VALIDATION`/`AR_UNEXPECTED` envelope, not an exception. |
| Checkpoints after every step | each node + `checkpoint` | Continues ADR-0006/0007/0008/0009/0010/0011's stricter pattern: each boundary records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (6 labels on the success path: `validate`, `collect`, `audit_log`, `summary`, `state`, `ar_audit`), persisted by `InMemorySaver` at each super-step (§11 — ADR-0012 §11). |

Graph edges: `START → ingest → validate → collect → build_audit_log →
build_execution_summary → build_state → checkpoint → respond → END`, with
conditional short-circuits to `respond` on any `failed` status
(`_after_ingest`/`_after_validate` return `state.status` against status-keyed
path maps — ADR-0003 §9). The tail (collect → … → respond) is all-static pure
compute (no `interrupt`/`Command`, no in-flow pause — read-only emission).

## The `AuditRequest` input contract

The validated-JSON wrapper the flow consumes (the PRIMARY input via `user_input`,
flow-internal — not a new schema file):

```json
{
  "trace_id": "trc-…",
  "tenant": "cosmic-vikings",
  "actor": "<keycloak sub>",
  "execution_history": [
    {"flow_id": "ar_calculation", "status": "completed", "code": "AR_OK",
     "started_at": "2026-07-08T09:00:00Z", "ended_at": "2026-07-08T09:00:05Z"}
  ],
  "input_files": [
    {"file_ref": "ar_file_intake/<ts>_sheet.xlsx", "doc_type": "invoice", "source": "zoho"}
  ],
  "validation_reports": [
    {"contract_name": "DocumentManifest", "valid": true, "errors": [], "warnings": []}
  ],
  "calculation_results": [
    {"result_type": "reconcile",
     "totals": {"matched": "1500.00", "outstanding": "0.00", "posted": "0.00"}}
  ],
  "invoices": [
    {"invoice_id": "INV-001", "status": "draft", "total": "1575.00", "currency": "SAR"}
  ],
  "approvals": [
    {"approval_ref": "ar-approval-12345678-1234-1234-1234-123456789abc",
     "decision": "approved", "decided_by": "sub-9", "decided_at": "2026-07-08T09:01:00Z"}
  ],
  "zoho_upload_results": [
    {"code": "AR_OK", "zoho_id": "zoho-inv-…", "duplicate": false}
  ],
  "execution_time": {"started_at": "2026-07-08T09:00:00Z",
                     "ended_at": "2026-07-08T09:02:00Z", "duration_ms": 120000},
  "errors":   [{"code": "AR_…", "message": "…", "flow_id": "ar_…"}],
  "warnings": [{"code": "AR_…", "message": "…", "flow_id": "ar_…"}]
}
```

- `actor` (the Keycloak sub — recorded on every `AuditRecord` as the emitter).
- `trace_id` / `tenant` (optional; `tenant` defaults to `cosmic-vikings`).
- **All artifact lists are optional** — an empty bundle audits an empty/no-op
  run (still valid; the audit log is just the terminal `audit.summary` record).

A malformed wrapper / non-object → `AR_VALIDATION`; a list field that is not a
list → `AR_VALIDATION`; a bad `execution_time` → `AR_VALIDATION`.

## The immutable-audit-log synthesis design (§13)

The `build_audit_log` node synthesizes one append-only `AuditRecord` per
collected artifact + a terminal `audit.summary` record. Every record satisfies
`audit-record.schema.json` (`additionalProperties:false`): `audit_id`
(deterministic uuid5 `_audit_ref(trace_id, label)`), `trace_id`, `tenant`,
`actor` (Keycloak sub — §13), `action`, `timestamp`, `append_only=true`,
`correlation_id = trace_id`, `contract_version`. Records are **only appended** —
the node never mutates a prior record, so the log is immutable by construction.

| Source artifact | `action` | `source_system` | `before` / `after` (scalar-only) |
|---|---|---|---|
| `input_files[i]` | `file.intake` | `"zoho"`/`"foodics"` if the file's `source` matches the enum, else omitted | `after={file_ref, doc_type}` |
| `validation_reports[i]` | `validation.report` | omitted | `after={contract_name, valid, n_errors, n_warnings}` |
| `calculation_results[i]` | `calculation.result` | omitted | `after={result_type, matched, outstanding, posted}` (totals flattened) |
| `invoices[i]` | `invoice.generated` | omitted | `before={status:"draft"}`, `after={invoice_id, status, total, currency}` |
| `approvals[i]` | `approval.decision` | omitted | `approval_ref` link, `before={status:"pending"}`, `after={decision, decided_by}` |
| `zoho_upload_results[i]` | `invoice.issue` | `"zoho"` | `source_ref=zoho_id`, `after={code, zoho_id, duplicate}` |
| (terminal) | `audit.summary` | omitted | `after={n_records, matched, outstanding, posted, n_errors, n_warnings, subflows_count, duration_ms}` |

**`source_system` only on zoho/foodics records** — the schema enum is
`[zoho, foodics]`; internal actions (validation/calculation/invoice-generation/
approval/audit.summary) omit it (optional field — no enum amendment: ADR-0012 §7).

**Scalar-only `state_delta`:** `audit-record.schema.json` `$defs/state_delta`
is `additionalProperties: {"type": ["string","number","boolean","null"]}` —
`before`/`after` MUST be scalar-only (no nested objects/arrays). The
`audit.summary` record's `after` therefore flattens totals and timing into
scalar keys (`n_records` int, `matched`/`outstanding`/`posted` 2dp strings,
`n_errors`/`n_warnings`/`subflows_count` ints, `duration_ms` int), and the
`calculation.result` record's `after` flattens totals to scalar
`matched`/`outstanding`/`posted`. A `_scalar_after(d, keys)` helper projects
keys from a dict, dropping any non-scalar value, to enforce the constraint
defensively (ADR-0012 §12).

## The ExecutionSummary design

`build_execution_summary` builds the `ExecutionSummary`
(`execution-summary.schema.json`, `additionalProperties:false`): `trace_id`,
`flow_id="ar_audit"`, `tenant`, `intent="ar_audit"`, `status="ok"`,
`code="AR_OK"`, `totals{matched, outstanding, posted}` (2dp, from the last
`calculation_result` carrying a `totals` dict, else `"0.00"`), `started_at`/
`ended_at` (from `execution_time` or `created_at`/`updated_at`), `approvals`
(the `approval_ref`s — omitted if empty), `audit_refs`, `checkpoint_id =
_audit_ref(trace_id, "ar_audit")`, `subflows_invoked` (unique `flow_id`s from
`execution_history`, in order), `contract_version`.

## The pure-compute / no-transport vs build-phase persistence design

- **v1 (this task):** pure compute. The flow aggregates the collected bundle in
  memory, synthesizes the audit log, builds the summary + workflow state, and
  returns everything in the envelope. No external calls, no side effects, no
  `set_transport` seam. Offline-testable, no DB, no credentials.
- **Build-phase:** persist `audit_log` to the Postgres `audit` table + emit
  Langfuse events after `build_audit_log`; the flow code is unchanged (an
  append after the node — ADR-0012 §5). Durability turns the in-envelope log
  into a durable compliance record.

## Canvas wiring (3 nodes / 2 edges)

`ar_audit.json` wires (modeled on `ar_issue_invoice.json`):

- `ChatInput.message → AuditFlowComponent.user_input`
- `AuditFlowComponent.audit_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Invoice Generation /
Zoho Upload canvas; the orchestrator node's full source is embedded as
`template.code.value` (LangFlow runs the embedded copy — it must stay in sync
with the on-disk `audit_flow.py`). There is **no `files` edge** — the 5th
subflow without one (after `ar_calculation` / `ar_invoice_generation` /
`ar_approval` / `ar_issue_invoice`).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, required, `tool_mode` — carries
  the `AuditRequest` JSON, the PRIMARY input), `model_name` (MessageTextInput,
  value `"glm-5.2:cloud"` — documented LLM hook; deterministic v1 ignores it).
  **No `files` HandleInput.**
- **Output:** `audit_output` (Message) — the §14 envelope JSON.

## The supervisor merge (no `AgentState` change)

The Audit Flow surfaces to the supervisor only via `subflows_invoked` +
`audit_refs`; the audit log + collected bundle stay in the envelope `data`. **No
field is added to `AgentState`** (ADR-0012 §13, mirrors ADR-0006–0011). The
supervisor routes the "audit"/"execution summary"/"run summary"/"run history"/
"audit trail" intent to `ar_audit`, which parses the `AuditRequest` from
`user_input` (caller-assembled). **Cross-subflow auto-accumulation** — the
supervisor auto-assembling the `AuditRequest` from the run's subflow envelopes
across a multi-subflow run — is a v2 build-phase feature (needs `AgentState`
artifact fields + a `_node_invoke` merge + multi-subflow-run support); a
manual/operator-assembled `AuditRequest` works now.

## Contracts emitted

- [`AuditRecord`](contracts.md) — `data.audit_log`, one per artifact + a
  terminal `audit.summary`; `append_only=true`, `actor`=Keycloak sub,
  `correlation_id=trace_id`, `source_system` only on zoho/foodics records.
  **No schema change** (§15 reuse).
- [`ExecutionSummary`](contracts.md) — `data.execution_summary`; `intent=
  "ar_audit"`, `totals`, `subflows_invoked`, `approvals`, `audit_refs`,
  `checkpoint_id`. **No schema change** (§15 reuse).
- [`WorkflowState`](contracts.md) — `data.workflow_state`; `status="completed"`,
  `intent="ar_audit"`, `pending_approvals=[]`, `idempotency_keys={}`. **No
  schema change** (§15 reuse).
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`; no
  `pending_approval` branch. **No schema change** (§15 reuse).
- **Flow-internal (no schema):** the `AuditRequest` wrapper + the collected
  bundle echoes in `data` (ADR-0012 §3/§9).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today. So the
orchestrator uses an **inline hand-rolled validator** for the `AuditRequest`
wrapper (`_parse_request`/`_validate_request`) — mirroring the File Intake /
Intercompany / Kitchen / Foodics / Calculation / Invoice Generation / Zoho
Upload flows. Wiring `ValidationEngineComponent` for the `AuditRequest` is a
documented build-phase step. The canonical schemas
(`audit-record`/`execution-summary`/`workflow-state`) remain the source of
truth and the self-test keeps the validator in sync (hand-rolled stdlib, no
`jsonschema` dep).

## Build-phase checklist (not done here)

1. **Persist the audit log** — append `audit_log` to the Postgres `audit` table
   + emit Langfuse events after `build_audit_log`; the flow code is unchanged
   (ADR-0012 §5).
2. **Cross-subflow auto-accumulation** — add `AgentState` artifact fields + a
   `_node_invoke` merge so the supervisor auto-assembles the `AuditRequest`
   from the run's subflow envelopes (ADR-0012 §13).
3. **Wire `ValidationEngineComponent`** for the `AuditRequest` wrapper (replace
   the inline validators).
4. **Durable Postgres checkpointer** — swap `InMemorySaver` for
   `langgraph-checkpoint-postgres` (shared with the supervisor — ADR-0003
   build-phase; this flow follows for free).
5. **Import the sixteen subflows first** (incl. `ar_audit.json`), then
   `supervisor.json`; open the supervisor flow so `RunFlow(ar_audit)` resolves
   `flow_id_selected`; `docker compose restart langflow`; `docker exec langflow
   python -m lfx extension validate /app/extensions/ar_common`.

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/audit_flow.py \
                     docker/langflow-extensions/ar_common/components/ar_common/audit_flow_selftest.py \
                     docker/langflow-extensions/ar_common/components/ar_common/supervisor.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_audit.json'))"
bash scripts/audit-flow.selftest.sh     # 180 pure-function + end-to-end checks
make validate                           # compose config unaffected
```