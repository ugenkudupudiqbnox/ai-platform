# ADR 0011 — Zoho Upload Flow: 7th subflow implemented, validated-JSON ZohoUploadRequest → §10-retried upload to Zoho Books → all-or-nothing rollback → store zoho_id + timestamp → ZohoUploadResult + WorkflowState + audit (§1 approval_ref required at the boundary, no in-flow interrupt)

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (implements the long-standing `ar_issue_invoice` row of
  architecture §4 / [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §1/§8/§9/§10/§11/§13/§14/§16/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [zoho upload flow](../zoho-upload-flow.md), [supervisor](../supervisor.md),
  [invoice generation](../invoice-generation.md),
  [approval flow](adr-0010-approval-flow.md)

## Context

`ar_issue_invoice` has been a row in architecture §4 (row 7) and a `RunFlow`
node on the supervisor canvas since [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md),
but its flow JSON was an **empty-graph placeholder** (`cosmic-ar/flows/ar_issue_invoice.json`
had `nodes: []`) and the bundled `ZohoBooksARTool` is a read-only scaffold (its
`create_invoice`/`delete_invoice` POST/DELETE are commented-out build-phase
pseudocode). The supervisor already has an **internal** `_node_gate` that calls
`interrupt()`/`Command(resume=approval_ref)` for approval-tier financial intents
*before* delegating to this subflow (`ar_issue_invoice` is in `FINANCIAL_INTENTS`
at tier `approval`). What was missing was a **real Zoho upload surface** — a flow
that takes an approved batch of invoices, posts each to Zoho Books with §10
retry, rolls back on partial failure, stores the Zoho ids, and logs it.

The request (`prompts/P14_zoho_upload_flow.md`, verbatim) is:

> Input Invoice JSON. Validate mandatory fields. Upload invoices. Retry
> failures. Rollback failed uploads. Store: Zoho Invoice ID, Upload Timestamp.
> Return Upload Result. Update Workflow State.

This ADR records the fourteen decisions made when the **Zoho Upload Flow**
(`ar_issue_invoice`) was implemented as a real LangGraph flow — the new
`ZohoUploadFlowComponent` orchestrator, the wired `ar_issue_invoice.json` canvas,
the §1 `approval_ref`-at-the-boundary design, the all-or-nothing rollback, the
deterministic stub transport, and the offline self-test with a custom graph
walker. **No count change** — architecture §4 stays "Fifteen reusable LangFlow
subflows"; row 7 simply changes from a placeholder to an implemented flow.

## Decisions

### 1. Implements the 7th subflow `ar_issue_invoice` (architecture §4 row 7) — NO count change

`ar_issue_invoice` is implemented as a real LangGraph flow
(`ZohoUploadFlowComponent`). Architecture §4 row 7's purpose + an amendment note
are updated; the heading stays "Fifteen reusable LangFlow subflows" —
`ar_issue_invoice` was already counted (it has been a row + a `RunFlow` node
since ADR-0003). This ADR records the implementation, not an addition. It is the
row that **POSTs** the invoice to Zoho — distinct from `ar_invoice_generation`
(#15, ADR-0009), which only *generates a draft "Zoho Upload File" artifact*.

- **Deviation:** none — row 7 already existed; this fills in its body.
- **Why:** the prompt asks for a Zoho Upload Flow; row 7 was the placeholder the
  prompt targets.

### 2. Standalone Zoho upload flow — NO `supervisor.py` / `supervisor.json` edit

`ar_issue_invoice` is a **standalone, direct-invocation upload surface** (its own
`flow_id` `ar_issue_invoice`): an authorized caller submits a
`ZohoUploadRequest` (an `approval_ref` + a batch of `InvoiceData`) → validate →
upload (§10 retry) → rollback on partial failure → store → audit. The
supervisor's **internal** `_node_gate` continues to capture §19 approval
*before* delegating a financial intent to this subflow, **unchanged**. **No
`supervisor.py` / `supervisor.json` edit this task** — `ar_issue_invoice` is
already pre-wired into the supervisor constants (`SUBFLOWS`,
`TIER["ar_issue_invoice"]="approval"`, `INTENT_KEYWORDS`
`"issue"/"create"/"present"/"new invoice"`, `FINANCIAL_INTENTS`) + a
`RunFlow(ar_issue_invoice)` node on the canvas (supervisor totals stay 18 nodes /
17 edges / 15 `RunFlow`).

- **Deviation:** the supervisor already captures approval internally; this flow
  additionally **requires** the `approval_ref` at its own boundary (decision 4).
  The supervisor resume-path ↔ `ar_issue_invoice` subflow live interaction (the
  `RunFlow` tool propagating the approval context into the subflow) is a
  **documented build-phase integration item** needing live LangGraph
  `Flow-as-Tool` testing (not exercisable offline).
- **Why:** the prompt's upload/rollback is a posting concern; a standalone flow
  is invokable directly and testable in isolation. Coupling it into the
  supervisor's resume path now would need live LangGraph testing to
  characterize.

### 3. Input = validated-JSON `ZohoUploadRequest` wrapper in `user_input`; 4th subflow with NO `files` HandleInput; NO new schema

The flow's primary input is `user_input` (a `MessageTextInput`, `tool_mode`,
required) carrying a validated-JSON **`ZohoUploadRequest`** wrapper:
`{approval_ref (ar-approval-<uuid>, required — §1), invoices:[InvoiceData,…]
(≥1; single = 1-element batch), trace_id?, tenant?}`. Each `InvoiceData` is the
Invoice JSON from `ar_invoice_generation`. The wrapper is **flow-internal**
(documented here + in the operational doc, not a new schema file). This is the
4th AR subflow with **no `files` HandleInput** (mirrors `ar_calculation` /
`ar_invoice_generation` / `ar_approval`).

- **Deviation:** a new flow-internal wrapper (not a contract schema). Recorded
  here per the Authority note.
- **Why:** upload needs the `approval_ref` (§1) + the invoice batch together;
  reusing the existing `InvoiceData` schema (§15) avoids a new contract.

### 4. §1 enforcement: `approval_ref` required at the boundary, missing/invalid → `AR_FORBIDDEN`, NO in-flow `interrupt`

The flow **does not pause via `interrupt()`**. Instead the `ingest` node
**requires** an `approval_ref` matching `^ar-approval-<uuid>$`; missing/invalid →
`AR_FORBIDDEN` (the run stops before any upload). This enforces constitution §1
("no money moves without SSO-attributable approval") **at the flow boundary**.
The `approval_ref` is echoed into every `AuditRecord` (§13 link). Approval
**capture** is the supervisor's `_node_gate`'s / `ar_approval`'s job; this flow
**executes the authorized, idempotent POST**. This mirrors the ADR-0010
standalone-surface precedent and avoids a double-gate (the supervisor already
gated the intent; this flow only re-checks the proof).

- **Deviation:** the architecture row 7 lists `ApprovalGate` as a constituent;
  this flow uses a boundary `approval_ref` check instead of an in-flow
  `ApprovalGate`/`interrupt`. Recorded here per the Authority note.
- **Why:** the supervisor already captures approval mid-run; a second in-flow
  pause would double-gate and is not exercisable offline. A boundary check keeps
  §1 enforced without a live `interrupt` round-trip, and the `approval_ref` echo
  preserves the §13 attributable-approval link.

### 5. Batch-aware all-or-nothing: each invoice uploaded with §10 retry; on any invoice failing after retries → rollback the created ones

A single invoice is a 1-element batch. Each invoice is uploaded with §10 retry
(decision 7). After all uploads: if every invoice succeeded (`AR_OK`/`AR_DUPLICATE`)
→ status `uploaded` (skip rollback); if **any** invoice failed after retries →
status `partial` → the **already-created** invoices (those with a `zoho_id`) are
**rolled back** (deleted best-effort). No partial batch is left in Zoho — the
batch is observably failed (`posted_total="0.00"`, `WorkflowState.status="failed"`).
A batch where every invoice failed (nothing created) skips rollback (nothing to
delete) but is still `failed`.

- **Deviation:** none — §1/§10 (no partial posting; failed batches are rolled
  back, never silently left).
- **Why:** a partial batch in Zoho would be an unattributable, unreconcilable
  state; all-or-nothing keeps the batch atomic and the audit trail unambiguous.

### 6. Deterministic stub transport v1; real `ZohoBooksARTool` create/delete at build-phase via `set_transport`

A module-level `_TRANSPORT = StubZohoUpload()` + `set_transport(t)` abstraction:
the stub returns deterministic result dicts
(`zoho_id = f"zoho-inv-<uuid5(invoice_id)>"`) so the flow is **offline-testable
with no live Zoho, no credentials**. The real `ZohoBooksARTool.create_invoice` /
`delete_invoice` (OAuth + POST/DELETE + 401-retry, mirroring `ZohoBooksAPTool`)
is wired at **build-phase** via `set_transport(RealZoho())`; the flow code is
unchanged. This matches every implemented flow (deterministic + offline-testable;
live external calls are build-phase).

- **Deviation:** v1 does not post to live Zoho (deterministic stub). Recorded
  here per the Authority note.
- **Why:** offline testability + no credentials in the scaffold; the transport
  seam makes the build-phase swap a one-line `set_transport` call.
- **Build-phase:** implement `ZohoBooksARTool.create_invoice`/`delete_invoice`
  (OAuth refresh-on-401 + POST/DELETE, mirror `ZohoBooksAPTool`); `set_transport(
  RealZoho())`; live-test the upload+retry+rollback round-trip.

### 7. §10 retry: ≤3 attempts, exp backoff `1s·2^n` ±25% parity jitter ≤30s, transient-only, no 4xx retry, `AR_DUPLICATE` safe replay

`_upload_one` runs the §10 retry loop over `_TRANSPORT.create_invoice`: ≤3
attempts; backoff `1s·2^n` with deterministic ±25% **parity-based** jitter (no
`Math.random`/`uuid4` — +25% on even attempts, −25% on odd), the **final** delay
capped at 30s. Retry only on **transient** results (the transport flagged it, OR
408/429, OR 5xx); **no 4xx retry** (except 401, handled inside the real
transport via token refresh — v1 stub treats 401 as hard `AR_AUTH`).
`AR_OK`/`AR_DUPLICATE` stop immediately; `AR_DUPLICATE` is a **safe idempotent
replay** (the invoice already exists — `duplicate=true`, `zoho_id` returned).
Hard codes (`AR_AUTH`/`AR_VALIDATION`/`AR_FORBIDDEN`/`AR_NOT_FOUND`) stop
immediately. Exhausted transient → `AR_UPSTREAM` (`attempts=3`). `attempted_at`
captures the terminal attempt's timestamp. A module-level `_SLEEP` hook (default
`time.sleep`) makes the backoff instant under the offline self-test.

- **Deviation:** none — §10 (deterministic parity jitter mirrors the
  calculation/kitchen-revenue backoff).
- **Why:** §10 mandates bounded, jittered, transient-only retry; deterministic
  jitter keeps the offline test reproducible.

### 8. Canonical `ZohoUploadResult` per create (`operation="invoice_issue"`) + enriched per-invoice view with `rolled_back`; rollback deletes are audit-only

The `store` node builds the canonical `ZohoUploadResult` (`zoho-upload-result`
schema, `additionalProperties:false`) per invoice: `operation="invoice_issue"`,
`http_status`, `code`, `idempotency_key`, `zoho_id`, `zoho_ref`, `duplicate`,
`attempted_at`, `attempts`, `trace_id`, `tenant`, `contract_version`. The flow
also emits an **enriched per-invoice view** (`data.upload_results`) adding
`invoice_id`/`invoice_number`/`customer_ref`/`total`/`currency` +
`rolled_back`/`rollback_code` + the `approval_ref` echo — this is flow-internal
(not the canonical contract). Rollback **deletes** are **audit-only**: there is
no `ZohoUploadResult` operation enum value for delete, so a delete does not
produce a canonical result; it produces a `rollback_results` entry + an
`AuditRecord` (decision 9). The `rolled_back` flag is carried in the enriched
view, not in the canonical contract.

- **Deviation:** the canonical `ZohoUploadResult` has no `rolled_back` field;
  rollback state is in the enriched view + audit only. Recorded here per the
  Authority note.
- **Why:** the contract is a per-POST result; a delete is a different operation.
  Keeping `rolled_back` out of the canonical contract preserves
  `additionalProperties:false` without amending the schema.

### 9. Logs one `AuditRecord` per create + one per rollback delete (§13)

The `audit` node builds one append-only `AuditRecord` per invoice **create**
(`action="invoice.issue"`, `actor=ctx.actor` (Keycloak sub — §13), `approval_ref`
echo (§19 link), `idempotency_key`, `source_system="zoho"`, `source_ref=zoho_id`,
`before={"status":"draft"}`, `after={"zoho_id", "status":"sent"|"failed", "code"}`,
`append_only=true`) — logged for **every** attempted invoice (success, duplicate,
or failure). It builds one per **rollback delete** (`action="invoice.rollback"`,
`source_ref=zoho_id`, `before={"zoho_id"}`, `after={"status":"voided",
"rollback_code"}`). Both are appended to `audit_records` + `audit_refs`.

- **Deviation:** none — §13 (append-only audit, actor = Keycloak sub, approval
  link).
- **Why:** §1 north star — no money moves without SSO-attributable approval; the
  audit record is the attributable record of who authorized what, and the
  rollback record is the attributable record of the corrective delete.

### 10. `idempotency_key = ar-idem:invoice_issue:<tenant>:<uuid5(invoice_id)>` deterministic (§10 replay-safe)

`_build_idempotency_key(tenant, invoice_id)` =
`ar-idem:invoice_issue:<tenant>:<uuid5(NAMESPACE_URL, "zoho-idem:<invoice_id>")>`.
`uuid5` is reproducible (§4.3) — the same invoice always yields the same key, so
a replay after a transient failure (or a duplicate POST) is safe (§10). It
matches the `zoho-upload-result.idempotency_key` pattern
`^ar-idem:[a-z_]+:[a-z0-9_-]+:[a-z0-9_-]+$`. The key is echoed into the
`ZohoUploadResult` and the create `AuditRecord`, and collected into
`WorkflowState.idempotency_keys` (`{"invoice_issue:<invoice_id>": key, …}`).

- **Deviation:** none — §10 (deterministic idempotency key).
- **Why:** a deterministic key makes retries + replays safe without a registry;
  the contract pattern is honoured.

### 11. `WorkflowState.status="completed"` (all succeeded) / `"failed"` (partial→rollback or all failed); `posted_total`=Σ non-rolled-back

The `build_state` node sets `WorkflowState.status="completed"` when every invoice
succeeded, else `"failed"` (partial→rollback, or all failed). `posted_total` =
`_sum_2dp` of the **non-rolled-back** invoice totals (those with
`AR_OK`/`AR_DUPLICATE` and `rolled_back=false`; `"0.00"` if all rolled
back/failed). `matched_amount`/`outstanding_balance="0.00"` (this is not a match
flow). `pending_approvals=[]` (approval was captured at the boundary, not
pending). `idempotency_keys` map (decision 10). `intent="ar_issue_invoice"`.
Immutable (§8). The envelope is `error` when `state.error` is set OR
`workflow_state.status=="failed"` (the §14 envelope `status` enum is
`[ok,error,pending_approval]` — there is **no pending branch** this flow).

- **Deviation:** none — §8/§14 (no pending_approval; the batch failed ⇒ error
  envelope).
- **Why:** the workflow state records what actually posted; a failed batch is an
  error, not a silent partial success.

### 12. Checkpoints after `validate`/`upload`/`rollback`/`store`/`audit`/`state` + aggregate `ar_issue_invoice` (§11)

The flow records a labeled `_audit_ref(trace_id, label)` into `audit_refs` and a
`checkpoints{<label>}` map at six boundaries: `validate` records `"validate"`,
`upload` records `"upload"`, `rollback` records `"rollback"` (only on a partial
batch), `store` records `"store"`, `audit` records `"audit"`, `build_state`
records `"state"`, and the final `checkpoint` node records the aggregate
`"ar_issue_invoice"` (6 on the success path — no rollback — 7 on the partial
path), persisted by `InMemorySaver` at each super-step. This continues
ADR-0006/0007/0008/0009/0010's stricter "checkpoints after every step" pattern.

- **Deviation:** none — §11 satisfied (and exceeded, per ADR-0006).
- **Why:** each upload boundary is an auditable, resumable point; the checkpoint
  is the source of truth for resume while Langfuse tracing is off.
- **Build-phase:** swap `InMemorySaver` for the Postgres checkpointer (decision
  below).

### 13. NO new contract schemas — `zoho-upload-result`/`invoice-data`/`audit-record`/`workflow-state`/`envelope` reused as-is (§15)

No contract schema file is added or amended. `ZohoUploadResult`
(`operation="invoice_issue"`, `code` enum incl. `AR_DUPLICATE`/`AR_UPSTREAM`/
`AR_AUTH`/etc.), `InvoiceData` (the input), `AuditRecord`
(`action="invoice.issue"`/`"invoice.rollback"`, `source_system="zoho"`),
`WorkflowState` (status incl. `completed`/`failed`), and `Envelope` (no
`pending_approval` branch) are reused verbatim (§15). The `ZohoUploadRequest`
wrapper is flow-internal (decision 3).

- **Deviation:** none — §15 reuse.
- **Why:** the existing contracts already cover upload results, audit, and
  workflow state; no new schema is needed.

### 14. One stdlib-only offline self-test with a custom graph walker (no interrupt)

A single stdlib-only offline self-test ships per the CLAUDE.md self-test
convention: `zoho_upload_flow_selftest.py` (162 checks over the flow's pure
functions + end-to-end graph). It stubs `lfx`/`langgraph` so it runs on the host
without the in-image venv, injects a controllable `ScenarioStub` transport via
`set_transport` (scenario map by `invoice_id` → success/duplicate/
transient-then-success/hard-4xx/auth-401/all-transient-exhausted), and sets
`c._SLEEP = lambda s: None` so the §10 backoff is instant. The custom
`_Compiled` walker drives the stub graph on `state.status` via the router fn +
path map (unknown status falls back to `respond`), reconstructing the frozen
dataclass after each node — **no interrupt walker needed** (no `interrupt` in
this flow). It is picked up by `make test` and CI via
`scripts/zoho-upload-flow.selftest.sh`.

- **Deviation:** none — the project self-test convention; the custom walker is
  an internal testability decision recorded here.
- **Why:** the upload/retry/rollback round-trip is the core behavior; the stub
  transport + injectable `_SLEEP` make every §10 path exercisable offline.

## Build-phase (not done here)

1. **Implement `ZohoBooksARTool.create_invoice` / `delete_invoice`** — OAuth
   refresh-on-401 (mirror `ZohoBooksAPTool._refresh_access_token`/
   `_make_request`) + POST (create) + DELETE (rollback); `set_transport(RealZoho())`;
   live-test the upload+retry+rollback round-trip against a Zoho Books sandbox.
2. **Supervisor ↔ `ar_issue_invoice` integration** — live-test the supervisor
   resume-path delegating an approved `issue invoice` intent into this subflow
   via `RunFlow` (the `approval_ref` from the supervisor's `_node_gate` flowing
   into the subflow's `user_input` wrapper). Not exercisable offline.
3. **`ValidationEngineComponent` wiring** for `InvoiceData` / `ZohoUploadResult`
   (replace the inline hand-rolled `_validate_invoice`). The canonical schema
   files remain the source of truth; the self-test keeps the validator in sync.
4. **Durable Postgres checkpointer** — swap `InMemorySaver` for
   `langgraph-checkpoint-postgres` (shared with the supervisor — ADR-0003
   build-phase; this flow follows for free).
5. **`ar_invoice_generation` → `ar_issue_invoice` hand-off** — wire the draft
   `InvoiceData`/`zoho_upload` from `ar_invoice_generation` (#15) into this
   flow's `ZohoUploadRequest.invoices` (today the caller assembles the wrapper).
6. **Adapter upload-result rendering** — surface `data.batch_summary` +
   per-invoice `ZohoUploadResult` (success/duplicate/rolled-back) readably in
   LibreChat.
7. **Import** the fifteen subflows (incl. the now-wired `ar_issue_invoice.json`)
   + `supervisor.json`; open the supervisor flow so `RunFlow(ar_issue_invoice)`
   resolves `flow_id_selected`; `docker compose restart langflow`;
   `docker exec langflow python -m lfx extension validate /app/extensions/ar_common`.