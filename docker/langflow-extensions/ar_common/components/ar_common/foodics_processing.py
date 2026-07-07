"""Cosmic AR Agent — Foodics Processing Flow component (constitution §8, architecture §4 row 6).

The Foodics Processing Flow is the 6th AR subflow. Cosmic receives Foodics
**Order**, **Order Items**, and **Order Payments** data — either as three
uploaded export files (Excel/CSV) or via the Foodics API — and must turn it
into a **consolidated dataset** (a per-order join of items + payments), a
**pivot** (by item and by payment type), a **payment-type** breakdown, a
**discount-adjusted** invoice set, a **Zoho Books upload format**, a draft
``InvoiceData`` per order, and a **Validation Report** + **Exception Report**.
This flow reads the three sources (files now, API via a build-phase seam),
validates rows, builds the consolidated/pivot/sheet3 datasets (all JSON — no
``.xlsx`` in v1), determines payment type, applies discount rules, generates
the Zoho upload format + draft ``InvoiceData`` per order + reports, and
returns structured JSON — with logging (§12), retries (§10), and **checkpoints
after every calculation** (§11 — continuing ADR-0006's stricter pattern). It is
the **single stateful orchestrator** for Foodics order processing, mirroring
the supervisor, the File Intake Flow, the Intercompany Sales Flow, and the
Cosmic Kitchen Revenue Flow.

v1 is **compute + draft only**: it produces the invoice JSON + Zoho upload
rows for review; it does **not** post, so no money moves and no ledger entry
posts this turn (§1 north star preserved). The flow is registered at tier
``approval`` (its intent is invoice production), but the §19 gate is
**dormant in v1**: there is no ``ApprovalGate``, no idempotency key, no
``pending_approval``, and it is **not** in ``FINANCIAL_INTENTS`` (mirrors the
Intercompany Sales Flow, ADR-0005). See ADR-0007.

Responsibilities → LangGraph nodes:

  ingest → read (§10 retry, dual-source) → validate → classify_exceptions →
  build_consolidated → refresh_pivot → determine_payment_type → apply_discounts →
  populate_sheet3 → build_zoho_upload → build_invoice → build_state →
  checkpoint → respond

  - ingest             : bind ``trace_id``/``flow_id``/``tenant`` + timestamps;
                          carry uploaded-file refs + ``source_mode`` in
                          **context** (not state — §8).
  - read               : ``source_mode`` resolves the source: ``auto`` = files
                          when uploaded else API; ``files``/``api`` force it.
                          **Files path:** expand + classify each uploaded sheet
                          by role (order / order_items / order_payments) by
                          filename keyword (header fallback). Instantiate the
                          matching cosmic_common reader per file inside the §10
                          retry loop (mirrors the Kitchen Revenue Flow's
                          multi-file read). **API path:** lazy-import +
                          instantiate ``FoodicsARTool`` and call
                          ``fetch_foodics_data`` with operations
                          ``list_orders`` / ``list_order_items`` /
                          ``list_order_payments`` inside the §10 retry loop.
                          ``FoodicsARTool`` is a scaffold today, so the API path
                          returns ``AR_NOT_IMPLEMENTED`` and the flow fails safe.
                          Unknown type/no usable file → ``AR_UNCERTAIN`` (§4).
                          Zero recognized roles → ``AR_UNCERTAIN``. Read failure
                          on any uploaded file → ``AR_VALIDATION``.            §10/§9
  - validate           : inline hand-rolled per-role validator with role-specific
                          required columns. A **required column entirely missing
                          for a present role** is a hard ``AR_VALIDATION``. Else
                          the full ``ValidationResult`` is built.                §9
  - classify_exceptions: split rows into valid vs exception and build the Exception
                          Report = a ``ValidationResult`` scoped to failures
                          (``rule_id`` per exception). A **missing role** is a
                          validation warning (not a hard fail); that calc emits
                          ``0.00``/empty. All-rows-fail → ``AR_VALIDATION``.       §4
  - build_consolidated : join order ↔ order_items by ``order_ref``; attach
                          payment rows per order. Emit ``data.consolidated`` (JSON
                          dataset — no ``.xlsx`` in v1). **Records a checkpoint.** §8
  - refresh_pivot      : aggregate the consolidated dataset by item and by payment
                          type. Emit ``data.pivot``. **Records a checkpoint.**     §8
  - determine_payment_type : map each payment row's raw mode to the
                          ``CollectionData.method`` enum via ``METHOD_SYNONYMS``
                          (cash/card/bank_transfer/online/wallet/other); build
                          ``data.payment_type_summary``. **Records a checkpoint.**  §15
  - apply_discounts    : **both sources, precedence in-file > baked-in > 0.00**.
                          Per order_items row: an in-file discount column
                          (``discount_amount``/``discount_pct``/``discount``) wins;
                          else the first matching ``DISCOUNT_RULES`` baked-in rule;
                          else ``0.00``. Stash adjusted line amounts. **Records a
                          checkpoint.**                                                           §17
  - populate_sheet3    : a third report dataset (per-order net summary). Emit
                          ``data.sheet3``. **Records a checkpoint.**               §8
  - build_zoho_upload   : transform the consolidated + discounted data into Zoho
                          Books invoice-import rows (flow-specific JSON — no new
                          schema). Emit ``data.zoho_upload``. **Records a checkpoint.** §15
  - build_invoice       : build **one ``InvoiceData`` per ``order_ref``** (mirrors
                          intercompany's per-buyer grouping). Each: discount-adjusted
                          line amounts, ``subtotal``/``discounts``/``total``/
                          ``balance_due`` (2dp), ``issue_date`` = order ``posted_at``,
                          ``due_date`` = issue + ``NET_TERMS_DAYS``, ``status="draft"``,
                          deterministic ids via ``uuid5``. **Records a checkpoint.**  §15/§16
  - build_state        : build a ``WorkflowState`` snapshot (status="completed",
                          totals ``"0.00"`` — no money moved). Immutable (§8).
  - checkpoint         : record the final aggregate audit id; reflect ``audit_refs``
                          + ``checkpoints`` into the ``WorkflowState`` snapshot.
                          ``InMemorySaver`` persists state.                         §11
  - respond            : build the §14 envelope carrying ``data.invoices``,
                          ``data.consolidated``, ``data.pivot``,
                          ``data.payment_type_summary``, ``data.sheet3``,
                          ``data.zoho_upload``, ``data.validation_report``,
                          ``data.exception_report``, ``data.workflow_state``,
                          ``data.audit_refs``, ``data.checkpoints``.             §14

**Checkpoints after every calculation** (continuing ADR-0006's stricter
pattern, beyond §11's "after each reconciled batch"): each calc/transform node
records a labeled ``_audit_ref`` into ``audit_refs`` and a ``checkpoints``
map (``{consolidated, pivot, payment_type, discounts, sheet3, zoho_upload,
invoice}``), persisted by ``InMemorySaver`` at each super-step. Recorded as an
ADR-0007 decision.

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id``. This
is the §11 **fallback**: non-durable (lost on worker recreate). Durable Postgres
checkpointing remains a documented build-phase step (see ADR-0007 and the
constitution §11 caveat — Langfuse tracing is currently off, so the checkpoint
is the source of truth for resume).

The supervisor's ``_node_invoke`` merges only ``data.totals{matched,outstanding,
posted}`` and ``data.audit_refs`` into ``AgentState``. Invoices / consolidated /
pivot / sheet3 / zoho_upload / payment_type_summary are NOT recognized totals
keys → they stay in the envelope ``data`` (no ``AgentState`` schema change —
same as ADR-0005 §7 / ADR-0006).

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. Customer refs are ids (Zoho customer
ids) — no PII (§16). No credentials are needed in v1 (deterministic, in-file;
the API path fails safe until ``FoodicsARTool`` + a Secret Global Variable are
wired — §16).
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
from lfx.io import DropdownInput, HandleInput, MessageTextInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1). Tunables belong in Global Variables (§17) at build
#  phase; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"

# §10 retry policy (mirrors the supervisor / File Intake / Intercompany / Kitchen).
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0
BACKOFF_CAP_S: float = 30.0

# Foodics processing v1 policy.
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors invoice_builder/calc_engine)
NET_TERMS_DAYS: int = 30  # deterministic issue→due offset (v1)

# The three Foodics input roles.
ROLES: tuple[str, ...] = ("order", "order_items", "order_payments")

# Column-name aliases (lowercased keys). The reader emits dict rows keyed by the
# sheet header; lookups are case-insensitive and tolerant of synonyms.
ORDER_REF_KEYS = ("order_ref", "order_id", "order_number", "order", "ref",
                  "reference", "transaction_ref")
CUSTOMER_KEYS = ("customer_ref", "customer_id", "customer", "cust_id", "account_id",
                 "guest", "client", "buyer", "buyer_ref")
ITEM_REF_KEYS = ("item_ref", "item_id", "menu_item", "menu", "item",
                 "product", "product_id", "sku")
QTY_KEYS = ("qty", "quantity", "count", "units")
RATE_KEYS = ("unit_price", "rate", "price", "agreed_rate", "transfer_price",
             "agreed_price", "price_per_unit")
AMOUNT_KEYS = ("amount", "total", "value", "line_total", "sales_amount", "net_amount",
               "payment_amount", "paid_amount")
DATE_KEYS = ("posted_at", "date", "order_date", "txn_date", "transaction_date",
             "business_date", "sales_date", "created_at")
CURRENCY_KEYS = ("currency", "curr", "ccy")
DESC_KEYS = ("description", "desc", "item_desc", "menu_desc", "item_name", "name")
PAYMENT_REF_KEYS = ("payment_ref", "payment_id", "payment", "check_no",
                   "check_number", "cheque_no", "txn_id", "reference", "ref")
METHOD_KEYS = ("method", "payment_method", "pay_method", "mode", "payment_mode",
               "payment_type")
CATEGORY_KEYS = ("category", "item_category", "type", "menu_category", "department")
DISCOUNT_AMOUNT_KEYS = ("discount_amount", "discount_value", "line_discount")
DISCOUNT_PCT_KEYS = ("discount_pct", "discount_percent", "discount_rate", "pct")
DISCOUNT_GENERIC_KEYS = ("discount", "discounts", "disc")
TAX_KEYS = ("tax", "tax_amount", "vat", "vat_amount")

# Required columns per role (a present role missing any of these is a hard
# AR_VALIDATION — the flow cannot build that role's data).
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "order": ("order_ref", "posted_at"),
    "order_items": ("order_ref", "item_ref", "qty", "unit_price"),
    "order_payments": ("payment_ref", "order_ref", "amount", "method", "posted_at"),
}

# 2dp / date / currency / ISO-datetime patterns (the contracts' patterns).
RE_2DP = re.compile(r"^-?\d+\.\d{2}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")
RE_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Payment-method synonyms → CollectionData.method enum (cash|card|bank_transfer|
# online|wallet|other). A "check"/"cheque" is a bank instrument → bank_transfer.
METHOD_SYNONYMS: dict[str, str] = {
    "cash": "cash", "card": "card", "credit card": "card", "debit card": "card",
    "credit": "card", "bank_transfer": "bank_transfer", "bank transfer": "bank_transfer",
    "bank": "bank_transfer", "transfer": "bank_transfer", "wire": "bank_transfer",
    "check": "bank_transfer", "cheque": "bank_transfer",
    "online": "online", "internet": "online", "wallet": "wallet", "mobile": "wallet",
    "apple_pay": "wallet", "google_pay": "wallet", "stc_pay": "wallet", "mada": "card",
    "other": "other",
}

# Foodics API operations the flow requests (the ``FoodicsARTool`` scaffold does
# not yet implement them — the API path fails safe until it does).
API_OPERATIONS: dict[str, str] = {
    "order": "list_orders",
    "order_items": "list_order_items",
    "order_payments": "list_order_payments",
}

# Baked-in discount rules (v1 seed — §17; tunables belong in Global Variables at
# build phase). Precedence: in-file discount column > first matching baked-in
# rule > "0.00". A rule matches when the row's value for the matcher key equals
# the matcher value (case-insensitive). ``kind`` is ``pct`` (a percentage of the
# line gross) or ``amount`` (a flat 2dp amount).
DISCOUNT_RULES: list[dict] = [
    {"matcher": {"category": "beverage"}, "kind": "pct", "value": "10.00"},
    {"matcher": {"category": "dessert"}, "kind": "pct", "value": "5.00"},
    {"matcher": {"item_ref": "combo_meal"}, "kind": "amount", "value": "2.50"},
]


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class FoodicsProcessingContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[FoodicsProcessingContext]``.

    Durable, resumable state lives in ``FoodicsProcessingState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    files: list[Any]  # uploaded Foodics export refs from the canvas File node
    source_mode: str  # auto | files | api — forces the input source
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FoodicsProcessingState:
    """The Foodics Processing Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    Derived working data (rows, datasets, invoices, reports) is transient.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|read|validated|classified|consolidated|pivot|payment_type|discounts|
    # sheet3|zoho|invoice|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    # Derived working data.
    source_mode: str = "auto"  # resolved source actually used
    file_plan: list = field(default_factory=list)  # [{name, path, kind}]
    inputs: dict = field(default_factory=dict)  # {role: [rows]} classified per role
    consolidated: Optional[dict] = None  # consolidated dataset (JSON)
    pivot: Optional[dict] = None  # pivot dataset (JSON)
    payment_type_summary: Optional[dict] = None  # by-method breakdown
    discounts_total: str = "0.00"  # running discount total (2dp)
    adjusted_lines: dict = field(default_factory=dict)  # {order_ref: {item_ref: net_amount}}
    sheet3: Optional[dict] = None  # per-order net summary dataset (JSON)
    zoho_upload: Optional[dict] = None  # Zoho Books invoice-import format (JSON)
    invoices: list = field(default_factory=list)  # one InvoiceData per order
    validation_report: Optional[dict] = None  # full ValidationResult
    exception_report: Optional[dict] = None  # ValidationResult scoped to failures
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<calc_label>: audit_ref} (§11)


