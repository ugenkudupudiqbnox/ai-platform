"""Cosmic AR Agent — Intercompany Sales Flow component (constitution §8, architecture §4 row 4).

The Intercompany Sales Flow is the 4th AR subflow. Cosmic sells to intercompany
customer-restaurants inside a Marriott hotel (HYP, Upyard) and receives those
sales as **KOT (Kitchen Order Ticket) Excel** sheets — one row per ordered menu
item carrying its quantity and the intercompany **agreed rate** (the transfer
price Cosmic charges the buyer). This flow reads the KOT, validates its rows,
looks up menu/quantity/agreed-rate **from the sheet columns** (deterministic,
no external calls), calculates intercompany revenue, generates a **draft**
``InvoiceData`` JSON per buyer, a Validation Report, and an Exception Report,
updates ``WorkflowState``, and returns structured JSON — with logging (§12),
retries (§10), and checkpoints (§11). It is the **single stateful orchestrator**
for intercompany sales, mirroring the supervisor and the File Intake Flow.

v1 is **compute + draft only**: it produces the invoice JSON for review; it does
NOT post/issue the invoice, so no money moves and no ledger entry posts this turn
(§1 north star preserved). The flow is registered at tier ``approval`` (its
intent is invoice production), but the §19 gate is **dormant in v1**: there is no
``ApprovalGate``, no idempotency key, no ``pending_approval``. Upgrading to actual
issuance (posting the intercompany invoice in Zoho) is a documented build-phase
step that adds the gate + idempotency + checkpoint-before-POST +
audit-with-``approval_ref`` (mirrors ``ar_issue_invoice``, architecture §4 row 1).
See ADR-0005.

Responsibilities → LangGraph nodes:

  ingest → read (§10 retry) → validate → classify_exceptions →
  calculate_revenue → build_invoice → build_state → checkpoint → respond

  - ingest             : bind ``trace_id``/``flow_id``/``tenant`` + timestamps;
                          carry uploaded-file refs in **context** (not state — §8).
  - read               : instantiate the matching cosmic_common reader (Excel/CSV),
                          call its output method inside the §10 retry/backoff loop,
                          parse its §14 envelope → ``raw_rows`` (list[dict]).
                          Unknown type/no-file → ``AR_UNCERTAIN`` (§4).          §10/§9
  - validate           : inline hand-rolled KOT-row validator. Required columns:
                          customer_ref, item_ref, qty, agreed_rate, posted_at.
                          Per-row checks: qty>0, agreed_rate>0, posted_at ISO date,
                          customer_ref non-empty. A **missing required column** is a
                          hard ``AR_VALIDATION`` (can't proceed). Else the full
                          ``ValidationResult`` is built.                         §9
  - classify_exceptions: split rows into valid vs exception (rows with any error).
                          Build the Exception Report = a ``ValidationResult`` scoped
                          to failures (``rule_id`` per exception). All-rows-fail →
                          ``AR_VALIDATION``.                                       §4
  - calculate_revenue   : for each valid row ``amount = qty × agreed_rate`` (2dp,
                          ``Decimal`` — deterministic, §4.3). Build ``RevenueData``
                          (total, by_segment=customer_ref, by_customer_ref, period). §8
  - build_invoice      : group valid rows by ``customer_ref``; emit **one
                          ``InvoiceData`` per buyer** (intercompany = one invoice per
                          seller→buyer pair; HYP+Upyard → two invoices). ``status="draft"``.
                          Backfill ``RevenueData.by_invoice``.                  §15/§16
  - build_state        : build a ``WorkflowState`` snapshot (status="completed",
                          totals ``"0.00"`` — no money moved). Immutable (§8).
  - checkpoint         : record the audit id; ``InMemorySaver`` persists state.   §11
  - respond            : build the §14 envelope carrying ``data.invoices``,
                          ``data.revenue``, ``data.validation_report``,
                          ``data.exception_report``, ``data.workflow_state``,
                          ``data.audit_refs``.                                     §14

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id``. This
is the §11 **fallback**: non-durable (lost on worker recreate). Durable Postgres
checkpointing remains a documented build-phase step (see ADR-0005 and the
constitution §11 caveat — Langfuse tracing is currently off, so the checkpoint
is the source of truth for resume).

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. Customer refs are ids (HYP/Upyard) — no
PII (§16). No credentials are needed in v1 (deterministic, in-file).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from lfx.custom import Component
from lfx.io import HandleInput, MessageTextInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1). Tunables belong in Global Variables (§17) at build
#  phase; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"

# §10 retry policy (mirrors the supervisor / File Intake Flow).
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0
BACKOFF_CAP_S: float = 30.0

# Intercompany sales v1 policy.
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors invoice_builder/calc_engine)
NET_TERMS_DAYS: int = 30  # deterministic issue→due offset (v1)

# KOT column-name aliases (lowercased keys). The reader emits dict rows keyed by
# the sheet header; lookups are case-insensitive and tolerant of synonyms.
CUSTOMER_KEYS = ("customer_ref", "customer_id", "customer", "cust_id",
                 "buyer", "buyer_ref", "account_id")
ITEM_KEYS = ("item_ref", "item_id", "menu_item", "menu", "item",
             "product", "product_id", "sku")
QTY_KEYS = ("qty", "quantity", "count", "units")
RATE_KEYS = ("agreed_rate", "rate", "unit_price", "price",
             "transfer_price", "agreed_price")
DATE_KEYS = ("posted_at", "date", "kot_date", "order_date", "txn_date",
             "transaction_date")
CURRENCY_KEYS = ("currency", "curr", "ccy")
DESC_KEYS = ("description", "desc", "item_desc", "menu_desc", "item_name")
TAX_KEYS = ("tax", "tax_rate", "vat")
DISCOUNT_KEYS = ("discount", "discounts")

# Canonical required columns (the v1 KOT contract). A sheet missing any of these
# is a hard validation failure (the flow cannot proceed).
REQUIRED_COLUMNS = ("customer_ref", "item_ref", "qty", "agreed_rate", "posted_at")

# 2dp string pattern (the contracts' amount/qty pattern).
RE_2DP = re.compile(r"^-?\d+\.\d{2}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class IntercompanySalesContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[IntercompanySalesContext]``.

    Durable, resumable state lives in ``IntercompanySalesState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    files: list[Any]  # uploaded KOT Excel refs from the canvas File node
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntercompanySalesState:
    """The Intercompany Sales Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    Derived working data (rows, invoices, reports) is transient, not raw input.
    """

    trace_id: str
    flow_id: str
    tenant: str
    status: str = "created"  # created|read|validated|classified|calculated|built|completed|failed
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    # Derived working data.
    file_plan: list = field(default_factory=list)  # [{name, path, kind}]
    raw_rows: list = field(default_factory=list)  # KOT rows from the reader
    valid_rows: list = field(default_factory=list)  # rows passing all rules
    exception_rows: list = field(default_factory=list)  # rows with ≥1 error
    revenue: Optional[dict] = None  # RevenueData
    invoices: list = field(default_factory=list)  # one InvoiceData per buyer
    validation_report: Optional[dict] = None  # full ValidationResult
    exception_report: Optional[dict] = None  # ValidationResult scoped to failures
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    audit_refs: list = field(default_factory=list)


def _state_to_dict(state: Any) -> dict:
    """Coerce an ``IntercompanySalesState`` (or dict) snapshot to a plain dict.

    ``graph.get_state().values`` is normally already a dict, but defend against a
    dataclass sneaking through so the envelope builder never raises.
    """
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


def _as_list(value: Any) -> list[Any]:
    """Coerce a HandleInput value (single / list / None) to a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def utc_now() -> str:
    """UTC ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` (contracts' timestamp pattern)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mint_id() -> str:
    """A fresh lowercase uuid4 string (trace_id / invoice_id seed)."""
    return str(uuid.uuid4())


def parse_envelope(text: str) -> Optional[dict[str, Any]]:
    """Best-effort parse of a §14 envelope from a reader/tool output string."""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _envelope(status: str, code: str, data: Optional[dict] = None,
              error: Optional[dict] = None, trace_id: str = "") -> dict[str, Any]:
    """Build a §14 envelope dict."""
    env: dict[str, Any] = {"status": status, "code": code, "data": data or {},
                           "trace_id": trace_id}
    if error:
        env["error"] = error
    return env


def detect_type(filename: str) -> str:
    """Identify the KOT file type by extension. ``excel``/``csv``/``unknown``.

    Intercompany sales consumes Excel or CSV KOTs. Unknown extensions fail safe
    (§4) at the ``read`` node → ``AR_UNCERTAIN``.
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm") or name.endswith(".xls"):
        return "excel"
    if name.endswith(".csv") or name.endswith(".tsv"):
        return "csv"
    return "unknown"


