# ADR 0013 — Retire the seven unimplemented placeholder subflows; renumber architecture §4 to the nine implemented subflows (rows 1-9)

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** partially — the reserved-slot premise of [ADR-0001](adr-0001-supervisor-and-checkpointer.md)
  and [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md) (which reserved nine business-subflow slots, only
  some of which were ever implemented). Does **not** supersede ADR-0004–0012 (those implement flows that stay).
- **Related:** [constitution](../../../docs/cosmic-ar-constitution.md) §4/§6/§8/§9/§19,
  [architecture](../../../docs/cosmic-ar-architecture.md) §4/§5,
  [supervisor](../supervisor.md),
  [ADR-0001](adr-0001-supervisor-and-checkpointer.md),
  [ADR-0003](adr-0003-supervisor-runflow-and-adapter.md),
  [ADR-0004](adr-0004-file-intake-flow.md)–[ADR-0012](adr-0012-audit-flow.md)

## Context

Architecture §4 (originally "Nine reusable subflows") grew to sixteen rows as
ADRs 0004–0012 implemented nine of the reserved/added slots. The other
**seven slots were never implemented**: `ar_fetch_invoices`,
`ar_fetch_receipts`, `ar_match_payments`, `ar_reconcile`, `ar_dunning`,
`ar_post_gl`, `ar_reporting`. They remain **empty-graph placeholder JSONs**
(`nodes: []`, no orchestrator component, no logic) in `cosmic-ar/flows/`, yet
they are wired into the supervisor as `RunFlow` nodes that resolve to nothing,
listed in `supervisor.py`'s `SUBFLOWS` / `TIER` / `INTENT_KEYWORDS`, and
`ar_post_gl` is in `FINANCIAL_INTENTS`. They are described throughout the
architecture/docs/READMEs as "seven placeholders" / "seven business subflows
remain".

