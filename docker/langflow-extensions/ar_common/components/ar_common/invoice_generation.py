"""Cosmic AR Agent — Invoice Generation Flow component (constitution §8, architecture §4 row 15).

The Invoice Generation Flow is the 15th AR subflow (ADR-0009). It takes a
**validated-JSON invoice request** — ``{customer_ref, line_items, totals, dates,
currency, ...}`` — assembles a draft ``InvoiceData`` (§15 reuse of the existing
contract), and generates **eight invoice artifacts** as structured JSON in the
§14 envelope: **Invoice JSON, Invoice PDF, Invoice Excel, Journal Entry,
Customer Statement, Zoho Upload File, Invoice Metadata**, and a **WorkflowState**
snapshot — with logging (§12) and **checkpoints after every generation step**
(§11).

It is distinct from ``ar_issue_invoice`` (#7, approval tier, in
``FINANCIAL_INTENTS`, keywords "issue/create/present/new invoice"), which *posts*
an invoice to Zoho. This flow only *generates draft artifacts for review* — no
posting, no idempotency key, no ``pending_approval``, **not** in
``FINANCIAL_INTENTS``, §19 gate dormant — §1 north star preserved.

**v1 PDF/Excel are render-ready JSON specs, not binaries** (ADR-0009): the
``langflow` image has no PDF-writing library, there is no file-delivery path
back to LibreChat, and no app MinIO bucket. So ``data.invoice_pdf`` /
``data.invoice_excel`` carry ``render_ready:true`` layout specs; real ``.pdf`` /
``.xlsx` materialization is a documented build-phase step (reportlab Dockerfile +
MinIO artifact bucket + adapter file-delivery) — the ADR-0007 §4 precedent.

**No §55/§3 waiver** — invoice generation is in-scope (constitution §2 "AR
invoice presentment (Zoho Books)"; §19 names "invoice issuance"). This flow
generates *draft* artifacts without the issuance POST, so no statutory-filing /
VAT concern (unlike ADR-0008's VAT-figures waiver). The Journal Entry is a
**draft** double-entry spec (``status="draft"``, no POST — §1).

Responsibilities → LangGraph nodes:

  ingest → validate_payload → classify_exceptions → build_invoice →
  build_journal_entry → build_customer_statement → build_zoho_upload →
  build_metadata → build_pdf_spec → build_excel_spec → build_state →
  checkpoint → respond

  - ingest              : parse the validated-JSON invoice request from
                          ``user_input``; bind ``trace_id``/``flow_id``/``tenant``
                          + timestamps; carry ``layout`` + ``model_name`` in
                          **context** (§8). Malformed JSON → ``AR_VALIDATION``.  §9
  - validate_payload    : inline hand-rolled validator for the invoice-request
                          contract (customer_ref present, line_items non-empty +
                          each {item_ref, description, qty>0, unit_price>0},
                          totals 2dp & consistent, issue_date ISO, currency
                          ``^[A-Z]{3}$``). Hard fail (no customer_ref / no
                          line_items / no issue_date) → ``AR_VALIDATION``; per-line
                          / totals / currency issues are warnings. Builds the full
                          ``ValidationResult``.                                 §9
  - classify_exceptions : the Exception Report = a ``ValidationResult`` scoped
                          to the warnings/failures.                             §4
  - build_invoice       : assemble the ``InvoiceData`` (the **Invoice JSON**
                          artifact) — deterministic ``uuid5`` invoice_id /
                          invoice_number shaped ``IG-<customer>-<8hex>``,
                          ``status="draft"``, ``due_date = issue + NET_TERMS_DAYS``,
                          2dp amounts, inline ``_validate_invoice`` guard. **Records
                          a checkpoint** ``"invoice"``.                         §15
  - build_journal_entry : draft GL **Journal Entry** (flow-specific JSON) —
                          balanced double-entry (debit AR / credit Revenue +
                          Tax Payable / debit Discounts; ``total_debit ==
                          total_credit``), ``status="draft"`` (no POST). **Records
                          a checkpoint** ``"journal_entry"``.                  §1
  - build_customer_statement : **Customer Statement** (flow-specific JSON) —
                          opening_balance "0.00" (v1: no prior AR history), the
                          invoice, closing_balance = total, aging. **Records a
                          checkpoint** ``"customer_statement"``.
  - build_zoho_upload   : **Zoho Upload File** (flow-specific JSON, mirrors
                          foodics ``data.zoho_upload``) — one zoho-books-invoice-
                          import row. **Records a checkpoint** ``"zoho_upload"``. §15
  - build_metadata      : **Invoice Metadata** (flow-specific JSON) — deterministic
                          content hash + source_refs. **Records a checkpoint**
                          ``"invoice_metadata"``.
  - build_pdf_spec      : **Invoice PDF** render-ready spec (flow-specific JSON,
                          ``render_ready:true``) — not a binary (build-phase). **Records
                          a checkpoint** ``"invoice_pdf"``.
  - build_excel_spec    : **Invoice Excel** render-ready spec (flow-specific JSON,
                          ``render_ready:true``) — not a binary (build-phase). **Records
                          a checkpoint** ``"invoice_excel"``.
  - build_state         : ``WorkflowState`` snapshot (status="completed", totals
                          ``"0.00"`` — no money moved). Immutable (§8).
  - checkpoint          : append the final aggregate audit id; reflect
                          ``audit_refs`` + ``checkpoints`` into the snapshot.
                          ``InMemorySaver`` persists state.                     §11
  - respond             : ``_finalize_envelope`` builds the §14 envelope.       §14

**Checkpoints after every generation step** (the stricter §11 pattern from
ADR-0006/ADR-0007/ADR-0008): each ``build_*`` node records a labeled
``_audit_ref` into ``audit_refs`` and a ``checkpoints`` map (``{invoice,
journal_entry, customer_statement, zoho_upload, invoice_metadata, invoice_pdf,
invoice_excel}`` + the final ``ar_invoice_generation`` aggregate), persisted by
``InMemorySaver`` at each super-step.

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id`` (the
§11 fallback — non-durable). The supervisor's ``_node_invoke`` merges only
``data.totals{matched,outstanding,posted}`` and ``data.audit_refs`` into
``AgentState``; the 8 artifacts are NOT those keys → they stay in the envelope
``data`` (no ``AgentState`` schema change — mirrors ADR-0006 §7 / ADR-0007 §8 /
ADR-0008).

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. No PII/secrets (§12/§16) — ``customer_ref``
is an id, never customer PII.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1). Tunables belong in Global Variables (§17) / the
#  overridable `layout` input; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors kitchen_revenue)
FLOW_ID: str = "ar_invoice_generation"
NET_TERMS_DAYS: int = 30  # due_date = issue_date + 30 (mirrors intercompany/foodics)

# 2dp / date / currency patterns (the contracts' patterns).
RE_2DP = re.compile(r"^-?\d+\.\d{2}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")

_TWO_PLACES = Decimal("0.01")

# The 8 artifacts this flow produces (in envelope data order).
ARTIFACT_KEYS: tuple[str, ...] = (
    "invoice", "journal_entry", "customer_statement", "zoho_upload",
    "invoice_metadata", "invoice_pdf", "invoice_excel", "workflow_state",
)

# Declarative PDF/Excel layout spec — the `layout` input default (§17 tunable,
# overridable). The real .pdf/.xlsx renderer (build-phase) reads this; v1 carries
# it through `data.invoice_pdf.layout` so the spec is self-describing.
LAYOUT: dict[str, Any] = {
    "page": {"size": "A4", "margins": {"top": 40, "bottom": 40, "left": 25, "right": 25}},
    "sections": ["header", "bill_to", "line_items_table", "totals", "footer"],
    "currency_position": "after",
    "locale": "en",
    "date_format": "YYYY-MM-DD",
}
LAYOUT_JSON: str = json.dumps(LAYOUT, indent=2)


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class InvoiceGenerationContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[InvoiceGenerationContext]``.

    Durable, resumable state lives in ``InvoiceGenerationState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    layout: dict  # parsed layout spec (the `layout` input default, overridable)
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvoiceGenerationState:
    """The Invoice Generation Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|validated|classified|invoiced|journaled|stated|zoho|metadata|
    # pdf_spec|excel_spec|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    payload: Optional[dict] = None  # the parsed invoice request
    invoice: Optional[dict] = None  # InvoiceData (the Invoice JSON artifact)
    journal_entry: Optional[dict] = None
    customer_statement: Optional[dict] = None
    zoho_upload: Optional[dict] = None
    invoice_metadata: Optional[dict] = None
    invoice_pdf: Optional[dict] = None
    invoice_excel: Optional[dict] = None
    validation_report: Optional[dict] = None  # full ValidationResult
    exception_report: Optional[dict] = None  # ValidationResult scoped to failures
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<label>: audit_ref} (§11)