def _basename(path: str) -> str:
    return os.path.basename(path or "") or path or "file"


def _normalize_file(ref: Any) -> dict[str, str]:
    """Coerce a canvas File-node ref to ``{name, path}`` (mirrors File Intake)."""
    if ref is None:
        return {"name": "", "path": ""}
    if isinstance(ref, str):
        return {"name": _basename(ref), "path": ref}
    data_attr = getattr(ref, "data", None)
    file_attr = getattr(ref, "file", None)
    file_path_attr = getattr(ref, "file_path", None)
    candidates: list[dict] = []
    if isinstance(data_attr, dict):
        candidates.append(data_attr)
    if isinstance(file_attr, dict):
        candidates.append(file_attr)
    if isinstance(ref, dict):
        candidates.append(ref)
    for c in candidates:
        path = c.get("file_path") or c.get("path") or ""
        name = c.get("file_name") or c.get("name") or c.get("filename") or ""
        if path:
            return {"name": name or _basename(path), "path": path}
    if isinstance(file_path_attr, str) and file_path_attr:
        return {"name": _basename(file_path_attr), "path": file_path_attr}
    for attr in ("file_path", "path", "file"):
        v = getattr(ref, attr, None)
        if isinstance(v, str) and v:
            return {"name": _basename(v), "path": v}
    return {"name": "", "path": ""}


