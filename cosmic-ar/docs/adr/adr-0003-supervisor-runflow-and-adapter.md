# ADR 0003 — Supervisor implementation: RunFlow nodes, MemorySaver fallback, and adapter file/approval forwarding

- **Status:** Accepted
- **Date:** 2026-07-06
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none (extends [0001](adr-0001-supervisor-and-checkpointer.md))
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §4/§8/§9/§10/§11/§14/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §1/§3/§5/§7/§11,
  [supervisor](../supervisor.md)

## Context

ADR-0001 set the supervisor's shape (custom `lfx` `SupervisorAgentComponent` +
LangGraph `StateGraph[AgentState]` + Postgres checkpointer) and the build-phase
follow-ups. This ADR records the four concrete decisions made when that shape
was implemented (the supervisor component, the wired `supervisor.json` canvas,
and the OpenAI adapter extension) and the small deviations from the
architecture/plan wording they required.

## Decisions

### 1. RunFlow (modern), not FlowTool (legacy), as the subflow-as-tool node

The supervisor flow wires each of the nine subflows to the supervisor's `tools`
HandleInput via a **`RunFlow`** node (`data.type="RunFlow"`,
`lfx.components.flow_controls.run_flow.RunFlowComponent`), each carrying
`flow_name_selected` set to the subflow name and `flow_id_selected=null`
(LangFlow resolves the id at runtime after the subflow is imported). The legacy
`FlowTool` (`lfx.base.tools.flow_tool.FlowTool`) is deprecated in 1.10.1.

- **Deviation:** architecture §1/§3 says "Flow as Tool" / "FlowToolComponent".
  `RunFlow` is the modern replacement and is what the 1.10.1 canvas and
  `flow_builder_tools` emit.
