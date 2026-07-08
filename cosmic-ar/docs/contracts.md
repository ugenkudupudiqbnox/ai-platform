# Cosmic AR Agent — JSON Contracts

The wire contracts for every payload that flows through the agent: the §14
envelope's `data` field, the checkpoint's `agent_state` jsonb, and the audit /
approval / upload stores. Each contract is a machine-validatable JSON Schema
(draft 2020-12) under [`../contracts/schemas/`](../contracts/schemas/), with a
valid example under [`../contracts/examples/`](../contracts/examples/) and a
version entry in [`../contracts/registry.json`](../contracts/registry.json).

> **Authority.** These contracts realize the [Constitution](../../docs/cosmic-ar-constitution.md)
> (§8 state, §9 errors, §13 audit, §14 envelope, §19 approval) and the
> [Architecture](../../docs/cosmic-ar-architecture.md) (§7 state lifecycle,
> §11 checkpoint row). A deviation from a contract requires a written waiver
> and a linked ADR, exactly as a constitution deviation does.
>
> This doc follows the repo convention (terse tables, no mermaid) except that
> example payloads are fenced JSON, which is the natural form for a contract.

## Cross-cutting validation rules

Apply to **every** contract unless a contract's own rules state otherwise.

- `additionalProperties: false` on every object — undeclared fields are
  rejected, not passed through (§8).
- **Amounts**: JSON **strings** (not numbers — Decimal-safe), pattern
  `^-?\d+\.\d{2}$` (exactly 2 decimal places). Non-negative everywhere except
  `ExpenseData` adjustments and `CalculationResult` totals/line items, which may
  be signed.
- **Currency**: ISO-4217 3-letter string (`^[A-Z]{3}$`).
- **Timestamps**: ISO-8601 UTC with `Z`
  (`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`). **Dates** (no time):
  `^\d{4}-\d{2}-\d{2}$`.
- **IDs**: non-empty strings. UUIDs (`approval_id`, `audit_id`,
  `checkpoint_id`/`last_checkpoint_id`, `manifest_id`, `notification_id`) match
  the UUID pattern `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`.
- **`trace_id`** required on every contract (§12 correlation, propagated
  end-to-end). **`tenant`** required where the payload is tenant-scoped.
- **`approval_ref`** format `^ar-approval-<uuid>$`. **`idempotency_key`** format
  `^ar-idem:<action>:<entity>:<nonce>$` (per `IdempotencyKeyComponent`, §10).
- **No PII, no secrets** (§12/§16): customers by `customer_ref` id, never
  name/email; no credential values ever; note fields must not contain PII.
- Each payload carries **`contract_version`** (semver) so consumers can branch.

## Versioning strategy

- Each schema declares `x-contract-version` (semver), `x-status`
  (`draft`|`stable`), `x-since` (date). See [`../contracts/README.md`](../contracts/README.md)
  for the validate command and the file-naming rules.
- **MAJOR** = breaking (field removal / type change / required↔optional flip /
  `additionalProperties` tightening) → ship `<name>.v2.schema.json` alongside
  the old version; keep both until flows migrate; update `registry.json` only on
  migration.
- **MINOR** = additive (new optional field only) → edit in place, bump minor.
- **PATCH** = description/rule-text fixes → edit in place, bump patch.

All contracts ship at `1.0.0`, status `draft`, since `2026-07-06`.

---

## Envelope (foundation, §14)

Every component output method and every flow run returns this wrapper. `data`
holds one of the 14 contract payloads. The envelope schema leaves `data` as an
open object so each contract validates standalone; polymorphic validation is
performed against the specific contract schema named by the flow.

- **Schema**: [`../contracts/schemas/envelope.schema.json`](../contracts/schemas/envelope.schema.json)
  · `$id` `…/contracts/envelope.schema.json` · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `status` | enum `ok\|error\|pending_approval` | required |
| `code` | `^AR_[A-Z_]+$` | required; §9 code |
| `trace_id` | string | required |
| `approval_ref` | `^ar-approval-<uuid>$` | only on `pending_approval` (§19) |

Optional: `data` (object — one of the 14 contracts), `error{message, detail}`.

```json
{
  "status": "pending_approval",
  "code": "AR_PENDING_APPROVAL",
  "data": { "...": "ApprovalRequest payload" },
  "trace_id": "ar-trace-07f3a1d2",
  "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e"
}
```
Full example: [`../contracts/examples/envelope.json`](../contracts/examples/envelope.json).

---

## 1. WorkflowState (§8/§11)

JSON serialization of the LangGraph `AgentState` (the typed dataclass in
`ar_common/components/ar_common/agent_state.py`) plus the lifecycle `status`.
A checkpoint row's `agent_state` jsonb **is** a `WorkflowState` instance. Nodes
return fragments; financial totals are explicit named fields.

- **Schema**: [`../contracts/schemas/workflow-state.schema.json`](../contracts/schemas/workflow-state.schema.json)
  · version `1.0.0` · `draft`.
- **Parity**: every `AgentState` field is present; the only additions are the
  lifecycle/operational optional fields below.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required (§12) |
| `flow_id` | string | required (§12) |
| `tenant` | string | required (§12) |
| `intent` | string | required |
| `status` | enum (lifecycle) | required — `created\|routed\|executing\|awaiting_approval\|resuming\|completed\|failed\|pending_approval` |
| `matched_amount` | 2dp string | required, non-negative (§8) |
| `outstanding_balance` | 2dp string | required, non-negative (§8) |
| `posted_total` | 2dp string | required, non-negative (§8) |
| `pending_approvals[]` | array of `approval` | required (§8/§19) |
| `idempotency_keys{}` | map action→idempotency key | required (§10); values `^ar-idem:…$` |
| `audit_refs[]` | array of string | required (§13) |
| `contract_version` | semver | required |

`approval` item: `approval_id` (uuid), `action`, `amount` (signed 2dp),
`requested_by` (Keycloak sub), `requested_at` (datetime), `approval_ref?`.

Optional: `tool_call_ref`, `current_node`, `last_checkpoint_id` (uuid),
`created_at`, `updated_at`, `error{code, message}` (when `status=failed`).

