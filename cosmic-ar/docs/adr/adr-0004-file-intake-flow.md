# ADR 0004 — File Intake Flow: a 10th subflow, openpyxl/pdfplumber Dockerfile dep, manifest-in-envelope

- **Status:** Accepted
- **Date:** 2026-07-06
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0003](adr-0003-supervisor-runflow-and-adapter.md); amends architecture §4)
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §4/§8/§9/§10/§11/§12/§15/§16/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [file-intake](../file-intake.md),
  [ADR-0002](adr-0002-reusable-component-library.md) (cosmic_common reuse + §15 waiver)

## Context

The supervisor already treats uploaded files as **opaque refs** — it never
parses them. Its `classify_intent` routed a *file-only* signal (an upload with
no recognisable intent keyword) to `ar_fetch_invoices` at confidence 0.4, below
`MIN_CONFIDENCE`, so an uploaded spreadsheet with no text keywords failed safe
with `AR_UNCERTAIN` and the file was never actually read. The architecture doc
enumerated exactly "Nine reusable LangFlow subflows" and none of them parsed an
uploaded file. This ADR records the seven decisions made when that gap was
closed with a dedicated **File Intake Flow** (`ar_file_intake`).

## Decisions

### 1. A new 10th subflow, amending architecture §4's "Nine reusable subflows"

