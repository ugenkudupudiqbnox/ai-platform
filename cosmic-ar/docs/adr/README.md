# Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [0001](adr-0001-supervisor-and-checkpointer.md) | Custom LangGraph StateGraph supervisor + Postgres-backed checkpointer | Accepted |
| [0002](adr-0002-reusable-component-library.md) | Reusable component library (`cosmic_common`) + §15 reader waiver | Accepted |
| [0003](adr-0003-supervisor-runflow-and-adapter.md) | Supervisor implementation: RunFlow nodes, MemorySaver fallback, adapter file/approval forwarding | Accepted |
| [0004](adr-0004-file-intake-flow.md) | File Intake Flow: 10th subflow, openpyxl/pdfplumber Dockerfile dep, manifest-in-envelope | Accepted |
| [0005](adr-0005-intercompany-sales-flow.md) | Intercompany Sales Flow: 11th subflow, KOT Excel → draft InvoiceData per buyer, compute + draft only | Accepted |
| [0006](adr-0006-kitchen-revenue-flow.md) | Cosmic Kitchen Revenue Flow: 12th subflow, four sheets → Revenue/Collections/Expenses/Net Receivable/Net Payable, read-only compute + report | Accepted |
| [0007](adr-0007-foodics-processing-flow.md) | Foodics Processing Flow: 13th subflow, Order + Order Items + Order Payments → consolidated/pivot/payment-type/discounts/Zoho upload format/draft InvoiceData per order, compute + draft only | Accepted |
| [0008](adr-0008-calculation-flow.md) | Calculation Flow: 14th subflow, validated JSON → Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/Expenses/Net Receivable/Net Payable via the Business Rule Engine (§55 waiver — figures only), read-only compute + report | Accepted |
| [0009](adr-0009-invoice-generation-flow.md) | Invoice Generation Flow: 15th subflow, validated-JSON invoice request → Invoice JSON/PDF render-spec/Excel render-spec/draft Journal Entry/Customer Statement/Zoho Upload File/Metadata + WorkflowState (read-only generate + draft; PDF/Excel binaries build-phase) | Accepted |
| [adr-000-template.md](adr-000-template.md) | Template for new ADRs | — |

## When to write an ADR

Per the constitution's Authority note, any deviation from a binding standard
requires a written waiver recorded in the flow's README **and a linked ADR**.
Create a new ADR (next number) whenever a decision is significant, hard to
reverse, or supersedes an earlier ADR.