A `RunFlow` node whose `flow_name_selected` points at an empty placeholder
graph has no runnable behavior and only adds dead surface area: the supervisor
router can route to it, the gate logic reads a tier for it, and intent
keywords (e.g. `ar_post_gl`'s bare `"gl"`/`"post"`, `ar_reporting`'s
`"report"`/`"aging"`, `ar_dunning`'s `"overdue"`) can shadow or collide with
intent routing for the implemented flows. Carrying them indefinitely commits
the project to slots that may never be built in this form and keeps the
constitution's row cross-references, the contract-schema descriptions, and
the operational docs referencing non-existent flows.

The nine implemented subflows (in build order) are: `ar_file_intake`,
`ar_intercompany_sales`, `ar_kitchen_revenue`, `ar_foodics_processing`,
`ar_calculation`, `ar_invoice_generation`, `ar_approval`, `ar_issue_invoice`,
`ar_audit`. Two of the seven retired slots (`ar_post_gl`, `ar_issue_invoice`)
were the original `FINANCIAL_INTENTS`; only `ar_issue_invoice` survives and is
the sole remaining financial-mutation flow.

## Decision

1. **Retire the seven unimplemented placeholder subflows entirely** — delete the
   seven flow JSONs, remove their `RunFlow` nodes + edges from
   `cosmic-ar/flows/supervisor.json`, and drop them from `supervisor.py`'s
   `SUBFLOWS` / `TIER` / `INTENT_KEYWORDS`. After removal the supervisor canvas
   is **12 nodes / 11 edges / 9 RunFlow** (the nine implemented subflows +
   ChatInput/ChatOutput/SupervisorAgentComponent).

2. **`FINANCIAL_INTENTS` → `frozenset({"ar_issue_invoice"})`** — `ar_post_gl`
   is retired; `ar_issue_invoice` stays (it is the surviving approval-tier
   financial POST flow). `ar_match_payments`' auto-match ceiling and
   `ar_post_gl`'s approval tier are removed with their entries.

3. **Renumber architecture §4 to the nine implemented subflows, rows 1–9**,
   in the nine flows' original relative order, and retitle the section
   "Nine reusable LangFlow subflows":

   | new # | flow id | was # |
   |---|---|---|
   | 1 | `ar_issue_invoice` | 7 |
   | 2 | `ar_approval` | 9 |
   | 3 | `ar_file_intake` | 10 |
   | 4 | `ar_intercompany_sales` | 11 |
   | 5 | `ar_kitchen_revenue` | 12 |
   | 6 | `ar_foodics_processing` | 13 |
   | 7 | `ar_calculation` | 14 |
   | 8 | `ar_invoice_generation` | 15 |
   | 9 | `ar_audit` | 16 |

   All §4/§5 row cross-references, the §5 state-diagram routing edges, and the
   §5 sequence diagram (retired `ar_post_gl` participant → `ar_issue_invoice`)
   are updated to the new numbering. The historical "growth" amendment notes
   (Nine→Ten→…→Sixteen) are replaced with a single consolidated history + renumber
   note that names all seven retired flows and cites this ADR.

4. **No `AgentState` schema change** — this is a removal/renumber only; the
   supervisor state shape is unaffected. No contract schema files are deleted
   (they remain valid reusable contracts); their `description` strings that
   attributed production/consumption to retired flows are rephrased to drop the
   retired-flow attribution, and the `execution-summary.json` example's
   `subflows_invoked` lists surviving flows.

5. **Test-fixture example strings** that named retired flows as illustrative
   data (`ar_post_gl` / `ar_dunning` in the approval + adapter self-tests) are
   swapped to surviving flows (`ar_issue_invoice` / `ar_audit`). The tests pass
   either way (the strings are example data, not flow dependencies); the swap
   is for consistency.

6. **ADRs 0001–0012 are left intact as immutable historical records.** Their
   references to "row 10/15/16", "the Nth subflow", and counts like "Fifteen" /
   "Sixteen" reflect the numbering as it stood when they were written and are
   not rewritten. This ADR is the record of the renumber.

7. **`prompts/P01_solution_architecture.md` is left intact** (historical input
   prompt).

## Consequences

- Positive:
  - The supervisor canvas, router constants, and intent keywords describe only
    flows that exist and run; no dead `RunFlow` nodes, no shadowing of
    implemented intents by retired-flow keywords (`ar_post_gl`'s bare `"gl"` /
    `"post"`, `ar_reporting`'s `"report"` / `"aging"`, `ar_dunning`'s
    `"overdue"` no longer compete).
  - `FINANCIAL_INTENTS` correctly models the v1 financial surface (one flow,
    `ar_issue_invoice`); the §19 gate logic and §10 retry-exhaustion semantics
    apply only to a flow that actually performs a financial mutation.
  - Architecture §4, the READMEs, and the operational docs all describe the
    agent as it actually is — nine implemented subflows — instead of nine
    implemented + seven notional placeholders.
  - Count references collapse from a sprawling Nine→…→Sixteen amendment chain to
    a single renumber note.
- Negative:
  - The seven retired flow ids are gone from live code; any external caller that
    had begun routing to them (none exist in v1 — the flows never ran) would
    now get `AR_UNCERTAIN` / no-tool-found instead of a (broken) placeholder
    call. Acceptable: the placeholders had no implementation.
  - Historical ADR cross-references to specific row numbers ("row 10", "row
    15", "row 16", "the 15th subflow") now point at a numbering that no longer
    exists in §4. Mitigated by this ADR's consolidated history note in §4
    (which names all seven retired flows and explains the renumber) and by
    leaving the ADRs immutable per the record convention.
- Risks and mitigations:
  - Risk: a future contributor reads an old ADR ("row 12 `ar_kitchen_revenue`")
    and is confused by the new §4 (row 5). Mitigation: the §4 history note
    explicitly calls out that ADRs retain pre-renumber numbering.
  - Risk: someone re-adds a retired id (e.g. rebuilds `ar_post_gl.json`) and
    expects `FINANCIAL_INTENTS` / `TIER` to recognize it. Mitigation: the
    `supervisor.py` constants comment names the seven retired ids and points
    here; re-adding one is a new ADR.
- Build-phase follow-ups:
  - If any of the seven retired capabilities are genuinely needed later (a GL
    post, payment matching, dunning, AR reporting), they are **new** subflows
    under new ADRs (new row numbers 10+), reusing the surviving contract schemas
    where applicable — not a resurrection of the retired ids.
  - `AR_APPROVAL_AUTO_MATCH_CEILING` (the env var that fed `ar_match_payments`'
    build-phase auto-match ceiling) remains in `.env.example` as a harmless
    build-phase knob; its `environment.md` description no longer names
    `ar_match_payments`.