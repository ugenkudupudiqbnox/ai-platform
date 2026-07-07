# Cosmic AR Supervisor — operational reference

The supervisor is the single stateful orchestrator. It owns an explicit LangGraph
`StateGraph[AgentState]` and drives the fifteen reusable AR subflows, which are
exposed to it as LangChain tools (one `RunFlow` node per subflow on the canvas).
This document is the operational companion to the implementation in
[`../docker/langflow-extensions/ar_common/components/ar_common/supervisor.py`](../docker/langflow-extensions/ar_common/components/ar_common/supervisor.py)
and the wired canvas in [`../flows/supervisor.json`](../flows/supervisor.json).

See the [constitution](../../docs/cosmic-ar-constitution.md) §8/§9/§10/§11/§14/§19
and [architecture](../../docs/cosmic-ar-architecture.md) §5/§7/§11. Decisions of
record live in [ADR-0001](adr/adr-0001-supervisor-and-checkpointer.md) and
[ADR-0003](adr/adr-0003-supervisor-runflow-and-adapter.md).

## Responsibilities → LangGraph nodes

The 11 responsibilities map to nodes inside `SupervisorAgentComponent`
(architecture §5: `ingest → classify → route → gate → invoke → respond`, with
`gate` pausing on approval via `interrupt()`):

