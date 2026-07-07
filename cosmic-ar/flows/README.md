# Cosmic AR — Flow import files

The 13 JSON files here are **LangFlow export skeletons** for the Cosmic AR Agent:

| File | Flow |
|------|------|
| `supervisor.json` | `ar_supervisor` — the **wired** supervisor flow: `SupervisorAgentComponent` (real LangGraph) + thirteen `RunFlow` subflow-as-tool nodes + `ChatInput`/`ChatOutput` (16 nodes / 15 edges). See [docs/supervisor.md](../docs/supervisor.md) and [ADR-0003](../docs/adr/adr-0003-supervisor-runflow-and-adapter.md). |
| `ar_file_intake.json` | `ar_file_intake` — the **wired** File Intake Flow: `FileIntakeFlowComponent` (real LangGraph) + `File` + `ChatInput`/`ChatOutput` (4 nodes / 3 edges). The 10th subflow. See [docs/file-intake.md](../docs/file-intake.md) and [ADR-0004](../docs/adr/adr-0004-file-intake-flow.md). |
| `ar_intercompany_sales.json` | `ar_intercompany_sales` — the **wired** Intercompany Sales Flow: `IntercompanySalesFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 11th subflow. See [docs/intercompany-sales.md](../docs/intercompany-sales.md) and [ADR-0005](../docs/adr/adr-0005-intercompany-sales-flow.md). |
| `ar_kitchen_revenue.json` | `ar_kitchen_revenue` — the **wired** Cosmic Kitchen Revenue Flow: `KitchenRevenueFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 12th subflow. See [docs/kitchen-revenue.md](../docs/kitchen-revenue.md) and [ADR-0006](../docs/adr/adr-0006-kitchen-revenue-flow.md). |
| `ar_foodics_processing.json` | `ar_foodics_processing` — the **wired** Foodics Processing Flow: `FoodicsProcessingFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 13th subflow. See [docs/foodics-processing.md](../docs/foodics-processing.md) and [ADR-0007](../docs/adr/adr-0007-foodics-processing-flow.md). |
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

The thirteen `RunFlow` nodes in `supervisor.json` reference each subflow by
`flow_name_selected` (e.g. `ar_fetch_invoices`, `ar_file_intake`,
`ar_intercompany_sales`, `ar_kitchen_revenue`, `ar_foodics_processing`) with
`flow_id_selected=null`, which LangFlow resolves at runtime **after** the
subflow is imported. So:

1. Import the **thirteen subflows first** (the nine placeholders, then wire each
   to the bundled `ar_common`/`ar_tools` components per the architecture's Flow
   Diagram; `ar_file_intake.json`, `ar_intercompany_sales.json`,
   `ar_kitchen_revenue.json`, and `ar_foodics_processing.json` are already wired
   — import them as-is).
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