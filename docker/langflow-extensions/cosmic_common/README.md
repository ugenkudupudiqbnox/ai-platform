# Cosmic Common — LangFlow Extension Bundle

Generic, reusable `lfx` custom components for the **Cosmic AR Agent** — the
reusable layer that the AR-specific [`ar_common`](../ar_common/) and
[`ar_tools`](../ar_tools/) bundles compose (constitution §15). See
[`docs/cosmic-ar-architecture.md`](../../../docs/cosmic-ar-architecture.md) for
the design and [`docs/cosmic-ar-constitution.md`](../../../docs/cosmic-ar-constitution.md)
for the binding standards. The per-component 9-facet reference is
[`cosmic-ar/docs/components.md`](../../../cosmic-ar/docs/components.md).

## What this bundle exposes

| Component | File | § | Role |
|-----------|------|---|------|
| `ExcelReaderComponent` | `components/cosmic_common/excel_reader.py` | §15/§16 | Read an Excel file; emit rows in the §14 envelope |
| `CSVReaderComponent` | `components/cosmic_common/csv_reader.py` | §15/§16 | Read a CSV file; emit rows in the §14 envelope |
| `PDFReaderComponent` | `components/cosmic_common/pdf_reader.py` | §15/§16 | Read a PDF (text/tables); emit in the §14 envelope |
| `DocumentClassifierComponent` | `components/cosmic_common/document_classifier.py` | §4 | Classify a document (invoice/receipt/credit_note/…); low confidence → `AR_UNCERTAIN` |
| `ExcelNormalizerComponent` | `components/cosmic_common/excel_normalizer.py` | §8 | Normalize messy Excel → typed rows (2dp amount strings, ISO dates) |
| `BusinessRuleEngineComponent` | `components/cosmic_common/business_rule_engine.py` | §9 | Evaluate a declarative rule set against a payload |
| `ValidationEngineComponent` | `components/cosmic_common/validation_engine.py` | §9 | Validate a payload against a JSON-Schema contract → `ValidationResult` |
| `CalculationEngineComponent` | `components/cosmic_common/calculation_engine.py` | §8 | Run a named calculation (match/reconcile/aging/rounding) → `CalculationResult` |
| `InvoiceBuilderComponent` | `components/cosmic_common/invoice_builder.py` | §2 | Assemble an `InvoiceData` contract payload from line items |
| `ZohoConnectorComponent` | `components/cosmic_common/zoho_connector.py` | §9/§10/§16 | Generic Zoho CRUD base; `ar_tools.ZohoBooksARTool` composes it |
| `AuditLoggerComponent` | `components/cosmic_common/audit_logger.py` | §13 | Generic immutable audit writer → `AuditRecord`; `ar_common.AuditRecordComponent` composes it |
| `NotificationComponent` | `components/cosmic_common/notification_component.py` | §2/§19 | Generic dispatcher → `Notification` |
| `CheckpointManagerComponent` | `components/cosmic_common/checkpoint_manager.py` | §11 | Generic checkpoint save/load; `ar_common.CheckpointComponent` composes it |
| `StateManagerComponent` | `components/cosmic_common/state_manager.py` | §8 | Generic typed-state get/set/merge/snapshot (immutable updates) |
| `ConfigurationLoaderComponent` | `components/cosmic_common/configuration_loader.py` | §17 | Load per-flow tunables from LangFlow Global Variables / config ref |

> **Scaffold only.** Every component is a valid, importable `lfx` Component
> skeleton whose output method returns a placeholder §14-envelope `Message` and
> never raises. Business logic (Excel/PDF parsing, Zoho OAuth, rule engine,
> SQLAlchemy saver, jsonschema validation) is filled in at the build phase. No
> business logic is implemented here.

## Layering (constitution §15 — reuse before authoring)

`cosmic_common` is the **generic reusable layer**. The AR-specific bundles are
thin adapters that compose it at build phase:

- `ar_common.CheckpointComponent` → delegates to `CheckpointManagerComponent`.
- `ar_common.AuditRecordComponent` → delegates to `AuditLoggerComponent`.
- `ar_common.SupervisorAgentComponent`'s `AgentState` handling → `StateManagerComponent`.
- `ar_tools.ZohoBooksARTool` → delegates Zoho CRUD to `ZohoConnectorComponent`
  and adds the AR operation set.

The `cosmic_common` generics are also the seed for the future **AP extension**
(§20) — AP flows reuse the readers, engines, audit, notification, checkpoint,
state, and config components unchanged.

### §15 reuse check — custom readers vs LangFlow built-ins

LangFlow 1.10 ships built-in file components. The custom Excel/CSV/PDF readers
here are justified (recorded in
[`adr-0002`](../../../cosmic-ar/docs/adr/adr-0002-reusable-component-library.md))
because they (a) emit output normalized to the project's contracts
(`DocumentManifest`/`ValidationResult`) inside the §14 envelope — built-ins
don't; (b) apply the §16 PII/SSRF rules; (c) are reusable across AR and AP.

## Credentials

This bundle **requires credentials** for the connectors:

- `ZohoConnectorComponent` — `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`,
  `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID` (LangFlow Secret Global Variables;
  `SecretStrInput(..., load_from_db=True)` — never in flow JSON, §16).
- `NotificationComponent` — channel credentials (e.g. `SMTP_*`) as Secret Global
  Variables, selected per channel at build phase.

`CheckpointManagerComponent` reaches the `ar_agent` Postgres DB via the
build-phase `AR_AGENT_DB_*` wiring (see
[`cosmic-ar/docs/environment.md`](../../../cosmic-ar/docs/environment.md)).

## Build-phase dependencies

`pyproject.toml` declares `dependencies = []` (inline bundles rely on the
langflow image venv). Components needing libs **not in the image** must be baked
into `docker/langflow/Dockerfile` at build phase:

| Component | Lib | In image? |
|-----------|-----|-----------|
| Excel Reader | `openpyxl` | no |
| PDF Reader | `pdfplumber` | no |
| Validation Engine | `jsonschema` | no |
| Checkpoint Manager | `langgraph-checkpoint-postgres` | no |
| CSV Reader | stdlib `csv` | yes |
| Zoho Connector / Notification | `requests` | yes |
| Calculation/Business-Rule/State/Invoice/Config | stdlib + in-image | yes |

## Layout

```
cosmic_common/                   # inline-bundle dir MUST be snake_case (bundle-name pattern)
  extension.json                 # v1 Extension manifest (bundle = cosmic_common; id = cosmic-common)
  pyproject.toml                 # pip metadata + langflow.extension entry-point
  components/cosmic_common/
    __init__.py
    excel_reader.py              # ExcelReaderComponent
    csv_reader.py                # CSVReaderComponent
    pdf_reader.py                # PDFReaderComponent
    document_classifier.py       # DocumentClassifierComponent
    excel_normalizer.py         # ExcelNormalizerComponent
    business_rule_engine.py      # BusinessRuleEngineComponent
    validation_engine.py         # ValidationEngineComponent
    calculation_engine.py        # CalculationEngineComponent
    invoice_builder.py           # InvoiceBuilderComponent
    zoho_connector.py            # ZohoConnectorComponent
    audit_logger.py              # AuditLoggerComponent
    notification_component.py    # NotificationComponent
    checkpoint_manager.py        # CheckpointManagerComponent
    state_manager.py             # StateManagerComponent
    configuration_loader.py      # ConfigurationLoaderComponent
```

## Validate offline

```bash
# Offline (host): syntax-only compile (lfx.* not importable on the host is fine).
python3 -m py_compile docker/langflow-extensions/cosmic_common/components/cosmic_common/*.py
# Post-deploy (running stack):
docker exec langflow python -m lfx extension validate /app/extensions/cosmic_common
```

Edits take effect on container **recreate** (the mount is read-only `:ro`);
never write into `/app/extensions` at runtime (§15).