- **Why:** future-proofing and matching the installed package's actual node
  (the legacy node's `update_build_config` path is unmaintained).
- **Import order:** import the nine subflows **first** (so each `RunFlow` node's
  `flow_id_selected` can resolve), then `supervisor.json`; record the supervisor
  UUID into `LANGFLOW_ADAPTER_FLOW_IDS`.

### 2. In-image `InMemorySaver` now, durable Postgres still build-phase

The supervisor compiles its graph with `InMemorySaver()` keyed by `session_id`
(the adapter forwards LibreChat's `conversationId` → LangFlow `session_id`).
This is the §11 **fallback**: non-durable (a `langflow`/`langflow-worker` recreate
loses in-flight checkpoints), but it makes resume work end-to-end today with
**zero** `docker-compose.yml`/`Dockerfile`/`.env`/`gen-secrets.sh` provisioning.

- **Deviation:** ADR-0001 chose a Postgres-backed saver as the primary. The
  durable saver remains the target — it is explicitly a **build-phase** step:
  provision the `ar_agent` DB
  ([`docker/postgres/init/02-ar-agent-db.sh`](../../../docker/postgres/init/02-ar-agent-db.sh)),
  bake `langgraph-checkpoint-postgres` into `docker/langflow/Dockerfile`, and
  wire `AR_AGENT_DB_*` onto the `langflow` service in `docker-compose.yml`.
- **Why:** keeps `make validate`/CI green now (the user's binding constraint:
  no platform edits in this task) while the §11 caveat (Langfuse tracing OFF,
  checkpoint is the source of truth for resume) is satisfied by the in-image
  saver for the dev/preview path.
- **Risk:** in-flight runs don't survive a worker recreate. Mitigation: the
  build-phase Postgres upgrade; the checkpoint id is surfaced in the
  `ExecutionSummary` so a lost run is observable, not silent.

### 3. Extend `AgentState` with the orchestration fields §7/§9 require

The scaffold `agent_state.py` defined only the financial/audit fields. The
supervisor's LangGraph conditional edges (`classify` → `respond` on fail;
`gate` → `respond` on reject) and the §9 error node need a `status` lifecycle
and an `error` record, and the `ExecutionSummary` needs `started_at`/`ended_at`.
The architecture's own §7 state-lifecycle diagram enumerates exactly these
statuses (`created → routed → executing → awaiting_approval → completed/failed`
+ `pending_approval`), and the §11 checkpoint table has a `status` column — so
the scaffold was incomplete relative to the design.

- **Change:** added `status` (`str`, default `"created"`), `error`
  (`Optional[dict]`, default `None`), `created_at`/`updated_at` (`str`, default
  `""`) — all defaulted, so every existing positional constructor
  (`AgentState(trace_id, flow_id, tenant, intent)`) and reader is unaffected
  (backward compatible). `AgentState` stays a frozen dataclass (§8).
- **Not a waiver:** this aligns the scaffold with architecture §7, it does not
  deviate from a binding standard.

### 4. Generate `AR_AGENT_DB_PASSWORD` in `gen-secrets.sh` now

The earlier AR scaffold added `AR_AGENT_DB_PASSWORD=__GENERATED__` to
`.env.example` but deferred the `gen-secrets.sh` wiring (per
[`cosmic-ar/README.md`](../README.md) build-phase step 2). That left
`make test` red (`gen-secrets.selftest.sh` + `install.selftest.sh` both assert
"no `__GENERATED__` placeholders remain").

- **Change:** one line in `scripts/gen-secrets.sh` —
  `ensure AR_AGENT_DB_PASSWORD rand_b64url 24` — mirroring the existing
  per-service DB passwords.
- **Scope:** the user's binding exclusion covered `docker-compose.yml`,
  `docker/langflow/Dockerfile`, and `.env.example` — **not** `gen-secrets.sh`.
  `cosmic-ar/README.md` build-phase step 2 prescribes exactly this line. The
  change only generates the secret so the placeholder is replaced; it does
  **not** wire the var onto the `postgres` service or provision the DB (those
  stay build-phase). `make test` went 2 failures → 273/273 green.

### 5. Adapter forwards uploaded files + surfaces pending_approval

`docker/langflow-adapter/adapter.py` (stdlib only, zero-dependency) gains:

- **File forwarding:** `extract_files(messages)` decodes OpenAI/LibreChat file
  parts (LibreChat `attachments`, `image_url`, `input_image`/`output_image`,
  `input_file`/`file`) from data URLs (remote URLs are deliberately **not**
  fetched — no SSRF surface). `upload_files(flow_id, files)` POSTs each as
  multipart to `/api/v1/files/upload/{flow_id}` and returns the `file_path`
  list. `run_flow`/`run_flow_stream` grow a `files` param added to the `/run`
  body. Best-effort: any file failure logs to stderr (filename + HTTP status,
  no PII, no file contents) and continues text-only — a file hiccup never
  blocks the chat.
- **Approval surfacing:** `parse_envelope(text)` best-effort-parses the §14
  envelope from the flow output; when `status=="pending_approval"`,
  `render_approval` produces a human-readable prompt ("Reply `approve <ref>`
  / `reject <ref>`") and a structured `x_cosmic_approval` object
  (`{status, approval_ref, action, checkpoint_id}`). The non-stream
  chat-completions path attaches it as a top-level `x_cosmic_approval` field;
  the non-stream Responses path attaches it under `response.metadata`.
  `detect_approval_reply(user_text)` (regex `ar-approval-<uuid>`) is the
  symmetric counterpart for debug logging — when a human replies with a ref,
  the adapter forwards the text unchanged and the supervisor's resume path
  detects the ref and resumes the paused checkpoint via `session_id`.
- **Out of scope here:** a LibreChat-side approval-button plugin (the adapter
  emits the metadata for it), and rewriting the **streaming** path's raw JSON
  into a friendly approval prompt (the streamed supervisor output is the
  envelope JSON; non-stream paths render it cleanly today — streaming
  approval rendering is a build-phase enhancement).

### 6. Deterministic classify/route (v1), LLM is a documented hook

`classify_intent` is a keyword/intent router (no LLM call, no API key required)
so the supervisor runs end-to-end today. The `model_name` input is retained as
the pluggable hook for an LLM-driven classifier at build phase (constitution
§4 design principle 3 still holds: low confidence → `AR_UNCERTAIN`, fail safe).

## Live-testing findings (post-deploy)

The supervisor flow was deployed to the live stack (`algomotiveai.com`,
LangFlow 1.10.1) and exercised end-to-end through the OpenAI adapter. Live
testing surfaced five real corrections — recorded here so the canonical
behavior matches the runtime, not the design's assumptions. After these, the
supervisor classifies, routes, and returns a proper §14 envelope through the
adapter (`/v1/models` lists it; `/v1/chat/completions` routes to it).

### 7. RunFlow's tool output is `component_as_tool`/`to_toolkit`, not `api_build_tool`

`supervisor.json`'s 10 `RunFlow` nodes were serialized with output
`{name:"api_build_tool", method:"api_build_tool"}`. That output name belongs
to the **`FlowTool`** component (`lfx.base.tools.flow_tool`), not `RunFlow`.
`RunFlow` exposes its tool via the base `Component._build_tool_output()`:
`Output(name="component_as_tool", display_name="Toolset", method="to_toolkit",
types=["Tool"])` (`TOOL_OUTPUT_NAME` in `lfx.base.tools.constants`). At run
time LangFlow called `self.api_build_tool()`, which resolved to the `Output`
field object → `"'Output' object is not callable"` (HTTP 500 at every
supervisor turn). Fixed by rewriting each RunFlow node's `outputs[0]` to
`{name:"component_as_tool", method:"to_toolkit", display_name:"Toolset"}`,
`selected_output="component_as_tool"`, and each RunFlow→supervisor edge's
`sourceHandle.name` to `component_as_tool`. `flow_id_selected=null` is fine —
`get_flow_by_id_or_name` resolves the subflow by `flow_name_selected`.

### 8. The supervisor canvas's standalone File node crashes text-only turns → removed

`supervisor.json` had a `File` node (`File-ar001`, display "Read File") wired
`File.message → SupervisorAgentComponent.files`. LangFlow's `File.process_files`
raises `ValueError("No files to process.")` when its file list is empty, and
the node runs unconditionally on every graph run — so every text-only supervisor
turn crashed *before* the supervisor logic ran. The supervisor's `files`
HandleInput is `required=False` and `classify_intent` handles the no-file case;
in v1 no file reaches the supervisor via the chat path anyway (adapter file
forwarding is a no-op against the simplified run API — see [ADR-0004 §8](adr-0004-file-intake-flow.md#live-testing-findings-post-deploy)),
and file intake is done via the dedicated `ar_file_intake` flow. Removed the
`File` node + its edge (canvas 14→13 nodes, 13→12 edges). The `files` input is
now unconnected (None); the file-only classify branch is dormant in v1.

### 9. Conditional edges return `state.status` (path-map keys), not node names

`_after_classify` returned `"respond"`/`"route"` and `_after_gate` returned
`"respond"`/`"invoke"` (node names), but their `add_conditional_edges` path
maps are keyed by status — `{"failed":"respond","routed":"route"}` and
`{"failed":"respond","executing":"invoke"}`. A node-name return value
`KeyError`s against the status-keyed map → `KeyError('respond')`. Fixed by
`return state.status` (the path-map keys are the node success statuses, the
same pattern as the File Intake Flow's `_after_*` functions — ADR-0004).

### 10. `graph.get_state(config).values` is a plain dict → reconstruct `AgentState`

LangGraph 1.2.6's `get_state(config).values` returns a plain dict, not the
typed dataclass (nodes receive the reconstructed dataclass; the snapshot does
not). `_finalize_envelope` read `state.trace_id`/`state.matched_amount`/
`state.pending_approvals` as typed fields → `'dict' object has no attribute
'trace_id'`. Fixed by a `_to_agent_state(vals)` helper that rebuilds the
`AgentState` dataclass from the dict, filtering to known fields so a stray
key never breaks construction (`AgentState(**{k:v for k,v in vals if k in
known_fields})`). Same pattern as the File Intake Flow's `_state_to_dict`
(ADR-0004 §11) — both orchestrators now snapshot-via-dict-reconstruction.

### 11. Adapter `input_type: "any"` (not `"text"`) so chat messages reach `ChatInput`

The adapter's `run_flow`/`run_flow_stream` posted `input_type: "text"` to the
simplified `/run/{flow_id}` API. LangFlow filters input vertices by type:
`INPUT_TYPE_COMPONENT_TYPES = {'chat': {'ChatInput'}, 'text': {'TextInput'}}`
(`lfx.graph.graph.base`). The AR flows use **`ChatInput`**, so `"text"`
silently dropped the user's message — the ChatInput kept its template default
(`"Hello"`) and the supervisor classified `"Hello"` → `AR_UNCERTAIN` on every
turn. Fixed by sending `input_type: "any"` (reaches both `ChatInput` and
`TextInput` — robust for the generic bridge; the AR flows' `ChatInput` is
included). After the fix, "fetch the outstanding invoices for CUST-001" →
`intent='ar_fetch_invoices'`, `subflows_invoked=['ar_fetch_invoices']`, and
"show me the AR aging report" → `intent='ar_reporting'`.

### 12. Empty scaffold subflows produce no RunFlow tools → `AR_NOT_FOUND` (build-phase)

With the above fixed, a real AR intent classifies and routes correctly, then
the supervisor reports `AR_NOT_FOUND "Subflow '…' is not wired on the canvas."`.
This is the expected build-phase state: the nine subflows are empty scaffold
canvases (`data={nodes:[],edges:[]}`), and `RunFlow._get_tools` returns `[]`
when the selected flow has no tool-mode input fields (`if not tool_mode_inputs:
return []`). So the supervisor's `tools` dict is empty and the routed subflow
isn't found. The supervisor reports this **gracefully** (a §14 `error` envelope,
not a crash). Populating the scaffolds with minimal tool-able flows (a
ChatInput with a tool-mode input + a stub component returning
`AR_NOT_IMPLEMENTED`) is part of the subflow-implementation phase, not the
RunFlow wiring — tracked as a build-phase follow-up.

## Consequences

- Positive: the supervisor is a real LangGraph graph (not a placeholder);
  resume works today; uploaded files reach the flow; a human is asked to
  approve financial mutations; `make test`/`make validate`/CI stay green.
- Negative: in-memory checkpoints are non-durable until the build-phase
  Postgres upgrade; streaming approval UX is raw JSON until the build-phase
  rewrite.
- Build-phase follow-ups: (a) Postgres checkpointer (provision `ar_agent` DB,
  bake `langgraph-checkpoint-postgres`, wire `AR_AGENT_DB_*` onto `langflow`);
  (b) import the 9 subflows + `supervisor.json`, resolve `flow_id_selected`,
  set `LANGFLOW_ADAPTER_FLOW_IDS`; (c) optional LLM classifier behind
  `model_name`; (d) LibreChat approval-button plugin consuming
  `x_cosmic_approval`; (e) streaming approval rendering.