| Responsibility | Node | Behavior |
|---|---|---|
| Accept uploaded files | `ingest` | Binds `trace_id` (minted), `flow_id`, `tenant`, timestamps; carries the `files` refs (from the canvas `File` node / adapter-uploaded files) into the run as **context** (not state — §8 keeps raw inputs out of the checkpoint). |
| Build document manifest | `ingest` | The file refs are the manifest seed; parsing is delegated to the readers inside the routed subflow (the supervisor holds references only). |
| Identify report types | `classify` | Deterministic keyword classifier (v1, no LLM key); below `MIN_CONFIDENCE` → `AR_UNCERTAIN` (§4 fail-safe) → `respond`. |
| Determine execution path | `route` | Maps intent → one of the fifteen subflow tools. On a resume (user text carries an `approval_ref`) records `ar_approval` so the gate's interrupt is reached. |
| Maintain LangGraph state | (graph) | `StateGraph[AgentState]`; nodes return fragments merged immutably (§8). Orchestration fields (`status`/`error`/`created_at`/`updated_at`) were added to `AgentState` to match architecture §7's lifecycle (see ADR-0003 §3). |
| Invoke child flows | `invoke` | Calls the selected `RunFlow` LangChain tool. **Never computes** financial amounts — delegates to the subflow. |
| Collect outputs | `invoke` | Parses the subflow's §14 envelope; merges `matched/outstanding/posted` totals, `audit_refs`, `pending_approvals` into state without recomputing (v1: single subflow per run ⇒ set, not accumulate). |
| Retry failures | `invoke` wrapper | §10 loop: ≤3 attempts, exponential backoff `1s·2^n` ±25% jitter (parity-based, deterministic) capped at 30s; no 4xx retry except 408/429; exhausted **financial** retry → `pending_approval` (never silent — §10). |
| Resume from checkpoints | `gate` + `run()` | §19 `interrupt()` pauses for approval; `run()` resumes with `Command(resume=approval_ref)` keyed by `session_id` (the adapter forwards LibreChat's `conversationId`). See [Checkpointing](#checkpointing--resume). |
| Never perform business calculations | (constraint) | No node does arithmetic on financial amounts; totals come from subflow envelopes (constitution §1 north star + §8). |
| Return Workflow Summary | `respond` / `run()` | Builds the `ExecutionSummary` (totals, `subflows_invoked`, `approvals`, `audit_refs`, `checkpoint_id`) in the §14 envelope. |

## Canvas wiring (`flows/supervisor.json`)

18 nodes, 17 edges:

```
ChatInput ──message──► SupervisorAgentComponent ──supervisor_output──► ChatOutput
RunFlow(ar_fetch_invoices) ──component_as_tool──┐
RunFlow(ar_fetch_receipts) ──component_as_tool──┤
RunFlow(ar_match_payments) ──component_as_tool──┤
RunFlow(ar_reconcile) ──component_as_tool────────┼─► SupervisorAgentComponent.tools
RunFlow(ar_dunning) ──component_as_tool────────┤
RunFlow(ar_post_gl) ──component_as_tool────────┤
RunFlow(ar_issue_invoice) ──component_as_tool──┤
RunFlow(ar_reporting) ──component_as_tool──────┤
RunFlow(ar_approval) ──component_as_tool──────┤
RunFlow(ar_file_intake) ──component_as_tool────┤
RunFlow(ar_intercompany_sales) ──component_as_tool┤
RunFlow(ar_kitchen_revenue) ──component_as_tool──┤
RunFlow(ar_foodics_processing) ──component_as_tool──┤
RunFlow(ar_calculation) ──component_as_tool──────┤
RunFlow(ar_invoice_generation) ──component_as_tool──┘
```

- Each node carries its full component source as `template.code.value`
  (LangFlow execs that to build the class — see `lfx/custom/eval.py`); the
  `RunFlow` nodes embed the real `lfx/components/flow_controls/run_flow.py`
  source, the `SupervisorAgentComponent` node embeds `supervisor.py` verbatim.
- Each `RunFlow` node's tool output is `component_as_tool` (method
  `to_toolkit`, display "Toolset") — LangFlow 1.10.1's `RunFlow` tool handle,
  NOT `api_build_tool` (that's the deprecated `FlowTool` component's output —
  see [ADR-0003 §7](adr/adr-0003-supervisor-runflow-and-adapter.md#live-testing-findings-post-deploy)).
- Each `RunFlow` node has `flow_name_selected` set to the subflow name and
  `flow_id_selected=null` (resolved at runtime after the subflow is imported).
- Edges use the exact `œ`-delimited handle strings LangFlow's own
  `lfx/graph/flow_builder/connect.py` emits.

## Run + resume behavior

`SupervisorAgentComponent.run()` (the only `lfx` entry point; **never raises**
per §5/§9 — it catches at the boundary and returns an `AR_UNEXPECTED` envelope):

1. Builds the run context (`user_input`, `files`, the wired `tools`, `actor`,
   `session_id`, `tenant`, `flow_id`) — raw inputs are **context**, not state.
2. Compiles the graph once with `InMemorySaver()` and caches it on the instance
   (so the same `session_id` thread resumes across runs on that instance).
3. If the user text carries an `approval_ref` **and** a checkpoint is paused at
   the gate, invokes `Command(resume=approval_ref)`; otherwise starts a fresh
   run with a new `AgentState`.
4. Reads `graph.get_state(config)`: if `.next` is non-empty the run is paused at
   the gate → emits a `pending_approval` envelope (§19); otherwise assembles
   the `ExecutionSummary` from the final state.
5. All logging is at the `run()` boundary (`event=supervisor.run outcome=…
   trace_id=… intent=…`); per-node `self.log` is build-phase (nodes are
   functions without `self`).

### The approval round-trip through the adapter (§19)

1. A financial mutation (`ar_post_gl`/`ar_issue_invoice`, or any unknown
   intent) hits the `gate`, which calls `interrupt(payload)` — the graph pauses,
   the checkpoint records the pending approval.
2. `run()` returns a `pending_approval` envelope with `approval_ref`.
3. The OpenAI adapter's `parse_envelope`/`render_approval` turn that into a
   human-readable prompt ("Reply `approve <ref>` / `reject <ref>`") plus a
   structured `x_cosmic_approval` object a future LibreChat plugin can render
   as a button.
4. The human replies with the ref in the same LibreChat conversation; the
   adapter forwards it unchanged (the same `conversationId` → `session_id`),
   the supervisor detects the ref, and `Command(resume=…)` continues the run
   past the gate to `invoke` → `respond`. One `approval_ref` authorizes exactly
   one idempotent action (§19, non-reusable).

## Checkpointing + resume

- **Now (v1):** in-image `InMemorySaver` keyed by `session_id`. Non-durable —
  a `langflow`/`langflow-worker` recreate loses in-flight checkpoints. This is
  the §11 fallback; the checkpoint id is surfaced in the `ExecutionSummary` so
  a lost run is observable, not silent.
- **Build phase (durable):** swap in `langgraph-checkpoint-postgres` against the
  `ar_agent` DB (`ar_checkpoints` table) — provision the DB
  ([`docker/postgres/init/02-ar-agent-db.sh`](../docker/postgres/init/02-ar-agent-db.sh)),
  bake the package into `docker/langflow/Dockerfile`, wire `AR_AGENT_DB_*` onto
  the `langflow` service. The §11 caveat holds either way: Langfuse tracing is
  OFF (`LANGFLOW_DEACTIVATE_TRACING=true`), so the checkpoint — not a span — is
  the source of truth for resume.

## Contracts emitted

- [`WorkflowState`](contracts.md) — the `AgentState` jsonb snapshot persisted in
  a checkpoint (build-phase).
- [`ExecutionSummary`](contracts.md) — the `data` of the §14 envelope returned
  by `respond`: `totals{matched,outstanding,posted}`, `approvals`,
  `audit_refs`, `checkpoint_id`, `subflows_invoked`, `status`, `code`.
- [`DocumentManifest`](contracts.md) — built from the `files` refs at `ingest`
  (references only; the readers in the routed subflow do the parsing).

## Build-phase checklist

- [ ] Provision the `ar_agent` DB + bake `langgraph-checkpoint-postgres` +
      wire `AR_AGENT_DB_*` onto `langflow` (durable checkpointer).
- [ ] Import the fifteen subflow placeholders (incl. the wired `ar_file_intake`,
      `ar_intercompany_sales`, `ar_kitchen_revenue`, `ar_foodics_processing`,
      `ar_calculation`, and `ar_invoice_generation`), then `supervisor.json`;
      open the supervisor flow so each `RunFlow` node resolves
      `flow_id_selected`.
- [ ] Set `LANGFLOW_ADAPTER_FLOW_IDS` (in `.env`) to the supervisor UUID.
- [ ] Optional: wire an LLM classifier behind `model_name` (deterministic v1
      needs no API key).
- [ ] LibreChat approval-button plugin consuming `x_cosmic_approval`.
- [ ] Streaming approval rendering (currently non-stream only).