Validation rules: the three financial totals are non-negative 2dp strings;
`pending_approvals[].amount` may be negative (refunds); `idempotency_keys`
values conform to the idempotency-key pattern.

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "flow_id": "9b1d4e7a-3c8f-4a2b-9e6d-1f2a3b4c5d6e",
  "tenant": "cosmic-vikings-ksa",
  "intent": "issue_invoice",
  "status": "awaiting_approval",
  "matched_amount": "1500.00",
  "outstanding_balance": "3500.00",
  "posted_total": "1500.00",
  "pending_approvals": [
    {
      "approval_id": "c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
      "action": "gl.post",
      "amount": "1500.00",
      "requested_by": "auth0|keycloak-sub-cv-admin-001",
      "requested_at": "2026-07-06T09:15:00Z",
      "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e"
    }
  ],
  "idempotency_keys": { "gl.post": "ar-idem:gl_post:inv-123:7f3a1d2e" },
  "tool_call_ref": "ar-trace-07f3a1d2:zoho_books_ar.gl_post:1",
  "audit_refs": ["audit-20260706-0001"],
  "current_node": "approvalGate",
  "last_checkpoint_id": "e1b2c3d4-7a6b-5c4d-3e2f-1a0b9c8d7e6f",
  "created_at": "2026-07-06T09:14:30Z",
  "updated_at": "2026-07-06T09:15:05Z",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/workflow-state.json`](../contracts/examples/workflow-state.json).

---

## 2. DocumentManifest (§2)

Ingest bundle of documents (invoices, receipts, credit notes, payments) fetched
for matching/reconciliation. Produced by AR intake subflows; consumed by AR
reconciliation subflows.

- **Schema**: [`../contracts/schemas/document-manifest.schema.json`](../contracts/schemas/document-manifest.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `manifest_id` | uuid | required |
| `trace_id` | string | required |
| `tenant` | string | required |
| `documents[]` | array of `document` | required |
| `totals{count, sum}` | object | required; `count` ≥ 0; `sum` is signed 2dp |
| `contract_version` | semver | required |

`document` item: `doc_id`, `doc_type` (`invoice\|receipt\|credit_note\|payment`),
`source` (`zoho\|foodics`), `source_ref`, `customer_ref` (no PII), `amount`
(signed 2dp), `currency`, `posted_at`, `status`, `fetched_at`.

Optional: `source_systems[]`, `period{start, end}`, `generated_at`.

Validation rules: `totals.sum` must equal the sum of `documents[].amount` to 2dp
(producer-side); `customer_ref` is an id only (§16).

```json
{
  "manifest_id": "a1b2c3d4-1a2b-3c4d-5e6f-7890abcdef01",
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "documents": [
    { "doc_id": "zoho-inv-INV-123", "doc_type": "invoice", "source": "zoho",
      "source_ref": "INV-123", "customer_ref": "cust-cv-0421",
      "amount": "1500.00", "currency": "SAR",
      "posted_at": "2026-07-01T08:00:00Z", "status": "open",
      "fetched_at": "2026-07-06T09:14:35Z" },
    { "doc_id": "foodics-rct-RCT-9087", "doc_type": "receipt", "source": "foodics",
      "source_ref": "RCT-9087", "customer_ref": "cust-cv-0421",
      "amount": "1500.00", "currency": "SAR",
      "posted_at": "2026-07-03T19:42:00Z", "status": "settled",
      "fetched_at": "2026-07-06T09:14:36Z" }
  ],
  "totals": { "count": 2, "sum": "3000.00" },
  "source_systems": ["zoho", "foodics"],
  "period": { "start": "2026-07-01", "end": "2026-07-06" },
  "generated_at": "2026-07-06T09:14:40Z",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/document-manifest.json`](../contracts/examples/document-manifest.json).

---

## 3. RevenueData (§2)

Recognized AR revenue (Zoho invoice presentment + Foodics POS sales) for a
period. Produced by AR reconciliation/reporting subflows.

- **Schema**: [`../contracts/schemas/revenue-data.schema.json`](../contracts/schemas/revenue-data.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `tenant` | string | required |
| `period{start, end}` | dates | required |
| `total` | 2dp string | required, non-negative |
| `currency` | ISO-4217 | required |
| `by_segment[]` | array of `segment` | required, ≥1 item |
| `contract_version` | semver | required |

`segment` item: `segment`, `amount` (non-neg 2dp), `count` (≥0).

Optional: `by_invoice[]` (`invoice_ref`, `customer_ref?`, `amount`),
`by_customer_ref[]` (`customer_ref`, `amount`, `count`),
`comparison_prior_period{prior_total, delta}`, `generated_at`.

Validation rules: `total` equals the sum of `by_segment[].amount` to 2dp;
`comparison_prior_period.delta` = current − prior (signed).

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "period": { "start": "2026-07-01", "end": "2026-07-06" },
  "total": "48200.00",
  "currency": "SAR",
  "by_segment": [
    { "segment": "dine_in", "amount": "31200.00", "count": 410 },
    { "segment": "delivery", "amount": "11800.00", "count": 220 },
    { "segment": "catering", "amount": "5200.00", "count": 6 }
  ],
  "by_invoice": [ { "invoice_ref": "INV-123", "customer_ref": "cust-cv-0421", "amount": "1500.00" } ],
  "by_customer_ref": [ { "customer_ref": "cust-cv-0421", "amount": "1500.00", "count": 1 } ],
  "comparison_prior_period": { "prior_total": "45100.00", "delta": "3100.00" },
  "generated_at": "2026-07-06T09:14:50Z",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/revenue-data.json`](../contracts/examples/revenue-data.json).

---

## 4. CollectionData (§2)

Payments received and their match status against invoices. Produced by AR
reconciliation/reporting subflows.

