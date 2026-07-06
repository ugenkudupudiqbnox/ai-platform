# Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [0001](adr-0001-supervisor-and-checkpointer.md) | Custom LangGraph StateGraph supervisor + Postgres-backed checkpointer | Accepted |
| [0002](adr-0002-reusable-component-library.md) | Reusable component library (`cosmic_common`) + §15 reader waiver | Accepted |
| [0003](adr-0003-supervisor-runflow-and-adapter.md) | Supervisor implementation: RunFlow nodes, MemorySaver fallback, adapter file/approval forwarding | Accepted |
| [0004](adr-0004-file-intake-flow.md) | File Intake Flow: 10th subflow, openpyxl/pdfplumber Dockerfile dep, manifest-in-envelope | Accepted |
| [adr-000-template.md](adr-000-template.md) | Template for new ADRs | — |

## When to write an ADR

Per the constitution's Authority note, any deviation from a binding standard
requires a written waiver recorded in the flow's README **and a linked ADR**.
Create a new ADR (next number) whenever a decision is significant, hard to
reverse, or supersedes an earlier ADR.