`ar_file_intake` is added as a 10th subflow: a new flow JSON
([`cosmic-ar/flows/ar_file_intake.json`](../../flows/ar_file_intake.json)), a new
`RunFlow` node in `supervisor.json` (→ 14 nodes / 13 edges), and a row 10 in
architecture §4 (amended "Nine" → "Ten"). Per the constitution's Authority note,
the deviation from a binding standard (the architecture's "Nine") is recorded
here as a waiver + ADR.

- **Why:** intake is a distinct responsibility (parse → classify → validate →
  manifest) that does not belong on `ar_fetch_invoices` (which lists *outstanding
  Zoho invoices*, not user uploads). A dedicated flow keeps each subflow
  single-purpose and lets the supervisor route uploads to a read-only tier.

### 2. Single-orchestrator-component design, mirroring the supervisor (§15 reuse)

The File Intake Flow is one `lfx` component — `FileIntakeFlowComponent`
([`ar_common/file_intake.py`](../../../docker/langflow-extensions/ar_common/components/ar_common/file_intake.py))
— whose responsibilities map to LangGraph nodes inside it (8 nodes:
ingest → detect_type → read → extract_metadata → validate → build_manifest →
checkpoint → respond), exactly as the supervisor's responsibilities map to
nodes inside `SupervisorAgentComponent`. The **generic, reusable** parts are
implemented in `cosmic_common` (§15 reuse before authoring): the CSV/Excel/PDF
readers, the `DocumentClassifier`, and the `validate_document` /
`validate_document_manifest` validators. The orchestrator composes them.

- **Why:** §15 says reuse before authoring. The readers/classifier/validator are
  generic (useful beyond AR); the orchestrator is AR-specific. Splitting them
  keeps `cosmic_common` the reuse base for the AP extension (§20) too.
- **Deviation from §15:** cross-bundle import (`ar_common` →
  `cosmic_common.validation_engine` / `document_classifier`). Acceptable: both
  bundles are pip-installed into the same image venv, so
  `components.cosmic_common.*` is importable from `ar_common` exactly as
  `components.ar_common.agent_state` is importable from `supervisor.py`.

### 3. openpyxl + pdfplumber baked into the Dockerfile (authorized boundary break)

`docker/langflow/Dockerfile` gains `pip install openpyxl pdfplumber` (run as
root, before `USER 1000`). CSV reading uses stdlib `csv` and needs no dep.

- **Deviation:** ADR-0003's task kept `make validate`/CI green with **no**
  Dockerfile/compose/.env edits. This task deliberately breaks that "no
  Dockerfile edit" boundary (authorised here). `make validate` (compose config)
  is **unaffected** — the Dockerfile is not compose config. A `langflow` /
  `langflow-worker` **image rebuild** is required to exercise Excel/PDF reading
  at runtime (`docker compose build langflow langflow-worker`).
- **Why:** the readers need real parsing libraries; stdlib alone cannot read
  `.xlsx`/`.pdf`. The readers import these lazily, so the bundles import cleanly
  even before the rebuild, and CSV intake works immediately.

### 4. The manifest lives in the envelope `data.manifest`, NOT in `AgentState`

The File Intake Flow returns a §14 envelope with
`data.manifest = DocumentManifest` and `data.audit_refs = [manifest_id]`. The
supervisor's `_node_invoke` already merges `data.audit_refs` into `AgentState`
and ignores unknown `data` keys — so the manifest is carried in the envelope,
**not** added to `AgentState`. No `AgentState` schema change is made.

- **Why:** adding a `manifest` field to `AgentState` would be a schema change
  with cross-cutting impact (every subflow's state). The manifest is the intake
  run's **output artifact**, not orchestration state. Downstream subflows that
  need it receive it via run context or re-fetch (v1 limitation — see below).
- **Limitation:** the supervisor does not forward `data.manifest` to the next
  subflow automatically. v1 treats intake as a terminal read-only run that
  returns the manifest to the user; wiring the manifest into a follow-on
  match/reconcile run is a later task.

### 5. Hand-rolled validation (stdlib), not `jsonschema`

`cosmic_common.validation_engine.validate_document` /
`validate_document_manifest` embed the `DocumentManifest` schema's
required/enum/pattern rules + `additionalProperties:false` + the totals
cross-check as pure stdlib functions (regex + `Decimal`). No `jsonschema`
dependency, no schema file read at runtime.

- **Why:** the contracts dir is not mounted in the container, and `jsonschema`
  is not a confirmed in-image dep. A hand-rolled validator with the patterns
  embedded is self-contained, testable offline, and avoids a new runtime dep for
  one contract. The canonical schema file
  ([`cosmic-ar/contracts/schemas/document-manifest.schema.json`](../../contracts/schemas/document-manifest.schema.json))
  remains the source of truth — the validator is a faithful, in-sync mirror;
  drift is caught by the self-test's valid/invalid cases.
- **The other 13 contracts** stay `AR_NOT_IMPLEMENTED` from the validator — they
  belong to other subflows and will be implemented (jsonschema or hand-rolled)
  when those subflows are built.

### 6. File-only classify reroute `ar_fetch_invoices` → `ar_file_intake`

The supervisor's `classify_intent` file-only branch (an upload with no intent
keyword) now returns `ar_file_intake` @ 0.4 (was `ar_fetch_invoices`). It stays
below `MIN_CONFIDENCE` (0.6), so a bare file with no keyword still fails safe with
`AR_UNCERTAIN` (§4) — **unless** the user adds an "intake/upload/parse" keyword,
which clears `MIN_CONFIDENCE` (1.0) and routes to intake. `ar_file_intake` is
**read-only** tier (not added to `FINANCIAL_INTENTS`).

- **Why:** a bare upload has no business intent to act on (§4 fail-safe); an
  explicit intake request is the signal to parse. This preserves §4 while
  making the intake path reachable.

### 7. `InMemorySaver` v1; durable Postgres still build-phase (same §11 fallback)

The File Intake Flow compiles its graph with `InMemorySaver()` keyed by
`session_id`, the same §11 fallback as the supervisor (ADR-0003 §2). Intake is
read-only and synchronous with no approval pause, so there is no §11 pause
point that benefits from durable resume — a Postgres checkpointer would buy
~nothing for intake specifically. Durable Postgres (when provisioned) helps the
**supervisor's** approval round-trip; intake continues to use the in-image saver.

- **Risk:** an in-flight intake run doesn't survive a worker recreate. The
  checkpoint id is surfaced in the envelope so a lost run is observable. The
  build-phase Postgres upgrade lifts this for free (intake's graph already uses a
  checkpointer; swapping the saver is the build-phase step).

## Live-testing findings (post-deploy)

The flow was deployed to the live stack (`algomotiveai.com`, LangFlow 1.10.1,
auto-login) and exercised end-to-end across CSV / XLSX / PDF fixtures. Live
testing surfaced four real integration corrections — recorded here so the
canonical behavior matches what the runtime actually does, not what the design
assumed.

### 8. LangFlow's simplified `/run/{flow_id}` API has no `files` field — use tweaks

The simplified run API's `SimplifiedAPIRequest`
(`input_value/input_type/output_type/output_component/tweaks/session_id/user_id`)
has **no `files` field**. `body["files"]` is silently ignored — `simple_run_flow`
never forwards it to `vertex.build`. Files only reach a flow via the
`/build/{flow_id}/flow` endpoint or via a **tweak** on the ChatInput node's
`files` FileInput:

```json
{"input_value":"intake this file","input_type":"text","output_type":"text",
 "tweaks":{"ChatInput-ar001":{"files":"<flow_id>/<name>"}}}
```

The tweak key is the **node id** (`ChatInput-ar001`), not the display name. The
`files` value is the storage path (`{flow_id}/{name}`) returned by
`POST /api/v1/files/upload/{flow_id}` — LangFlow prepends a timestamp to the
filename, so the stored path is `{flow_id}/{timestamp}_{basename}`.

- **Component impact:** none — the component's `files` HandleInput receives the
  storage path either way. This finding is about **how the run API routes files
  to the canvas**, not the component contract.
- **Adapter impact (real production gap):** the OpenAI-compatible adapter
  ([`docker/langflow-adapter/adapter.py`](../../../docker/langflow-adapter/adapter.py))
  forwards LibreChat's uploaded files to `/api/v1/run/{flow_id}` via
  `body["files"]`. Because the simplified run API ignores that field, the
  adapter's file forwarding is a **no-op** for any flow that needs a file. This
  is harmless for the text-only supervisor, but it breaks any file-needing flow
  reached through the adapter (e.g. intake reached from a chat turn). The fix is
  to forward files as a ChatInput tweak keyed by node id (the same shape above),
  not as a top-level `files` body field. Tracked as a follow-up; the adapter's
  direct `/build` path is unaffected.

### 9. ChatOutput rejects `.xlsx` uploads — known LangFlow limitation, not a component defect

LangFlow's `ChatOutput` aggregates the uploaded file as a session artifact and
validates it through `lfx/utils/schemas.py`, which derives the file `type` from
the extension against `TEXT_FILE_TYPES + IMG_FILE_TYPES`. `.xlsx` / `.xls` are
**not** in that set (`TEXT_FILE_TYPES = [txt,md,mdx,csv,json,yaml,yml,xml,html,
htm,pdf,docx,py,sh,sql,js,ts,tsx]`), so ChatOutput raises
`"File type is required."` and the run API returns **HTTP 500 at ChatOutput —
after the File Intake component has already built its manifest**.

- **This is not a File Intake component defect.** The component produces its §14
  envelope (with the full `DocumentManifest`) before ChatOutput runs; ChatOutput
  fails on its own session-artifact aggregation, independent of the component.
- **Verification:** the `.xlsx` path is verified via the **direct component
  test** (`/tmp/ar-direct.py`), which instantiates `FileIntakeFlowComponent`,
  sets `.files=[<storage path>]`, calls `.run()`, and asserts the envelope's
  `data.manifest` — openpyxl extracts `INV-2001` / `2500.50` / `CUST-002` (15/15
  checks pass).
- **Run-API workaround:** target the File Intake node directly via
  `output_component: "FileIntakeFlowComponent-ar001"` with
  `output_type: "any"` — this returns the component envelope without building
  ChatOutput, sidestepping the file-type validation. This is the shape the
  supervisor's `RunFlow` subflow-as-tool invocation uses (it targets the custom
  component, not ChatOutput), so the supervisor path is **not** affected by this
  limitation — only the human-facing `/run/{flow_id}` turn is.
