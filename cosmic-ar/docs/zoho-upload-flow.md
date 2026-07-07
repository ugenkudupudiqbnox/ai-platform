# Zoho Upload Flow (`ar_issue_invoice`)

The **Zoho Upload Flow** is the 1st AR subflow (architecture §4 row 1;
[ADR-0011](adr/adr-0011-zoho-upload-flow.md)). It takes a
**validated-JSON `ZohoUploadRequest`** wrapper — an `approval_ref` (§1) plus a
batch of `InvoiceData` (the Invoice JSON from `ar_invoice_generation`) —
**validates** each invoice's mandatory fields, **uploads** each to Zoho Books
with §10 retry, **rolls back** (deletes) the already-created invoices when any
invoice in the batch fails (all-or-nothing), **stores** the Zoho invoice id +
upload timestamp per invoice, **logs** an `AuditRecord` per create/rollback
(§13), **updates** `WorkflowState`, and returns a per-invoice
`ZohoUploadResult` (+ batch summary) in the §14 envelope. It is the **single
stateful orchestrator** for Zoho invoice posting, mirroring the supervisor, the
File Intake Flow, the Intercompany Sales Flow, the Cosmic Kitchen Revenue Flow,
the Foodics Processing Flow, the Calculation Flow, the Invoice Generation Flow,
and the Human Approval Flow: its responsibilities map to LangGraph nodes inside
one `lfx` component, `ZohoUploadFlowComponent`.

It is the row that **POSTs** an invoice to Zoho Books — distinct from
`ar_invoice_generation` (#15, [ADR-0009](adr/adr-0009-invoice-generation-flow.md),
`read-only` tier, not in `FINANCIAL_INTENTS`), which only **generates a draft
"Zoho Upload File" artifact** for review. This flow is registered at tier
`approval` and is in `FINANCIAL_INTENTS` — money moves here, so §1 applies.

> **v1 uses a deterministic stub transport, not live Zoho.** A module-level
> `_TRANSPORT = StubZohoUpload()` returns deterministic result dicts
> (`zoho_id = f"zoho-inv-<uuid5(invoice_id)>"`) so the upload+retry+rollback
> round-trip is **offline-testable with no live Zoho, no credentials**. The real
> `ZohoBooksARTool.create_invoice`/`delete_invoice` (OAuth + POST/DELETE +
> 401-retry, mirroring `ZohoBooksAPTool`) is wired at **build-phase** via
> `set_transport(RealZoho())`; the flow code is unchanged (ADR-0011 §6).

## The §1 `approval_ref`-at-the-boundary design

