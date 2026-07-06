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