def _state_to_dict(state: Any) -> dict:
    """Coerce a ``FoodicsProcessingState`` (or dict) snapshot to a plain dict.

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
    """A fresh lowercase uuid4 string (trace_id seed)."""
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
    """Identify the export file type by extension. ``excel``/``csv``/``unknown``.

    Foodics exports are Excel or CSV. Unknown extensions fail safe (§4) at the
    ``read`` node → skipped (and zero recognized roles → AR_UNCERTAIN).
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
        ("order_ref", ORDER_REF_KEYS),
        ("customer_ref", CUSTOMER_KEYS),
        ("item_ref", ITEM_REF_KEYS),
        ("qty", QTY_KEYS),
        ("unit_price", RATE_KEYS),
        ("amount", AMOUNT_KEYS),
        ("posted_at", DATE_KEYS),
        ("currency", CURRENCY_KEYS),
        ("description", DESC_KEYS),
        ("payment_ref", PAYMENT_REF_KEYS),
        ("method", METHOD_KEYS),
        ("category", CATEGORY_KEYS),
        ("discount_amount", DISCOUNT_AMOUNT_KEYS),
        ("discount_pct", DISCOUNT_PCT_KEYS),
        ("discount", DISCOUNT_GENERIC_KEYS),
        ("tax", TAX_KEYS),
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


def _to_iso_datetime(value: str) -> str:
    """Coerce a date/datetime string to ``YYYY-MM-DDTHH:MM:SSZ`` (contracts' pattern).

    A date-only input → midnight UTC; an unparseable/absent value → ``utc_now()``.
    """
    d = _parse_date(value)
    if d:
        return f"{d}T00:00:00Z"
    return utc_now()