Constitution §1: **no money moves without SSO-attributable approval.** The
supervisor's internal `_node_gate` captures §19 approval **before** delegating a
financial intent to this subflow (`ar_issue_invoice` is in `FINANCIAL_INTENTS`
at tier `approval`). Per [ADR-0011](adr/adr-0011-zoho-upload-flow.md) §4, **the
flow itself does NOT pause via `interrupt()`** — instead the `ingest` node
**requires** an `approval_ref` matching `^ar-approval-<uuid>$` in the
`ZohoUploadRequest` wrapper; missing/invalid → `AR_FORBIDDEN` (the run stops
before any upload). The `approval_ref` is echoed into every `AuditRecord` (§13
link — the attributable-approval record). This mirrors the
[ar_approval](approval-flow.md) standalone-surface precedent (ADR-0010): approval
**capture** is the supervisor's `_node_gate` / ar_approval's job; this flow
**executes the authorized, idempotent POST** and re-checks the proof at its own
boundary (no double-gate). There is **no `pending_approval` envelope branch** in
this flow.

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§1/§8/§9/§10/§11/§13/§14/§16/§19, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0011](adr/adr-0011-zoho-upload-flow.md),
[ADR-0010](adr/adr-0010-approval-flow.md),
[ADR-0009](adr/adr-0009-invoice-generation-flow.md),
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md),
[ADR-0002](adr/adr-0002-reusable-component-library.md),
[invoice generation](invoice-generation.md),
[supervisor](supervisor.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow.py`](../../docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow.py)
  — `ZohoUploadFlowComponent` (internal LangGraph
  `StateGraph[ZohoUploadState]` + `InMemorySaver`).
- **Flow JSON:** [`flows/ar_issue_invoice.json`](../flows/ar_issue_invoice.json).
- **Self-test:**
  [`zoho_upload_flow_selftest.py`](../../docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow_selftest.py)
  (162 stdlib-only pure-function + end-to-end checks) via
  `scripts/zoho-upload-flow.selftest.sh`.

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept inputs + §1 gate | `ingest` | Parse the validated-JSON `ZohoUploadRequest` from `user_input`; bind `trace_id` (request.`trace_id` else minted), `flow_id="ar_issue_invoice"`, `tenant` (request.`tenant` else `cosmic-vikings`), `approval_ref` (request.`approval_ref`), `created_at`/`updated_at`; carry `model_name` in **context** (not state — §8). **§1 gate:** missing/invalid `approval_ref` (not matching `^ar-approval-<uuid>$`) → `AR_FORBIDDEN`. Malformed JSON / non-object / missing `invoices` or empty → `AR_VALIDATION`. status="created". Router `_after_ingest`: `{failed:respond, created:validate}`. |
| Validate mandatory fields | `validate` | Inline hand-rolled validator for the wrapper + **each** `InvoiceData` against `invoice-data.schema.json` mandatory fields: required keys present, `customer_ref` non-empty (§16 no PII), money 2dp `^\d+\.\d{2}$`, `currency ^[A-Z]{3}$`, `issue_date`/`due_date ^\d{4}-\d{2}-\d{2}$`, `line_items` non-empty, each line item's 2dp fields, `status` enum. Collect per-invoice per-field errors. Any error → `AR_VALIDATION` with the structured error map (no upload attempted). status="validated". **Record checkpoint** `"validate"`. Router `_after_validate`: `{failed:respond, validated:upload}`. |
| Upload invoices (§10 retry) | `upload` | For each invoice: build `idempotency_key = _build_idempotency_key(tenant, invoice_id)` (deterministic `ar-idem:invoice_issue:<tenant>:<uuid5(invoice_id)>`, §10 replay-safe). Run `_upload_one` = §10 retry loop (≤3 attempts, exp backoff `1s·2^n` ±25% deterministic parity-based jitter ≤30s; retry only on transient = transport-flagged/408/429/5xx; **no 4xx retry** except 401 handled inside the real transport; `AR_OK`/`AR_DUPLICATE` stop immediately, `AR_DUPLICATE` is a safe replay). Each attempt calls `_TRANSPORT.create_invoice(invoice, idempotency_key)` → `{ok, http_status, code, zoho_id, zoho_ref, duplicate, transient}`. Capture `attempted_at = utc_now()` on the terminal attempt. After all invoices: status=`"uploaded"` (all `AR_OK`/`AR_DUPLICATE`) or `"partial"` (≥1 failed). **Record checkpoint** `"upload"`. Router `_after_upload`: `{partial:rollback, uploaded:store}`. |
| Rollback failed uploads | `rollback` | Only acts when status=`partial` AND ≥1 invoice has a `zoho_id` (was created). For each created invoice, call `_TRANSPORT.delete_invoice(zoho_id)` (best-effort §10 retry; if delete fails, record `rollback_code=AR_UPSTREAM` but still mark `rolled_back=true` so the batch is observably failed). Mark those enriched results `rolled_back=true`. If status=`uploaded` → no-op pass-through. **Record checkpoint** `"rollback"`. status="rolled_back" (or stays "uploaded"). Static edge → `store`. |
| Store result | `store` | Build the canonical `ZohoUploadResult` per invoice (operation=`invoice_issue`, `additionalProperties:false`). Build the enriched per-invoice view (canonical fields + `invoice_id`/`invoice_number`/`customer_ref`/`total`/`currency` + `rolled_back`/`rollback_code` + `approval_ref` echo). Build `batch_summary = {total, succeeded, failed, rolled_back, posted_total, status}`. **Record checkpoint** `"store"`. status="stored". |
| Log (§13) | `audit` | One `AuditRecord` per invoice **create** (`action="invoice.issue"`, `actor=ctx.actor` (Keycloak sub), `approval_ref` echo (§19 link), `idempotency_key`, `source_system="zoho"`, `source_ref=zoho_id`, `before={"status":"draft"}`, `after={"zoho_id", "status":"sent"|"failed", "code"}`, `append_only=true`) — logged for **every** attempted invoice. One `AuditRecord` per **rollback delete** (`action="invoice.rollback"`, `source_ref=zoho_id`, `before={"zoho_id"}`, `after={"status":"voided", "rollback_code"}`). Append to `audit_records` + `audit_refs`. **Record checkpoint** `"audit"`. status="audited". |
| Update Workflow State | `build_state` | `WorkflowState` snapshot: `status="completed"` (all succeeded) / `"failed"` (partial→rollback or all failed); `intent="ar_issue_invoice"`; `posted_total` = `_sum_2dp` of non-rolled-back invoice totals (2dp; `"0.00"` if all rolled back/failed); `matched_amount`/`outstanding_balance="0.00"`; `pending_approvals=[]` (approval captured at the boundary); `idempotency_keys={"invoice_issue:<invoice_id>": key, …}`; `audit_refs`; `contract_version`. Immutable (§8). **Record checkpoint** `"state"`. status="stated". |
| Checkpoint | `checkpoint` | Append the final aggregate `_audit_ref(trace_id,"ar_issue_invoice")`; reflect `audit_refs`+`checkpoints` into the WorkflowState snapshot. `InMemorySaver` persists state (§11 fallback, non-durable v1). |
| Return structured JSON | `respond` | `_finalize_envelope` builds `data={upload_results, zoho_upload_results, rollback_results, batch_summary, workflow_state, audit_records, audit_refs, checkpoints, flow_id, tenant, started_at, ended_at, contract_version}` and the §14 envelope `{"status":"ok","code":"AR_OK",…}` (or `{"status":"error","code":<err.code>,"error":<err>}` on `failed`). **No pending branch.** |
| Logging | `run()` boundary | §12 structured `key=value` via `self.log`: `event=zoho_upload.run outcome=… trace_id=… flow_id=… ar_entity=issue_invoice posted_total=… code=…`; failure boundary `code=AR_UNEXPECTED`. No PII/secrets (§16 — `customer_ref` is an id). |
| Never raises | `run()` boundary | §5/§9 — `run()` catches at the boundary and returns an `AR_UNEXPECTED` envelope; bad input → `AR_VALIDATION`/`AR_FORBIDDEN`/`AR_UNEXPECTED` envelope, not an exception. |
| Checkpoints after every step | each node + `checkpoint` | Continues ADR-0006/0007/0008/0009/0010's stricter pattern: each upload boundary records a labeled `_audit_ref` into `audit_refs` and a `checkpoints{<label>}` map (success path 6 labels: `validate`, `upload`, `store`, `audit`, `state`, `ar_issue_invoice`; partial path adds `rollback` = 7), persisted by `InMemorySaver` at each super-step (§11 — ADR-0011 §12). |

Graph edges: `START → ingest → validate → upload → rollback → store → audit →
build_state → checkpoint → respond → END`, with conditional short-circuits to
`respond` on any `failed` status (`_after_ingest`/`_after_validate` return
`state.status` against status-keyed path maps — ADR-0003 §9).
`_after_upload` routes `{partial:rollback, uploaded:store}`; `rollback → store`
is static (rollback is a no-op when nothing was created). No `interrupt`/`Command`
(no in-flow pause). Only ingest/validate can short-circuit; the downstream
nodes are pure compute/transport → static edges, unexpected errors caught at the
`run()` boundary.

## The `ZohoUploadRequest` input contract

The validated-JSON wrapper the flow consumes (the PRIMARY input via `user_input`,
flow-internal — not a new schema file):

```json
{
  "approval_ref": "ar-approval-12345678-1234-1234-1234-123456789abc",
  "invoices": [
    {
      "invoice_id": "INV-001",
      "invoice_number": "IG-CUST-42-a1b2c3d4",
      "customer_ref": "CUST-42",
      "tenant": "cosmic-vikings",
      "issue_date": "2026-07-07",
      "due_date": "2026-08-06",
      "line_items": [
        {"line_id": "L1", "item_ref": "ITEM-A", "description": "Catering", "qty": "10", "unit_price": "150.00", "amount": "1500.00"}
      ],
      "subtotal": "1500.00",
      "total": "1575.00",
      "currency": "SAR",
      "status": "draft",
      "balance_due": "1575.00",
      "contract_version": "1.0.0"
    }
  ],
  "trace_id": "trc-…",
  "tenant": "cosmic-vikings"
}
```

- `approval_ref` (required, `^ar-approval-<uuid>$` — §1). Missing/invalid →
  `AR_FORBIDDEN` (no upload).
- `invoices` (required, ≥1; **single invoice = a 1-element batch**). Each is an
  `InvoiceData` (§15 reuse — the Invoice JSON from `ar_invoice_generation`).
- `trace_id` / `tenant` (optional; `tenant` defaults to `cosmic-vikings`).

A **missing** `approval_ref` → `AR_FORBIDDEN`; a missing/empty `invoices` or a
malformed wrapper → `AR_VALIDATION`; a per-invoice mandatory-field failure →
`AR_VALIDATION` with the structured per-field error map (no upload attempted).

## The upload + retry + rollback design

- **Per-invoice §10 retry** (`_upload_one`): ≤3 attempts; backoff `1s·2^n` with
  deterministic ±25% **parity-based** jitter (+25% on even attempts, −25% on
  odd — no `Math.random`/`uuid4`, so the offline test is reproducible); the
  **final** delay capped at 30s. Retry only on **transient** results
  (transport-flagged, OR 408/429, OR 5xx); **no 4xx retry** (except 401, handled
  inside the real transport via token refresh — v1 stub treats 401 as hard
  `AR_AUTH`). `AR_OK`/`AR_DUPLICATE` stop immediately; `AR_DUPLICATE` is a
  **safe idempotent replay** (the invoice already exists — `duplicate=true`,
  `zoho_id` returned). Hard codes (`AR_AUTH`/`AR_VALIDATION`/`AR_FORBIDDEN`/
  `AR_NOT_FOUND`) stop immediately. Exhausted transient → `AR_UPSTREAM`
  (`attempts=3`). A module-level `_SLEEP` hook (default `time.sleep`) makes the
  backoff instant under the offline self-test.
- **All-or-nothing rollback**: after all uploads, if every invoice succeeded →
  status `uploaded` (skip rollback); if **any** invoice failed after retries →
  status `partial` → the **already-created** invoices (those with a `zoho_id`)
  are **rolled back** (deleted best-effort). No partial batch is left in Zoho.
  A batch where every invoice failed (nothing created) skips rollback but is
  still `failed`.
- **Idempotency key** = `ar-idem:invoice_issue:<tenant>:<uuid5(invoice_id)>`
  (deterministic — §10 replay-safe; matches the `zoho-upload-result` pattern).

## The stub transport vs build-phase real `ZohoBooksARTool`

A module-level `_TRANSPORT = StubZohoUpload()` + `set_transport(t)` seam:

- **v1 (this task):** `StubZohoUpload.create_invoice`/`delete_invoice` return
  deterministic result dicts (`zoho_id = f"zoho-inv-<uuid5(invoice_id)>"`). The
  self-test injects a controllable `ScenarioStub` via `set_transport` (scenario
  map by `invoice_id` → success/duplicate/transient-then-success/hard-4xx/
  auth-401/all-transient-exhausted). Offline-testable, no live Zoho, no
  credentials.
- **Build-phase:** `set_transport(RealZoho())` wraps
  `ZohoBooksARTool.create_invoice`/`delete_invoice` (OAuth refresh-on-401 +
  POST/DELETE, mirroring `ZohoBooksAPTool._refresh_access_token`/`_make_request`);
  the flow code is unchanged. Live-test the upload+retry+rollback round-trip
  against a Zoho Books sandbox.

## The canonical `ZohoUploadResult` + enriched per-invoice view

The `store` node builds the canonical `ZohoUploadResult`
(`zoho-upload-result.schema.json`, `additionalProperties:false`) per invoice:
`operation="invoice_issue"`, `http_status`, `code`, `idempotency_key`,
`zoho_id`, `zoho_ref`, `duplicate`, `attempted_at`, `attempts`, `trace_id`,
`tenant`, `contract_version`. The flow also emits an **enriched per-invoice
view** (`data.upload_results`) adding `invoice_id`/`invoice_number`/
`customer_ref`/`total`/`currency` + `rolled_back`/`rollback_code` + the
`approval_ref` echo — this is flow-internal (not the canonical contract).

Rollback **deletes** are **audit-only**: there is no `ZohoUploadResult`
operation enum value for delete, so a delete does not produce a canonical
result; it produces a `rollback_results` entry + an `AuditRecord`. The
`rolled_back` flag is carried in the enriched view, not in the canonical
contract (keeps `additionalProperties:false` without amending the schema —
ADR-0011 §8).

## Audit-logging design (§13)

One append-only `AuditRecord` per invoice **create** (`action="invoice.issue"`,
`actor` = Keycloak sub, `approval_ref` link (§19), `idempotency_key`,
`source_system="zoho"`, `source_ref=zoho_id`, `before={"status":"draft"}`,
`after={"zoho_id", "status":"sent"|"failed", "code"}`, `append_only=true`) —
logged for **every** attempted invoice (success, duplicate, or failure). One
per **rollback delete** (`action="invoice.rollback"`, `source_ref=zoho_id`,
`before={"zoho_id"}`, `after={"status":"voided", "rollback_code"}`). Both are
appended to `audit_records` + `audit_refs`. §1 north star: the audit record is
the attributable record of who authorized what; the rollback record is the
attributable record of the corrective delete.

## Canvas wiring (3 nodes / 2 edges)

`ar_issue_invoice.json` wires (modeled on `ar_invoice_generation.json`):

- `ChatInput.message → ZohoUploadFlowComponent.user_input`
- `ZohoUploadFlowComponent.zoho_upload_output → ChatOutput.input_value`

`ChatInput` and `ChatOutput` are copied verbatim from the Invoice Generation
canvas; the orchestrator node's full source is embedded as
`template.code.value` (LangFlow runs the embedded copy — it must stay in sync
with the on-disk `zoho_upload_flow.py`). There is **no `files` edge** — the 4th
subflow without one (after `ar_calculation` / `ar_invoice_generation` /
`ar_approval`).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, required, `tool_mode` — carries
  the `ZohoUploadRequest` JSON, the PRIMARY input), `model_name`
  (MessageTextInput, value `"glm-5.2:cloud"` — documented LLM hook; deterministic
  v1 ignores it). **No `files` HandleInput.**
- **Output:** `zoho_upload_output` (Message) — the §14 envelope JSON.

## The supervisor merge (no `AgentState` change)

The per-invoice results are not under `data.totals{matched,outstanding,posted}`,
so the supervisor's `_node_invoke` does not recognise them as financial totals.
The flow surfaces to the supervisor only via `subflows_invoked` + `audit_refs`;
the results stay in the envelope `data`. **No field is added to `AgentState`**
(ADR-0011 §2, mirrors ADR-0006/0007/0008/0009/0010). The supervisor resume-path
delegating an approved `issue invoice` intent into this subflow via `RunFlow`
(the `approval_ref` from the supervisor's `_node_gate` flowing into the
subflow's `user_input` wrapper) is a **documented build-phase integration item**
needing live LangGraph `Flow-as-Tool` testing (not exercisable offline).

## Contracts emitted

- [`ZohoUploadResult`](contracts.md) — `data.zoho_upload_results`, one per
  create, `operation="invoice_issue"`, `code` enum incl. `AR_OK`/`AR_DUPLICATE`/
  `AR_UPSTREAM`/`AR_AUTH`/`AR_VALIDATION`/`AR_FORBIDDEN`/`AR_NOT_FOUND`,
  deterministic `idempotency_key`. **No schema change** (§15 reuse).
- [`InvoiceData`](contracts.md) — the input (the Invoice JSON). **No schema
  change** (§15 reuse).
- [`AuditRecord`](contracts.md) — `data.audit_records`, `action="invoice.issue"`
  (create) / `"invoice.rollback"` (delete), `source_system="zoho"`,
  `source_ref=zoho_id`, `approval_ref` link. **No schema change** (§15 reuse).
- [`WorkflowState`](contracts.md) — `data.workflow_state`;
  `status="completed"`/`"failed"`; `posted_total` = Σ non-rolled-back totals;
  `intent="ar_issue_invoice"`. **No schema change** (§15 reuse).
- [`Envelope`](contracts.md) — §14 shape; `additionalProperties:false`; no
  `pending_approval` branch. **No schema change** (§15 reuse).
- **Flow-internal (no schema):** `data.upload_results` (enriched per-invoice
  view + `rolled_back`), `data.rollback_results`, `data.batch_summary`,
  the `ZohoUploadRequest` wrapper (ADR-0011 §3/§8).

## Validation

`ValidationEngineComponent` only implements `DocumentManifest` today. So the
orchestrator uses **inline hand-rolled validators** for the wrapper
(`_parse_request`/`_validate_request`) and `InvoiceData` (`_validate_invoice`) —
mirroring the File Intake / Intercompany / Kitchen / Foodics / Calculation /
Invoice Generation flows. Wiring `ValidationEngineComponent` for
`InvoiceData`/`ZohoUploadResult` is a documented build-phase step. The canonical
schema files remain the source of truth and the self-test keeps the validators
in sync (hand-rolled stdlib, no `jsonschema` dep).

## Build-phase checklist (not done here)

1. **Implement `ZohoBooksARTool.create_invoice`/`delete_invoice`** — OAuth
   refresh-on-401 (mirror `ZohoBooksAPTool._refresh_access_token`/`_make_request`)
   + POST (create) + DELETE (rollback); `set_transport(RealZoho())`; live-test
   the upload+retry+rollback round-trip against a Zoho Books sandbox
   (ADR-0011 §6).
2. **Supervisor ↔ `ar_issue_invoice` integration** — live-test the supervisor
   resume-path delegating an approved `issue invoice` intent into this subflow
   via `RunFlow` (the `approval_ref` from the supervisor's `_node_gate` flowing
   into the subflow's `user_input` wrapper). Not exercisable offline.
3. **`ar_invoice_generation` → `ar_issue_invoice` hand-off** — wire the draft
   `InvoiceData`/`zoho_upload` from `ar_invoice_generation` (#15) into this
   flow's `ZohoUploadRequest.invoices` (today the caller assembles the wrapper).
4. **Wire `ValidationEngineComponent`** for `InvoiceData`/`ZohoUploadResult`
   (replace the inline validators).
5. **Durable Postgres checkpointer** — swap `InMemorySaver` for
   `langgraph-checkpoint-postgres` (shared with the supervisor — ADR-0003
   build-phase; this flow follows for free).
6. **Adapter upload-result rendering** — surface `data.batch_summary` +
   per-invoice `ZohoUploadResult` (success/duplicate/rolled-back) readably in
   LibreChat.
7. **Import the nine subflows first** (incl. the now-wired
   `ar_issue_invoice.json`), then `supervisor.json`; open the supervisor flow so
   the `RunFlow(ar_issue_invoice)` resolves `flow_id_selected`;
   `docker compose restart langflow`; `docker exec langflow python -m lfx
   extension validate /app/extensions/ar_common`.

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow.py \
                     docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow_selftest.py \
                     docker/langflow-extensions/ar_common/components/ar_common/supervisor.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_issue_invoice.json'))"
bash scripts/zoho-upload-flow.selftest.sh     # 162 pure-function + end-to-end checks
make validate                                 # compose config unaffected
```