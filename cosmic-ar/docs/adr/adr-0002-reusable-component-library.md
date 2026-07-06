# ADR 0002 — Reusable component library (`cosmic_common`) + §15 reader waiver

- **Status:** Accepted
- **Date:** 2026-07-06
- **Deciders:** Principal Enterprise Architect
- **Supersedes:** none

## Context

The [architecture](../../../docs/cosmic-ar-architecture.md) calls for nine
reusable LangFlow subflows plus a custom supervisor, all operating on the typed
`AgentState` (constitution [§8](../../../docs/cosmic-ar-constitution.md)). Two
runtime bundles already ship as stubs: `ar_common` (6 AR-specific cross-cutting
components + `AgentState`) and `ar_tools` (`ZohoBooksARTool`, `FoodicsARTool`).
The 14 [contracts](../contracts.md) define the wire shapes.

A gap remains: the generic, reusable mechanics the subflows need — document
readers, a normalizer, a classifier, rule/validation/calculation engines, an
invoice builder, a generic Zoho CRUD base, an audit/logger/notification pair,
and checkpoint/state/configuration managers — have no home. Putting them in
`ar_common` would entangle generic logic with AR-specific namespaces and block
the §20 AP extension from reusing them; duplicating them in a future AP bundle
would violate §15 (reuse before authoring).

Constitution §15 is binding: *"check LangFlow built-ins first."* LangFlow 1.10.1
ships built-in file components (Excel/CSV/PDF readers). The readers are therefore
**not** obviously authorable.

See constitution §5/§6/§8/§9/§11/§12/§13/§15/§16/§17/§20.

## Decision

1. **Add a third runtime bundle, `cosmic_common`**, holding 15 generic, reusable
   `lfx` Components. It is the *generic reusable layer*; `ar_common`/`ar_tools`
   stay as the AR-specific adapters that **compose** the `cosmic_common`
   generics at build phase. No existing bundle is edited or deleted
   (layered approach — see "Consequences").
2. **The bundle is scaffolding only.** Each component is a valid, importable
   `lfx` Component (mirrors `ar_common/audit.py` + `ar_tools/zoho_books_ar.py`):
   bare `name = "ClassName"`, `inputs`/`outputs`, `tool_mode=True` on dynamic
   inputs, `SecretStrInput(..., load_from_db=True)` for credentials, output
   method returns a placeholder §14-envelope `Message`, never raises. Business
   logic (parsing, OAuth, rule evaluation, SQLAlchemy saver, jsonschema
   validation) is build-phase.
3. **§15 reader waiver (recorded).** The custom Excel/CSV/PDF readers are
   justified over the LangFlow built-ins because they:
   - (a) emit output normalized to the project's
     [contracts](../contracts.md) inside the §14 envelope — the built-ins emit
     raw `Data`/text, not a `DocumentManifest`-shaped envelope;
   - (b) apply the §16 PII/SSRF rules at the read boundary (built-ins don't);
   - (c) are reusable across AR and the future AP extension (§20).
   Every other component (engines, builder, connector, audit, notification,
   checkpoint, state, config) has **no built-in equivalent** in LangFlow 1.10,
   so §15 is satisfied with no further waiver.
4. **No platform touch.** `docker-compose.yml` already bind-mounts the whole
   `./docker/langflow-extensions` directory to `/app/extensions:ro` with
   `LANGFLOW_COMPONENTS_PATH=/app/extensions`. Any new subfolder with an
   `extension.json` is auto-discovered — so adding `cosmic_common/` needs **no
   `docker-compose.yml` or `.env.example` edit**; `make validate`/CI stay green.
5. **Build-phase dependencies are recorded, not installed.** The bundle
   `pyproject.toml` declares `dependencies = []` (mirrors `ar_common`/`ar_tools`;
   relies on the langflow image venv). Libs not confirmed in the image are
   bake-into-`docker/langflow/Dockerfile` items at build phase:

   | Component | Build-phase lib | In-image? |
   |-----------|-----------------|-----------|
   | Excel Reader | `openpyxl` | no — bake |
   | PDF Reader | `pdfplumber` | no — bake |
   | Validation Engine | `jsonschema` | no — bake |
   | Checkpoint Manager | `langgraph-checkpoint-postgres` | no — bake |

   No `Dockerfile` edit is made by this task.

## Consequences

- Positive: a single generic layer reusable by AR today and AP (§20) tomorrow
  without duplication; §15 satisfied with one recorded, narrow waiver; platform
  validation unchanged because the bundle is auto-mounted.
- Negative: a third bundle to maintain; build-phase baking of four libs into the
  langflow image.
- Risks: layering confusion between `cosmic_common` (generic) and
  `ar_common`/`ar_tools` (AR adapters). Mitigation: each component's "Future
  Reuse Guidance" facet in [components.md](../components.md) states exactly which
  AR component composes it and how AP reuses it.
- Build-phase follow-ups:
  - Bake `openpyxl`, `pdfplumber`, `jsonschema`,
    `langgraph-checkpoint-postgres` into `docker/langflow/Dockerfile`.
  - Implement the skeletons' output methods (real parsing, OAuth, rule
    evaluation, SQLAlchemy saver, jsonschema validation, checkpoint saver).
  - Compose `ar_common.AuditRecordComponent`/`CheckpointComponent` over
    `cosmic_common.AuditLogger`/`CheckpointManager`; compose
    `ar_tools.ZohoBooksARTool` over `cosmic_common.ZohoConnector`.
  - Create the referenced Secret Global Variables (`ZOHO_*`, channel `SMTP_*`)
    per §16 at build phase.