- **CSV/PDF** pass the full `/run/{flow_id}` pipeline (upload → ChatInput →
  component → graph → ChatOutput → envelope). PDF's `source_ref` reflects
  LangFlow's upload-renaming (the stored filename carries a timestamp prefix),
  not a component defect.

### 10. Classifier regex widened for underscore-headers and hyphenated IDs

The live fixtures use `invoice_number` / `invoice_date` (underscore = a word
char, so `\binvoice\b` does **not** match `invoice_number` — `\b` is between a
non-word and a word char) and `INV-1001` (hyphen, so `\binv\s*#?\d` does not
match `INV-1001`). `RULES["invoice"]` in
[`document_classifier.py`](../../docker/langflow-extensions/cosmic_common/components/cosmic_common/document_classifier.py)
was widened:

```python
"invoice": [
    (r"\binvoice\b", 2),
    (r"\btax\s+invoice\b", 3),
    (r"\binvoice[_\s]+(no|number|#|num|date|id)\b", 3),  # was \binvoice\s+(no|number|#)\b
    (r"\binv[-\s#]*\d", 2),                                # was \binv\s*#?\d
    (r"\bbill\s+to\b", 1),
    (r"\bsub[\s-]?total\b", 1),
    (r"\bamount\s+due\b", 2),
    (r"\bnet\s+total\b", 1),
    (r"\bP[Oo]\s*(number|no|#)\b", 1),
],
```