def _state_to_dict(state: Any) -> dict:
    """Coerce an ``InvoiceGenerationState`` (or dict) snapshot to a plain dict."""
    if isinstance(state, dict):
        return state
    if hasattr(state, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(state)
    return {}


# --------------------------------------------------------------------------- #
#  Pure helpers (testable without LangFlow/LangGraph).
# --------------------------------------------------------------------------- #


def _to_str(value: Any) -> str:
    """Coerce an lfx input value (str / Message / None) to a plain string."""
    if value is None:
        return ""
    text = getattr(value, "text", None)
    if text is not None:
        return str(text)
    return str(value)


def utc_now() -> str:
    """UTC ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` (contracts' timestamp pattern)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mint_id() -> str:
    """A fresh lowercase uuid4 string (trace_id seed)."""
    return str(uuid.uuid4())


def _envelope(status: str, code: str, data: Optional[dict] = None,
              error: Optional[dict] = None, trace_id: str = "") -> dict[str, Any]:
    """Build a §14 envelope dict."""
    env: dict[str, Any] = {"status": status, "code": code, "data": data or {},
                           "trace_id": trace_id}
    if error:
        env["error"] = error
    return env


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a numeric string to ``Decimal``; ``None`` on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    s = str(value).strip().replace(",", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        m = re.search(r"-?\d+(\.\d+)?", s)
        if not m:
            return None
        try:
            return Decimal(m.group(0))
        except (InvalidOperation, ValueError):
            return None


def _to_2dp(value: Any) -> str:
    """Coerce a numeric to a non-negative 2dp string; ``"0.00"`` on failure."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    q = d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if q < 0:
        q = Decimal("0.00")
    return f"{q}"


def _to_signed_2dp(value: Any) -> str:
    """Coerce a numeric to a signed 2dp string (allows negatives)."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    return f"{d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)}"


def _sum_2dp(amounts: list[str]) -> str:
    """Sum a list of 2dp-string amounts to a 2dp string."""
    total = Decimal("0.00")
    for a in amounts:
        try:
            total += Decimal(str(a))
        except (InvalidOperation, ValueError):
            continue
    return f"{total.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)}"


def _parse_date(value: str) -> Optional[str]:
    """Parse a date string to ``YYYY-MM-DD``; ``None`` when unparseable."""
    s = (value or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _add_days(date_str: str, days: int) -> str:
    """Add ``days`` to a ``YYYY-MM-DD`` date; passthrough on unparseable."""
    d = _parse_date(date_str)
    if not d:
        return date_str
    try:
        dt = datetime.strptime(d, "%Y-%m-%d") + timedelta(days=days)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return d


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-generation audit record id (§11/§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"invoice-generation-audit:{trace_id}:{label}"))


def _content_hash(invoice: dict, trace_id: str) -> str:
    """Deterministic content hash of the canonical InvoiceData JSON (uuid5).

    ``uuid5`` is reproducible from its inputs (§4.3) — the same invoice JSON
    always yields the same hash. Used by ``build_metadata``.
    """
    canonical = json.dumps(invoice, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"invoice-content:{trace_id}:{canonical}"))


# --------------------------------------------------------------------------- #
#  Payload parse / validation / exception report (pure).
# --------------------------------------------------------------------------- #


def _parse_payload(user_input: str) -> tuple[Optional[dict], Optional[dict]]:
    """Parse the validated-JSON invoice request from ``user_input``.

    Returns ``(payload, error)`` — exactly one is set. Malformed JSON / non-object
    → ``AR_VALIDATION`` (§9).
    """
    text = (user_input or "").strip()
    if not text:
        return None, {"code": "AR_VALIDATION",
                      "message": "no invoice-request payload supplied"}
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, {"code": "AR_VALIDATION",
                      "message": f"payload JSON parse error: {exc}"}
    if not isinstance(obj, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "payload must be a JSON object"}
    return obj, None


def _build_validation_report(valid: bool, errors: list[dict],
                             warnings: list[dict],
                             trace_id: str) -> dict:
    """Build a ``ValidationResult`` (pure). ``contract_name="InvoiceGenerationInputs"``."""
    return {
        "valid": valid,
        "contract_name": "InvoiceGenerationInputs",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": list(errors),
        "warnings": list(warnings),
    }


def _validate_line_item(li: Any, idx: int) -> list[dict]:
    """Validate one line item → list of warning entries (no hard errors here)."""
    warns: list[dict] = []
    if not isinstance(li, dict):
        warns.append({"path": f"line_items[{idx}]", "code": "AR_VALIDATION",
                      "message": f"line_items[{idx}] must be an object",
                      "rule_id": "ig.line_item_object"})
        return warns
    if not li.get("item_ref"):
        warns.append({"path": f"line_items[{idx}].item_ref", "code": "AR_VALIDATION",
                      "message": f"line_items[{idx}] missing item_ref",
                      "rule_id": "ig.line_item_item_ref"})
    if not li.get("description"):
        warns.append({"path": f"line_items[{idx}].description", "code": "AR_VALIDATION",
                      "message": f"line_items[{idx}] missing description",
                      "rule_id": "ig.line_item_description"})
    qty = _to_decimal(li.get("qty"))
    if qty is None or qty <= 0:
        warns.append({"path": f"line_items[{idx}].qty", "code": "AR_VALIDATION_AMOUNT",
                      "message": f"line_items[{idx}].qty must be > 0 — "
                                 f"will contribute 0.00",
                      "rule_id": "ig.line_item_qty"})
    price = _to_decimal(li.get("unit_price"))
    if price is None or price <= 0:
        warns.append({"path": f"line_items[{idx}].unit_price",
                      "code": "AR_VALIDATION_AMOUNT",
                      "message": f"line_items[{idx}].unit_price must be > 0 — "
                                 f"will contribute 0.00",
                      "rule_id": "ig.line_item_unit_price"})
    return warns


def _validate_payload(payload: dict, trace_id: str) -> tuple[dict, list[dict], Optional[dict]]:
    """Validate the invoice-request contract; build the full ``ValidationResult``.

    Returns ``(validation_report, warnings, error)``. A hard failure (no
    customer_ref / no line_items / no issue_date) sets ``error`` (→ ``failed``);
    per-line / totals / currency issues are warnings (fail-safe — §4).
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    customer_ref = payload.get("customer_ref")
    if not customer_ref or not str(customer_ref).strip():
        errors.append({"path": "customer_ref", "code": "AR_VALIDATION",
                       "message": "customer_ref is required",
                       "rule_id": "ig.customer_ref_required"})

    items = payload.get("line_items")
    if not isinstance(items, list) or not items:
        errors.append({"path": "line_items", "code": "AR_VALIDATION",
                       "message": "line_items must be a non-empty array",
                       "rule_id": "ig.line_items_required"})
    else:
        for i, li in enumerate(items):
            warnings.extend(_validate_line_item(li, i))

    issue = _parse_date(str(payload.get("issue_date") or ""))
    if not issue:
        errors.append({"path": "issue_date", "code": "AR_VALIDATION_DATE",
                       "message": "issue_date is required and must be ISO YYYY-MM-DD",
                       "rule_id": "ig.issue_date_required"})

    currency = payload.get("currency")
    if currency and not RE_CURRENCY.match(str(currency)):
        warnings.append({"path": "currency", "code": "AR_VALIDATION_CURRENCY",
                         "message": "currency must match ^[A-Z]{3}$ — defaulting to SAR",
                         "rule_id": "ig.currency"})

    # Totals consistency (when a totals block is supplied) — warning, not hard.
    totals = payload.get("totals")
    if isinstance(totals, dict):
        sub = _to_decimal(totals.get("subtotal"))
        tax = _to_decimal(totals.get("tax")) or Decimal("0.00")
        disc = _to_decimal(totals.get("discounts")) or Decimal("0.00")
        tot = _to_decimal(totals.get("total"))
        if sub is not None and tot is not None:
            expected = (sub + tax - disc).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
            if (tot - expected).quantize(_TWO_PLACES) != Decimal("0.00"):
                warnings.append({"path": "totals", "code": "AR_VALIDATION_AMOUNT",
                                 "message": f"totals inconsistent — total {tot} != "
                                            f"subtotal + tax - discounts ({expected})",
                                 "rule_id": "ig.totals_consistency"})

    if errors:
        report = _build_validation_report(False, errors, warnings, trace_id)
        return report, warnings, {"code": "AR_VALIDATION",
                                  "message": errors[0]["message"]}
    report = _build_validation_report(True, [], warnings, trace_id)
    return report, warnings, None


def _classify_exceptions(validation_report: dict, trace_id: str) -> dict:
    """Build the Exception Report = a ``ValidationResult`` scoped to failures.

    Mirrors the calculation / kitchen-revenue exception report: errors + the
    per-line/totals warnings that mean a figure was defaulted (the "exceptions").
    With no hard errors, the exception report carries the warnings (so the
    consumer sees which lines/totals were flagged).
    """
    errs = list((validation_report or {}).get("errors") or [])
    warns = list((validation_report or {}).get("warnings") or [])
    items = errs + [w for w in warns
                    if w.get("code") in ("AR_VALIDATION_AMOUNT",
                                         "AR_VALIDATION_CURRENCY",
                                         "AR_VALIDATION_DATE")]
    valid = len(items) == 0
    return _build_validation_report(valid, errs, items, trace_id)


# --------------------------------------------------------------------------- #
#  Artifact builders (pure).
# --------------------------------------------------------------------------- #


def _deterministic_invoice_id(trace_id: str, customer_ref: str,
                              issue_date: str) -> tuple[str, str]:
    """Derive a deterministic (trace+customer+issue) invoice_id + invoice_number.

    ``uuid5`` is reproducible from its inputs (§4.3) — no ``Math.random`` /
    ``uuid4`` here, so the same trace + customer + issue always yields the same
    invoice ids. Shaped ``IG-<customer>-<8hex>`` (mirrors foodics ``FP-`` /
    intercompany ``IC-``).
    """
    seed = f"invoice-gen:{trace_id}:{customer_ref}:{issue_date}"
    u = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    return str(u), f"IG-{customer_ref}-{u.hex[:8].upper()}"


def _build_invoice(payload: dict, trace_id: str, tenant: str) -> dict:
    """Assemble the ``InvoiceData`` (the Invoice JSON artifact) — pure.

    ``amount = qty × unit_price`` per line (2dp, defaulted to 0.00 on bad input —
    fail-safe §4). ``subtotal`` = Σ line amounts; ``total = subtotal + tax -
    discounts``; ``balance_due = total``; ``status="draft"``; ``due_date = issue
    + NET_TERMS_DAYS``. Optional ``po_number`` / ``salesperson_ref`` / ``notes``
    passed through (no PII — §16). Deterministic ``uuid5`` ids.
    """
    customer_ref = str(payload.get("customer_ref") or "CUST-UNKNOWN")
    issue = _parse_date(str(payload.get("issue_date") or "")) \
        or time.strftime("%Y-%m-%d", time.gmtime())
    due = _add_days(issue, NET_TERMS_DAYS)
    currency = str(payload.get("currency") or DEFAULT_CURRENCY).upper()
    if not RE_CURRENCY.match(currency):
        currency = DEFAULT_CURRENCY
    tax = _to_2dp(payload.get("tax") or (payload.get("totals") or {}).get("tax") or "0.00")
    discounts = _to_2dp(payload.get("discounts")
                        or (payload.get("totals") or {}).get("discounts") or "0.00")

    line_items: list[dict] = []
    line_amounts: list[str] = []
    for i, li in enumerate(payload.get("line_items") or []):
        if not isinstance(li, dict):
            continue
        item_ref = str(li.get("item_ref") or f"item-{i + 1}")
        desc = str(li.get("description") or item_ref)
        qty = _to_decimal(li.get("qty")) or Decimal("0.00")
        price = _to_decimal(li.get("unit_price")) or Decimal("0.00")
        amount = (qty * price).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        if amount < 0:
            amount = Decimal("0.00")
        amt_s = f"{amount}"
        line_amounts.append(amt_s)
        line_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                 f"invoice-line:{trace_id}:{item_ref}:{i}"))
        line_items.append({
            "line_id": line_id,
            "item_ref": item_ref,
            "description": desc,
            "qty": _to_2dp(qty),
            "unit_price": _to_2dp(price),
            "amount": amt_s,
        })

    subtotal = _sum_2dp(line_amounts)
    total_d = (Decimal(subtotal) + Decimal(tax) - Decimal(discounts)) \
        .quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if total_d < 0:
        total_d = Decimal("0.00")
    total = f"{total_d}"
    inv_id, inv_num = _deterministic_invoice_id(trace_id, customer_ref, issue)

    inv: dict[str, Any] = {
        "invoice_id": inv_id,
        "invoice_number": inv_num,
        "customer_ref": customer_ref,
        "tenant": tenant,
        "issue_date": issue,
        "due_date": due,
        "line_items": line_items,
        "subtotal": subtotal,
        "total": total,
        "currency": currency,
        "status": "draft",
        "balance_due": total,
        "contract_version": CONTRACT_VERSION,
    }
    # Optional fields (no PII — §16): tax, discounts, po_number, salesperson_ref,
    # notes, source_ref. tax/discounts are always emitted (AR-bundle convention).
    inv["tax"] = tax
    inv["discounts"] = discounts
    po = payload.get("po_number")
    if po:
        inv["po_number"] = str(po)
    sp = payload.get("salesperson_ref")
    if sp:
        inv["salesperson_ref"] = str(sp)
    notes = payload.get("notes")
    if notes:
        inv["notes"] = str(notes)
    inv["source_ref"] = f"invoice-gen:{trace_id}"
    return inv


