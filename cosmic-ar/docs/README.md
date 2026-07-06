# Cosmic AR Agent — Documentation

| Doc | Purpose |
|-----|---------|
| [Project Constitution](../../docs/cosmic-ar-constitution.md) | Binding engineering standards (the 20 sections) |
| [Architecture](../../docs/cosmic-ar-architecture.md) | Supervisor + ten subflows + shared components + LangGraph state + diagrams |
| [Contracts](contracts.md) | JSON Schema + validation rules + examples for the 14 wire contracts |
| [Components](components.md) | Reusable lfx components (`cosmic_common`) — 15 components with the 9 facets |
| [Supervisor](supervisor.md) | The supervisor agent — responsibilities→nodes, canvas wiring, run/resume, approval round-trip, build-phase checklist |
| [File Intake Flow](file-intake.md) | The 10th subflow — accept Excel/CSV/PDF, classify, validate, build a DocumentManifest; responsibilities→nodes, canvas wiring, build-phase checklist |
| [Environment variables](environment.md) | `.env` vars, LangFlow Global Variables, build-phase vars |
| [ADRs](adr/README.md) | Architecture Decision Records |
| [Runbooks](runbooks/README.md) | Operational procedures (placeholders) |

For the platform itself (install, SSO, backups, scaling), see the repo
[`docs/`](../../docs/) set.