- **Schema**: [`../contracts/schemas/collection-data.schema.json`](../contracts/schemas/collection-data.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `tenant` | string | required |
| `period{start, end}` | dates | required |
| `total_collected` | 2dp string | required, non-negative |
| `currency` | ISO-4217 | required |
| `payments[]` | array of `payment` | required |
| `matched_amount` | 2dp string | required, non-negative |
| `unmatched_amount` | 2dp string | required, non-negative |
| `contract_version` | semver | required |

`payment` item: `payment_id`, `customer_ref` (no PII), `amount` (non-neg 2dp),
`method` (`cash\|card\|bank_transfer\|online\|wallet\|other`), `posted_at`,
`matched_invoice_ref?`, `match_status` (`matched\|unmatched\|partial`).

Optional: `aging_buckets[]` (`bucket` `current\|1_30\|31_60\|61_90\|91_plus`,
`amount`, `count`), `by_method[]` (`method`, `amount`, `count`), `generated_at`.

Validation rules: `matched_amount + unmatched_amount` must equal `total_collected`
to 2dp; `matched_invoice_ref` is present iff `match_status` is `matched` or
`partial` (producer-side).

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "period": { "start": "2026-07-01", "end": "2026-07-06" },
  "total_collected": "16500.00",
  "currency": "SAR",
  "payments": [
    { "payment_id": "pay-zoho-PMT-5512", "customer_ref": "cust-cv-0421",
      "amount": "1500.00", "method": "bank_transfer",
      "posted_at": "2026-07-03T19:42:00Z",
      "matched_invoice_ref": "INV-123", "match_status": "matched" },
    { "payment_id": "pay-zoho-PMT-5513", "customer_ref": "cust-cv-0188",
      "amount": "320.00", "method": "card",
      "posted_at": "2026-07-04T12:10:00Z", "match_status": "unmatched" }
  ],
  "matched_amount": "1500.00",
  "unmatched_amount": "320.00",
  "aging_buckets": [
    { "bucket": "current", "amount": "320.00", "count": 1 },
    { "bucket": "1_30", "amount": "2100.00", "count": 3 },
    { "bucket": "31_60", "amount": "0.00", "count": 0 },
    { "bucket": "61_90", "amount": "0.00", "count": 0 },
    { "bucket": "91_plus", "amount": "0.00", "count": 0 }
  ],
  "by_method": [
    { "method": "bank_transfer", "amount": "1500.00", "count": 1 },
    { "method": "card", "amount": "320.00", "count": 1 }
  ],
  "generated_at": "2026-07-06T09:14:55Z",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/collection-data.json`](../contracts/examples/collection-data.json).

---

## 5. ExpenseData (§19)

AR adjustments — refunds, write-offs and credit notes — the expense-like entries
that occur on the AR side under dual-control. **Not** vendor/AP expenses (§20
seed only). Every adjustment requires ≥ `approval` tier.

- **Schema**: [`../contracts/schemas/expense-data.schema.json`](../contracts/schemas/expense-data.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `tenant` | string | required |
| `period{start, end}` | dates | required |
| `total` | signed 2dp string | required (negative = net reduction) |
| `currency` | ISO-4217 | required |
| `adjustments[]` | array of `adjustment` | required |
| `contract_version` | semver | required |

`adjustment` item: `adjustment_id`, `type` (`refund\|write_off\|credit_note`),
`customer_ref` (no PII), `invoice_ref?`, `amount` (signed), `currency`,
`reason_code`, `posted_at`, `approval_ref` (required — §19),
`idempotency_key` (required — §10).

Optional: `by_type[]` (`type`, `amount`, `count`), `approval_refs[]`,
`generated_at`.

Validation rules: every `adjustment` carries an `approval_ref` (no approval =
no adjustment, §19); `total` equals the sum of `adjustments[].amount` to 2dp;
refunds/write-offs are negative.

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "period": { "start": "2026-07-01", "end": "2026-07-06" },
  "total": "-750.00",
  "currency": "SAR",
  "adjustments": [
    { "adjustment_id": "adj-rfd-2103", "type": "refund",
      "customer_ref": "cust-cv-0421", "invoice_ref": "INV-118",
      "amount": "-500.00", "currency": "SAR", "reason_code": "goods_returned",
      "posted_at": "2026-07-05T16:05:00Z",
      "approval_ref": "ar-approval-d4e5f6a7-1b2c-3d4e-9f8a-7b6c5d4e3f2a",
      "idempotency_key": "ar-idem:refund:inv-118:9a2b" },
    { "adjustment_id": "adj-woff-2104", "type": "write_off",
      "customer_ref": "cust-cv-0900", "invoice_ref": "INV-077",
      "amount": "-250.00", "currency": "SAR", "reason_code": "bad_debt",
      "posted_at": "2026-07-05T17:20:00Z",
      "approval_ref": "ar-approval-e5f6a7b8-2c3d-4e5f-8a9b-0c1d2e3f4a5b",
      "idempotency_key": "ar-idem:write_off:inv-077:1c3d" }
  ],
  "by_type": [
    { "type": "refund", "amount": "-500.00", "count": 1 },
    { "type": "write_off", "amount": "-250.00", "count": 1 }
  ],
  "approval_refs": [
    "ar-approval-d4e5f6a7-1b2c-3d4e-9f8a-7b6c5d4e3f2a",
    "ar-approval-e5f6a7b8-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
  ],
  "generated_at": "2026-07-06T09:15:00Z",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/expense-data.json`](../contracts/examples/expense-data.json).

---

## 6. ValidationResult (§9)

Outcome of validating an input/payload against a contract schema. Carries
`AR_VALIDATION_*` codes.