def _validate_invoice(inv: dict) -> list[str]:
    """Inline hand-rolled ``InvoiceData`` validation → list of error strings.

    Checks the required fields and 2dp patterns (mirrors the schema). Used by the
    ``build_invoice`` node as a guard; wiring this into
    ``ValidationEngineComponent`` for ``InvoiceData`` is build-phase (ADR-0009).
    Mirrors the foodics / intercompany inline validator.
    """
    errs: list[str] = []
    required = ("invoice_id", "invoice_number", "customer_ref", "tenant",
                "issue_date", "due_date", "line_items", "subtotal", "total",
                "currency", "status", "balance_due", "contract_version")
    for k in required:
        if not inv.get(k) and inv.get(k) != 0:
            errs.append(f"missing required field: {k}")
    for k in ("subtotal", "tax", "discounts", "total", "balance_due"):
        v = inv.get(k, "")
        if not RE_2DP.match(str(v)):
            errs.append(f"{k} must be a 2dp string, got {v!r}")
    if not RE_CURRENCY.match(str(inv.get("currency", ""))):
        errs.append("currency must be ^[A-Z]{3}$")
    if not RE_DATE.match(str(inv.get("issue_date", ""))):
        errs.append("issue_date must be YYYY-MM-DD")
    if not RE_DATE.match(str(inv.get("due_date", ""))):
        errs.append("due_date must be YYYY-MM-DD")
    items = inv.get("line_items")
    if not isinstance(items, list) or not items:
        errs.append("line_items must be a non-empty array")
    else:
        for i, li in enumerate(items):
            for k in ("line_id", "item_ref", "description", "qty", "unit_price",
                      "amount"):
                if not li.get(k) and li.get(k) != 0:
                    errs.append(f"line_items[{i}] missing {k}")
            for k in ("qty", "unit_price", "amount"):
                if not RE_2DP.match(str(li.get(k, ""))):
                    errs.append(f"line_items[{i}].{k} must be 2dp")
    return errs


