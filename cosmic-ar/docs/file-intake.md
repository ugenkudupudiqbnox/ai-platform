# File Intake Flow (`ar_file_intake`)

The **File Intake Flow** is the 10th AR subflow (architecture §4 row 10;
[ADR-0004](adr/adr-0004-file-intake-flow.md)). It accepts an uploaded
Excel/CSV/PDF, identifies its report type, extracts metadata, validates it,
builds a `DocumentManifest`, updates workflow state, and returns structured
JSON — with logging (§12), retries (§10), and checkpoints (§11). It is the
**single stateful orchestrator** for file ingestion, mirroring the supervisor:
its responsibilities map to LangGraph nodes inside one `lfx` component,
`FileIntakeFlowComponent`.

Cross-links: [constitution](../../docs/cosmic-ar-constitution.md)
§4/§8/§9/§10/§11/§12/§15/§16, [architecture](../../docs/cosmic-ar-architecture.md)
§4/§5, [ADR-0004](adr/adr-0004-file-intake-flow.md),
[ADR-0002](adr/adr-0002-reusable-component-library.md).

## Component & bundle

- **Orchestrator (AR-specific):**
  [`docker/langflow-extensions/ar_common/components/ar_common/file_intake.py`](../../docker/langflow-extensions/ar_common/components/ar_common/file_intake.py)
  — `FileIntakeFlowComponent` (internal LangGraph `StateGraph[FileIntakeState]`
  + `InMemorySaver`).