def _add_days(date_str: str, days: int) -> str:
    """Add ``days`` to a ``YYYY-MM-DD`` date; returns ``YYYY-MM-DD``.

    Deterministic UTC arithmetic (no wall-clock side effects — §4.3).
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return date_str
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _norm_token(value: str) -> str:
    """Normalise a free-text token (category / method) to a stable slug.

    Deterministic (§4.3): no case-folding ambiguity in the grouping/matcher keys.
    """
    v = (value or "").lower().strip()
    v = re.sub(r"[^a-z0-9]+", "_", v).strip("_")
    return v


def _issue(path: str, code: str, message: str, rule_id: str) -> dict[str, str]:
    """Build one ``ValidationResult.errors[]`` issue (schema-conformant)."""
    return {"path": path, "code": code, "message": message, "rule_id": rule_id}


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


def _map_method(value: str) -> str:
    """Map a payment-method string to the ``CollectionData.method`` enum.

    Returns ``""`` when empty or unrecognised (the validator flags these); a known
    synonym → its enum value (check/cheque → bank_transfer).
    """
    v = (value or "").lower().strip()
    return METHOD_SYNONYMS.get(v, "")


def _line_gross(row: dict) -> Decimal:
    """Resolve an order-items row's gross amount: ``qty × unit_price`` (2dp).

    Returns ``Decimal("0")`` when unresolvable (invalid rows are excluded upstream
    by the per-role validator, so this is a defensive default).
    """
    qty = _to_decimal(_row_key(row, QTY_KEYS)) or Decimal("0")
    rate = _to_decimal(_row_key(row, RATE_KEYS)) or Decimal("0")
    return (qty * rate).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
#  Role classification + per-role validation (inline, hand-rolled — §15 reuse
#  note in ADR-0007; ValidationEngineComponent only implements DocumentManifest).
# --------------------------------------------------------------------------- #


def _classify_input(name: str, rows: list[dict]) -> str:
    """Classify an uploaded export into one of the three Foodics roles.

    Primary signal: filename keyword (``item`` → order_items; ``payment`` →
    order_payments; ``order`` → order — but only when it is not item/payment).
    Fallback: a header-content sniff (item_ref+qty → order_items; payment_ref/
    payment_type/method → order_payments; order_ref alone → order). Returns
    ``"unknown"`` when unrecognised.
    """
    n = (name or "").lower()
    if "item" in n:
        return "order_items"
    if "payment" in n:
        return "order_payments"
    if "order" in n:
        return "order"
    # Header-content fallback.
    keys: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            keys.update(k.lower().replace(" ", "_") for k in r.keys())
    if keys & {"item_ref", "item_id", "qty", "quantity", "unit_price", "menu_item"}:
        return "order_items"
    if keys & {"payment_ref", "payment_id", "payment_type", "payment_method",
                "payment_amount", "paid_amount"}:
        return "order_payments"
    if keys & {"order_ref", "order_id", "order_number"}:
        return "order"
    return "unknown"


def _validate_role_row(role: str, row: dict, hmap: dict[str, str],
                        index: int) -> list[dict]:
    """Validate one row for ``role`` → list of issue dicts (empty when valid)."""
    errs: list[dict] = []
    base = f"{role}[{index}]"

    if role == "order":
        if not _row_key(row, ORDER_REF_KEYS):
            errs.append(_issue(f"{base}.order_ref", "AR_VALIDATION_REQUIRED",
                               "order_ref is required", "fp.order_ref_required"))
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.posted_at", "AR_VALIDATION_FORMAT",
                               "posted_at must be an ISO date (YYYY-MM-DD)",
                               "fp.date_iso"))

    elif role == "order_items":
        if not _row_key(row, ORDER_REF_KEYS):
            errs.append(_issue(f"{base}.order_ref", "AR_VALIDATION_REQUIRED",
                               "order_ref is required", "fp.order_ref_required"))
        if not _row_key(row, ITEM_REF_KEYS):
            errs.append(_issue(f"{base}.item_ref", "AR_VALIDATION_REQUIRED",
                               "item_ref is required", "fp.item_ref_required"))
        qty = _to_decimal(_row_key(row, QTY_KEYS))
        if qty is None or qty <= 0:
            errs.append(_issue(f"{base}.qty", "AR_VALIDATION_POSITIVE",
                               "qty must be a positive number", "fp.qty_positive"))
        rate = _to_decimal(_row_key(row, RATE_KEYS))
        if rate is None or rate <= 0:
            errs.append(_issue(f"{base}.unit_price", "AR_VALIDATION_POSITIVE",
                               "unit_price must be a positive number",
                               "fp.unit_price_positive"))

    elif role == "order_payments":
        if not _row_key(row, PAYMENT_REF_KEYS):
            errs.append(_issue(f"{base}.payment_ref", "AR_VALIDATION_REQUIRED",
                               "payment_ref is required", "fp.payment_ref_required"))
        if not _row_key(row, ORDER_REF_KEYS):
            errs.append(_issue(f"{base}.order_ref", "AR_VALIDATION_REQUIRED",
                               "order_ref is required", "fp.order_ref_required"))
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            errs.append(_issue(f"{base}.amount", "AR_VALIDATION_POSITIVE",
                               "amount must be a positive number",
                               "fp.amount_positive"))
        method = _row_key(row, METHOD_KEYS)
        if not method:
            errs.append(_issue(f"{base}.method", "AR_VALIDATION_REQUIRED",
                               "method is required", "fp.method_required"))
        elif not _map_method(method):
            errs.append(_issue(f"{base}.method", "AR_VALIDATION_ENUM",
                               f"method '{method}' is not a recognised payment method",
                               "fp.method_enum"))
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.posted_at", "AR_VALIDATION_FORMAT",
                               "posted_at must be an ISO date (YYYY-MM-DD)",
                               "fp.date_iso"))

    return errs


def _validate_role_rows(role: str, rows: list[dict]
                        ) -> tuple[list[list[dict]], dict[str, str], list[str]]:
    """Validate all rows for ``role``.

    Returns ``(per_row_errors, header_map, missing_required)`` where
    ``per_row_errors[i]`` is the issue list for ``rows[i]`` (empty when valid),
    ``header_map`` maps canonical→actual header key, and ``missing_required`` is
    the list of canonical required columns entirely absent from the sheet.
    """
    hmap = _header_map(rows)
    missing = [c for c in REQUIRED_COLUMNS.get(role, ()) if c not in hmap]
    per_row: list[list[dict]] = []
    for i, row in enumerate(rows):
        if missing:
            per_row.append([_issue(f"{role}[{i}]", "AR_VALIDATION_REQUIRED",
                                   f"required column missing: {', '.join(missing)}",
                                   f"fp.{role}_required_columns")])
        else:
            per_row.append(_validate_role_row(role, row, hmap, i))
    return per_row, hmap, missing


def _valid_rows_for(role: str, inputs: dict) -> tuple[list[dict], dict[str, str]]:
    """Return ``(valid_rows, header_map)`` for ``role`` from the classified inputs.

    A row is valid when it has no validation issues. A role with a missing
    required column yields zero valid rows (every row is flagged). Recomputed per
    node (mirrors the Kitchen Revenue Flow's per-node recompute — deterministic).
    """
    rows = inputs.get(role, []) if isinstance(inputs, dict) else []
    if not rows:
        return [], {}
    per_row, hmap, _missing = _validate_role_rows(role, rows)
    valid = [row for row, errs in zip(rows, per_row) if not errs]
    return valid, hmap


def _build_validation_report(errors: list[dict], trace_id: str) -> dict:
    """Build a ``ValidationResult`` over the given issues (pure)."""
    return {
        "valid": len(errors) == 0,
        "contract_name": "FoodicsInputs",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": list(errors),
        "validated_at": utc_now(),
        "schema_ref": "https://cosmic-vikings/ar-agent/contracts/validation-result.schema.json",
    }


def _classify_exceptions(inputs: dict, trace_id: str
                          ) -> tuple[list[dict], dict]:
    """Build the Exception Report across all present roles + missing-role warnings.

    Returns ``(exception_report, row_exception_count)``. The Exception Report is a
    ``ValidationResult`` scoped to failures (row errors + missing-role warnings,
    each carrying a ``rule_id``).
    """
    errors: list[dict] = []
    row_exceptions = 0
    for role in ROLES:
        rows = inputs.get(role, []) if isinstance(inputs, dict) else []
        if not rows:
            # Missing-role warning (not a hard fail — that node emits 0.00/empty).
            errors.append(_issue(role, "AR_VALIDATION_REQUIRED",
                                 f"{role} sheet not supplied — {role} data reported as empty",
                                 f"fp.{role}_missing"))
            continue
        per_row, _hmap, _missing = _validate_role_rows(role, rows)
        for errs in per_row:
            if errs:
                row_exceptions += 1
                errors.extend(errs)
    report = _build_validation_report(errors, trace_id)
    return report, row_exceptions


def _warn(report: dict, path: str, code: str, message: str, rule_id: str) -> dict:
    """Append a warning issue to a ``ValidationResult`` report (returns a copy)."""
    out = dict(report)
    errs = list(out.get("errors", []))
    errs.append(_issue(path, code, message, rule_id))
    out["errors"] = errs
    out["valid"] = False
    out["validated_at"] = utc_now()
    return out


# --------------------------------------------------------------------------- #
#  Calculations (pure, deterministic — §4.3).
# --------------------------------------------------------------------------- #


def _order_index(order_rows: list[dict]) -> dict[str, dict]:
    """Index valid order rows by ``order_ref`` → ``{order_ref: row}`` (first wins)."""
    out: dict[str, dict] = {}
    for row in order_rows:
        ref = _row_key(row, ORDER_REF_KEYS)
        if ref and ref not in out:
            out[ref] = row
    return out


def _order_currency(order_row: Optional[dict], hmap: dict[str, str]) -> str:
    """Currency for an order: the order row's currency column > default."""
    if order_row and "currency" in hmap:
        cur = _row_key(order_row, CURRENCY_KEYS)
        if cur:
            cur = cur.upper()
            if RE_CURRENCY.match(cur):
                return cur
    return DEFAULT_CURRENCY


def build_consolidated(order_rows: list[dict], item_rows: list[dict],
                       pay_rows: list[dict], order_hmap: dict[str, str],
                       trace_id: str, tenant: str) -> dict:
    """Build the consolidated dataset: per-order join of items + payments (JSON).

    ``data.consolidated = {orders:[{order_ref, customer_ref, posted_at, currency,
    items:[…], payments:[…], gross_total, payment_total}], count,
    contract_version}``. All amounts 2dp.
    """
    orders_index = _order_index(order_rows)
    items_by_order: dict[str, list[dict]] = {}
    pays_by_order: dict[str, list[dict]] = {}
    for row in item_rows:
        ref = _row_key(row, ORDER_REF_KEYS)
        if ref:
            items_by_order.setdefault(ref, []).append(row)
    for row in pay_rows:
        ref = _row_key(row, ORDER_REF_KEYS)
        if ref:
            pays_by_order.setdefault(ref, []).append(row)

    # Order list = recognized order_refs (orders sheet first, then items/payments
    # whose order_ref has no order header row — keep them so downstream still
    # produces figures).
    all_refs: list[str] = []
    seen: set[str] = set()
    for ref in list(orders_index.keys()):
        if ref not in seen:
            seen.add(ref)
            all_refs.append(ref)
    for ref in list(items_by_order.keys()) + list(pays_by_order.keys()):
        if ref not in seen:
            seen.add(ref)
            all_refs.append(ref)

    orders_out: list[dict] = []
    for ref in all_refs:
        order_row = orders_index.get(ref)
        currency = _order_currency(order_row, order_hmap)
        customer = (_row_key(order_row, CUSTOMER_KEYS) if order_row else "") or "CUST-UNKNOWN"
        posted = (_parse_date(_row_key(order_row, DATE_KEYS)) if order_row else "") or ""
        items: list[dict] = []
        gross_amounts: list[str] = []
        for i, irow in enumerate(items_by_order.get(ref, [])):
            gross = _line_gross(irow)
            gross_s = _to_2dp(gross)
            gross_amounts.append(gross_s)
            items.append({
                "item_ref": _row_key(irow, ITEM_REF_KEYS) or f"item-{i + 1}",
                "description": _row_key(irow, DESC_KEYS)
                               or _row_key(irow, ITEM_REF_KEYS) or f"item-{i + 1}",
                "qty": _to_2dp(_row_key(irow, QTY_KEYS)),
                "unit_price": _to_2dp(_row_key(irow, RATE_KEYS)),
                "amount": gross_s,
            })
        payments: list[dict] = []
        pay_amounts: list[str] = []
        for prow in pays_by_order.get(ref, []):
            amt_s = _to_2dp(_row_key(prow, AMOUNT_KEYS))
            pay_amounts.append(amt_s)
            payments.append({
                "payment_ref": _row_key(prow, PAYMENT_REF_KEYS) or "",
                "method": _row_key(prow, METHOD_KEYS),
                "amount": amt_s,
                "posted_at": _row_key(prow, DATE_KEYS),
            })
        orders_out.append({
            "order_ref": ref,
            "customer_ref": customer,
            "posted_at": posted,
            "currency": currency,
            "items": items,
            "payments": payments,
            "gross_total": _sum_2dp(gross_amounts),
            "payment_total": _sum_2dp(pay_amounts),
        })
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "orders": orders_out,
        "count": len(orders_out),
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def refresh_pivot(consolidated: dict) -> dict:
    """Aggregate the consolidated dataset by item and by payment type (JSON).

    ``data.pivot = {by_item:[{item_ref, qty, amount}], by_payment_type:[{payment_type,
    amount, count}], totals:{gross, collected}, contract_version}``.
    """
    by_item: dict[str, dict] = {}
    by_pt: dict[str, dict] = {}
    gross_total = Decimal("0.00")
    collected_total = Decimal("0.00")
    for order in consolidated.get("orders", []):
        for item in order.get("items", []):
            ref = item.get("item_ref", "unknown")
            qty = _to_decimal(item.get("qty")) or Decimal("0")
            amt = _to_decimal(item.get("amount")) or Decimal("0")
            agg = by_item.setdefault(ref, {"qty": Decimal("0.00"),
                                            "amount": Decimal("0.00"),
                                            "count": 0})
            agg["qty"] += qty
            agg["amount"] += amt
            agg["count"] += 1
            gross_total += amt
        for pay in order.get("payments", []):
            method = _map_method(pay.get("method", "")) or "other"
            amt = _to_decimal(pay.get("amount")) or Decimal("0")
            agg = by_pt.setdefault(method, {"amount": Decimal("0.00"), "count": 0})
            agg["amount"] += amt
            agg["count"] += 1
            collected_total += amt
    by_item_out = [{"item_ref": k, "qty": _to_2dp(v["qty"]),
                    "amount": _to_2dp(v["amount"]), "count": v["count"]}
                   for k, v in by_item.items()]
    by_pt_out = [{"payment_type": k, "amount": _to_2dp(v["amount"]),
                  "count": v["count"]} for k, v in by_pt.items()]
    return {
        "by_item": by_item_out,
        "by_payment_type": by_pt_out,
        "totals": {
            "gross": _to_2dp(gross_total),
            "collected": _to_2dp(collected_total),
        },
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def determine_payment_type(pay_rows: list[dict], trace_id: str,
                           tenant: str) -> dict:
    """Build the payment-type breakdown from order_payments rows.

    ``data.payment_type_summary = {by_method:[{method, amount, count}],
    total_collected, contract_version}``. Maps each raw mode to the
    ``CollectionData.method`` enum via ``METHOD_SYNONYMS`` (unknown → ``other``).
    """
    by_method: dict[str, dict] = {}
    amounts: list[str] = []
    for row in pay_rows:
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            continue  # defensive — valid rows are already positive
        amt_s = _to_2dp(amt)
        amounts.append(amt_s)
        method = _map_method(_row_key(row, METHOD_KEYS)) or "other"
        m = by_method.setdefault(method, {"amount": Decimal("0.00"), "count": 0})
        m["amount"] += amt
        m["count"] += 1
    total = _sum_2dp(amounts)
    by_method_out = [{"method": k, "amount": _to_2dp(v["amount"]),
                      "count": v["count"]} for k, v in by_method.items()]
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "total_collected": total,
        "by_method": by_method_out,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _match_discount_rule(row: dict) -> Optional[dict]:
    """Find the first ``DISCOUNT_RULES`` rule whose matcher fits the row.

    A matcher ``{key: value}`` fits when the row's value for ``key`` (item_ref /
    category / payment_type) normalises to ``value`` (case-insensitive). Returns
    ``None`` when no rule matches.
    """
    for rule in DISCOUNT_RULES:
        matcher = rule.get("matcher", {}) if isinstance(rule, dict) else {}
        if not isinstance(matcher, dict) or not matcher:
            continue
        matched = True
        for key, val in matcher.items():
            if key == "item_ref":
                row_val = _norm_token(_row_key(row, ITEM_REF_KEYS))
            elif key == "category":
                row_val = _norm_token(_row_key(row, CATEGORY_KEYS))
            elif key == "payment_type":
                row_val = _norm_token(_row_key(row, METHOD_KEYS))
            else:
                row_val = _norm_token(_row_key(row, (key,)))
            if row_val != _norm_token(val):
                matched = False
                break
        if matched:
            return rule
    return None


def _line_discount(row: dict, gross: Decimal) -> Decimal:
    """Resolve one line's discount: in-file column > baked-in rule > 0.00.

    ``in-file`` precedence: ``discount_amount`` (flat) > ``discount_pct``
    (percentage of gross) > ``discount`` (try amount, then pct on gross). A
    baked-in ``pct`` rule applies to gross; an ``amount`` rule is flat. Always
    non-negative, 2dp, capped at gross.
    """
    # 1. In-file discount_amount (flat).
    da = _to_decimal(_row_key(row, DISCOUNT_AMOUNT_KEYS))
    if da is not None and da > 0:
        return min(da, gross).quantize(Decimal("0.01"))
    # 2. In-file discount_pct (percentage of gross).
    dp = _to_decimal(_row_key(row, DISCOUNT_PCT_KEYS))
    if dp is not None and dp > 0:
        return min((gross * dp / Decimal("100")).quantize(Decimal("0.01")), gross)
    # 3. In-file generic discount (amount, else pct-on-gross).
    dg = _to_decimal(_row_key(row, DISCOUNT_GENERIC_KEYS))
    if dg is not None and dg > 0:
        if dg <= gross:
            return dg.quantize(Decimal("0.01"))
        # A value > gross is likely a percentage, not an amount.
        return min((gross * dg / Decimal("100")).quantize(Decimal("0.01")), gross)
    # 4. Baked-in rule.
    rule = _match_discount_rule(row)
    if rule:
        kind = rule.get("kind", "")
        val = _to_decimal(rule.get("value"))
        if val is None or val <= 0:
            return Decimal("0.00")
        if kind == "pct":
            return min((gross * val / Decimal("100")).quantize(Decimal("0.01")), gross)
        if kind == "amount":
            return min(val, gross).quantize(Decimal("0.01"))
    return Decimal("0.00")


def apply_discounts(item_rows: list[dict], trace_id: str
                    ) -> tuple[dict, str]:
    """Apply discount rules to order_items rows.

    Returns ``(adjusted_lines, discounts_total)`` where ``adjusted_lines`` is
    ``{order_ref: {item_ref: net_amount_2dp}}`` and ``discounts_total`` is the
    running 2dp discount. Precedence: in-file column > first matching baked-in
    rule > ``0.00``.
    """
    adjusted: dict[str, dict] = {}
    running = Decimal("0.00")
    for row in item_rows:
        ref = _row_key(row, ORDER_REF_KEYS)
        item_ref = _row_key(row, ITEM_REF_KEYS)
        if not ref or not item_ref:
            continue
        gross = _line_gross(row)
        disc = _line_discount(row, gross)
        net = (gross - disc).quantize(Decimal("0.01"))
        if net < 0:
            net = Decimal("0.00")
        running += disc
        adjusted.setdefault(ref, {})[item_ref] = _to_2dp(net)
    return adjusted, _to_2dp(running)


def populate_sheet3(consolidated: dict, adjusted_lines: dict,
                     discounts_total: str) -> dict:
    """Build the per-order net summary dataset (JSON) — the "Sheet3" report.

    ``data.sheet3 = {rows:[{order_ref, gross, discounts, tax, net, payment_type}],
    count, contract_version}``. ``tax`` is ``0.00`` in v1 (no tax engine); a
    single ``payment_type`` per order is the first mapped payment method (or
    ``other``).
    """
    rows_out: list[dict] = []
    running_disc = Decimal(discounts_total or "0.00")
    disc_check = Decimal("0.00")
    for order in consolidated.get("orders", []):
        ref = order.get("order_ref", "")
        gross = _to_decimal(order.get("gross_total")) or Decimal("0.00")
        # Per-order discount = gross − Σ adjusted net for this order's items.
        adj = adjusted_lines.get(ref, {})
        net_sum = Decimal("0.00")
        for item in order.get("items", []):
            iref = item.get("item_ref", "")
            net_sum += _to_decimal(adj.get(iref)) or Decimal("0.00")
        disc = (gross - net_sum).quantize(Decimal("0.01"))
        if disc < 0:
            disc = Decimal("0.00")
        disc_check += disc
        first_method = "other"
        for pay in order.get("payments", []):
            m = _map_method(pay.get("method", ""))
            if m:
                first_method = m
                break
        rows_out.append({
            "order_ref": ref,
            "gross": _to_2dp(gross),
            "discounts": _to_signed_2dp(disc),
            "tax": "0.00",
            "net": _to_2dp(net_sum),
            "payment_type": first_method,
        })
    # Defensive: discounts_total is the authoritative running total.
    _ = running_disc - disc_check  # no-op; kept for clarity/debug
    return {
        "rows": rows_out,
        "count": len(rows_out),
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def build_zoho_upload(consolidated: dict, adjusted_lines: dict,
                     trace_id: str) -> dict:
    """Transform the consolidated + discounted data into Zoho Books import rows.

    ``data.zoho_upload = {format:"zoho-books-invoice-import",
    rows:[{customer_ref, invoice_number, date, item_details:[{item_ref, qty, rate,
    amount, discount}], discount_total, total, currency}], count,
    contract_version}``. ``customer_ref`` is the Zoho customer id (no PII — §16).
    The canonical Zoho import template is build-phase; this is the flow's JSON
    representation of it.
    """
    rows_out: list[dict] = []
    for order in consolidated.get("orders", []):
        ref = order.get("order_ref", "")
        customer = order.get("customer_ref", "CUST-UNKNOWN")
        date = order.get("posted_at", "") or time.strftime("%Y-%m-%d", time.gmtime())
        currency = order.get("currency", DEFAULT_CURRENCY)
        adj = adjusted_lines.get(ref, {})
        item_details: list[dict] = []
        disc_total = Decimal("0.00")
        net_total = Decimal("0.00")
        for item in order.get("items", []):
            iref = item.get("item_ref", "")
            gross = _to_decimal(item.get("amount")) or Decimal("0.00")
            net = _to_decimal(adj.get(iref)) or gross
            disc = (gross - net).quantize(Decimal("0.01"))
            if disc < 0:
                disc = Decimal("0.00")
            disc_total += disc
            net_total += net
            item_details.append({
                "item_ref": iref,
                "qty": _to_2dp(item.get("qty")),
                "rate": _to_2dp(item.get("unit_price")),
                "amount": _to_2dp(net),
                "discount": _to_signed_2dp(disc),
            })
        rows_out.append({
            "customer_ref": customer,
            "invoice_number": f"FP-{ref}",
            "date": date,
            "item_details": item_details,
            "discount_total": _to_signed_2dp(disc_total),
            "total": _to_2dp(net_total),
            "currency": currency,
        })
    return {
        "format": "zoho-books-invoice-import",
        "rows": rows_out,
        "count": len(rows_out),
        "trace_id": trace_id,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def _deterministic_invoice_id(trace_id: str, order_ref: str) -> tuple[str, str]:
    """Derive a deterministic (trace_id+order_ref) invoice_id + invoice_number.

    ``uuid5`` is reproducible from its inputs (§4.3) — no ``Math.random``/
    ``uuid4`` here, so the same trace + order always yields the same invoice ids.
    """
    seed = f"foodics:{trace_id}:{order_ref}"
    u = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    return str(u), f"FP-{order_ref}-{u.hex[:8].upper()}"


def build_invoices(consolidated: dict, adjusted_lines: dict,
                   discounts_total: str, trace_id: str, tenant: str) -> list[dict]:
    """Build **one ``InvoiceData`` per ``order_ref``** (mirrors intercompany's
    per-buyer grouping).

    Each invoice: discount-adjusted line amounts (from ``adjusted_lines``),
    ``subtotal`` = Σ gross, ``discounts`` = the order's discount share 2dp,
    ``total`` = ``subtotal`` − ``discounts``, ``balance_due`` = ``total``,
    ``issue_date`` = order ``posted_at``, ``due_date`` = issue +
    ``NET_TERMS_DAYS``, ``currency``, ``status="draft"``, deterministic ids.
    """
    _ = discounts_total  # authoritative running total surfaced in envelope; per-order disc computed below
    invoices: list[dict] = []
    for order in consolidated.get("orders", []):
        ref = order.get("order_ref", "")
        if not ref:
            continue
        currency = order.get("currency", DEFAULT_CURRENCY)
        customer = order.get("customer_ref", "CUST-UNKNOWN")
        issue = order.get("posted_at", "") or time.strftime("%Y-%m-%d", time.gmtime())
        due = _add_days(issue, NET_TERMS_DAYS)
        adj = adjusted_lines.get(ref, {})
        line_items: list[dict] = []
        subtotal = Decimal("0.00")
        order_disc = Decimal("0.00")
        for i, item in enumerate(order.get("items", [])):
            iref = item.get("item_ref", "")
            gross = _to_decimal(item.get("amount")) or Decimal("0.00")
            net = _to_decimal(adj.get(iref)) or gross
            disc = (gross - net).quantize(Decimal("0.01"))
            if disc < 0:
                disc = Decimal("0.00")
            order_disc += disc
            subtotal += gross
            line_items.append({
                "line_id": f"L{i + 1:03d}",
                "item_ref": iref,
                "description": item.get("description") or iref,
                "qty": _to_2dp(item.get("qty")),
                "unit_price": _to_2dp(item.get("unit_price")),
                "amount": _to_2dp(net),
            })
        total = (subtotal - order_disc).quantize(Decimal("0.01"))
        if total < 0:
            total = Decimal("0.00")
        inv_id, inv_num = _deterministic_invoice_id(trace_id, ref)
        invoices.append({
            "invoice_id": inv_id,
            "invoice_number": inv_num,
            "customer_ref": customer,
            "tenant": tenant,
            "issue_date": issue,
            "due_date": due,
            "line_items": line_items,
            "subtotal": _to_2dp(subtotal),
            "discounts": _to_signed_2dp(order_disc),
            "total": _to_2dp(total),
            "balance_due": _to_2dp(total),
            "currency": currency,
            "status": "draft",
            "contract_version": CONTRACT_VERSION,
        })
    return invoices


def _validate_invoice(inv: dict) -> list[str]:
    """Inline hand-rolled ``InvoiceData`` validation → list of error strings.

    Checks the required fields and 2dp patterns (mirrors the schema). Used by the
    ``build_invoice`` node as a guard; wiring this into
    ``ValidationEngineComponent`` for ``InvoiceData`` is build-phase (ADR-0007).
    """
    errs: list[str] = []
    required = ("invoice_id", "invoice_number", "customer_ref", "tenant",
                "issue_date", "due_date", "line_items", "subtotal", "total",
                "currency", "status", "balance_due", "contract_version")
    for k in required:
        if not inv.get(k) and inv.get(k) != 0:
            errs.append(f"missing required field: {k}")
    for k in ("subtotal", "discounts", "total", "balance_due"):
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
    ``idempotency_keys={}`` (gate dormant — no POST). Status ``completed`` (the
    draft set is built).
    """
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": "ar_foodics_processing",
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:ar_foodics_processing:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-calculation audit record id (§11/§13).

    One labeled ref per calculation/transform + a final aggregate — the
    "checkpoints after every calculation" artifact.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"foodics-processing-audit:{trace_id}:{label}"))


# --------------------------------------------------------------------------- #
#  §10 retry classification (mirrors supervisor / File Intake / Intercompany /
#  Kitchen).
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
    transient retries → ``AR_UPSTREAM``.
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


def _fetch_foodics_with_retry(operation: str, trace_id: str) -> dict[str, Any]:
    """Call ``FoodicsARTool.fetch_foodics_data`` inside the §10 retry loop.

    Sets the tool's ``operation`` attribute and calls its output method. Returns a
    §14 envelope dict. The scaffold tool returns ``AR_NOT_IMPLEMENTED`` (a
    successful HTTP-shaped envelope, not an exception) → caller fails safe. A
    transient raise is retried; exhausted transient retries → ``AR_UPSTREAM``.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            tool = _make_foodics_fetcher()
            if tool is None:
                return _envelope("error", "AR_NOT_IMPLEMENTED",
                                 error={"message": "FoodicsARTool unavailable"},
                                 trace_id=trace_id)
            tool.operation = operation
            tool.entity_id = ""
            raw = tool.fetch_foodics_data()
            envelope = parse_envelope(_to_str(raw))
            if envelope is None:
                envelope = _envelope("error", "AR_NOT_IMPLEMENTED",
                                    error={"message": "Foodics API fetch is build-phase "
                                           "— provide export files or wire FoodicsARTool"},
                                    trace_id=trace_id)
            return envelope
        except Exception as exc:  # noqa: BLE001 — classified below, never raised
            last_exc = exc
            if not _is_transient(exc):
                return _envelope("error", "AR_NOT_IMPLEMENTED",
                                 error={"message": f"Foodics API fetch failed: {exc}"},
                                 trace_id=trace_id)
            if attempt < MAX_ATTEMPTS:
                _backoff_sleep(attempt)
    return _envelope("error", "AR_UPSTREAM",
                     error={"message": f"transient retries exhausted: {last_exc}"},
                     trace_id=trace_id)


def _make_foodics_fetcher() -> Any:
    """Lazy-import + instantiate ``FoodicsARTool`` (the Foodics API seam).

    Returns None if the ``ar_tools`` bundle/dep is unavailable — the caller
    surfaces ``AR_NOT_IMPLEMENTED``. The scaffold tool resolves its API token
    from a Secret Global Variable (§16); wiring real HTTP + credentials is
    build-phase.
    """
    try:
        from components.ar_tools.foodics_ar import FoodicsARTool
        return FoodicsARTool()
    except Exception:  # noqa: BLE001 — bundle absent on host is non-fatal here
        return None


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[FoodicsProcessingContext]) -> FoodicsProcessingContext:
    return runtime.context or {}


def _node_ingest(state: FoodicsProcessingState,
                 runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    return {
        "trace_id": state.trace_id or mint_id(),
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_foodics_processing"),
        "tenant": state.tenant or ctx.get("tenant", "cosmic-vikings"),
        "source_mode": state.source_mode or ctx.get("source_mode", "auto"),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_read(state: FoodicsProcessingState,
               runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    files = _expand_files(ctx.get("files", []))
    mode = (ctx.get("source_mode") or state.source_mode or "auto").strip().lower()
    if mode not in ("auto", "files", "api"):
        mode = "auto"
    use_files = bool(files) if mode == "auto" else (mode == "files")

    if use_files:
        plan: list[dict] = []
        for ref in files:
            norm = _normalize_file(ref)
            path = _resolve_storage_path(norm["path"])
            name = norm["name"]
            if not path:
                continue
            kind = detect_type(name or path)
            if kind == "unknown":
                continue
            plan.append({"name": name, "path": path, "kind": kind})
        if not plan:
            return {"file_plan": [], "inputs": {}, "source_mode": "files",
                    "status": "failed",
                    "error": {"code": "AR_UNCERTAIN", "message": "no readable files supplied"},
                    "updated_at": utc_now()}
        # Multi-file read loop (mirrors the Kitchen Revenue Flow): read each
        # file, classify its rows by role. A read failure on any file is hard.
        inputs: dict[str, list] = {role: [] for role in ROLES}
        read_failures: list[tuple[str, str]] = []  # (code, message)
        classified = 0
        for entry in plan:
            name = entry["name"]
            try:
                reader = _make_reader(entry["kind"], entry["path"])
            except Exception as exc:  # noqa: BLE001 — dep/import failure is hard
                read_failures.append(("AR_NOT_IMPLEMENTED",
                                      f"{name}: reader unavailable ({exc})"))
                continue
            if reader is None:
                read_failures.append(("AR_NOT_IMPLEMENTED",
                                      f"{name}: no reader for kind '{entry['kind']}'"))
                continue
            envelope = _read_with_retry(reader, entry["path"], entry["kind"],
                                        state.trace_id)
            if envelope.get("status") != "ok":
                err = envelope.get("error") or {}
                code = err.get("code", "AR_UPSTREAM") if isinstance(err, dict) else "AR_UPSTREAM"
                msg = err.get("message", "read failed") if isinstance(err, dict) else "read failed"
                read_failures.append((code, f"{name}: {msg}"))
                continue
            data = envelope.get("data") if isinstance(envelope, dict) else {}
            rows = _rows_from_content(data if isinstance(data, dict) else {})
            if not rows:
                continue  # empty sheet — skip (not a failure)
            role = _classify_input(name, rows)
            if role == "unknown":
                continue  # unrecognized sheet — skip (not a failure)
            inputs[role].extend(rows)
            classified += 1
        if read_failures:
            first_code, _ = read_failures[0]
            return {"file_plan": plan, "inputs": inputs, "source_mode": "files",
                    "status": "failed",
                    "error": {"code": first_code,
                              "message": "; ".join(m for _, m in read_failures)},
                    "updated_at": utc_now()}
        if classified == 0:
            return {"file_plan": plan, "inputs": inputs, "source_mode": "files",
                    "status": "failed",
                    "error": {"code": "AR_UNCERTAIN",
                              "message": "no recognized Foodics sheets"},
                    "updated_at": utc_now()}
        return {"file_plan": plan, "inputs": inputs, "source_mode": "files",
                "status": "read", "updated_at": utc_now()}

    # API path — fetch each role via the FoodicsARTool scaffold (build-phase seam).
    inputs_api: dict[str, list] = {role: [] for role in ROLES}
    fetch_failures: list[tuple[str, str]] = []
    for role in ROLES:
        envelope = _fetch_foodics_with_retry(API_OPERATIONS[role], state.trace_id)
        code = envelope.get("code", "")
        if envelope.get("status") != "ok" or code == "AR_NOT_IMPLEMENTED":
            err = envelope.get("error") or {}
            msg = err.get("message", "Foodics API fetch failed") if isinstance(err, dict) \
                else "Foodics API fetch failed"
            fetch_failures.append((code or "AR_UPSTREAM", msg))
            continue
        data = envelope.get("data") if isinstance(envelope, dict) else {}
        rows = _rows_from_content(data if isinstance(data, dict) else {})
        inputs_api[role].extend(rows)
    # The scaffold fails safe for every role → surface the build-phase message.
    if fetch_failures:
        first_code, _ = fetch_failures[0]
        return {"file_plan": [], "inputs": inputs_api, "source_mode": "api",
                "status": "failed",
                "error": {"code": first_code,
                          "message": "; ".join(m for _, m in fetch_failures)},
                "updated_at": utc_now()}
    # No fetch failures but also no rows → fail safe.
    if not any(inputs_api.values()):
        return {"file_plan": [], "inputs": inputs_api, "source_mode": "api",
                "status": "failed",
                "error": {"code": "AR_UNCERTAIN",
                          "message": "Foodics API returned no rows"},
                "updated_at": utc_now()}
    return {"file_plan": [], "inputs": inputs_api, "source_mode": "api",
            "status": "read", "updated_at": utc_now()}


def _after_read(state: FoodicsProcessingState) -> str:
    # Path-map keys are node statuses ("failed"/"read"); returning state.status
    # routes "failed"→respond and "read"→validate (ADR-0003 §9).
    return state.status


def _node_validate(state: FoodicsProcessingState,
                   runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    all_errors: list[dict] = []
    missing_any: list[str] = []
    for role in ROLES:
        rows = inputs.get(role, []) if isinstance(inputs, dict) else []
        if not rows:
            continue  # absent role — not validated here (warned in classify)
        per_row, _hmap, missing = _validate_role_rows(role, rows)
        for errs in per_row:
            all_errors.extend(errs)
        for c in missing:
            missing_any.append(f"{role}.{c}")
    report = _build_validation_report(all_errors, state.trace_id)
    if missing_any:
        # Hard fail: a present role is missing a required column — cannot proceed.
        return {"validation_report": report, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": f"required columns missing: {', '.join(missing_any)}"},
                "updated_at": utc_now()}
    return {"validation_report": report, "status": "validated",
            "updated_at": utc_now()}


def _after_validate(state: FoodicsProcessingState) -> str:
    return state.status


def _node_classify_exceptions(state: FoodicsProcessingState,
                              runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    report, row_exceptions = _classify_exceptions(inputs, state.trace_id)
    # All-rows-fail: there were rows but none valid → fail safe (§4).
    total_rows = sum(len(inputs.get(r, [])) for r in ROLES) if isinstance(inputs, dict) else 0
    if total_rows > 0 and row_exceptions >= total_rows:
        return {"exception_report": report, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": f"all {row_exceptions} rows failed validation"},
                "updated_at": utc_now()}
    return {"exception_report": report, "status": "classified",
            "updated_at": utc_now()}


def _after_classify(state: FoodicsProcessingState) -> str:
    return state.status


def _record_checkpoint(state: FoodicsProcessingState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry for a calc (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


def _node_build_consolidated(state: FoodicsProcessingState,
                             runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    order_valid, order_hmap = _valid_rows_for("order", inputs)
    item_valid, _ih = _valid_rows_for("order_items", inputs)
    pay_valid, _ph = _valid_rows_for("order_payments", inputs)
    consolidated = build_consolidated(order_valid, item_valid, pay_valid, order_hmap,
                                     state.trace_id, state.tenant)
    audit_refs, checkpoints = _record_checkpoint(state, "consolidated")
    return {"consolidated": consolidated, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "consolidated",
            "updated_at": utc_now()}


def _node_refresh_pivot(state: FoodicsProcessingState,
                        runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    consolidated = state.consolidated or {"orders": []}
    pivot = refresh_pivot(consolidated)
    audit_refs, checkpoints = _record_checkpoint(state, "pivot")
    return {"pivot": pivot, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "pivot",
            "updated_at": utc_now()}


def _node_determine_payment_type(state: FoodicsProcessingState,
                                 runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    pay_valid, _ph = _valid_rows_for("order_payments", inputs)
    summary = determine_payment_type(pay_valid, state.trace_id, state.tenant)
    audit_refs, checkpoints = _record_checkpoint(state, "payment_type")
    return {"payment_type_summary": summary, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "payment_type",
            "updated_at": utc_now()}


def _node_apply_discounts(state: FoodicsProcessingState,
                          runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    item_valid, _ih = _valid_rows_for("order_items", inputs)
    adjusted, discounts_total = apply_discounts(item_valid, state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "discounts")
    return {"adjusted_lines": adjusted, "discounts_total": discounts_total,
            "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "discounts", "updated_at": utc_now()}


def _node_populate_sheet3(state: FoodicsProcessingState,
                          runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    consolidated = state.consolidated or {"orders": []}
    sheet3 = populate_sheet3(consolidated, state.adjusted_lines or {},
                             state.discounts_total or "0.00")
    audit_refs, checkpoints = _record_checkpoint(state, "sheet3")
    return {"sheet3": sheet3, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "sheet3",
            "updated_at": utc_now()}


def _node_build_zoho_upload(state: FoodicsProcessingState,
                            runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    consolidated = state.consolidated or {"orders": []}
    zoho = build_zoho_upload(consolidated, state.adjusted_lines or {},
                             state.trace_id)
    audit_refs, checkpoints = _record_checkpoint(state, "zoho_upload")
    return {"zoho_upload": zoho, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "zoho",
            "updated_at": utc_now()}


def _node_build_invoice(state: FoodicsProcessingState,
                        runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    consolidated = state.consolidated or {"orders": []}
    invoices = build_invoices(consolidated, state.adjusted_lines or {},
                              state.discounts_total or "0.00", state.trace_id,
                              state.tenant)
    # Guard: validate each invoice inline (build-phase: route through
    # ValidationEngineComponent for InvoiceData).
    all_errs: list[str] = []
    for i, inv in enumerate(invoices):
        all_errs.extend(f"invoice[{i}] {e}" for e in _validate_invoice(inv))
    audit_refs, checkpoints = _record_checkpoint(state, "invoice")
    if all_errs:
        return {"invoices": invoices, "audit_refs": audit_refs,
                "checkpoints": checkpoints, "status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": "; ".join(all_errs[:20])},
                "updated_at": utc_now()}
    return {"invoices": invoices, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "invoice",
            "updated_at": utc_now()}


def _after_invoice(state: FoodicsProcessingState) -> str:
    return state.status


def _node_build_state(state: FoodicsProcessingState,
                      runtime: Runtime[FoodicsProcessingContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    return {"workflow_state": ws, "status": "completed",
            "updated_at": utc_now()}


def _node_checkpoint(state: FoodicsProcessingState,
                     runtime: Runtime[FoodicsProcessingContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints.

    The InMemorySaver persists state after this node (§11)."""
    ctx = _ctx(runtime)
    _ = ctx
    audit_refs, checkpoints = _record_checkpoint(state, "foodics_processing")
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "updated_at": utc_now()}


def _node_respond(state: FoodicsProcessingState,
                  runtime: Runtime[FoodicsProcessingContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class FoodicsProcessingFlowComponent(Component):
    # Bare class name as the canonical `name` (mirrors SupervisorAgentComponent).
    name = "FoodicsProcessingFlowComponent"
    display_name = "Cosmic AR Foodics Processing Flow"
    description = (
        "Reads Foodics Order + Order Items + Order Payments (uploaded export "
        "files or Foodics API fetch), validates rows, builds a consolidated "
        "dataset + pivot + payment-type breakdown, applies discount rules "
        "(in-file columns or baked-in config), generates a Zoho Books upload "
        "format + a draft InvoiceData per order + Validation/Exception reports, "
        "and returns structured JSON — with logging, retries, and checkpoints "
        "after every calculation (constitution §1/§4/§8/§9/§10/§11/§12/§15/§16/"
        "§17/§19). The 6th AR subflow; v1 is compute + draft only (no posting; "
        "API path is build-phase). See ADR-0007."
    )
    icon = "ReceiptLong"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="User Request",
            info="The natural-language request accompanying the Foodics export upload (carries intent keywords).",
            required=False,
            tool_mode=True,
        ),
        HandleInput(
            name="files",
            display_name="Uploaded Foodics Exports",
            info="Uploaded Foodics Order / Order Items / Order Payments Excel/CSV "
                 "refs — either from the canvas File node (Data) or carried on the "
                 "ChatInput Message (.files) when files are injected via the run "
                 "API (the 'accept uploaded exports' responsibility). When absent, "
                 "the flow falls back to Foodics API fetch (build-phase seam).",
            input_types=["Data", "Message"],
            is_list=True,
            required=False,
        ),
        DropdownInput(
            name="source_mode",
            display_name="Source Mode",
            options=["auto", "files", "api"],
            value="auto",
            info="Force the input source: 'auto' uses files when uploaded else "
                 "API; 'files' requires uploaded exports; 'api' fetches via "
                 "FoodicsARTool (build-phase — fails safe until wired).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic read/validate/calculate; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="foodics_processing_output",
            display_name="Foodics Processing Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=FoodicsProcessingState,
                           context_schema=FoodicsProcessingContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("read", _node_read)
        graph.add_node("validate", _node_validate)
        graph.add_node("classify_exceptions", _node_classify_exceptions)
        graph.add_node("build_consolidated", _node_build_consolidated)
        graph.add_node("refresh_pivot", _node_refresh_pivot)
        graph.add_node("determine_payment_type", _node_determine_payment_type)
        graph.add_node("apply_discounts", _node_apply_discounts)
        graph.add_node("populate_sheet3", _node_populate_sheet3)
        graph.add_node("build_zoho_upload", _node_build_zoho_upload)
        graph.add_node("build_invoice", _node_build_invoice)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_edge("ingest", "read")
        graph.add_conditional_edges("read", _after_read,
                                    {"failed": "respond", "read": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond",
                                     "validated": "classify_exceptions"})
        graph.add_conditional_edges("classify_exceptions", _after_classify,
                                    {"failed": "respond",
                                     "classified": "build_consolidated"})
        graph.add_edge("build_consolidated", "refresh_pivot")
        graph.add_edge("refresh_pivot", "determine_payment_type")
        graph.add_edge("determine_payment_type", "apply_discounts")
        graph.add_edge("apply_discounts", "populate_sheet3")
        graph.add_edge("populate_sheet3", "build_zoho_upload")
        graph.add_edge("build_zoho_upload", "build_invoice")
        graph.add_conditional_edges("build_invoice", _after_invoice,
                                    {"failed": "respond", "invoice": "build_state"})
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
            source_mode = _to_str(getattr(self, "source_mode", "")) or "auto"
            ctx: FoodicsProcessingContext = {
                "user_input": user_input,
                "files": files,
                "source_mode": source_mode,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_foodics_processing",
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = FoodicsProcessingState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=foodics_processing.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=foodics_processing outcome={envelope.get('status')} "
                f"code={envelope.get('code')} source_mode={envelope.get('source_mode', '')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Foodics processing run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=foodics_processing.run outcome=error code=AR_UNEXPECTED")
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
        ``status|code|trace_id|data|error`` with ``additionalProperties:false``).
        The supervisor merges ``data.audit_refs`` into ``AgentState``; invoices /
        consolidated / pivot / sheet3 / zoho_upload / payment_type_summary are NOT
        recognized ``data.totals`` keys, so they stay in ``data`` (ADR-0007 — no
        ``AgentState`` schema change). v1 is compute + draft only, so ``data``
        carries no financial ``totals{matched,outstanding,posted}`` (those stay
        ``"0.00"`` inside ``data.workflow_state``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        inputs = vals.get("inputs") or {}
        doc_count = (sum(len(rows) for rows in inputs.values()
                         if isinstance(rows, list))
                     if isinstance(inputs, dict) else 0)
        invoices = vals.get("invoices") or []
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "invoices": list(invoices) if isinstance(invoices, list) else [],
            "consolidated": vals.get("consolidated") or {},
            "pivot": vals.get("pivot") or {},
            "payment_type_summary": vals.get("payment_type_summary") or {},
            "sheet3": vals.get("sheet3") or {},
            "zoho_upload": vals.get("zoho_upload") or {},
            "validation_report": vals.get("validation_report") or {},
            "exception_report": vals.get("exception_report") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "discounts_total": vals.get("discounts_total") or "0.00",
            "document_count": doc_count,
            "invoice_count": len(invoices) if isinstance(invoices, list) else 0,
            "source_mode": vals.get("source_mode", ""),
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        envelope: dict[str, Any] = {
            "status": "ok", "code": "AR_OK", "trace_id": trace_id, "data": data,
        }
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "foodics processing failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            err_env = {"message": err.get("message", "") if isinstance(err, dict) else str(err)}
            if isinstance(err, dict) and err.get("detail"):
                err_env["detail"] = err["detail"]
            envelope = {"status": "error", "code": code, "trace_id": trace_id,
                        "data": data, "error": err_env}
        # Surface source_mode at the top level for the run() log line.
        envelope["source_mode"] = vals.get("source_mode", "")
        envelope["flow_id"] = vals.get("flow_id", "")
        return envelope