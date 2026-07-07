# Cosmic AR — Flow import files

The 17 JSON files here are **LangFlow export skeletons** for the Cosmic AR Agent:

| File | Flow |
|------|------|
| `supervisor.json` | `ar_supervisor` — the **wired** supervisor flow: `SupervisorAgentComponent` (real LangGraph) + sixteen `RunFlow` subflow-as-tool nodes + `ChatInput`/`ChatOutput` (19 nodes / 18 edges). See [docs/supervisor.md](../docs/supervisor.md) and [ADR-0003](../docs/adr/adr-0003-supervisor-runflow-and-adapter.md). |
| `ar_file_intake.json` | `ar_file_intake` — the **wired** File Intake Flow: `FileIntakeFlowComponent` (real LangGraph) + `File` + `ChatInput`/`ChatOutput` (4 nodes / 3 edges). The 10th subflow. See [docs/file-intake.md](../docs/file-intake.md) and [ADR-0004](../docs/adr/adr-0004-file-intake-flow.md). |
| `ar_intercompany_sales.json` | `ar_intercompany_sales` — the **wired** Intercompany Sales Flow: `IntercompanySalesFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 11th subflow. See [docs/intercompany-sales.md](../docs/intercompany-sales.md) and [ADR-0005](../docs/adr/adr-0005-intercompany-sales-flow.md). |
| `ar_kitchen_revenue.json` | `ar_kitchen_revenue` — the **wired** Cosmic Kitchen Revenue Flow: `KitchenRevenueFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 12th subflow. See [docs/kitchen-revenue.md](../docs/kitchen-revenue.md) and [ADR-0006](../docs/adr/adr-0006-kitchen-revenue-flow.md). |
| `ar_foodics_processing.json` | `ar_foodics_processing` — the **wired** Foodics Processing Flow: `FoodicsProcessingFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 3 edges). The 13th subflow. See [docs/foodics-processing.md](../docs/foodics-processing.md) and [ADR-0007](../docs/adr/adr-0007-foodics-processing-flow.md). |
| `ar_calculation.json` | `ar_calculation` — the **wired** Calculation Flow: `CalculationFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 2 edges, no `files` edge). The 14th subflow. See [docs/calculation.md](../docs/calculation.md) and [ADR-0008](../docs/adr/adr-0008-calculation-flow.md). |
| `ar_invoice_generation.json` | `ar_invoice_generation` — the **wired** Invoice Generation Flow: `InvoiceGenerationFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 2 edges, no `files` edge). The 15th subflow. See [docs/invoice-generation.md](../docs/invoice-generation.md) and [ADR-0009](../docs/adr/adr-0009-invoice-generation-flow.md). |
| `ar_approval.json` | `ar_approval` — the **wired** Human Approval Flow: `HumanApprovalFlowComponent` (real LangGraph, §19 interrupt pause/resume) + `ChatInput`/`ChatOutput` (3 nodes / 2 edges, no `files` edge). The 9th subflow. See [docs/approval-flow.md](../docs/approval-flow.md) and [ADR-0010](../docs/adr/adr-0010-approval-flow.md). |
| `ar_fetch_invoices.json` | `ar_fetch_invoices` |
| `ar_fetch_receipts.json` | `ar_fetch_receipts` |
| `ar_match_payments.json` | `ar_match_payments` |
| `ar_reconcile.json` | `ar_reconcile` |
| `ar_dunning.json` | `ar_dunning` |
| `ar_post_gl.json` | `ar_post_gl` |
| `ar_issue_invoice.json` | `ar_issue_invoice` — the **wired** Zoho Upload Flow: `ZohoUploadFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 2 edges, no `files` edge). The 7th subflow. See [docs/zoho-upload-flow.md](../docs/zoho-upload-flow.md) and [ADR-0011](../docs/adr/adr-0011-zoho-upload-flow.md). |
| `ar_audit.json` | `ar_audit` — the **wired** Audit Flow: `AuditFlowComponent` (real LangGraph) + `ChatInput`/`ChatOutput` (3 nodes / 2 edges, no `files` edge). The 16th subflow. See [docs/audit-flow.md](../docs/audit-flow.md) and [ADR-0012](../docs/adr/adr-0012-audit-flow.md). |
| `ar_reporting.json` | `ar_reporting` |
| `ar_approval.json` | `ar_approval` |

> **Flows live in the LangFlow Postgres DB, not on disk** (constitution §7).
> These files are **import artifacts** — `supervisor.json`, `ar_file_intake.json`,
> `ar_intercompany_sales.json`, `ar_kitchen_revenue.json`,
> `ar_foodics_processing.json`, `ar_calculation.json`,
> `ar_invoice_generation.json`, `ar_approval.json`, `ar_issue_invoice.json`,
> and `ar_audit.json` are fully wired (their nodes embed the real component
> sources, so they import as working canvases); the seven business-subflow
> skeletons are empty-graph
> placeholders you import and then wire to the bundled components. None are
> auto-loaded by the `langflow` container.

## Import order / `flow_id` resolution

The sixteen `RunFlow` nodes in `supervisor.json` reference each subflow by
`flow_name_selected` (e.g. `ar_fetch_invoices`, `ar_file_intake`,
`ar_intercompany_sales`, `ar_kitchen_revenue`, `ar_foodics_processing`,
`ar_calculation`, `ar_invoice_generation`, `ar_approval`, `ar_issue_invoice`,
`ar_audit`)
with
`flow_id_selected=null`, which LangFlow resolves at runtime **after** the
subflow is imported. So:

1. Import the **sixteen subflows first** (the seven placeholders, then wire each
   to the bundled `ar_common`/`ar_tools` components per the architecture's Flow
   Diagram; `ar_file_intake.json`, `ar_intercompany_sales.json`,
   `ar_kitchen_revenue.json`, `ar_foodics_processing.json`,
   `ar_calculation.json`, `ar_invoice_generation.json`, `ar_approval.json`,
   `ar_issue_invoice.json`, and `ar_audit.json` are already wired — import them
   as-is).
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
per the architecture's Flow Diagram. The supervisor flow exposes its
subflows as tools via the built-in **Flow as Tool** node.

## After import

Record each flow's UUID and set `LANGFLOW_ADAPTER_FLOW_IDS` (in `.env`) to the
supervisor flow's UUID so LibreChat routes to it via the OpenAI adapter.