def _expand_files(files: Any) -> list:
    """Flatten a ``files`` input into individual refs (mirrors File Intake)."""
    if files is None:
        return []
    if not isinstance(files, list):
        files = [files]
    out: list = []
    for ref in files:
        if ref is None or ref == "":
            continue
        msg_files = getattr(ref, "files", None)
        if isinstance(msg_files, list) and msg_files and not isinstance(ref, str):
            out.extend(f for f in msg_files if f is not None and f != "")
        else:
            out.append(ref)
    return out


def _resolve_storage_path(path: str) -> str:
    """Resolve a LangFlow uploaded-file storage path (mirrors File Intake)."""
    if not path or os.path.isabs(path):
        return path
    if "/" in path and path.count("/") == 1:
        cfg = os.environ.get("LANGFLOW_CONFIG_DIR", "")
        if cfg:
            return os.path.join(cfg, path)
    return path


def _rows_from_content(content: Any) -> list[dict]:
    """Extract list[dict] rows from a reader envelope's ``data``.

    The cosmic_common Excel/CSV readers (``has_header=True``) return ``data.rows``
    as a list of dict rows keyed by the sheet header. Normalise values to strings
    so downstream alias lookups are consistent.
    """
    if not isinstance(content, dict):
        return []
    rows = content.get("rows")
    if isinstance(rows, list):
        out: list[dict] = []
        for r in rows:
            if isinstance(r, dict):
                out.append({str(k): ("" if v is None else str(v))
                            for k, v in r.items()})
            elif isinstance(r, list):
                out.append({f"col{i}": ("" if c is None else str(c))
                            for i, c in enumerate(r)})
        return out
    return []


def _row_key(row: dict, keys: tuple[str, ...]) -> str:
    """Case-insensitive alias lookup of a value in a single row.

    Returns the first non-empty string value for any of ``keys`` (matched against
    the lowercased/underscore-normalised row key), or ``""``.
    """
    if not isinstance(row, dict):
        return ""
    lowered = {k.lower() for k in keys}
    for rk, rv in row.items():
        if rk.lower().replace(" ", "_") in lowered:
            if rv is not None and str(rv).strip():
                return str(rv).strip()
    return ""


def _header_map(rows: list[dict]) -> dict[str, str]:
    """Map each canonical column name to the actual header key found in ``rows``.

    Returns ``{canonical: actual_header_key}`` for the columns present. A
    canonical name absent from the map is a missing required column.
    """
    all_keys: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            all_keys.update(str(k) for k in r.keys())
    norm = {k.lower().replace(" ", "_"): k for k in all_keys}
    out: dict[str, str] = {}
    for canonical, aliases in (
        ("customer_ref", CUSTOMER_KEYS),
        ("item_ref", ITEM_KEYS),
        ("qty", QTY_KEYS),
        ("agreed_rate", RATE_KEYS),
        ("posted_at", DATE_KEYS),
        ("currency", CURRENCY_KEYS),
        ("description", DESC_KEYS),
        ("tax", TAX_KEYS),
        ("discount", DISCOUNT_KEYS),
    ):
        for a in aliases:
            n = a.lower().replace(" ", "_")
            if n in norm:
                out[canonical] = norm[n]
                break
    return out


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a numeric string to ``Decimal``; ``None`` on failure."""
    if value is None or value == "":
        return None
    s = str(value).strip().replace(",", "")  # strip thousands separators
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        m = re.search(r"-?\d+(\.\d+)?", s)  # e.g. "SAR 1,234.50"
        if not m:
            return None
        try:
            return Decimal(m.group(0))
        except (InvalidOperation, ValueError):
            return None


def _to_2dp(value: Any) -> str:
    """Coerce a numeric to a non-negative 2dp string; ``"0.00"`` on failure.

    Quantised with ``ROUND_HALF_UP`` for deterministic producer-side output (§4.3).
    """
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    q = d.quantize(Decimal("0.01"))
    if q < 0:
        q = Decimal("0.00")
    return f"{q}"


def _to_signed_2dp(value: Any) -> str:
    """Coerce a numeric to a signed 2dp string (allows negatives)."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    return f"{d.quantize(Decimal('0.01'))}"


def _sum_2dp(amounts: list[str]) -> str:
    """Sum a list of 2dp-string amounts to a 2dp string (producer-side check)."""
    total = Decimal("0.00")
    for a in amounts:
        try:
            total += Decimal(str(a))
        except (InvalidOperation, ValueError):
            continue
    return f"{total.quantize(Decimal('0.01'))}"


