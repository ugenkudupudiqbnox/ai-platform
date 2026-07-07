# Cosmic AR Agent — Documentation

| Doc | Purpose |
|-----|---------|
| [Project Constitution](../../docs/cosmic-ar-constitution.md) | Binding engineering standards (the 20 sections) |
| [Architecture](../../docs/cosmic-ar-architecture.md) | Supervisor + thirteen subflows + shared components + LangGraph state + diagrams |
| [Contracts](contracts.md) | JSON Schema + validation rules + examples for the 14 wire contracts |
| [Components](components.md) | Reusable lfx components (`cosmic_common`) — 15 components with the 9 facets |
| [Supervisor](supervisor.md) | The supervisor agent — responsibilities→nodes, canvas wiring, run/resume, approval round-trip, build-phase checklist |
| [File Intake Flow](file-intake.md) | The 10th subflow — accept Excel/CSV/PDF, classify, validate, build a DocumentManifest; responsibilities→nodes, canvas wiring, build-phase checklist |
| [Intercompany Sales Flow](intercompany-sales.md) | The 11th subflow — read a KOT Excel, validate rows, calculate revenue at the agreed rate, generate draft InvoiceData per buyer + Validation/Exception reports; v1 compute + draft only |
| [Kitchen Revenue Flow](kitchen-revenue.md) | The 12th subflow — read the four Cosmic Kitchen sheets (Menu Sales Analysis, Daily Sales, Detailed Check Payment, Marriott Backup), compute Revenue (Breakfast/Half Board segments), Collections, Expenses, Net Receivable, Net Payable + Revenue JSON + Validation/Exception reports; v1 read-only compute + report |
| [Foodics Processing Flow](foodics-processing.md) | The 13th subflow — read Foodics Order + Order Items + Order Payments (export files or Foodics API), build a consolidated dataset + pivot + payment-type breakdown, apply discount rules, generate a Zoho Books upload format + draft InvoiceData per order + Validation/Exception reports; v1 compute + draft only |
| [Environment variables](environment.md) | `.env` vars, LangFlow Global Variables, build-phase vars |
| [ADRs](adr/README.md) | Architecture Decision Records |
| [Runbooks](runbooks/README.md) | Operational procedures (placeholders) |

For the platform itself (install, SSO, backups, scaling), see the repo
[`docs/`](../../docs/) set.