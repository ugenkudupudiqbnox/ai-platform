# Cosmic AR Agent — JSON Contracts

Machine-validatable JSON Schema (draft 2020-12) for every wire contract that
flows through the agent: the §14 envelope's `data` payload, the checkpoint's
`agent_state` jsonb, and the audit/approval/upload stores. The human reference
is [`../docs/contracts.md`](../docs/contracts.md); this README is the operational
guide (layout, validation, versioning).

## Layout

```
contracts/
  registry.json              # name -> {version, schema, example, status, since, constitution_ref}
  schemas/                   # one *.schema.json per contract (+ envelope)
  examples/                  # one valid payload per contract (+ envelope)
```

Fifteen schemas ship: the **envelope** (§14 wrapper) plus the 14 contracts.

| Contract | Schema | Constitution |
|----------|--------|--------------|
| Envelope | [`schemas/envelope.schema.json`](schemas/envelope.schema.json) | §14 |
| WorkflowState | [`schemas/workflow-state.schema.json`](schemas/workflow-state.schema.json) | §8/§11 |
| DocumentManifest | [`schemas/document-manifest.schema.json`](schemas/document-manifest.schema.json) | §2 |
| RevenueData | [`schemas/revenue-data.schema.json`](schemas/revenue-data.schema.json) | §2 |
| CollectionData | [`schemas/collection-data.schema.json`](schemas/collection-data.schema.json) | §2 |
| ExpenseData | [`schemas/expense-data.schema.json`](schemas/expense-data.schema.json) | §19 |
| ValidationResult | [`schemas/validation-result.schema.json`](schemas/validation-result.schema.json) | §9 |
| CalculationResult | [`schemas/calculation-result.schema.json`](schemas/calculation-result.schema.json) | §8 |
| InvoiceData | [`schemas/invoice-data.schema.json`](schemas/invoice-data.schema.json) | §2 |
| ApprovalRequest | [`schemas/approval-request.schema.json`](schemas/approval-request.schema.json) | §19 |
| ApprovalResult | [`schemas/approval-result.schema.json`](schemas/approval-result.schema.json) | §19 |
| ZohoUploadResult | [`schemas/zoho-upload-result.schema.json`](schemas/zoho-upload-result.schema.json) | §9/§10 |
| AuditRecord | [`schemas/audit-record.schema.json`](schemas/audit-record.schema.json) | §13 |
| Notification | [`schemas/notification.schema.json`](schemas/notification.schema.json) | §2/§19 |
| ExecutionSummary | [`schemas/execution-summary.schema.json`](schemas/execution-summary.schema.json) | §14 |

Current versions are listed in [`registry.json`](registry.json).

## Validate

Each contract schema is self-contained (sub-objects live in `$defs`; only the
envelope's `data` is intentionally open — see [`../docs/contracts.md`](../docs/contracts.md)
— so each contract validates standalone). Validate every example against its
schema:

```bash
python - <<'PY'
import json, os
from jsonschema import Draft202012Validator
reg = json.load(open("cosmic-ar/contracts/registry.json"))["contracts"]
fails = 0
for name, m in reg.items():
    schema = json.load(open(f"cosmic-ar/contracts/{m['schema']}"))
    inst   = json.load(open(f"cosmic-ar/contracts/{m['example']}"))
    errs = list(Draft202012Validator(schema).iter_errors(inst))
    print(f"{'OK ' if not errs else 'FAIL'} {name}")
    fails += bool(errs)
raise SystemExit(1 if fails else 0)
PY
```

> A pure `json.load` parse-check (no `jsonschema` dependency) is enough for CI
> that only guards against malformed JSON:
> ```bash
> python -c "import json,glob; [json.load(open(f)) for f in glob.glob('cosmic-ar/contracts/schemas/*.json')+glob.glob('cosmic-ar/contracts/examples/*.json')]"
> ```

At build phase, run the validator inside the LangFlow container (where
`jsonschema` is available) as a flow-import precondition.

## Versioning strategy

- Each schema declares `x-contract-version` (semver), `x-status`
  (`draft`|`stable`), and `x-since` (date). Each payload carries
  `contract_version` so consumers can branch.
- **MAJOR** = breaking: field removal, type change, required↔optional flip, or
  `additionalProperties` tightening. A new MAJOR ships `<name>.v2.schema.json`
  **alongside** the previous version; the old file stays until flows migrate.
  Update `registry.json` to point at the new version only when migration is
  complete.
- **MINOR** = additive (new optional field only). Edit the schema in place;
  bump `x-contract-version` minor.
- **PATCH** = description/rule-text fixes. Edit in place; bump patch.
- `additionalProperties: false` on every object (constitution §8: undeclared
  fields are rejected, not passed through).

See [`../docs/contracts.md`](../docs/contracts.md) for the per-contract field
tables, validation rules, and example payloads.