def _parse_date(value: str) -> Optional[str]:
    """Parse a date string to ``YYYY-MM-DD``; ``None`` when unparseable.

    Accepts ``YYYY-MM-DD`` and ``YYYY/MM/DD``. Datetime strings are reduced to
    their date portion.
    """
    s = (value or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _add_days(date_str: str, days: int) -> str:
    """Add ``days`` to a ``YYYY-MM-DD`` date; returns ``YYYY-MM-DD``.

    Deterministic UTC arithmetic (no wall-clock side effects — §4.3).
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return date_str
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _issue(path: str, code: str, message: str, rule_id: str) -> dict[str, str]:
    """Build one ``ValidationResult.errors[]`` issue (schema-conformant)."""
    return {"path": path, "code": code, "message": message, "rule_id": rule_id}


# --------------------------------------------------------------------------- #
#  KOT-row validation (inline, hand-rolled — §15 reuse note in ADR-0005).
# --------------------------------------------------------------------------- #


def _validate_kot_row(row: dict, hmap: dict[str, str], index: int) -> list[dict]:
    """Validate one KOT row → list of issue dicts (empty when valid).

    Rules (each emits a ``rule_id``): qty parseable & > 0; agreed_rate parseable
    & > 0; posted_at ISO date; customer_ref non-empty.
    """
    errs: list[dict] = []
    base = f"row[{index}]"

    cust = _row_key(row, CUSTOMER_KEYS)
    if not cust:
        errs.append(_issue(f"{base}.customer_ref", "AR_VALIDATION_REQUIRED",
                           "customer_ref is required", "kot.customer_ref_required"))

    qty_s = _row_key(row, QTY_KEYS)
    qty = _to_decimal(qty_s)
    if qty is None or qty <= 0:
        errs.append(_issue(f"{base}.qty", "AR_VALIDATION_POSITIVE",
                           "qty must be a positive number", "kot.qty_positive"))

    rate_s = _row_key(row, RATE_KEYS)
    rate = _to_decimal(rate_s)
    if rate is None or rate <= 0:
        errs.append(_issue(f"{base}.agreed_rate", "AR_VALIDATION_POSITIVE",
                           "agreed_rate must be a positive number",
                           "kot.rate_positive"))

    date_s = _row_key(row, DATE_KEYS)
    if not _parse_date(date_s):
        errs.append(_issue(f"{base}.posted_at", "AR_VALIDATION_FORMAT",
                           "posted_at must be an ISO date (YYYY-MM-DD)",
                           "kot.date_iso"))

    return errs


def _validate_kot_rows(rows: list[dict]) -> tuple[list[dict], dict[str, str], list[str]]:
    """Validate all KOT rows.

    Returns ``(per_row_errors, header_map, missing_required)`` where
    ``per_row_errors[i]`` is the issue list for ``rows[i]`` (empty when valid),
    ``header_map`` maps canonical→actual header key, and ``missing_required`` is
    the list of canonical required columns entirely absent from the sheet.
    """
    hmap = _header_map(rows)
    missing = [c for c in REQUIRED_COLUMNS if c not in hmap]
    per_row: list[list[dict]] = []
    for i, row in enumerate(rows):
        per_row.append(_validate_kot_row(row, hmap, i) if not missing else
                       [_issue(f"row[{i}]", "AR_VALIDATION_REQUIRED",
                               f"required column missing: {', '.join(missing)}",
                               "kot.required_columns")])
    return per_row, hmap, missing


def _build_validation_report(rows: list[dict], per_row_errors: list[list[dict]],
                             trace_id: str) -> dict:
    """Build the full ``ValidationResult`` over all KOT rows (pure)."""
    errors: list[dict] = []
    for errs in per_row_errors:
        errors.extend(errs)
    return {
        "valid": len(errors) == 0,
        "contract_name": "KOTrows",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": errors,
        "validated_at": utc_now(),
        "schema_ref": "https://cosmic-vikings/ar-agent/contracts/validation-result.schema.json",
    }


def _classify_exceptions(rows: list[dict], per_row_errors: list[list[dict]],
                         trace_id: str) -> tuple[list[dict], list[dict], dict]:
    """Split rows into valid/exception and build the Exception Report.

    Returns ``(valid_rows, exception_rows, exception_report)``. The Exception
    Report is a ``ValidationResult`` scoped to the failing rows (``valid`` is
    True iff there are no exceptions).
    """
    valid: list[dict] = []
    exception: list[dict] = []
    errors: list[dict] = []
    for row, errs in zip(rows, per_row_errors):
        if errs:
            exception.append(row)
            errors.extend(errs)
        else:
            valid.append(row)
    report = {
        "valid": len(errors) == 0,
        "contract_name": "KOTrows",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": errors,
        "validated_at": utc_now(),
        "schema_ref": "https://cosmic-vikings/ar-agent/contracts/validation-result.schema.json",
    }
    return valid, exception, report


def _currency_from(rows: list[dict], hmap: dict[str, str]) -> str:
    """Determine the currency: explicit column > default. Upper-cased; bad → default."""
    cur = ""
    if "currency" in hmap:
        for r in rows:
            cur = _row_key(r, CURRENCY_KEYS)
            if cur:
                break
    cur = (cur or DEFAULT_CURRENCY).upper()
    if not RE_CURRENCY.match(cur):
        cur = DEFAULT_CURRENCY
    return cur


def calculate_revenue(valid_rows: list[dict], hmap: dict[str, str],
                      trace_id: str, tenant: str) -> dict:
    """Build ``RevenueData`` from valid rows (pure, deterministic — §4.3).

    ``amount = qty × agreed_rate`` per row, quantised 2dp. ``by_segment`` and
    ``by_customer_ref`` group by buyer ``customer_ref`` (intercompany segments).
    ``period`` is the min/max ``posted_at`` date. ``by_invoice`` is filled later
    by ``build_invoices`` (left empty here).
    """
    currency = _currency_from(valid_rows, hmap)
    by_segment: dict[str, dict] = {}  # customer_ref → {amount, count}
    amounts: list[str] = []
    dates: list[str] = []
    for row in valid_rows:
        qty = _to_decimal(_row_key(row, QTY_KEYS)) or Decimal("0")
        rate = _to_decimal(_row_key(row, RATE_KEYS)) or Decimal("0")
        amount = (qty * rate).quantize(Decimal("0.01"))
        if amount < 0:
            amount = Decimal("0.00")
        amt_s = f"{amount}"
        amounts.append(amt_s)
        cust = _row_key(row, CUSTOMER_KEYS) or "CUST-UNKNOWN"
        seg = by_segment.setdefault(cust, {"amount": Decimal("0.00"), "count": 0})
        seg["amount"] += amount
        seg["count"] += 1
        d = _parse_date(_row_key(row, DATE_KEYS))
        if d:
            dates.append(d)
    total = _sum_2dp(amounts)
    period = {"start": min(dates), "end": max(dates)} if dates else {
        "start": time.strftime("%Y-%m-%d", time.gmtime()),
        "end": time.strftime("%Y-%m-%d", time.gmtime()),
    }
    by_segment_out = [{"segment": k, "amount": f"{v['amount'].quantize(Decimal('0.01'))}",
                       "count": v["count"]} for k, v in by_segment.items()]
    by_customer = [{"customer_ref": k,
                    "amount": f"{v['amount'].quantize(Decimal('0.01'))}",
                    "count": v["count"]} for k, v in by_segment.items()]
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "period": period,
        "total": total,
        "currency": currency,
        "by_segment": by_segment_out,
        "by_customer_ref": by_customer,
        "by_invoice": [],
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _deterministic_invoice_id(trace_id: str, customer_ref: str) -> tuple[str, str]:
    """Derive a deterministic (trace_id+customer_ref) invoice_id + invoice_number.

    ``uuid5`` is reproducible from its inputs (§4.3) — no ``Math.random``/
    ``uuid4`` here, so the same trace + buyer always yields the same invoice ids.
    """
    seed = f"intercompany:{trace_id}:{customer_ref}"
    u = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    return str(u), f"IC-{customer_ref}-{u.hex[:8].upper()}"


def build_invoices(valid_rows: list[dict], hmap: dict[str, str],
                   revenue: dict, trace_id: str, tenant: str) -> list[dict]:
    """Build one ``InvoiceData`` per buyer (intercompany = one invoice per pair).

    Groups valid rows by ``customer_ref``; each invoice gets one ``line_item`` per
    row (``item_ref`` = menu item, ``unit_price`` = agreed rate, ``amount`` =
    qty × rate). ``status="draft"`` (v1: no posting). ``issue_date`` = the
    invoice's earliest ``posted_at``; ``due_date`` = issue + ``NET_TERMS_DAYS``.
    Backfills ``revenue['by_invoice']``.
    """
    currency = revenue.get("currency", DEFAULT_CURRENCY)
    # Group rows by buyer, preserving first-seen order.
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in valid_rows:
        cust = _row_key(row, CUSTOMER_KEYS) or "CUST-UNKNOWN"
        if cust not in groups:
            groups[cust] = []
            order.append(cust)
        groups[cust].append(row)

    invoices: list[dict] = []
    by_invoice: list[dict] = []
    for cust in order:
        rows = groups[cust]
        line_items: list[dict] = []
        line_amounts: list[str] = []
        dates: list[str] = []
        for i, row in enumerate(rows):
            qty = _to_decimal(_row_key(row, QTY_KEYS)) or Decimal("0")
            rate = _to_decimal(_row_key(row, RATE_KEYS)) or Decimal("0")
            amount = (qty * rate).quantize(Decimal("0.01"))
            if amount < 0:
                amount = Decimal("0.00")
            amt_s = f"{amount}"
            line_amounts.append(amt_s)
            item_ref = _row_key(row, ITEM_KEYS) or f"item-{i + 1}"
            desc = _row_key(row, DESC_KEYS) or item_ref
            line_items.append({
                "line_id": f"L{i + 1:03d}",
                "item_ref": item_ref,
                "description": desc,
                "qty": f"{qty.quantize(Decimal('0.01'))}",
                "unit_price": f"{rate.quantize(Decimal('0.01'))}",
                "amount": amt_s,
            })
            d = _parse_date(_row_key(row, DATE_KEYS))
            if d:
                dates.append(d)
        subtotal = _sum_2dp(line_amounts)
        issue = min(dates) if dates else time.strftime("%Y-%m-%d", time.gmtime())
        due = _add_days(issue, NET_TERMS_DAYS)
        inv_id, inv_num = _deterministic_invoice_id(trace_id, cust)
        inv = {
            "invoice_id": inv_id,
            "invoice_number": inv_num,
            "customer_ref": cust,
            "tenant": tenant,
            "issue_date": issue,
            "due_date": due,
            "line_items": line_items,
            "subtotal": subtotal,
            "total": subtotal,
            "balance_due": subtotal,
            "currency": currency,
            "status": "draft",
            "contract_version": CONTRACT_VERSION,
        }
        invoices.append(inv)
        by_invoice.append({"invoice_ref": inv_num, "customer_ref": cust,
                           "amount": subtotal})
    revenue["by_invoice"] = by_invoice
    return invoices


def _validate_invoice(inv: dict) -> list[str]:
    """Inline hand-rolled ``InvoiceData`` validation → list of error strings.

    Checks the required fields and 2dp patterns (mirrors the schema). Used by the
    ``build_invoice`` node as a guard; wiring this into
    ``ValidationEngineComponent`` for ``InvoiceData`` is build-phase (ADR-0005).
    """
    errs: list[str] = []
    required = ("invoice_id", "invoice_number", "customer_ref", "tenant",
                "issue_date", "due_date", "line_items", "subtotal", "total",
                "currency", "status", "balance_due", "contract_version")
    for k in required:
        if not inv.get(k) and inv.get(k) != 0:
            errs.append(f"missing required field: {k}")
    for k in ("subtotal", "total", "balance_due"):
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


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         audit_refs: list, created_at: str, updated_at: str) -> dict:
    """Build a ``WorkflowState`` snapshot (pure). v1: draft only, no money moved.

    Financial totals are ``"0.00"`` (no posting), ``pending_approvals=[]``,
    ``idempotency_keys={}`` (no POST). Status ``completed`` (the draft is built).
    """
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": "ar_intercompany_sales",
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:ar_intercompany_sales:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _audit_ref(trace_id: str) -> str:
    """Deterministic audit record id for this run (§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"intercompany-audit:{trace_id}"))


