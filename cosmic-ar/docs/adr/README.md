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
| [0010](adr-0010-approval-flow.md) | Human Approval Flow: 9th subflow implemented, validated-JSON review packet → §19 interrupt pause/present/capture/resume + Approve/Reject/Request-Changes + WorkflowState + audit (standalone presentational surface; `approval-result` `decision` enum += `request_changes`) | Accepted |
| [0011](adr-0011-zoho-upload-flow.md) | Zoho Upload Flow: 7th subflow implemented, validated-JSON `ZohoUploadRequest` (§1 `approval_ref` + `InvoiceData` batch) → validate → §10-retried upload to Zoho Books → all-or-nothing rollback of created invoices on partial failure → store zoho_id + timestamp → canonical `ZohoUploadResult` per create + enriched per-invoice view with `rolled_back` + `WorkflowState` + audit per create/rollback (§13); §1 approval_ref required at the boundary, NO in-flow interrupt; deterministic stub transport v1 (real `ZohoBooksARTool` POST build-phase); standalone surface, no supervisor edit | Accepted |
| [adr-000-template.md](adr-000-template.md) | Template for new ADRs | — |

## When to write an ADR

Per the constitution's Authority note, any deviation from a binding standard
requires a written waiver recorded in the flow's README **and a linked ADR**.
Create a new ADR (next number) whenever a decision is significant, hard to
reverse, or supersedes an earlier ADR.