The self-test (`file_intake_selftest.py` §8) was extended with an
underscore-header case (`invoice_number` / `invoice_date` / `customer_id` /
`amount` / `currency`) asserting `invoice` clears `MIN_CONFIDENCE` (88/88, was
86/86).

- **Operational note:** classifier `.py` edits on the host are live in the
  container (the extensions dir is a read-only bind-mount), but the imported
  module is cached in the gunicorn `sys.modules` — run
  `docker compose restart langflow` to pick up a classifier change.

### 11. §14 envelope nests the manifest under `data` (not top-level)

The §14 envelope schema is `additionalProperties:false` with top-level keys
`status/code/data/trace_id/approval_ref` (plus optional `error`). The
`DocumentManifest` and audit refs belong **under `data`**, not at the envelope
top level — the supervisor merges `data.audit_refs` / `data.totals` from
`data`, and a top-level `manifest` key would violate the schema. The
component's `_finalize_envelope` builds:

```python
data = {"manifest": manifest, "audit_refs": audit_refs,
        "document_count": ..., "flow_id":..., "tenant":..., "started_at":...,
        "ended_at":..., "contract_version": "1.0.0"}
return {"status": status, "code": code, "trace_id": trace_id, "data": data,
        "error": err}   # error omitted when status=ok
```

Only the four §14 top-level keys are emitted (plus `error` on failure). The
earlier implementation placed `manifest` at the top level, which produced an
empty manifest in the merged state and a schema violation — corrected by
nesting under `data`.

## Consequences

- Architecture §4 is amended (Nine → Ten subflows); the §5 diagram gains a
  `route → file_intake` edge.
- `make validate`/CI stay green (no compose/.env/gen-secrets/postgres-init
  edits); the only infra edit is the Dockerfile, which is not compose config.
- A `langflow`/`langflow-worker` image rebuild is required before Excel/PDF
  intake works at runtime (CSV works without it).
- The manifest is not forwarded to follow-on subflows in v1 (see §4 limitation).
- Validation is hand-rolled for `DocumentManifest` only; other contracts are
  deferred.
- The OpenAI adapter's `body["files"]` forwarding is a no-op against the
  simplified run API — file-needing flows reached through the adapter need a
  ChatInput-tweak fix (§8). Harmless for the text-only supervisor; tracked
  follow-up.
- Human-facing `/run/{flow_id}` turns with `.xlsx` uploads return 500 at
  ChatOutput (LangFlow file-type validation), not at the component — the
  supervisor's `RunFlow` path is unaffected (§9). Verify `.xlsx` via the direct
  component test or via `output_component`.

## Build-phase checklist (not done here)

1. Rebuild the `langflow` image so openpyxl/pdfplumber are available
   (`docker compose build langflow langflow-worker`).
2. Import the ten subflows first (incl. `ar_file_intake.json`), then
   `supervisor.json`; open the supervisor flow so the 10th `RunFlow` resolves
   `flow_id_selected`.
3. Confirm `jsonschema` availability (or keep hand-rolled) when other contracts
   are implemented.
4. Swap `InMemorySaver` → Postgres saver for the supervisor's approval
   round-trip (ADR-0003 build-phase); intake follows for free.