# --------------------------------------------------------------------------- #
#  §10 retry classification (mirrors supervisor / File Intake).
# --------------------------------------------------------------------------- #


def _is_transient(exc: BaseException) -> bool:
    """§10: classify an exception as transient (retryable) vs hard."""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "temporary", "unreachable")):
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, int) and (code >= 500 or code in (408, 429)):
        return True
    return False


def _backoff_sleep(attempt: int) -> None:
    """§10 exponential backoff with ±25% jitter (attempt-parity), capped 30s."""
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
    jitter = delay * 0.25
    slept = delay + (jitter if attempt % 2 else -jitter)
    time.sleep(max(0.0, slept))


def _read_with_retry(reader: Any, file_path: str, kind: str,
                     trace_id: str) -> dict[str, Any]:
    """Call a reader's ``read()`` inside the §10 retry/backoff loop.

    Returns a §14 envelope dict (``data`` carries the reader's rows). Reader
    error envelopes (``AR_VALIDATION`` / ``AR_NOT_IMPLEMENTED``) are HARD → no
    retry. A reader that *raises* a transient exception is retried; exhausted
    transient retries → ``error`` (intercompany sales v1 is read-only compute).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = reader.read()
            envelope = parse_envelope(_to_str(raw))
            if envelope is None:
                envelope = _envelope("error", "AR_UPSTREAM",
                                    error={"message": "reader returned no envelope"},
                                    trace_id=trace_id)
            return envelope
        except Exception as exc:  # noqa: BLE001 — classified below, never raised
            last_exc = exc
            if not _is_transient(exc):
                return _envelope("error", "AR_VALIDATION",
                                 error={"message": f"{kind} read failed: {exc}"},
                                 trace_id=trace_id)
            if attempt < MAX_ATTEMPTS:
                _backoff_sleep(attempt)
    return _envelope("error", "AR_UPSTREAM",
                     error={"message": f"transient retries exhausted: {last_exc}"},
                     trace_id=trace_id)


def _make_reader(kind: str, file_path: str) -> Any:
    """Instantiate the matching cosmic_common reader and bind its file_path.

    Lazy import so the module imports cleanly even before the Dockerfile rebuild
    (openpyxl may be absent on the host). Returns None if the bundle/dep is
    unavailable — the caller surfaces ``AR_NOT_IMPLEMENTED``.
    """
    if kind == "csv":
        from components.cosmic_common.csv_reader import CSVReaderComponent
        r = CSVReaderComponent()
        r.file_path = file_path
        r.has_header = True
        return r
    if kind == "excel":
        from components.cosmic_common.excel_reader import ExcelReaderComponent
        r = ExcelReaderComponent()
        r.file_path = file_path
        r.has_header = True
        return r
    return None


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[IntercompanySalesContext]) -> IntercompanySalesContext:
    return runtime.context or {}


def _node_ingest(state: IntercompanySalesState,
                 runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    return {
        "trace_id": state.trace_id or mint_id(),
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_intercompany_sales"),
        "tenant": state.tenant or ctx.get("tenant", "cosmic-vikings"),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_read(state: IntercompanySalesState,
               runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    files = _expand_files(ctx.get("files", []))
    plan: list[dict] = []
    unknowns: list[str] = []
    for ref in files:
        norm = _normalize_file(ref)
        path = _resolve_storage_path(norm["path"])
        name = norm["name"]
        if not path:
            unknowns.append(name or "(no path)")
            continue
        kind = detect_type(name or path)
        if kind == "unknown":
            unknowns.append(name or path)
            continue
        plan.append({"name": name, "path": path, "kind": kind})
    if unknowns or not plan:
        # §4 fail-safe: unknown type or no usable file → AR_UNCERTAIN.
        msg = "unknown file type" if unknowns else "no files supplied"
        if unknowns:
            msg = f"unknown file type for: {', '.join(unknowns)}"
        return {"file_plan": plan, "status": "failed",
                "error": {"code": "AR_UNCERTAIN", "message": msg},
                "updated_at": utc_now()}
    # Read the first usable KOT file (v1: one KOT per run).
    entry = plan[0]
    try:
        reader = _make_reader(entry["kind"], entry["path"])
    except Exception as exc:  # noqa: BLE001 — dep/import failure is hard
        return {"raw_rows": [], "status": "failed",
                "error": {"code": "AR_NOT_IMPLEMENTED",
                          "message": f"reader unavailable: {exc}"},
                "updated_at": utc_now()}
    if reader is None:
        return {"raw_rows": [], "status": "failed",
                "error": {"code": "AR_NOT_IMPLEMENTED",
                          "message": f"no reader for kind '{entry['kind']}'"},
                "updated_at": utc_now()}
    envelope = _read_with_retry(reader, entry["path"], entry["kind"], state.trace_id)
    if envelope.get("status") != "ok":
        err = envelope.get("error") or {}
        msg = err.get("message", "read failed") if isinstance(err, dict) else "read failed"
        return {"raw_rows": [], "status": "failed",
                "error": {"code": envelope.get("code", "AR_UPSTREAM"),
                          "message": msg},
                "updated_at": utc_now()}
    data = envelope.get("data") if isinstance(envelope, dict) else {}
    rows = _rows_from_content(data if isinstance(data, dict) else {})
    if not rows:
        return {"raw_rows": [], "status": "failed",
                "error": {"code": "AR_UNCERTAIN",
                          "message": "KOT sheet has no rows"},
                "updated_at": utc_now()}
    return {"raw_rows": rows, "status": "read", "updated_at": utc_now()}


def _after_read(state: IntercompanySalesState) -> str:
    # Path-map keys are node statuses ("failed"/"read"); returning state.status
    # routes "failed"→respond and "read"→validate (ADR-0003 §9).
    return state.status


def _node_validate(state: IntercompanySalesState,
                   runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    rows = list(state.raw_rows)
    per_row, hmap, missing = _validate_kot_rows(rows)
    report = _build_validation_report(rows, per_row, state.trace_id)
    if missing:
        # Hard fail: a required column is entirely absent — cannot proceed.
        return {"validation_report": report, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": f"required columns missing: {', '.join(missing)}"},
                "updated_at": utc_now()}
    return {"validation_report": report, "status": "validated",
            "updated_at": utc_now()}


def _after_validate(state: IntercompanySalesState) -> str:
    return state.status


def _node_classify_exceptions(state: IntercompanySalesState,
                              runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    rows = list(state.raw_rows)
    per_row, _hmap, _missing = _validate_kot_rows(rows)
    valid, exception, report = _classify_exceptions(rows, per_row, state.trace_id)
    if not valid:
        # All rows are exceptions → nothing to invoice → fail safe (§4).
        return {"valid_rows": valid, "exception_rows": exception,
                "exception_report": report, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": f"all {len(exception)} rows failed validation"},
                "updated_at": utc_now()}
    return {"valid_rows": valid, "exception_rows": exception,
            "exception_report": report, "status": "classified",
            "updated_at": utc_now()}


def _after_classify(state: IntercompanySalesState) -> str:
    return state.status


def _node_calculate_revenue(state: IntercompanySalesState,
                            runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    rows = list(state.raw_rows)
    per_row, hmap, _missing = _validate_kot_rows(rows)
    valid, _exc, _rep = _classify_exceptions(rows, per_row, state.trace_id)
    revenue = calculate_revenue(valid, hmap, state.trace_id, state.tenant)
    return {"revenue": revenue, "valid_rows": valid, "status": "calculated",
            "updated_at": utc_now()}


def _node_build_invoice(state: IntercompanySalesState,
                        runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    rows = list(state.raw_rows)
    per_row, hmap, _missing = _validate_kot_rows(rows)
    valid, _exc, _rep = _classify_exceptions(rows, per_row, state.trace_id)
    revenue = state.revenue or calculate_revenue(valid, hmap, state.trace_id,
                                                 state.tenant)
    invoices = build_invoices(valid, hmap, revenue, state.trace_id, state.tenant)
    # Guard: validate each invoice inline (build-phase: route through
    # ValidationEngineComponent for InvoiceData).
    all_errs: list[str] = []
    for i, inv in enumerate(invoices):
        all_errs.extend(f"invoice[{i}] {e}" for e in _validate_invoice(inv))
    if all_errs:
        return {"invoices": invoices, "revenue": revenue, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": "; ".join(all_errs[:20])},
                "updated_at": utc_now()}
    return {"invoices": invoices, "revenue": revenue, "status": "built",
            "updated_at": utc_now()}


def _node_build_state(state: IntercompanySalesState,
                      runtime: Runtime[IntercompanySalesContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    return {"workflow_state": ws, "status": "completed",
            "updated_at": utc_now()}


def _node_checkpoint(state: IntercompanySalesState,
                     runtime: Runtime[IntercompanySalesContext]) -> dict:
    """Record the audit id (§11 — the draft invoice set is the auditable artifact).

    The InMemorySaver persists state after this node."""
    ctx = _ctx(runtime)
    _ = ctx
    audit_refs = list(state.audit_refs)
    aid = _audit_ref(state.trace_id)
    if aid not in audit_refs:
        audit_refs.append(aid)
    # Reflect the audit ref into the WorkflowState snapshot too.
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "updated_at": utc_now()}


def _node_respond(state: IntercompanySalesState,
                  runtime: Runtime[IntercompanySalesContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class IntercompanySalesFlowComponent(Component):
    # Bare class name as the canonical `name` (mirrors SupervisorAgentComponent).
    name = "IntercompanySalesFlowComponent"
    display_name = "Cosmic AR Intercompany Sales Flow"
    description = (
        "Reads an uploaded KOT (Kitchen Order Ticket) Excel from intercompany "
        "buyer restaurants (HYP, Upyard), validates rows, looks up "
        "menu/qty/agreed-rate from the sheet, calculates intercompany revenue, "
        "and generates a draft InvoiceData JSON per buyer + Validation Report + "
        "Exception Report — with logging, retries, and checkpoints (constitution "
        "§1/§4/§8/§9/§10/§11/§12/§15/§16). The 4th AR subflow; v1 is compute + "
        "draft only (no posting). See ADR-0005."
    )
    icon = "ReceiptText"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="User Request",
            info="The natural-language request accompanying the KOT upload (carries intent keywords).",
            required=False,
            tool_mode=True,
        ),
        HandleInput(
            name="files",
            display_name="Uploaded KOT",
            info="Uploaded KOT Excel/CSV refs — either from the canvas File node (Data) "
                 "or carried on the ChatInput Message (.files) when files are injected via "
                 "the run API (the 'accept uploaded KOT' responsibility).",
            input_types=["Data", "Message"],
            is_list=True,
            required=False,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic validate/calculate/build; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="intercompany_output",
            display_name="Intercompany Sales Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=IntercompanySalesState,
                           context_schema=IntercompanySalesContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("read", _node_read)
        graph.add_node("validate", _node_validate)
        graph.add_node("classify_exceptions", _node_classify_exceptions)
        graph.add_node("calculate_revenue", _node_calculate_revenue)
        graph.add_node("build_invoice", _node_build_invoice)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_edge("ingest", "read")
        graph.add_conditional_edges("read", _after_read,
                                    {"failed": "respond", "read": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond", "validated": "classify_exceptions"})
        graph.add_conditional_edges("classify_exceptions", _after_classify,
                                    {"failed": "respond",
                                     "classified": "calculate_revenue"})
        graph.add_edge("calculate_revenue", "build_invoice")
        graph.add_edge("build_invoice", "build_state")
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
            files = _expand_files(getattr(self, "files", None))
            session_id = _to_str(getattr(self, "session_id", "")) or mint_id()
            actor = _to_str(getattr(self, "actor", ""))
            model_name = _to_str(getattr(self, "model_name", ""))
            ctx: IntercompanySalesContext = {
                "user_input": user_input,
                "files": files,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_intercompany_sales",
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = IntercompanySalesState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=intercompany_sales.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=intercompany_sales outcome={envelope.get('status')} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Intercompany sales run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=intercompany_sales.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state → §14 envelope (deterministic from state).

        ``graph.get_state(config).values`` returns the merged channel values as a
        *plain dict* (not the typed dataclass — nodes receive the reconstructed
        dataclass, but the snapshot does not), so we access fields by key here
        (ADR-0003 §10).

        The payload nests under ``data`` (§14: top level is
        ``status|code|trace_id|data|error|approval_ref`` with
        ``additionalProperties:false``). The supervisor merges ``data.audit_refs``
        into ``AgentState``; revenue is NOT a recognized ``data.totals`` key, so
        the revenue/invoices/reports stay in ``data`` (ADR-0005 §7 — no
        ``AgentState`` schema change). v1 is compute + draft only, so ``data``
        carries no financial ``totals{matched,outstanding,posted}`` (those stay
        ``"0.00"`` inside ``data.workflow_state``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        raw_rows = vals.get("raw_rows") or []
        invoices = vals.get("invoices") or []
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "invoices": list(invoices) if isinstance(invoices, list) else [],
            "revenue": vals.get("revenue") or {},
            "validation_report": vals.get("validation_report") or {},
            "exception_report": vals.get("exception_report") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "document_count": len(raw_rows) if isinstance(raw_rows, list) else 0,
            "invoice_count": len(invoices) if isinstance(invoices, list) else 0,
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "intercompany sales failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err}
        return {"status": "ok", "code": "AR_OK", "trace_id": trace_id,
                "data": data}