- **Schema**: [`../contracts/schemas/validation-result.schema.json`](../contracts/schemas/validation-result.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `valid` | boolean | required |
| `contract_name` | string | required (e.g. `InvoiceData`) |
| `contract_version` | semver | required (version validated against) |
| `trace_id` | string | required |
| `errors[]` | array of `issue` | present when `valid=false` |
| `warnings[]` | array of `issue` | non-blocking |

`issue` item: `path` (JSON pointer), `code` (`^AR_VALIDATION(_[A-Z_]+)?$`),
`message`, `rule_id?`.

Optional: `validated_at`, `schema_ref` (the schema `$id`).

Validation rules: `valid=false` requires ≥1 `errors[]` entry; `code` is a stable
`AR_VALIDATION_*` string the caller branches on (§9).

```json
{
  "valid": false,
  "contract_name": "InvoiceData",
  "contract_version": "1.0.0",
  "trace_id": "ar-trace-07f3a1d2",
  "errors": [
    { "path": "/data/total", "code": "AR_VALIDATION_AMOUNT",
      "message": "total must equal subtotal + tax - discounts.", "rule_id": "INV-001" },
    { "path": "/data/line_items/0/qty", "code": "AR_VALIDATION_PATTERN",
      "message": "qty must be a 2-decimal string.", "rule_id": "INV-002" }
  ],
  "warnings": [
    { "path": "/data/notes", "code": "AR_VALIDATION",
      "message": "notes field is long; ensure it contains no PII (§16).", "rule_id": "PII-001" }
  ],
  "validated_at": "2026-07-06T09:14:42Z",
  "schema_ref": "https://cosmic-vikings/ar-agent/contracts/invoice-data.schema.json"
}
```
Full example: [`../contracts/examples/validation-result.json`](../contracts/examples/validation-result.json).

---

## 7. CalculationResult (§8)

Output of a financial calculation (match, reconcile, aging, rounding). Produced
by AR calculation/reconciliation subflows (e.g. `ar_calculation`).

- **Schema**: [`../contracts/schemas/calculation-result.schema.json`](../contracts/schemas/calculation-result.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `tenant` | string | required |
| `calculation_type` | enum | required — `match\|reconcile\|aging\|rounding` |
| `totals{}` | map string→signed 2dp | required, ≥1 key; keys vary by type |
| `line_items[]` | array of `line_item` | required |
| `currency` | ISO-4217 | required |
| `contract_version` | semver | required |

`line_item` item: `label`, `amount` (signed 2dp), `source_refs[]` (≥1).

Optional: `inputs_ref`, `computed_at`, `rounding_adjustment` (signed 2dp).

Validation rules: `totals` keys are calculation-type-specific
(`matched\|outstanding\|posted` for match/reconcile; bucket totals for aging;
`rounding_adjustment` for rounding); values are signed 2dp strings.

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "calculation_type": "match",
  "totals": { "matched": "1500.00", "outstanding": "3500.00", "posted": "0.00" },
  "line_items": [
    { "label": "matched:INV-123:RCT-9087", "amount": "1500.00",
      "source_refs": ["zoho-inv-INV-123", "foodics-rct-RCT-9087", "pay-zoho-PMT-5512"] }
  ],
  "currency": "SAR",
  "inputs_ref": "a1b2c3d4-1a2b-3c4d-5e6f-7890abcdef01",
  "computed_at": "2026-07-06T09:14:48Z",
  "rounding_adjustment": "0.00",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/calculation-result.json`](../contracts/examples/calculation-result.json).

---

## 8. InvoiceData (§2)

A Zoho Books AR invoice. Authored by `ar_issue_invoice` (and assembled as a
draft by `ar_invoice_generation`). Customer referenced by id only (§16).

- **Schema**: [`../contracts/schemas/invoice-data.schema.json`](../contracts/schemas/invoice-data.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `invoice_id` | string | required (Zoho id) |
| `invoice_number` | string | required |
| `customer_ref` | string | required (no PII) |
| `tenant` | string | required |
| `issue_date` | date | required |
| `due_date` | date | required |
| `line_items[]` | array of `line_item` | required, ≥1 |
| `subtotal` | 2dp string | required, non-negative |
| `total` | 2dp string | required, non-negative |
| `currency` | ISO-4217 | required |
| `status` | enum | required — `draft\|sent\|open\|paid\|partial\|void\|overdue` |
| `balance_due` | 2dp string | required, non-negative |
| `contract_version` | semver | required |

`line_item` item: `line_id`, `item_ref`, `description`, `qty` (2dp string),
`unit_price` (non-neg 2dp), `amount` (non-neg 2dp).

Optional: `tax`, `discounts` (non-neg 2dp), `po_number`, `salesperson_ref` (no
PII), `notes` (no PII/secrets), `source_ref`.

Validation rules: each `line_item.amount` = `qty × unit_price` to 2dp;
`subtotal` = Σ `line_items[].amount`; `total` = `subtotal + tax − discounts`;
`balance_due` ≤ `total`.

```json
{
  "invoice_id": "zoho-inv-INV-123",
  "invoice_number": "INV-123",
  "customer_ref": "cust-cv-0421",
  "tenant": "cosmic-vikings-ksa",
  "issue_date": "2026-07-01",
  "due_date": "2026-07-15",
  "line_items": [
    { "line_id": "li-01", "item_ref": "item-catering-tray-A",
      "description": "Catering tray A — 10 pax", "qty": "1.00",
      "unit_price": "1200.00", "amount": "1200.00" },
    { "line_id": "li-02", "item_ref": "item-service-charge",
      "description": "Delivery + setup", "qty": "1.00",
      "unit_price": "300.00", "amount": "300.00" }
  ],
  "subtotal": "1500.00",
  "tax": "225.00",
  "discounts": "0.00",
  "total": "1725.00",
  "currency": "SAR",
  "status": "open",
  "balance_due": "1725.00",
  "po_number": "PO-CV-2026-0098",
  "salesperson_ref": "sp-cv-007",
  "notes": "Catering for branch opening event.",
  "source_ref": "INV-123",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/invoice-data.json`](../contracts/examples/invoice-data.json).

---

## 9. ApprovalRequest (§19)

What `ApprovalGateComponent` emits to request human approval. Carries the tier,
the proposed action, and the SSO-attributable requester.

- **Schema**: [`../contracts/schemas/approval-request.schema.json`](../contracts/schemas/approval-request.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `approval_id` | uuid | required |
| `trace_id` | string | required |
| `tenant` | string | required |
| `action` | string | required (e.g. `gl.post`) |
| `amount` | signed 2dp string | required |
| `currency` | ISO-4217 | required |
| `tier` | enum | required — `read-only\|auto\|approval\|dual-control` |
| `requested_by` | string | required (Keycloak sub) |
| `requested_at` | datetime | required |
| `proposal{}` | object | required — `{operation, target, …}` |
| `contract_version` | semver | required |

`proposal`: `operation`, `target`, `amount?`, `currency?`, `details?` (no PII).

Optional: `idempotency_key`, `expires_at`, `approval_ref` (assigned on issue;
present on the resume path), `second_approver_required`.

Validation rules: financial mutations require `tier` ≥ `approval` (§19);
`dual-control` requires `second_approver_required=true`.

```json
{
  "approval_id": "c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "action": "gl.post",
  "amount": "1500.00",
  "currency": "SAR",
  "tier": "approval",
  "requested_by": "auth0|keycloak-sub-cv-admin-001",
  "requested_at": "2026-07-06T09:15:00Z",
  "proposal": {
    "operation": "post_payment", "target": "INV-123",
    "amount": "1500.00", "currency": "SAR",
    "details": { "payment_ref": "pay-zoho-PMT-5512", "matched_receipt": "RCT-9087" }
  },
  "idempotency_key": "ar-idem:gl_post:inv-123:7f3a1d2e",
  "expires_at": "2026-07-06T21:15:00Z",
  "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "second_approver_required": false,
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/approval-request.json`](../contracts/examples/approval-request.json).

---

## 10. ApprovalResult (§19)

Outcome of a human approval. Non-reusable: one `approval_ref` authorizes exactly
one idempotent action. `consumed=true` once the authorized action has posted;
replay with the same ref is then rejected.

- **Schema**: [`../contracts/schemas/approval-result.schema.json`](../contracts/schemas/approval-result.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `approval_id` | uuid | required |
| `approval_ref` | `^ar-approval-<uuid>$` | required (non-reusable) |
| `decision` | enum | required — `approved\|rejected\|request_changes\|expired` |
| `decided_by` | string | required (Keycloak sub) |
| `decided_at` | datetime | required |
| `trace_id` | string | required |
| `contract_version` | semver | required |

Optional: `tier`, `second_approver_ref` (required for dual-control — distinct
from `decided_by`), `idempotency_key`, `reason`, `consumed` (default false).

Validation rules: for `tier=dual-control`, `second_approver_ref` must be present
and differ from `decided_by`; `consumed=true` blocks further replay (§19).

```json
{
  "approval_id": "c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "decision": "approved",
  "decided_by": "auth0|keycloak-sub-cv-finance-lead-002",
  "decided_at": "2026-07-06T09:22:00Z",
  "trace_id": "ar-trace-07f3a1d2",
  "tier": "approval",
  "idempotency_key": "ar-idem:gl_post:inv-123:7f3a1d2e",
  "reason": "Matched to RCT-9087; posting authorized.",
  "consumed": true,
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/approval-result.json`](../contracts/examples/approval-result.json).

---

## 11. ZohoUploadResult (§9/§10)

Outcome of a Zoho Books write (GL post, invoice issue, payment post, credit
note). Carries §9 `AR_*` codes and the idempotency key used (§10).

- **Schema**: [`../contracts/schemas/zoho-upload-result.schema.json`](../contracts/schemas/zoho-upload-result.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `tenant` | string | required |
| `operation` | enum | required — `gl_post\|invoice_issue\|payment_post\|credit_note` |
| `http_status` | integer 100–599 | required |
| `code` | enum | required — `AR_OK\|AR_DUPLICATE\|AR_UPSTREAM\|AR_AUTH\|AR_VALIDATION\|AR_FORBIDDEN\|AR_NOT_FOUND` |
| `idempotency_key` | `^ar-idem:…$` | required (§10) |
| `contract_version` | semver | required |

Optional: `zoho_id`, `zoho_ref`, `duplicate` (true when `code=AR_DUPLICATE`),
`raw_response_ref`, `attempted_at`, `attempts` (≥1).

Validation rules: `code=AR_DUPLICATE` implies `duplicate=true`; `attempts ≤ 3`
for transient retries (§10); raw response is referenced, never inlined (§9).

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "operation": "gl_post",
  "http_status": 200,
  "code": "AR_OK",
  "idempotency_key": "ar-idem:gl_post:inv-123:7f3a1d2e",
  "zoho_id": "zoho-pay-PMT-5512",
  "zoho_ref": "PMT-5512",
  "duplicate": false,
  "raw_response_ref": "store://zoho-resp/2026/07/06/07f3a1d2-gl-post.json",
  "attempted_at": "2026-07-06T09:22:45Z",
  "attempts": 1,
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/zoho-upload-result.json`](../contracts/examples/zoho-upload-result.json).

---

## 12. AuditRecord (§13)

Immutable audit record for an action that affects money or the ledger.
Append-only: correction is a new compensating entry, never an edit.

- **Schema**: [`../contracts/schemas/audit-record.schema.json`](../contracts/schemas/audit-record.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `audit_id` | uuid | required |
| `trace_id` | string | required (§12) |
| `tenant` | string | required |
| `actor` | string | required (Keycloak sub) |
| `action` | string | required (e.g. `gl.post`) |
| `timestamp` | datetime | required, UTC |
| `append_only` | const `true` | required — always true |
| `contract_version` | semver | required |

Optional: `approval_ref`, `idempotency_key`, `before` (state delta), `after`
(state delta), `source_system` (`zoho\|foodics`), `source_ref`,
`correlation_id`.

Validation rules: `append_only` is the constant `true`; `before`/`after` are
free-key deltas whose values are string/number/boolean/null (amounts as 2dp
strings); a correction is a new record, not an edit (§13).

```json
{
  "audit_id": "f1e2d3c4-9a8b-7c6d-5e4f-3a2b1c0d9e8f",
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "actor": "auth0|keycloak-sub-cv-finance-lead-002",
  "action": "gl.post",
  "timestamp": "2026-07-06T09:22:46Z",
  "append_only": true,
  "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "idempotency_key": "ar-idem:gl_post:inv-123:7f3a1d2e",
  "before": { "balance_due": "1725.00", "status": "open" },
  "after": { "balance_due": "225.00", "status": "partial" },
  "source_system": "zoho",
  "source_ref": "INV-123",
  "correlation_id": "e1b2c3d4-7a6b-5c4d-3e2f-1a0b9c8d7e6f",
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/audit-record.json`](../contracts/examples/audit-record.json).

---

## 13. Notification (§2/§19)

A dunning or approval notification dispatched by AR notification/approval
subflows (e.g. `ar_approval`). Recipients are referenced by id only — no PII in
the payload (§16).

- **Schema**: [`../contracts/schemas/notification.schema.json`](../contracts/schemas/notification.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `notification_id` | uuid | required |
| `trace_id` | string | required |
| `tenant` | string | required |
| `channel` | enum | required — `email\|sms\|chat\|in_app` |
| `recipient_ref` | string | required (customer id; no PII) |
| `template` | string | required (e.g. `dunning.reminder_1`) |
| `status` | enum | required — `queued\|sent\|failed` |
| `triggered_at` | datetime | required |
| `contract_version` | semver | required |

Optional: `approval_ref`, `body_ref`, `subject_ref`, `sent_at` (when
`status=sent`), `dunning_level` (≥1).

Validation rules: the rendered body/subject are referenced (`body_ref`/
`subject_ref`), never inlined — keeps PII out of logs (§16); the channel resolves
the address server-side from `recipient_ref`.

```json
{
  "notification_id": "b3c4d5e6-1a2b-3c4d-9e8f-7a6b5c4d3e2f",
  "trace_id": "ar-trace-07f3a1d2",
  "tenant": "cosmic-vikings-ksa",
  "channel": "email",
  "recipient_ref": "cust-cv-0421",
  "template": "dunning.reminder_1",
  "status": "sent",
  "triggered_at": "2026-07-06T08:00:00Z",
  "approval_ref": "ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e",
  "body_ref": "store://notify-body/2026/07/06/b3c4d5e6.json",
  "subject_ref": "store://notify-subject/2026/07/06/b3c4d5e6.txt",
  "sent_at": "2026-07-06T08:00:12Z",
  "dunning_level": 1,
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/notification.json`](../contracts/examples/notification.json).

---

## 14. ExecutionSummary (§14)

End-of-run summary returned in the envelope `data` field. Aggregates the run's
totals, approvals, audit refs and checkpoint handle.

- **Schema**: [`../contracts/schemas/execution-summary.schema.json`](../contracts/schemas/execution-summary.schema.json)
  · version `1.0.0` · `draft`.

| Field | Type | Rule |
|-------|------|------|
| `trace_id` | string | required |
| `flow_id` | string | required (§12) |
| `tenant` | string | required |
| `intent` | string | required |
| `status` | enum | required — `ok\|error\|pending_approval` (mirrors envelope) |
| `code` | `^AR_[A-Z_]+$` | required (§9) |
| `totals{matched, outstanding, posted}` | object | required; non-neg 2dp |
| `started_at` | datetime | required |
| `ended_at` | datetime | required |
| `contract_version` | semver | required |

Optional: `approvals[]` (`approval_ref`), `audit_refs[]`, `checkpoint_id`
(uuid), `subflows_invoked[]`, `error{code, message}`.

Validation rules: `totals` mirror `WorkflowState`'s three financial fields at
completion (§8); `status=error` requires an `error` object.

```json
{
  "trace_id": "ar-trace-07f3a1d2",
  "flow_id": "9b1d4e7a-3c8f-4a2b-9e6d-1f2a3b4c5d6e",
  "tenant": "cosmic-vikings-ksa",
  "intent": "issue_invoice",
  "status": "ok",
  "code": "AR_OK",
  "totals": { "matched": "1500.00", "outstanding": "225.00", "posted": "1500.00" },
  "started_at": "2026-07-06T09:14:30Z",
  "ended_at": "2026-07-06T09:22:50Z",
  "approvals": ["ar-approval-c2a7b1e4-6d5f-4a3b-8e2c-9f1a2b3c4d5e"],
  "audit_refs": ["f1e2d3c4-9a8b-7c6d-5e4f-3a2b1c0d9e8f"],
  "checkpoint_id": "e1b2c3d4-7a6b-5c4d-3e2f-1a0b9c8d7e6f",
  "subflows_invoked": ["ar_calculation", "ar_approval", "ar_issue_invoice", "ar_audit"],
  "contract_version": "1.0.0"
}
```
Full example: [`../contracts/examples/execution-summary.json`](../contracts/examples/execution-summary.json).

---

## Documented v1 caveats (not gaps)

The v1 implementation deviates from strict conformance in the following named,
tracked ways. Each is a deliberate scoping decision with a deferred follow-up —
**not** a latent defect to "fix" piecemeal. A deviation outside this list
requires a written waiver + ADR (per the authority note above).

- **`V1-ENVELOPE-META`** — the §14 envelope is strictly six keys
  (`status, code, data, error{message,detail}, trace_id, approval_ref`), and
  `execution-summary.schema.json` defines the richer run-metadata
  (`flow_id, tenant, intent, totals, audit_refs, checkpoint_id,
  subflows_invoked, started_at, ended_at, contract_version, approvals,
  error{code,message}`) as living **under the envelope `data` field**. The v1
  components, however, emit those run-metadata keys at the **envelope top
  level** (alongside `status`/`code`/`data`/…), and the adapter reads
  `checkpoint_id` from the top level (`adapter.py`). Self-tests assert this
  hybrid shape, so it is the de-facto v1 contract. The strict-conformance
  reshape — moving the metadata under `data.execution_summary` across the nine
  components + supervisor, updating the adapter's `checkpoint_id` read, and
  rewriting the self-tests + both schemas — is **deferred to v2** because it is
  a coordinated multi-file contract change, not a single-flow fix. Until then
  the top-level metadata is an accepted extension. The unambiguous
  §14-aligned sub-defects (omit empty `approval_ref`; strip `error.code` at the
  envelope `error` level; conditional `idempotency_key`; thread `trace_id` on
  `AR_UNEXPECTED`) **were** closed in this pass; only the metadata relocation is
  deferred here.
- **`V1-DUAL-CONTROL`** — §19 specifies dual-control approval (a second human
  approver distinct from the requester). The v1 Human Approval Flow models a
  single approval pause/resume (`HumanApprovalFlowComponent`, §19 interrupt);
  it records the approver and the decision but does **not** enforce a distinct
  second approver or a separation-of-duties check. Enforcing dual control
  (second-approver identity, SoD, approval-chain immutability) is a
  build-phase feature tracked here — a documentation caveat, not a behavioral
  regression, since no v1 path mutates money on approval alone (the §1
  approval boundary + `approval_ref`+`idempotency_key` gate still holds).
- **`V1-RESUME`** — the supervisor's **Flow-as-Tool routing is now live**
  (resolved). Previously `_node_invoke` returned `AR_NOT_FOUND` ("Subflow
  '<flow>' is not wired on the canvas") for every routed subflow: each `RunFlow`
  node's `to_toolkit` output builds a LangChain tool named
  `<flow_name_selected>_tool` (e.g. `ar_calculation_tool`) via `lfx`'s
  `ComponentToolkit.get_tools` (`tool_name=f"{flow_name_selected}_tool"`,
  then `_format_tool_name`), `SupervisorAgentComponent._tools_by_name` indexed
  those tools by `tool.name`, and `_node_invoke` looked the tool up by the bare
  `intent` (`ar_calculation`) — which was not a key — so the lookup missed and
  the no-such-tool branch fired `AR_NOT_FOUND`. Fix: `_tools_by_name` now also
  indexes each `<flow>_tool` tool under the stripped bare name (`name[:-5]`
  when `name.endswith("_tool")`), retaining the full-name key. Verified live
  against LangFlow 1.10.1 / `lfx` 1.10.1: every routed subflow now populates
  `subflows_invoked` (no `AR_NOT_FOUND`); `ar_approval` and `ar_issue_invoice`
  run end-to-end (`pending_approval` / `AR_APPROVAL_REQUIRED`). The remaining
  Flow-as-Tool live-interaction gap — the input-binding caveat below — is now
  also resolved (see `V1-FLOW-TWEAK-DATA` / `V1-RUNFLOW-TOOL-INPUT`); the
  LangGraph §19 resume path (`Command(resume=approval_ref)`) is still
  build-phase.
- **`V1-FLOW-TWEAK-DATA`** (resolved) — when the supervisor invokes a `RunFlow`
  subflow as a tool, `lfx` derives an `InputSchema` whose sole required field is
  `flow_tweak_data` (an `InnerModel` whose one sub-field is named after the
  subflow's `ChatInput` node, e.g. `ChatInput-ar001~input_value`, type `str`).
  The old `_call_tool` called `tool.invoke({"input_value": …})` (sync, wrong
  shape); the `RunFlow` tools are async-only `StructuredTool`s, so sync `invoke`
  raised `NotImplementedError` / pydantic `ValidationError` for all 9 subflows,
  surfaced as `AR_UNEXPECTED` ("subflow <flow> failed: … InputSchema /
  flow_tweak_data"). Fix: `_call_tool` now derives the sub-field dynamically from
  `tool.args_schema.model_fields["flow_tweak_data"].annotation` and invokes the
  tool async via a sync bridge (`asyncio.run(tool.ainvoke({"flow_tweak_data":
  {<sub-field>: user_input}}))`). The bridge is sync because the supervisor's
  output method runs sync under `lfx` (`asyncio.to_thread`, no running loop in
  the worker thread) and `lfx`'s custom-component loader only exposes SYNC
  module-level free functions to the component's methods (`ast.FunctionDef`
  filter in `lfx/custom/validate.py` — an `async def` here is silently dropped
  from the method globals → `NameError`). Verified live on LangFlow 1.10.1 /
  `lfx` 1.10.1: the `flow_tweak_data` `AR_UNEXPECTED` is gone; every routed
  subflow now executes (returns its own `AR_*` envelope, not the tool-invocation
  error).
- **`V1-RUNFLOW-TOOL-INPUT`** (resolved) — the deeper half of Flow-as-Tool live
  interaction. Even with the correct `flow_tweak_data` shape, the user's input
  did not reach the subflow (it ran with an empty `ChatInput` →
  `AR_VALIDATION` "payload JSON parse error"). Root cause is two `lfx` 1.10.1
  behaviours: (1) the `RunFlow` tool's dynamic output resolver is built as
  `MethodType(_dynamic_resolver, self)` bound to the **original** `RunFlow`
  component at tool-build time (`lfx/base/tools/run_flow.py
  _register_flow_output_method`); (2) at invoke time `lfx`'s `output_function`
  deepcopies that component and calls `comp.set(flow_tweak_data=…)` on the
  **copy** (`lfx/base/tools/component_tool.py`), but the resolver still runs on
  the original — so the per-call `flow_tweak_data` is ignored and results cache
  (`_last_run_outputs`) on the original. Fix: `_call_tool` recovers the original
  `RunFlowBaseComponent` from the tool's `coroutine`/`func` closure cells
  (`_extract_runflow_component`), sets this call's `flow_tweak_data` on it
  (`comp.set(flow_tweak_data=InnerModel(**{<sub-field>: user_input}))`) and
  resets `comp._last_run_outputs = None` before invoking, so the resolver reads
  fresh input. This is race-free because the supervisor runs one subflow at a
  time (sync `graph.invoke`). Verified live: `ar_calculation` runs end-to-end
  through the supervisor → `AR_OK`. Note: this verification used a pure-JSON
  tool input (it bypassed `_finalize_envelope`), so it did NOT catch the
  payload-extraction gap below (`V1-PAYLOAD-EXTRACT`).
- **`V1-PAYLOAD-EXTRACT`** (resolved) — the deeper live blocker. The OpenAI
  adapter forwards the user's raw chat text as `input_value`; that text is
  natural language with an embedded JSON payload (e.g. ``"Calculate AR for
  January with this payload JSON: {…}"``) because the classifier matches NL
  keywords. But every JSON subflow ``json.loads`` its `ChatInput` directly and
  rejects the NL prefix → `AR_UNEXPECTED "payload JSON parse error: Expecting
  value: line 1 column 1 (char 0)"`. The supervisor's `_node_invoke` passed the
  whole `user_input` straight to the subflow. Fix: `_subflow_input(user_input,
  intent)` extracts the first balanced `{…}` JSON object (brace-balanced,
  string-literal/escape aware, validated to parse to a JSON object) and hands
  only that to JSON subflows; `ar_approval` (natural-language decision reply)
  is passed through verbatim; if no JSON object is found, the raw message is
  passed so the subflow returns its own graceful `AR_VALIDATION` (§9). Verified
  live through the real REST run path: `ar_calculation` with a real NL+JSON
  message → `AR_OK` (`subflows_invoked=["ar_calculation"]`, 3 audit checkpoints,
  checkpoint present); `ar_audit` → `AR_OK`; `ar_kitchen_revenue` routes +
  executes and returns its own `"no readable files supplied"` (no parse-error
  regression); `ar_issue_invoice` still gates to `pending_approval` /
  `AR_APPROVAL_REQUIRED` with a minted `approval_ref` (§19 intact). The
  supervisor's top-level `totals` (`matched`/`outstanding`/`posted`) stay
  `"0.00"` and the envelope `data` is `null` on the `AR_OK` path because
  `_finalize_envelope`'s `base` carries no `data` and `_node_invoke` only lifts
  `totals`/`audit_refs` into top-level fields — the subflow's computed figures
  (e.g. `ar_calculation`'s 9 totals under its own `data.calculation_result`)
  are not yet surfaced in the supervisor envelope. That cross-flow
  run-metadata / result-merge reshape is the deferred `V1-ENVELOPE-META`
  (v2 — coordinated adapter + self-test contract change), not a regression
  introduced here.
- **`V1-RESULT-SURFACE`** (resolved) — the supervisor's `AR_OK` envelope
  dropped the subflow's computed figures: `_finalize_envelope` built `base`
  with no `data`, and `_node_invoke` only lifted `totals`/`audit_refs` into
  top-level fields, so the response was `{"status":"ok","code":"AR_OK",
  "data":null,...}` with no numbers (the subflow's real figures lived under its
  own `data.calculation_result.totals` and never reached the response). Fix:
  `_node_invoke` now stores the subflow's §14 `data` into a new additive
  `AgentState.result_data` field (defaulted `None` — backward compatible, same
  pattern as the ADR-0003 orchestration fields); `_finalize_envelope`'s `base`
  sets `data = {"result": state.result_data} if state.result_data else {}`. The
  subflow payload is nested under `data.result` (not flat) so the deferred
  `data.execution_summary` (V1-ENVELOPE-META) can be added later without
  restructuring. The `pending_approval`/`awaiting_approval` branches still
  override `data` to `{action, tier}` (approval contract unchanged). On the
  `failed` path `data.result` carries the subflow's `validation_report`/
  `exception_report`. **Deploy note:** because `agent_state.py` is a regular
  imported module cached in the long-running LangFlow process's `sys.modules`
  (unlike the embedded component code, which lfx recompiles per run), this
  change required a `docker restart aiplatform-langflow-1` to reload the
  module — the no-restart property held by the prior V1-* deploys (which only
  touched embedded code) does NOT extend to imported-module edits. Verified
  live: `ar_calculation` NL+JSON → `AR_OK` with `data.result.calculation_result
  .totals` = the 9 figures (revenue 9700.00, vat 1455.00, municipality_tax
  1358.00, royalty 194.00, collections 5000.00, expenses 4000.00,
  net_receivable 7013.00, net_payable 5552.00); `ar_audit` → `AR_OK` with
  `data.result` (audit_log/execution_summary/…); `ar_issue_invoice` →
  `pending_approval` / `AR_APPROVAL_REQUIRED` with `data={action,tier}` (§19
  intact); `ar_kitchen_revenue` → error with `data.result.validation_report`
  (no parse-error regression).

- **`V1-STUB`** (resolved for Zoho + Foodics) — the vendor-touching AR subflows
  were transport-stubbed: `ar_issue_invoice` used `StubZohoUpload` (deterministic
  fake `zoho-inv-<uuid5>` IDs) and `ar_foodics_processing`'s API path hit a
  broken cross-bundle import (`from components.ar_tools.foodics_ar import
  FoodicsARTool` — `ar_tools` was never on `sys.path`) → `None` →
  `AR_NOT_IMPLEMENTED`. Real transports now live in `ar_common`:
  `zoho_transport.RealZoho` (OAuth refresh-on-401 + POST `/invoices` + DELETE
  `/invoices/{id}`, `organization_id` query param — mirrors `ap_tools`'s working
  pattern) wired via `ZohoUploadFlowComponent.run` → `set_transport(RealZoho(
  creds))`; `foodics_transport.RealFoodics` (OAuth 2.0 client-id/secret/refresh
  → 14-day Bearer + `X-Business`, `list_orders`/`list_order_items`/
  `list_order_payments`, Laravel pagination, transient raises so the §10 loop
  owns retry) wired via `foodics_processing._make_foodics_fetcher()` → the new
  `set_foodics_creds(creds)` seam set by `FoodicsProcessingFlowComponent.run`.
  Both resolve credentials **by name** from LangFlow Secret Global Variables via
  `vendor_secrets.read_secret(component, name)` (the subflow component carries
  `user_id`; no `SecretStrInput` added → no flow-JSON surgery); when a required
  cred is absent they keep the fail-safe path (Zoho → `StubZohoUpload`; Foodics
  → files / `AR_NOT_IMPLEMENTED`). Deployed via re-embed + in-place PATCH (both
  subflow UUIDs unchanged → no adapter repoint) + `docker restart
  aiplatform-langflow-1` (the new imported modules `vendor_secrets.py`/
  `zoho_transport.py`/`foodics_transport.py` are cached in `sys.modules`).
  Offline `make test` stays green (22 suites / 1776 checks); egress verified
  (TCP:443 to `accounts.zoho.com` / `www.zohoapis.com` / `api.foodics.com` /
  `console-sandbox.foodics.com`); no-creds regression sweep clean
  (`ar_foodics_processing` routes → §19 `pending_approval`; `ar_calculation` →
  `AR_OK`). **Live real-vendor calls are gated on the operator creating the
  Secret Global Variables** — see [`environment.md`](environment.md#langflow-
  secret-global-variables-managed-in-the-langflow-ui--not-env). **Infrasys
  remains absent** (no flow/transport; Shiji partner endorsement is long-lead).

For the historical build-phase caveat set still carried by the individual flows
(PDF/Excel render-ready specs, InMemorySaver non-durability, draft-only gates,
cross-subflow audit auto-accumulation, `SecretStrInput` absence on the subflow
components, resume-path live interaction, the retired `ar08` RunFlow slot), see
the per-flow docs and the cited ADRs.

---

## Appendix — validating

See [`../contracts/README.md`](../contracts/README.md) for the layout, the
one-shot validate script, and the versioning file-naming rules. The validate
script reads `registry.json` and checks every example against its schema.