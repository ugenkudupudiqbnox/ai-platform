# Cosmic AR Agent — Documentation

| Doc | Purpose |
|-----|---------|
| [Project Constitution](../../docs/cosmic-ar-constitution.md) | Binding engineering standards (the 20 sections) |
| [Architecture](../../docs/cosmic-ar-architecture.md) | Supervisor + fifteen subflows + shared components + LangGraph state + diagrams |
| [Contracts](contracts.md) | JSON Schema + validation rules + examples for the 14 wire contracts |
| [Components](components.md) | Reusable lfx components (`cosmic_common`) — 15 components with the 9 facets |
| [Supervisor](supervisor.md) | The supervisor agent — responsibilities→nodes, canvas wiring, run/resume, approval round-trip, build-phase checklist |
| [File Intake Flow](file-intake.md) | The 10th subflow — accept Excel/CSV/PDF, classify, validate, build a DocumentManifest; responsibilities→nodes, canvas wiring, build-phase checklist |
| [Intercompany Sales Flow](intercompany-sales.md) | The 11th subflow — read a KOT Excel, validate rows, calculate revenue at the agreed rate, generate draft InvoiceData per buyer + Validation/Exception reports; v1 compute + draft only |
| [Kitchen Revenue Flow](kitchen-revenue.md) | The 12th subflow — read the four Cosmic Kitchen sheets (Menu Sales Analysis, Daily Sales, Detailed Check Payment, Marriott Backup), compute Revenue (Breakfast/Half Board segments), Collections, Expenses, Net Receivable, Net Payable + Revenue JSON + Validation/Exception reports; v1 read-only compute + report |
| [Foodics Processing Flow](foodics-processing.md) | The 13th subflow — read Foodics Order + Order Items + Order Payments (export files or Foodics API), build a consolidated dataset + pivot + payment-type breakdown, apply discount rules, generate a Zoho Books upload format + draft InvoiceData per order + Validation/Exception reports; v1 compute + draft only |
| [Calculation Flow](calculation.md) | The 14th subflow — take a validated-JSON payload (aggregated facts + parameters, the P10 Validation Flow output) and compute Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/Expenses/Net Receivable/Net Payable via the Business Rule Engine (zero hardcoded formulas; §55 waiver — figures only, not statutory filing) + Validation/Exception reports; v1 read-only compute + report |
| [Invoice Generation Flow](invoice-generation.md) | The 15th subflow — take a validated-JSON invoice request (customer_ref, line_items, totals, issue_date, currency) and assemble a draft InvoiceData, then generate 8 artifacts (Invoice JSON/PDF render-spec/Excel render-spec/draft Journal Entry/Customer Statement/Zoho Upload File/Invoice Metadata + WorkflowState) as JSON-in-envelope; v1 read-only generate + draft (no posting; PDF/Excel binaries are build-phase) |
| [Human Approval Flow](approval-flow.md) | The 9th subflow — take a validated-JSON review packet (Revenue/Expense/Invoice/Validation summaries + proposal), PAUSE via §19 interrupt, PRESENT the packet, CAPTURE Approve/Reject/Request-Changes on resume, UPDATE WorkflowState, LOG an audit record (§13); emits an ApprovalResult. Standalone presentational surface (no supervisor change); v1 single-approver + InMemorySaver |
| [Zoho Upload Flow](zoho-upload-flow.md) | The 7th subflow — take a validated-JSON `ZohoUploadRequest` (§1 `approval_ref` + a batch of `InvoiceData`), VALIDATE mandatory fields, UPLOAD each to Zoho Books with §10 retry, ROLL BACK (delete) the created invoices on any partial failure (all-or-nothing), STORE zoho_id + upload timestamp, LOG an AuditRecord per create/rollback (§13), UPDATE WorkflowState, RETURN a per-invoice `ZohoUploadResult` + batch summary; §1 `approval_ref` required at the boundary (no in-flow interrupt); deterministic stub transport v1 (real `ZohoBooksARTool` POST/DELETE build-phase) |
| [Environment variables](environment.md) | `.env` vars, LangFlow Global Variables, build-phase vars |
| [ADRs](adr/README.md) | Architecture Decision Records |
| [Runbooks](runbooks/README.md) | Operational procedures (placeholders) |

For the platform itself (install, SSO, backups, scaling), see the repo
[`docs/`](../../docs/) set.