- **Reused generic parts (`cosmic_common`, §15):**
  [`csv_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/csv_reader.py),
  [`excel_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/excel_reader.py),
  [`pdf_reader.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/pdf_reader.py),
  [`document_classifier.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/document_classifier.py),
  [`validation_engine.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/validation_engine.py).
- **Flow JSON:** [`flows/ar_file_intake.json`](../flows/ar_file_intake.json).

## Responsibilities → LangGraph nodes

| Responsibility | Node | Behavior |
|---|---|---|
| Accept uploaded files | `ingest` | Bind `trace_id`/`flow_id`/`tenant` + timestamps; carry file refs in **context** (not state — §8). |
| Identify report types | `detect_type` | Dispatch by extension (`.xlsx`/`.xls`→excel, `.csv`→csv, `.pdf`→pdf); unknown → `AR_UNCERTAIN` (§4). |
| Read Excel/CSV/PDF | `read` | Instantiate the matching `cosmic_common` reader, call its output method inside the §10 retry/backoff loop, parse its §14 envelope. Read-only tier ⇒ exhausted → `error` (not `pending_approval`). |
| Extract metadata | `extract_metadata` | Classify via `DocumentClassifier` (§15); deterministic rules over rows/content → `customer_ref` (id-only, §16), `amount` (2dp), `currency`, `posted_at`, `source_ref`. Below `MIN_CONFIDENCE` → `AR_UNCERTAIN`. |
| Validate files | `validate` | Per-document field validation via `validate_document` (cosmic_common, §15). Invalid → `AR_VALIDATION` with per-field errors. |
| Build Document Manifest | `build_manifest` | Assemble `DocumentManifest` (manifest_id, documents, `totals{count,sum}` — `sum` = Σ amounts to 2dp, `source_systems`, `period`, `generated_at`, `contract_version`). |
| Update Workflow State | (envelope) | The manifest is returned in the envelope `data.manifest`; `audit_refs` carries the manifest id. The supervisor merges `audit_refs` into `AgentState`; the manifest is **not** added to `AgentState` (ADR-0004 §4). |
| Return structured JSON | `respond` | Build the §14 envelope `{"status":"ok","code":"AR_OK","data":{"manifest":...,"audit_refs":[...]},...}`. |
| Implement logging | `run()` boundary | §12 structured `key=value`: `trace_id`/`flow_id`/`tenant`/`ar_entity=intake`/`event`/`outcome`/`code`; no PII/secrets. |
| Implement retries | `read` | §10 loop (3 attempts, exp backoff 1s·2^n ±25% jitter ≤30s; non-transient = file-not-found/corrupt → `AR_VALIDATION`, no retry). |
| Implement checkpoints | `checkpoint` node + `InMemorySaver` | Records the manifest id as the audit ref; `InMemorySaver` persists state (non-durable v1 — ADR-0004 §7). |

Graph edges: `START → ingest → detect_type → read → extract_metadata → validate
→ build_manifest → checkpoint → respond → END`, with conditional short-circuits
to `respond` on any `failed` status (§4/§9).

## Canvas wiring (4 nodes / 3 edges)

`ar_file_intake.json` wires:

- `ChatInput.message → FileIntakeFlowComponent.user_input`
- `File.message → FileIntakeFlowComponent.files` (types `["Data","Message"]` → `["Data"]`)
- `FileIntakeFlowComponent.intake_output → ChatOutput.input_value`

The component's full source is embedded as `template.code.value` (the node
imports the bundle's installed `FileIntakeFlowComponent`).

## Inputs / output

- **Inputs:** `user_input` (MessageTextInput, optional — carries intent
  keywords), `files` (HandleInput, `is_list`, `input_types=["Data"]` — uploaded
  file refs), `model_name` (MessageTextInput — documented LLM hook; deterministic
  v1 ignores it).
- **Output:** `intake_output` (Message) — the §14 envelope JSON.

## Run / resume behavior

`run()` (the only `lfx` entry point; **never raises** — §5/§9, catches at the
boundary → `AR_UNEXPECTED` envelope) builds the `FileIntakeContext`, compiles +
caches the graph once, invokes it with
`config={"configurable":{"thread_id":session_id}}`, reads the final state, and
emits the envelope. Resume is keyed by `session_id` (the adapter forwards
LibreChat's `conversationId` → `session_id`). Intake is read-only and synchronous
with no approval pause, so the §11 durable-resume value is low for intake
specifically (ADR-0004 §7).

## The manifest-in-envelope design

The `DocumentManifest` is returned in `data.manifest`, **not** added to
`AgentState` (no schema change — ADR-0004 §4). The supervisor's `_node_invoke`
already merges `data.audit_refs` into `AgentState` and ignores unknown `data`
keys. v1 treats intake as a terminal read-only run that returns the manifest to
the user; forwarding the manifest into a follow-on match/reconcile run is a
later task.

## Deterministic extraction rules (v1)

- **`source`**: explicit `source`/`system` column > keyword heuristic
  (`foodics`/`pos receipt` in content → `foodics`) > default `zoho`.
- **`source_ref`**: `invoice_number`/`receipt_no`/`reference`/`ref`/`doc_no`/`id`
  column > filename stem.
- **`customer_ref`**: `customer_id`/`customer`/`cust_id`/`account_id` column >
  `CUST-UNKNOWN` (id-only — §16, no PII).
- **`amount`**: `amount`/`total`/`grand_total`/`balance_due`/`amount_due` column,
  normalised to a signed 2dp string (strips thousands separators + currency
  prefix); default `0.00`.
- **`currency`**: `currency`/`curr`/`ccy` column > default `USD` (must match
  `^[A-Z]{3}$`; non-ISO falls back to `USD`).
- **`posted_at`**: `posted_at`/`invoice_date`/`date`/`posted_date`/`txn_date`
  column → ISO-8601 UTC; date-only → midnight UTC; unparseable/absent →
  `utc_now()`.
- **`status`**: `status`/`state`/`invoice_status`/`payment_status` column >
  default `open`.
- **`fetched_at`**: always `utc_now()` (the moment the file was read).
- **`doc_type`**: from `DocumentClassifier` (rule-based keyword scoring, §15);
  below `MIN_CONFIDENCE` (0.6) → `AR_UNCERTAIN`.

## Validation

`cosmic_common.validation_engine.validate_document` checks each document's
fields against the `DocumentManifest` schema's required/enum/pattern rules
(`amount` 2dp, `currency` `^[A-Z]{3}$`, ISO timestamps, `doc_type`/`source`
enums, required non-empty strings) + `additionalProperties:false`. The full
`validate_document_manifest` adds the totals cross-check
(`sum == Σ amounts`, `count == len(documents)`) — structurally guaranteed by
`build_manifest` (which computes `sum` from the documents). Hand-rolled stdlib
(no `jsonschema` dep — ADR-0004 §5); the canonical schema file remains the
source of truth and the self-test keeps the validator in sync.

## Live testing (post-deploy, `algomotiveai.com` / LangFlow 1.10.1)

Deployed and exercised end-to-end across CSV / XLSX / PDF fixtures. Four real
integration corrections emerged — see [ADR-0004 §8–§11](adr/adr-0004-file-intake-flow.md#live-testing-findings-post-deploy)
for the full rationale. Operational summary:

- **Passing files into a `/run/{flow_id}` turn:** the simplified run API has no
  `files` field (`body["files"]` is silently ignored). Pass the upload's storage
  path as a **tweak** on the ChatInput node id:
  `{"tweaks":{"ChatInput-ar001":{"files":"<flow_id>/<name>"}}}`. The upload API
  returns `<flow_id>/<timestamp>_<basename>` (LangFlow prefixes a timestamp).
- **`.xlsx` via the human-facing run turn returns HTTP 500 at ChatOutput**
  (LangFlow's file-artifact validator rejects extensions outside
  `TEXT_FILE_TYPES`; `.xlsx` is not in that set). This is **not** a component
  defect — the manifest is built before ChatOutput runs. Verify `.xlsx` via the
  direct component test (`/tmp/ar-direct.py`, 15/15) or by targeting the
  component directly with `output_component:"FileIntakeFlowComponent-ar001"` +
  `output_type:"any"` (the supervisor's `RunFlow` path uses this shape and is
  unaffected). CSV and PDF pass the full run pipeline.
- **Classifier edits need a `docker compose restart langflow`** to take effect:
  the extensions dir is a read-only bind-mount (host edits are live on disk),
  but the imported module is cached in the gunicorn `sys.modules`.
- **Envelope shape:** the manifest nests under `data.manifest` (§14
  `additionalProperties:false`); do not place it at the envelope top level.

## Build-phase checklist (not done here)

1. **Rebuild the `langflow` image** so `openpyxl`/`pdfplumber` are available
   (`docker compose build langflow langflow-worker`) — CSV works without a
   rebuild; Excel/PDF do not.
2. **Import the fifteen subflows first** (incl. `ar_file_intake.json`,
   `ar_intercompany_sales.json`, `ar_kitchen_revenue.json`,
   `ar_foodics_processing.json`, `ar_calculation.json`, and
   `ar_invoice_generation.json`), then `supervisor.json`; open the supervisor
   flow so each `RunFlow` node (incl. the 10th/11th/12th/13th/14th/15th) resolves
   `flow_id_selected`.
3. Confirm `jsonschema` availability (or keep hand-rolled) when other contracts
   are implemented.
4. Swap `InMemorySaver` → Postgres saver for the supervisor's approval round-trip
   (ADR-0003 build-phase); intake follows for free.

## Validate (offline)

```bash
python3 -m py_compile docker/langflow-extensions/ar_common/components/ar_common/file_intake.py \
                     docker/langflow-extensions/cosmic_common/components/cosmic_common/{csv,excel,pdf}_reader.py \
                     docker/langflow-extensions/cosmic_common/components/cosmic_common/{document_classifier,validation_engine}.py
python3 -c "import json; json.load(open('cosmic-ar/flows/ar_file_intake.json'))"
bash scripts/file-intake.selftest.sh          # 88 pure-function checks
make validate                                 # compose config unaffected
```