def _build_journal_entry(invoice: dict, trace_id: str) -> dict:
    """Build the draft GL **Journal Entry** (flow-specific JSON, no schema).

    Balanced double-entry: debit AR ``total``, credit Revenue ``subtotal``,
    credit Tax Payable ``tax``, debit Discounts ``discounts``. Since
    ``total = subtotal + tax - discounts``, ``total_debit == total_credit``
    (asserted). ``status="draft"`` (no POST — §1). ``entry_id`` is deterministic
    ``uuid5``.
    """
    total = Decimal(invoice.get("total", "0.00"))
    subtotal = Decimal(invoice.get("subtotal", "0.00"))
    tax = Decimal(invoice.get("tax", "0.00"))
    discounts = Decimal(invoice.get("discounts", "0.00"))
    lines: list[dict] = [
        {"account": "AR", "debit": _to_signed_2dp(total), "credit": "0.00"},
        {"account": "Revenue", "debit": "0.00", "credit": _to_signed_2dp(subtotal)},
        {"account": "TaxPayable", "debit": "0.00", "credit": _to_signed_2dp(tax)},
        {"account": "Discounts", "debit": _to_signed_2dp(discounts), "credit": "0.00"},
    ]
    total_debit = total + discounts
    total_credit = subtotal + tax
    balanced = (total_debit - total_credit).quantize(_TWO_PLACES) == Decimal("0.00")
    entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                              f"invoice-gen-je:{trace_id}:{invoice.get('invoice_id')}"))
    return {
        "entry_id": entry_id,
        "invoice_ref": invoice.get("invoice_number"),
        "customer_ref": invoice.get("customer_ref"),
        "tenant": invoice.get("tenant"),
        "je_date": invoice.get("issue_date"),
        "lines": lines,
        "total_debit": _to_signed_2dp(total_debit),
        "total_credit": _to_signed_2dp(total_credit),
        "balanced": balanced,
        "currency": invoice.get("currency"),
        "status": "draft",  # no POST — §1
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _build_customer_statement(invoice: dict, trace_id: str) -> dict:
    """Build the **Customer Statement** (flow-specific JSON, no schema).

    v1: ``opening_balance="0.00"`` (no prior AR history fetched — build-phase to
    wire ZohoBooksARTool to pull prior invoices/payments), ``payments:[]``,
    ``closing_balance = total``, aging split current vs overdue (due_date vs
    today; v1 treats all as current since the invoice is just-issued draft).
    """
    total = invoice.get("total", "0.00")
    return {
        "customer_ref": invoice.get("customer_ref"),
        "tenant": invoice.get("tenant"),
        "period": {"start": invoice.get("issue_date"), "end": invoice.get("due_date")},
        "opening_balance": "0.00",
        "invoices": [{
            "invoice_number": invoice.get("invoice_number"),
            "issue_date": invoice.get("issue_date"),
            "due_date": invoice.get("due_date"),
            "total": total,
            "balance_due": invoice.get("balance_due", total),
            "status": invoice.get("status"),
        }],
        "payments": [],  # v1: none
        "closing_balance": _to_signed_2dp(total),
        "aging": {"current": _to_signed_2dp(total), "overdue": "0.00"},
        "currency": invoice.get("currency"),
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _build_zoho_upload(invoice: dict, trace_id: str) -> dict:
    """Build the **Zoho Upload File** (flow-specific JSON, mirrors foodics).

    ``{format:"zoho-books-invoice-import", rows:[{customer_ref, invoice_number,
    date, item_details:[{item_ref, qty, rate, amount, discount}], discount_total,
    total, currency}], count, trace_id, contract_version, generated_at}``.
    ``customer_ref`` is the Zoho customer id (no PII — §16).
    """
    item_details: list[dict] = []
    for li in invoice.get("line_items") or []:
        item_details.append({
            "item_ref": li.get("item_ref"),
            "qty": li.get("qty"),
            "rate": li.get("unit_price"),
            "amount": li.get("amount"),
            "discount": "0.00",  # per-line discount not modeled v1
        })
    row = {
        "customer_ref": invoice.get("customer_ref"),
        "invoice_number": invoice.get("invoice_number"),
        "date": invoice.get("issue_date"),
        "item_details": item_details,
        "discount_total": invoice.get("discounts", "0.00"),
        "total": invoice.get("total"),
        "currency": invoice.get("currency"),
    }
    return {
        "format": "zoho-books-invoice-import",
        "rows": [row],
        "count": 1,
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _build_metadata(invoice: dict, trace_id: str, flow_id: str) -> dict:
    """Build the **Invoice Metadata** (flow-specific JSON, no schema).

    Deterministic ``content_hash`` (uuid5 of the canonical InvoiceData JSON) +
    ``source_refs`` + counts. Self-describing index for the artifact set.
    """
    return {
        "invoice_id": invoice.get("invoice_id"),
        "invoice_number": invoice.get("invoice_number"),
        "customer_ref": invoice.get("customer_ref"),
        "tenant": invoice.get("tenant"),
        "trace_id": trace_id,
        "flow_id": flow_id,
        "issue_date": invoice.get("issue_date"),
        "due_date": invoice.get("due_date"),
        "currency": invoice.get("currency"),
        "status": invoice.get("status"),
        "line_item_count": len(invoice.get("line_items") or []),
        "subtotal": invoice.get("subtotal"),
        "tax": invoice.get("tax"),
        "discounts": invoice.get("discounts"),
        "total": invoice.get("total"),
        "content_hash": _content_hash(invoice, trace_id),
        "source_refs": ["build_invoice"],
        "generated_at": utc_now(),
        "contract_version": CONTRACT_VERSION,
    }


def _build_pdf_spec(invoice: dict, layout: dict, trace_id: str) -> dict:
    """Build the **Invoice PDF** render-ready spec (flow-specific JSON).

    ``render_ready:true`` layout spec — NOT a binary (build-phase: reportlab →
    MinIO → adapter file-delivery). Self-describing: sections + the data ref.
    """
    lay = layout if isinstance(layout, dict) and layout else LAYOUT
    return {
        "format": "invoice-pdf",
        "render_ready": True,
        "page": lay.get("page", LAYOUT["page"]),
        "sections": [
            {"name": "header", "fields": ["invoice_number", "issue_date", "due_date"]},
            {"name": "bill_to", "fields": ["customer_ref"]},
            {"name": "line_items_table",
             "columns": ["line_id", "item_ref", "description", "qty", "unit_price", "amount"],
             "rows": invoice.get("line_items") or []},
            {"name": "totals",
             "rows": [{"label": "Subtotal", "amount": invoice.get("subtotal")},
                      {"label": "Tax", "amount": invoice.get("tax")},
                      {"label": "Discounts", "amount": invoice.get("discounts")},
                      {"label": "Total", "amount": invoice.get("total")}]},
            {"name": "footer", "fields": ["currency", "status"]},
        ],
        "data_ref": invoice.get("invoice_id"),
        "layout": lay,
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _build_excel_spec(invoice: dict, trace_id: str) -> dict:
    """Build the **Invoice Excel** render-ready spec (flow-specific JSON).

    ``render_ready:true`` workbook spec — NOT a binary (build-phase: openpyxl
    writer). Two sheets: Invoice (header) + Line Items.
    """
    invoice_row = {
        "invoice_id": invoice.get("invoice_id"),
        "invoice_number": invoice.get("invoice_number"),
        "customer_ref": invoice.get("customer_ref"),
        "issue_date": invoice.get("issue_date"),
        "due_date": invoice.get("due_date"),
        "subtotal": invoice.get("subtotal"),
        "tax": invoice.get("tax"),
        "discounts": invoice.get("discounts"),
        "total": invoice.get("total"),
        "currency": invoice.get("currency"),
        "status": invoice.get("status"),
    }
    return {
        "format": "invoice-excel",
        "render_ready": True,
        "sheets": [
            {"name": "Invoice",
             "columns": ["invoice_id", "invoice_number", "customer_ref", "issue_date",
                         "due_date", "subtotal", "tax", "discounts", "total",
                         "currency", "status"],
             "rows": [invoice_row]},
            {"name": "Line Items",
             "columns": ["line_id", "item_ref", "description", "qty", "unit_price",
                         "amount"],
             "rows": invoice.get("line_items") or []},
        ],
        "data_ref": invoice.get("invoice_id"),
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         audit_refs: list, created_at: str,
                         updated_at: str) -> dict:
    """Build a ``WorkflowState`` snapshot (pure). v1: read-only, no money moved."""
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": FLOW_ID,
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:{FLOW_ID}:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _record_checkpoint(state: InvoiceGenerationState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry for a generation (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[InvoiceGenerationContext]) -> InvoiceGenerationContext:
    return runtime.context or {}


def _node_ingest(state: InvoiceGenerationState,
                 runtime: Runtime[InvoiceGenerationContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    user_input = ctx.get("user_input", "")
    payload, err = _parse_payload(user_input)
    if err is not None:
        return {"status": "failed", "error": err,
                "payload": None, "created_at": now, "updated_at": now}
    trace_id = str(payload.get("trace_id") or state.trace_id or mint_id())
    tenant = str(payload.get("tenant") or state.tenant
                 or ctx.get("tenant", "cosmic-vikings"))
    return {
        "trace_id": trace_id,
        "flow_id": state.flow_id or ctx.get("flow_id", FLOW_ID),
        "tenant": tenant,
        "payload": payload,
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_validate(state: InvoiceGenerationState,
                   runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    payload = state.payload or {}
    report, _warnings, err = _validate_payload(payload, state.trace_id)
    if err is not None:
        return {"validation_report": report, "status": "failed",
                "error": err, "updated_at": utc_now()}
    return {"validation_report": report, "status": "validated",
            "updated_at": utc_now()}


def _node_classify_exceptions(state: InvoiceGenerationState,
                              runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    report = state.validation_report or _build_validation_report(True, [], [],
                                                                 state.trace_id)
    exception_report = _classify_exceptions(report, state.trace_id)
    return {"exception_report": exception_report, "status": "classified",
            "updated_at": utc_now()}


def _node_build_invoice(state: InvoiceGenerationState,
                        runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    payload = state.payload or {}
    inv = _build_invoice(payload, state.trace_id, state.tenant)
    errs = _validate_invoice(inv)
    if errs:
        err = {"code": "AR_VALIDATION",
               "message": f"built invoice failed validation: {errs[0]}"}
        return {"invoice": inv, "error": err, "status": "failed",
                "updated_at": utc_now()}
    audit_refs, checkpoints = _record_checkpoint(state, "invoice")
    return {"invoice": inv, "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "invoiced", "updated_at": utc_now()}


def _node_build_journal_entry(state: InvoiceGenerationState,
                              runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    je = _build_journal_entry(state.invoice or {}, state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "journal_entry")
    return {"journal_entry": je, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "journaled",
            "updated_at": utc_now()}


def _node_build_customer_statement(state: InvoiceGenerationState,
                                   runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    cs = _build_customer_statement(state.invoice or {}, state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "customer_statement")
    return {"customer_statement": cs, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "stated",
            "updated_at": utc_now()}


def _node_build_zoho_upload(state: InvoiceGenerationState,
                            runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    zu = _build_zoho_upload(state.invoice or {}, state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "zoho_upload")
    return {"zoho_upload": zu, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "zoho",
            "updated_at": utc_now()}


def _node_build_metadata(state: InvoiceGenerationState,
                         runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    md = _build_metadata(state.invoice or {}, state.trace_id, state.flow_id)
    audit_refs, checkpoints = _record_checkpoint(state, "invoice_metadata")
    return {"invoice_metadata": md, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "metadata",
            "updated_at": utc_now()}


def _node_build_pdf_spec(state: InvoiceGenerationState,
                         runtime: Runtime[InvoiceGenerationContext]) -> dict:
    ctx = _ctx(runtime)
    spec = _build_pdf_spec(state.invoice or {}, ctx.get("layout") or LAYOUT,
                           state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "invoice_pdf")
    return {"invoice_pdf": spec, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "pdf_spec",
            "updated_at": utc_now()}


def _node_build_excel_spec(state: InvoiceGenerationState,
                           runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    spec = _build_excel_spec(state.invoice or {}, state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "invoice_excel")
    return {"invoice_excel": spec, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "excel_spec",
            "updated_at": utc_now()}


def _node_build_state(state: InvoiceGenerationState,
                      runtime: Runtime[InvoiceGenerationContext]) -> dict:
    _ = _ctx(runtime)
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    return {"workflow_state": ws, "status": "completed",
            "updated_at": utc_now()}


def _node_checkpoint(state: InvoiceGenerationState,
                     runtime: Runtime[InvoiceGenerationContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints."""
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, FLOW_ID)
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "updated_at": utc_now()}


def _node_respond(state: InvoiceGenerationState,
                  runtime: Runtime[InvoiceGenerationContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# Conditional routers (return state.status against status-keyed path maps).
def _after_ingest(state: InvoiceGenerationState) -> str:
    return state.status


def _after_validate(state: InvoiceGenerationState) -> str:
    return state.status


def _after_classify(state: InvoiceGenerationState) -> str:
    return state.status


def _after_invoice(state: InvoiceGenerationState) -> str:
    return state.status


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class InvoiceGenerationFlowComponent(Component):
    name = "InvoiceGenerationFlowComponent"
    display_name = "Cosmic AR Invoice Generation Flow"
    description = (
        "Reads a validated-JSON invoice request ({customer_ref, line_items, "
        "totals, issue_date, currency, ...}) and generates eight invoice "
        "artifacts — Invoice JSON, Invoice PDF (render-ready spec), Invoice Excel "
        "(render-ready spec), Journal Entry (draft), Customer Statement, Zoho "
        "Upload File, Invoice Metadata, and a WorkflowState snapshot — as "
        "structured JSON in the §14 envelope, with logging and checkpoints after "
        "every generation step (constitution §1/§4/§8/§9/§11/§12/§14/§15/§16/§17/"
        "§19). The 15th AR subflow; v1 is read-only generate + draft (no posting). "
        "PDF/Excel binaries are build-phase; no §55 waiver. See ADR-0009."
    )
    icon = "FileText"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="Invoice Request (JSON)",
            info=(
                "The validated-JSON invoice request: {customer_ref, line_items:"
                "[{item_ref, description, qty, unit_price}], totals?, tax?, "
                "discounts?, issue_date, currency?, po_number?, salesperson_ref?, "
                "notes?}. This is the primary input — the flow assembles the "
                "InvoiceData then derives all 8 artifacts."
            ),
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="layout",
            display_name="Layout (JSON)",
            info=(
                "Declarative PDF/Excel layout spec (page size, margins, section "
                "order). Defaults to a sensible A4 layout; override to rebrand "
                "without touching code (§17 tunable). The real .pdf/.xlsx renderer "
                "(build-phase) reads this; v1 carries it through data.invoice_pdf."
            ),
            value=LAYOUT_JSON,
            required=False,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic generate; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="invoice_generation_output",
            display_name="Invoice Artifacts",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=InvoiceGenerationState,
                           context_schema=InvoiceGenerationContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("validate", _node_validate)
        graph.add_node("classify_exceptions", _node_classify_exceptions)
        graph.add_node("build_invoice", _node_build_invoice)
        graph.add_node("build_journal_entry", _node_build_journal_entry)
        graph.add_node("build_customer_statement", _node_build_customer_statement)
        graph.add_node("build_zoho_upload", _node_build_zoho_upload)
        graph.add_node("build_metadata", _node_build_metadata)
        graph.add_node("build_pdf_spec", _node_build_pdf_spec)
        graph.add_node("build_excel_spec", _node_build_excel_spec)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _after_ingest,
                                    {"failed": "respond", "created": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond",
                                     "validated": "classify_exceptions"})
        graph.add_conditional_edges("classify_exceptions", _after_classify,
                                    {"failed": "respond",
                                     "classified": "build_invoice"})
        graph.add_conditional_edges("build_invoice", _after_invoice,
                                    {"failed": "respond",
                                     "invoiced": "build_journal_entry"})
        # The remaining build nodes are pure compute → static edges (unexpected
        # errors caught at the run() boundary → AR_UNEXPECTED).
        graph.add_edge("build_journal_entry", "build_customer_statement")
        graph.add_edge("build_customer_statement", "build_zoho_upload")
        graph.add_edge("build_zoho_upload", "build_metadata")
        graph.add_edge("build_metadata", "build_pdf_spec")
        graph.add_edge("build_pdf_spec", "build_excel_spec")
        graph.add_edge("build_excel_spec", "build_state")
        graph.add_edge("build_state", "checkpoint")
        graph.add_edge("checkpoint", "respond")
        graph.add_edge("respond", END)
        return graph.compile(checkpointer=InMemorySaver())

    def _get_graph(self) -> Any:
        cached = getattr(self, "_compiled_graph", None)
        if cached is None:
            cached = self._build_graph()
            self._compiled_graph = cached  # type: ignore[attr-defined]
        return cached

    # ------------------------------------------------------------------ #
    #  Output method — the only lfx entry point. Never raises (§5/§9).
    # ------------------------------------------------------------------ #
    def run(self) -> Message:
        try:
            user_input = _to_str(self.user_input)
            session_id = _to_str(getattr(self, "session_id", "")) or mint_id()
            actor = _to_str(getattr(self, "actor", ""))
            model_name = _to_str(getattr(self, "model_name", ""))
            layout_raw = _to_str(getattr(self, "layout", "")) or LAYOUT_JSON
            try:
                layout = json.loads(layout_raw)
            except (TypeError, ValueError):
                layout = LAYOUT
            ctx: InvoiceGenerationContext = {
                "user_input": user_input,
                "layout": layout,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": FLOW_ID,
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = InvoiceGenerationState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=invoice_generation.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=invoice_generation outcome={envelope.get('status')} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Invoice generation run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=invoice_generation.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state → §14 envelope (deterministic from state).

        ``graph.get_state(config).values`` is a plain dict (ADR-0003 §10), so
        fields are read by key. The 8 artifacts are NOT ``data.totals{matched,
        outstanding, posted}`` keys, so the supervisor's ``_node_invoke`` does
        not recognize them — they stay in the envelope ``data`` (no ``AgentState``
        schema change — ADR-0009). v1 is read-only generate + draft, so ``data``
        carries no financial ``totals{matched,outstanding,posted}`` (those stay
        ``"0.00"`` inside ``data.workflow_state``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        invoice = vals.get("invoice") or {}
        line_items = (invoice.get("line_items") or []) \
            if isinstance(invoice, dict) else []
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "invoice": invoice,
            "journal_entry": vals.get("journal_entry") or {},
            "customer_statement": vals.get("customer_statement") or {},
            "zoho_upload": vals.get("zoho_upload") or {},
            "invoice_metadata": vals.get("invoice_metadata") or {},
            "invoice_pdf": vals.get("invoice_pdf") or {},
            "invoice_excel": vals.get("invoice_excel") or {},
            "validation_report": vals.get("validation_report") or {},
            "exception_report": vals.get("exception_report") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "artifact_count": len(ARTIFACT_KEYS),
            "line_item_count": len(line_items),
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "invoice generation failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err,
                    "flow_id": vals.get("flow_id", "")}
        return {"status": "ok", "code": "AR_OK", "trace_id": trace_id,
                "data": data, "flow_id": vals.get("flow_id", "")}


# Guard so importing the module (for the self-test) does not execute main logic.
if __name__ == "__main__":  # pragma: no cover
    dataclasses  # noqa: B018 — keep the import live for tooling that prunes
    raise SystemExit("This is a LangFlow component module; import it, do not run it.")