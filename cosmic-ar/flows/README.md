# Cosmic AR — Flow import files

The 10 JSON files here are **LangFlow export skeletons** for the Cosmic AR Agent:

| File | Flow |
|------|------|
| `supervisor.json` | `ar_supervisor` — the **wired** supervisor flow: `SupervisorAgentComponent` (real LangGraph) + ten `RunFlow` subflow-as-tool nodes + `File` + `ChatInput`/`ChatOutput` (14 nodes / 13 edges). See [docs/supervisor.md](../docs/supervisor.md) and [ADR-0003](../docs/adr/adr-0003-supervisor-runflow-and-adapter.md). |
| `ar_file_intake.json` | `ar_file_intake` — the **wired** File Intake Flow: `FileIntakeFlowComponent` (real LangGraph) + `File` + `ChatInput`/`ChatOutput` (4 nodes / 3 edges). The 10th subflow. See [docs/file-intake.md](../docs/file-intake.md) and [ADR-0004](../docs/adr/adr-0004-file-intake-flow.md). |
| `ar_fetch_invoices.json` | `ar_fetch_invoices` |
| `ar_fetch_receipts.json` | `ar_fetch_receipts` |
| `ar_match_payments.json` | `ar_match_payments` |
| `ar_reconcile.json` | `ar_reconcile` |
| `ar_dunning.json` | `ar_dunning` |
| `ar_post_gl.json` | `ar_post_gl` |
| `ar_issue_invoice.json` | `ar_issue_invoice` |
| `ar_reporting.json` | `ar_reporting` |
| `ar_approval.json` | `ar_approval` |

> **Flows live in the LangFlow Postgres DB, not on disk** (constitution §7).
> These files are **import artifacts** — `supervisor.json` and
> `ar_file_intake.json` are fully wired (their nodes embed the real component
> sources, so they import as working canvases); the nine business-subflow
> skeletons are empty-graph placeholders you import and then wire to the bundled
> components. None are auto-loaded by the `langflow` container.

## Import order / `flow_id` resolution

The ten `RunFlow` nodes in `supervisor.json` reference each subflow by
`flow_name_selected` (e.g. `ar_fetch_invoices`, `ar_file_intake`) with
`flow_id_selected=null`, which LangFlow resolves at runtime **after** the
subflow is imported. So:

1. Import the **ten subflows first** (the nine placeholders, then wire each to
   the bundled `ar_common`/`ar_tools` components per the architecture's Flow
   Diagram; `ar_file_intake.json` is already wired — import it as-is).
2. Import `supervisor.json` **last**, then open it so each `RunFlow` node
   resolves its `flow_id_selected`.
3. Record the supervisor flow's UUID into `LANGFLOW_ADAPTER_FLOW_IDS` (in
   `.env`) so LibreChat routes to it via the OpenAI adapter.

## Import

## Import

```bash
# Via the API (requires an API key):
curl -X POST http://langflow:7860/api/v1/flows/ \
  -H "x-api-key: $LANGFLOW_API_KEY" -H "Content-Type: application/json" \
  --data @ar_fetch_invoices.json
```

Or via the LangFlow UI at `https://flow.<domain>`: **Import → upload the JSON**,
then open the flow and drag the bundled components (`ZohoBooksARTool`,
`FoodicsARTool`, the `ar_common` helpers) onto the canvas, then wire the graph
per the architecture's Flow Diagram. The supervisor flow exposes its nine
subflows as tools via the built-in **Flow as Tool** node.

## After import

Record each flow's UUID and set `LANGFLOW_ADAPTER_FLOW_IDS` (in `.env`) to the
supervisor flow's UUID so LibreChat routes to it via the OpenAI adapter.