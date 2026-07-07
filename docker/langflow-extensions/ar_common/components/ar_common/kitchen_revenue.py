"""Cosmic AR Agent — Cosmic Kitchen Revenue Flow component (constitution §8, architecture §4 row 5).

The Cosmic Kitchen Revenue Flow is the 5th AR subflow. Cosmic Kitchen operates
inside a Marriott hotel and produces four daily Excel/CSV sheets — **Menu Sales
Analysis**, **Daily Sales**, **Detailed Check Payment**, and **Marriott Backup**
— that together describe a period's revenue (split by meal period: Breakfast,
Half Board, …), the payments collected against it, the kitchen's expenses, and
the resulting **Net Receivable** / **Net Payable** positions. This flow reads the
four sheets, classifies each by role (filename keyword, header fallback),
validates their rows, calculates **Revenue** (Breakfast/Half Board as
``RevenueData.by_segment``), **Collections** (``CollectionData``), **Expenses**
(a reported total — not an AP posting), and the **Net Receivable** /
**Net Payable** positions (a ``CalculationResult`` of ``calculation_type
"reconcile"``), generates a **Revenue JSON**, a **Validation Report**, and an
**Exception Report**, and returns structured JSON — with logging (§12),
retries (§10), and **checkpoints after every calculation** (§11 — the user's
explicit stricter requirement). It is the **single stateful orchestrator** for
kitchen revenue, mirroring the supervisor, the File Intake Flow, and the
Intercompany Sales Flow.

v1 is **read-only compute + report**: it produces the figures for review; it
does **not** post anything, so no money moves and no ledger entry posts this
turn (§1 north star preserved, like the other read-only report flows). The flow is
registered at tier ``read-only`` — there is no §19 gate, no idempotency key, no
``pending_approval``. ``Net Receivable`` / ``Net Payable`` are **reported**
figures, not ledger mutations. "Expenses" is a reported total from the Marriott
Backup sheet, **not** an ``ExpenseData`` (AR-adjustments-only, requires
``approval_ref``+``idempotency_key``) and **not** an AP posting (§20 seed-only).
See ADR-0006.

Responsibilities → LangGraph nodes:

  ingest → read (§10 retry, multi-file) → validate → classify_exceptions →
  calc_revenue → calc_collections → calc_expenses → calc_nets →
  build_state → checkpoint → respond

  - ingest             : bind ``trace_id``/``flow_id``/``tenant`` + timestamps;
                          carry uploaded-file refs in **context** (not state — §8).
  - read               : expand + classify each uploaded sheet by role (menu_sales,
                          daily_sales, check_payment, marriott_backup) by filename
                          keyword (header fallback). Instantiate the matching
                          cosmic_common reader per file inside the §10 retry loop
                          (mirrors the File Intake Flow's multi-file read).
                          Unknown type/no readable file → ``AR_UNCERTAIN`` (§4).
                          Zero recognized roles → ``AR_UNCERTAIN``. Read failure
                          on any uploaded file → ``AR_VALIDATION``.              §10/§9
  - validate           : inline hand-rolled per-role validator with role-specific
                          required columns. A **required column entirely missing
                          for a present role** is a hard ``AR_VALIDATION``. Else
                          the full ``ValidationResult`` is built.                §9
  - classify_exceptions: split rows into valid vs exception and build the Exception
                          Report = a ``ValidationResult`` scoped to failures
                          (``rule_id`` per exception). A **missing role** is a
                          validation warning (not a hard fail); that calc emits
                          ``0.00``. All-rows-fail → ``AR_VALIDATION``.            §4
  - calc_revenue       : build ``RevenueData`` from the sales roles. Menu Sales
                          Analysis is authoritative (line items with the segment
                          column); Daily Sales is a **cross-check** (a material
                          divergence is an Exception Report warning, not a hard
                          fail) — avoids silent double-counting. ``by_segment``
                          groups by the meal-period column (Breakfast, Half Board).
                          **Records a checkpoint** (audit ref).                  §8
  - calc_collections   : build ``CollectionData`` from check_payment rows. v1 has
                          no invoice list to match against, so every payment is
                          ``match_status="unmatched"``. **Records a checkpoint.** §15
  - calc_expenses      : build a **reported** expense total + ``by_category`` from
                          marriott_backup rows (signed 2dp). NOT an ``ExpenseData``
                          and NOT an AP posting. **Records a checkpoint.**          §20
  - calc_nets          : build a ``CalculationResult`` (``calculation_type
                          "reconcile"``) carrying ``total_revenue``, ``total_collections``,
                          ``total_expenses``, ``net_receivable`` (= revenue −
                          collections), ``net_payable`` (= total expenses) +
                          ``line_items``. **Records a checkpoint.**              §15
  - build_state        : build a ``WorkflowState`` snapshot (status="completed",
                          totals ``"0.00"`` — no money moved). Immutable (§8).
  - checkpoint         : record the final aggregate audit id; reflect ``audit_refs``
                          + ``checkpoints`` into the ``WorkflowState`` snapshot.
                          ``InMemorySaver`` persists state.                         §11
  - respond            : build the §14 envelope carrying ``data.revenue``,
                          ``data.collections``, ``data.nets``, ``data.validation_report``,
                          ``data.exception_report``, ``data.workflow_state``,
                          ``data.audit_refs``, ``data.checkpoints``.             §14

**Checkpoints after every calculation** (the user's explicit stricter
requirement, beyond §11's "after each reconciled batch"): each calc node
records a labeled ``_audit_ref`` into ``audit_refs`` and a ``checkpoints``
map (``{revenue, collections, expenses, nets}``), persisted by ``InMemorySaver``
at each super-step. This is a new, stricter pattern than the File Intake /
Intercompany single-end-checkpoint — recorded as an ADR-0006 decision.

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id``. This
is the §11 **fallback**: non-durable (lost on worker recreate). Durable Postgres
checkpointing remains a documented build-phase step (see ADR-0006 and the
constitution §11 caveat — Langfuse tracing is currently off, so the checkpoint
is the source of truth for resume).

The supervisor's ``_node_invoke`` merges only ``data.totals{matched,outstanding,
posted}`` and ``data.audit_refs`` into ``AgentState``. Revenue / collections /
nets are NOT recognized totals keys → they stay in the envelope ``data`` (no
``AgentState`` schema change — same as ADR-0005 §7).

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. Customer refs are ids — no PII (§16). No
credentials are needed in v1 (deterministic, in-file).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
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

# §10 retry policy (mirrors the supervisor / File Intake / Intercompany flows).
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0
BACKOFF_CAP_S: float = 30.0

# Kitchen revenue v1 policy.
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors invoice_builder/calc_engine)

# The four input roles (the kitchen's daily sheets).
ROLES: tuple[str, ...] = ("menu_sales", "daily_sales", "check_payment", "marriott_backup")

# Column-name aliases (lowercased keys). The reader emits dict rows keyed by the
# sheet header; lookups are case-insensitive and tolerant of synonyms.
AMOUNT_KEYS = ("amount", "total", "sales", "sales_amount", "net_sales", "revenue",
               "gross_sales", "sales_total", "amount_due", "value", "total_sales")
QTY_KEYS = ("qty", "quantity", "count", "units", "covers")
RATE_KEYS = ("rate", "unit_price", "price", "avg_price", "avg_check", "rate_per_unit")
SEGMENT_KEYS = ("meal_period", "service_type", "package", "meal", "period",
                "service", "meal_type", "session")
DATE_KEYS = ("posted_at", "date", "sales_date", "txn_date", "transaction_date",
             "business_date", "order_date")
CURRENCY_KEYS = ("currency", "curr", "ccy")
CUSTOMER_KEYS = ("customer_ref", "customer_id", "customer", "cust_id", "account_id",
                 "guest", "client")
PAYMENT_ID_KEYS = ("payment_id", "payment", "check_no", "check_number", "cheque_no",
                   "reference", "ref", "receipt_no", "txn_id")
METHOD_KEYS = ("method", "payment_method", "pay_method", "mode", "payment_mode")
CATEGORY_KEYS = ("category", "expense_category", "type", "expense_type", "cost_type",
                 "gl_account", "head", "department")

# Required columns per role (a present role missing any of these is a hard
# AR_VALIDATION — the flow cannot produce that role's figure).
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "menu_sales": ("segment", "date"),
    "daily_sales": ("date",),
    "check_payment": ("payment_id", "amount", "method", "date"),
    "marriott_backup": ("amount", "date"),
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
    "other": "other",
}


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class KitchenRevenueContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[KitchenRevenueContext]``.

    Durable, resumable state lives in ``KitchenRevenueState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    files: list[Any]  # uploaded kitchen-sheet refs from the canvas File node
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KitchenRevenueState:
    """The Cosmic Kitchen Revenue Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    Derived working data (rows, figures, reports) is transient, not raw input.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|read|validated|classified|revenue|collections|expenses|nets|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    # Derived working data.
    file_plan: list = field(default_factory=list)  # [{name, path, kind}]
    inputs: dict = field(default_factory=dict)  # {role: [rows]} classified per role
    revenue: Optional[dict] = None  # RevenueData
    collections: Optional[dict] = None  # CollectionData
    expenses: Optional[dict] = None  # reported expense working data (NOT a contract)
    nets: Optional[dict] = None  # CalculationResult (calculation_type="reconcile")
    validation_report: Optional[dict] = None  # full ValidationResult
    exception_report: Optional[dict] = None  # ValidationResult scoped to failures
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<calc_label>: audit_ref} (§11)


def _state_to_dict(state: Any) -> dict:
    """Coerce a ``KitchenRevenueState`` (or dict) snapshot to a plain dict.

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
    """Identify the sheet file type by extension. ``excel``/``csv``/``unknown``.

    Kitchen revenue consumes Excel or CSV sheets. Unknown extensions fail safe
    (§4) at the ``read`` node → skipped (and zero recognized roles → AR_UNCERTAIN).
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
        ("amount", AMOUNT_KEYS),
        ("qty", QTY_KEYS),
        ("rate", RATE_KEYS),
        ("segment", SEGMENT_KEYS),
        ("date", DATE_KEYS),
        ("currency", CURRENCY_KEYS),
        ("customer", CUSTOMER_KEYS),
        ("payment_id", PAYMENT_ID_KEYS),
        ("method", METHOD_KEYS),
        ("category", CATEGORY_KEYS),
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


def _norm_token(value: str) -> str:
    """Normalise a free-text token (segment / category) to a stable slug.

    ``"Half Board"`` → ``"half_board"``, ``"Breakfast"`` → ``"breakfast"``.
    Deterministic (§4.3): no case-folding ambiguity in the grouping keys.
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


def _period_from(dates: list[str]) -> dict[str, str]:
    """Build a ``RevenueData``/``CollectionData`` ``period`` from a date list."""
    if dates:
        return {"start": min(dates), "end": max(dates)}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    return {"start": today, "end": today}


def _map_method(value: str) -> str:
    """Map a payment-method string to the ``CollectionData.method`` enum.

    Returns ``""`` when empty or unrecognised (the validator flags these); a known
    synonym → its enum value (check/cheque → bank_transfer).
    """
    v = (value or "").lower().strip()
    return METHOD_SYNONYMS.get(v, "")


def _row_amount(row: dict) -> Decimal:
    """Resolve a sales row's amount: explicit amount column > qty × rate.

    Returns ``Decimal("0")`` when unresolvable (invalid rows are excluded upstream
    by the per-role validator, so this is a defensive default).
    """
    amt_s = _row_key(row, AMOUNT_KEYS)
    if amt_s:
        d = _to_decimal(amt_s)
        return d if d is not None and d > 0 else Decimal("0")
    qty = _to_decimal(_row_key(row, QTY_KEYS)) or Decimal("0")
    rate = _to_decimal(_row_key(row, RATE_KEYS)) or Decimal("0")
    return (qty * rate).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
#  Role classification + per-role validation (inline, hand-rolled — §15 reuse
#  note in ADR-0006; ValidationEngineComponent only implements DocumentManifest).
# --------------------------------------------------------------------------- #


def _classify_input(name: str, rows: list[dict]) -> str:
    """Classify an uploaded sheet into one of the four kitchen roles.

    Primary signal: filename keyword (``menu``/``daily``/``check``|``payment``/
    ``marriott``|``backup``). Fallback: a header-content sniff (payment_id/method
    → check_payment; meal_period/service_type/package → menu_sales; category/
    expense → marriott_backup). Returns ``"unknown"`` when unrecognised.
    """
    n = (name or "").lower()
    if "menu" in n:
        return "menu_sales"
    if "daily" in n:
        return "daily_sales"
    if "check" in n or "payment" in n:
        return "check_payment"
    if "marriott" in n or "backup" in n:
        return "marriott_backup"
    # Header-content fallback.
    keys: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            keys.update(k.lower().replace(" ", "_") for k in r.keys())
    if keys & {"payment_id", "check_no", "check_number", "cheque_no", "payment_method"}:
        return "check_payment"
    if keys & {"meal_period", "service_type", "package", "meal_type"}:
        return "menu_sales"
    if keys & {"expense_category", "expense_type", "cost_type", "category"}:
        return "marriott_backup"
    if keys & {"daily_sales", "daily_total", "day_total"}:
        return "daily_sales"
    return "unknown"


def _validate_role_row(role: str, row: dict, hmap: dict[str, str],
                       index: int) -> list[dict]:
    """Validate one row for ``role`` → list of issue dicts (empty when valid)."""
    errs: list[dict] = []
    base = f"{role}[{index}]"

    if role == "menu_sales":
        seg = _row_key(row, SEGMENT_KEYS)
        if not seg:
            errs.append(_issue(f"{base}.segment", "AR_VALIDATION_REQUIRED",
                               "segment (meal period) is required",
                               "kr.segment_required"))
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.date", "AR_VALIDATION_FORMAT",
                               "date must be an ISO date (YYYY-MM-DD)",
                               "kr.date_iso"))
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            qty = _to_decimal(_row_key(row, QTY_KEYS))
            rate = _to_decimal(_row_key(row, RATE_KEYS))
            if qty is None or rate is None or qty <= 0 or rate <= 0:
                errs.append(_issue(f"{base}.amount", "AR_VALIDATION_POSITIVE",
                                   "amount (or qty × rate) must be a positive number",
                                   "kr.amount_positive"))

    elif role == "daily_sales":
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.date", "AR_VALIDATION_FORMAT",
                               "date must be an ISO date (YYYY-MM-DD)",
                               "kr.date_iso"))
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            qty = _to_decimal(_row_key(row, QTY_KEYS))
            rate = _to_decimal(_row_key(row, RATE_KEYS))
            if qty is None or rate is None or qty <= 0 or rate <= 0:
                errs.append(_issue(f"{base}.amount", "AR_VALIDATION_POSITIVE",
                                   "amount (or qty × rate) must be a positive number",
                                   "kr.amount_positive"))

    elif role == "check_payment":
        if not _row_key(row, PAYMENT_ID_KEYS):
            errs.append(_issue(f"{base}.payment_id", "AR_VALIDATION_REQUIRED",
                               "payment_id is required", "kr.payment_id_required"))
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            errs.append(_issue(f"{base}.amount", "AR_VALIDATION_POSITIVE",
                               "amount must be a positive number",
                               "kr.amount_positive"))
        method = _row_key(row, METHOD_KEYS)
        if not method:
            errs.append(_issue(f"{base}.method", "AR_VALIDATION_REQUIRED",
                               "method is required", "kr.method_required"))
        elif not _map_method(method):
            errs.append(_issue(f"{base}.method", "AR_VALIDATION_ENUM",
                               f"method '{method}' is not a recognised payment method",
                               "kr.method_enum"))
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.posted_at", "AR_VALIDATION_FORMAT",
                               "posted_at must be an ISO date (YYYY-MM-DD)",
                               "kr.date_iso"))

    elif role == "marriott_backup":
        if _to_decimal(_row_key(row, AMOUNT_KEYS)) is None:
            errs.append(_issue(f"{base}.amount", "AR_VALIDATION_REQUIRED",
                               "amount is required (must be a number)",
                               "kr.amount_required"))
        if not _parse_date(_row_key(row, DATE_KEYS)):
            errs.append(_issue(f"{base}.posted_at", "AR_VALIDATION_FORMAT",
                               "posted_at must be an ISO date (YYYY-MM-DD)",
                               "kr.date_iso"))

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
                                   f"kr.{role}_required_columns")])
        else:
            per_row.append(_validate_role_row(role, row, hmap, i))
    return per_row, hmap, missing


def _valid_rows_for(role: str, inputs: dict) -> tuple[list[dict], dict[str, str]]:
    """Return ``(valid_rows, header_map)`` for ``role`` from the classified inputs.

    A row is valid when it has no validation issues. A role with a missing
    required column yields zero valid rows (every row is flagged). Recomputed per
    node (mirrors the Intercompany Sales Flow's per-node recompute — deterministic,
    no cross-node state needed for the working data).
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
        "contract_name": "KitchenRevenueInputs",
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
            # Missing-role warning (not a hard fail — that calc emits 0.00).
            errors.append(_issue(role, "AR_VALIDATION_REQUIRED",
                                 f"{role} sheet not supplied — {role} figures reported as 0.00",
                                 f"kr.{role}_missing"))
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


def calculate_revenue(menu_rows: list[dict], daily_rows: list[dict],
                     menu_hmap: dict[str, str], daily_hmap: dict[str, str],
                     trace_id: str, tenant: str,
                     exception_report: Optional[dict]) -> tuple[dict, dict]:
    """Build ``RevenueData`` from the sales roles + cross-check Daily Sales.

    **Source priority:** Menu Sales Analysis (line items with the segment column)
    is authoritative; Daily Sales is a **cross-check** (a material divergence is
    appended to the Exception Report as a warning, not a hard fail) — this avoids
    silent double-counting. If Menu Sales is absent, fall back to Daily Sales
    (segment = ``"daily_summary"`` unless the row carries its own segment).

    ``amount`` per row → 2dp; ``total`` = Σ; ``by_segment`` groups by the
    meal-period column (Breakfast, Half Board, …); ``by_customer_ref`` if a
    customer column exists; ``period`` = min/max date. ``by_segment`` always has
    at least one entry (RevenueData ``minItems: 1``).
    """
    exc_report = exception_report or _build_validation_report([], trace_id)
    use_menu = bool(menu_rows)
    rows = menu_rows if use_menu else daily_rows
    hmap = menu_hmap if use_menu else daily_hmap
    currency = _currency_from(rows, hmap)

    by_segment: dict[str, dict] = {}
    by_customer: dict[str, dict] = {}
    amounts: list[str] = []
    dates: list[str] = []
    cust_present = "customer" in hmap if hmap else False

    for row in rows:
        amt = _row_amount(row)
        amt_s = _to_2dp(amt)
        amounts.append(amt_s)
        seg_raw = _row_key(row, SEGMENT_KEYS)
        if seg_raw:
            seg = _norm_token(seg_raw)
        elif not use_menu:
            seg = "daily_summary"  # Daily-Sales fallback without a segment column
        else:
            seg = "unspecified"
        s = by_segment.setdefault(seg, {"amount": Decimal("0.00"), "count": 0})
        s["amount"] += amt
        s["count"] += 1
        d = _parse_date(_row_key(row, DATE_KEYS))
        if d:
            dates.append(d)
        if cust_present:
            c = _row_key(row, CUSTOMER_KEYS) or "CUST-UNKNOWN"
            cc = by_customer.setdefault(c, {"amount": Decimal("0.00"), "count": 0})
            cc["amount"] += amt
            cc["count"] += 1

    total = _sum_2dp(amounts)
    period = _period_from(dates)
    by_segment_out = ([{"segment": k, "amount": _to_2dp(v["amount"]),
                        "count": v["count"]} for k, v in by_segment.items()]
                      or [{"segment": "unspecified", "amount": "0.00", "count": 0}])
    by_customer_out = [{"customer_ref": k, "amount": _to_2dp(v["amount"]),
                        "count": v["count"]} for k, v in by_customer.items()]
    revenue = {
        "trace_id": trace_id,
        "tenant": tenant,
        "period": period,
        "total": total,
        "currency": currency,
        "by_segment": by_segment_out,
        "by_invoice": [],
        "by_customer_ref": by_customer_out,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }

    # Cross-check: Menu Sales (authoritative) vs Daily Sales (summary).
    if use_menu and daily_rows:
        daily_total = _sum_2dp([_to_2dp(_row_amount(r)) for r in daily_rows])
        diff = (Decimal(total) - Decimal(daily_total)).quantize(Decimal("0.01"))
        if abs(diff) > Decimal("0.01"):
            exc_report = _warn(exc_report, "revenue.cross_check",
                               "AR_VALIDATION_RECONCILE",
                               f"Menu Sales total {total} diverges from Daily Sales "
                               f"total {daily_total} by {diff}",
                               "kr.revenue_cross_check")
    return revenue, exc_report


def calculate_collections(check_rows: list[dict], hmap: dict[str, str],
                          trace_id: str, tenant: str) -> dict:
    """Build ``CollectionData`` from check_payment rows.

    v1 has no invoice list to match against, so every payment is
    ``match_status="unmatched"`` (``matched_amount="0.00"``,
    ``unmatched_amount=total_collected``). ``by_method`` groups by the mapped
    method enum. ``posted_at`` is a full ISO-8601 UTC datetime (date-only input →
    midnight UTC).
    """
    currency = _currency_from(check_rows, hmap)
    payments: list[dict] = []
    amounts: list[str] = []
    by_method: dict[str, dict] = {}
    dates: list[str] = []
    for i, row in enumerate(check_rows):
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None or amt <= 0:
            continue  # defensive — valid rows are already positive
        amt_s = _to_2dp(amt)
        pid = _row_key(row, PAYMENT_ID_KEYS) or f"P{i + 1:04d}"
        cust = _row_key(row, CUSTOMER_KEYS) or "CUST-UNKNOWN"
        method = _map_method(_row_key(row, METHOD_KEYS)) or "other"
        posted = _to_iso_datetime(_row_key(row, DATE_KEYS))
        payments.append({
            "payment_id": pid,
            "customer_ref": cust,
            "amount": amt_s,
            "method": method,
            "posted_at": posted,
            "match_status": "unmatched",
        })
        amounts.append(amt_s)
        m = by_method.setdefault(method, {"amount": Decimal("0.00"), "count": 0})
        m["amount"] += amt
        m["count"] += 1
        d = _parse_date(_row_key(row, DATE_KEYS))
        if d:
            dates.append(d)
    total = _sum_2dp(amounts)
    period = _period_from(dates)
    by_method_out = [{"method": k, "amount": _to_2dp(v["amount"]),
                      "count": v["count"]} for k, v in by_method.items()]
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "period": period,
        "total_collected": total,
        "currency": currency,
        "payments": payments,
        "matched_amount": "0.00",
        "unmatched_amount": total,
        "by_method": by_method_out,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }


def calculate_expenses(marriott_rows: list[dict], hmap: dict[str, str],
                       trace_id: str, tenant: str) -> dict:
    """Build the **reported** expense total + ``by_category`` from marriott_backup rows.

    This is a **reported** figure, NOT an ``ExpenseData`` (AR-adjustments-only) and
    NOT an AP posting (§20 seed-only). Amounts are signed 2dp (a refund/credit
    row may be negative). ``by_category`` carries the breakdown for the nets
    ``CalculationResult.line_items``.
    """
    currency = _currency_from(marriott_rows, hmap)
    amounts: list[str] = []
    by_category: dict[str, dict] = {}
    dates: list[str] = []
    for row in marriott_rows:
        amt = _to_decimal(_row_key(row, AMOUNT_KEYS))
        if amt is None:
            continue  # defensive
        amt_s = _to_signed_2dp(amt)
        amounts.append(amt_s)
        cat = _norm_token(_row_key(row, CATEGORY_KEYS)) or "uncategorized"
        c = by_category.setdefault(cat, {"amount": Decimal("0.00"), "count": 0})
        c["amount"] += amt
        c["count"] += 1
        d = _parse_date(_row_key(row, DATE_KEYS))
        if d:
            dates.append(d)
    total = _sum_2dp(amounts)
    period = _period_from(dates)
    by_category_out = [{"category": cat, "amount": _to_signed_2dp(v["amount"]),
                        "count": v["count"]} for cat, v in by_category.items()]
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "period": period,
        "total": total,
        "by_category": by_category_out,
        "currency": currency,
    }


def calculate_nets(revenue: Optional[dict], collections: Optional[dict],
                   expenses: Optional[dict], trace_id: str, tenant: str) -> dict:
    """Build the ``CalculationResult`` (``calculation_type="reconcile"``) for nets.

    ``totals`` carries ``total_revenue``, ``total_collections``,
    ``total_expenses``, ``net_receivable`` (= revenue − collections), and
    ``net_payable`` (= total expenses) — all signed 2dp strings (signed pattern
    ``^-?\\d+\\.\\d{2}$``). ``line_items`` carries one entry per top-level figure
    plus one per expense category (each with ``source_refs``).
    """
    rev_total = (revenue or {}).get("total", "0.00") or "0.00"
    col_total = (collections or {}).get("total_collected", "0.00") or "0.00"
    exp_total = (expenses or {}).get("total", "0.00") or "0.00"
    rev_d = Decimal(rev_total)
    col_d = Decimal(col_total)
    exp_d = Decimal(exp_total)
    net_rec = (rev_d - col_d).quantize(Decimal("0.01"))
    net_pay = exp_d.quantize(Decimal("0.01"))
    currency = (revenue or collections or {}).get("currency", DEFAULT_CURRENCY)
    totals = {
        "total_revenue": _to_signed_2dp(rev_d),
        "total_collections": _to_signed_2dp(col_d),
        "total_expenses": _to_signed_2dp(exp_d),
        "net_receivable": _to_signed_2dp(net_rec),
        "net_payable": _to_signed_2dp(net_pay),
    }
    line_items: list[dict] = [
        {"label": "Revenue", "amount": totals["total_revenue"],
         "source_refs": ["menu_sales", "daily_sales"]},
        {"label": "Collections", "amount": totals["total_collections"],
         "source_refs": ["check_payment"]},
        {"label": "Expenses", "amount": totals["total_expenses"],
         "source_refs": ["marriott_backup"]},
        {"label": "Net Receivable", "amount": totals["net_receivable"],
         "source_refs": ["menu_sales", "daily_sales", "check_payment"]},
        {"label": "Net Payable", "amount": totals["net_payable"],
         "source_refs": ["marriott_backup"]},
    ]
    for cat in (expenses or {}).get("by_category", []):
        line_items.append({"label": f"Expense: {cat['category']}",
                           "amount": cat["amount"],
                           "source_refs": ["marriott_backup"]})
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "calculation_type": "reconcile",
        "totals": totals,
        "line_items": line_items,
        "currency": currency,
        "inputs_ref": trace_id,
        "computed_at": utc_now(),
        "contract_version": CONTRACT_VERSION,
    }


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         audit_refs: list, created_at: str, updated_at: str) -> dict:
    """Build a ``WorkflowState`` snapshot (pure). v1: read-only, no money moved.

    Financial totals are ``"0.00"`` (no posting), ``pending_approvals=[]``,
    ``idempotency_keys={}`` (no POST). Status ``completed`` (the report is built).
    """
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": "ar_kitchen_revenue",
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:ar_kitchen_revenue:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-calculation audit record id (§11/§13).

    One labeled ref per calculation (revenue/collections/expenses/nets) + a final
    aggregate — the "checkpoints after every calculation" artifact.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"kitchen-revenue-audit:{trace_id}:{label}"))


# --------------------------------------------------------------------------- #
#  §10 retry classification (mirrors supervisor / File Intake / Intercompany).
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
    transient retries → ``AR_UPSTREAM``. Kitchen revenue v1 is read-only compute,
    so exhausted transient retries surface as ``error`` (not ``pending_approval``).
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


def _ctx(runtime: Runtime[KitchenRevenueContext]) -> KitchenRevenueContext:
    return runtime.context or {}


def _node_ingest(state: KitchenRevenueState,
                 runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    return {
        "trace_id": state.trace_id or mint_id(),
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_kitchen_revenue"),
        "tenant": state.tenant or ctx.get("tenant", "cosmic-vikings"),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_read(state: KitchenRevenueState,
               runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    files = _expand_files(ctx.get("files", []))
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
        # §4 fail-safe: no readable file → AR_UNCERTAIN.
        return {"file_plan": [], "inputs": {},
                "status": "failed",
                "error": {"code": "AR_UNCERTAIN", "message": "no readable files supplied"},
                "updated_at": utc_now()}
    # Multi-file read loop (mirrors the File Intake Flow): read each file, classify
    # its rows by role. A read failure on any uploaded file is hard (AR_VALIDATION).
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
        envelope = _read_with_retry(reader, entry["path"], entry["kind"], state.trace_id)
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
        return {"file_plan": plan, "inputs": inputs, "status": "failed",
                "error": {"code": first_code,
                          "message": "; ".join(m for _, m in read_failures)},
                "updated_at": utc_now()}
    if classified == 0:
        return {"file_plan": plan, "inputs": inputs, "status": "failed",
                "error": {"code": "AR_UNCERTAIN",
                          "message": "no recognized kitchen sheets"},
                "updated_at": utc_now()}
    return {"file_plan": plan, "inputs": inputs, "status": "read",
            "updated_at": utc_now()}


def _after_read(state: KitchenRevenueState) -> str:
    # Path-map keys are node statuses ("failed"/"read"); returning state.status
    # routes "failed"→respond and "read"→validate (ADR-0003 §9).
    return state.status


def _node_validate(state: KitchenRevenueState,
                   runtime: Runtime[KitchenRevenueContext]) -> dict:
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


def _after_validate(state: KitchenRevenueState) -> str:
    return state.status


def _node_classify_exceptions(state: KitchenRevenueState,
                              runtime: Runtime[KitchenRevenueContext]) -> dict:
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


def _after_classify(state: KitchenRevenueState) -> str:
    return state.status


def _record_checkpoint(state: KitchenRevenueState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry for a calc (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


def _node_calc_revenue(state: KitchenRevenueState,
                       runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    menu_valid, menu_hmap = _valid_rows_for("menu_sales", inputs)
    daily_valid, daily_hmap = _valid_rows_for("daily_sales", inputs)
    exc_report = state.exception_report or _build_validation_report([], state.trace_id)
    revenue, exc_report = calculate_revenue(menu_valid, daily_valid, menu_hmap,
                                            daily_hmap, state.trace_id, state.tenant,
                                            exc_report)
    audit_refs, checkpoints = _record_checkpoint(state, "revenue")
    return {"revenue": revenue, "exception_report": exc_report,
            "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "revenue", "updated_at": utc_now()}


def _node_calc_collections(state: KitchenRevenueState,
                           runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    check_valid, check_hmap = _valid_rows_for("check_payment", inputs)
    collections = calculate_collections(check_valid, check_hmap,
                                        state.trace_id, state.tenant)
    audit_refs, checkpoints = _record_checkpoint(state, "collections")
    return {"collections": collections, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "collections",
            "updated_at": utc_now()}


def _node_calc_expenses(state: KitchenRevenueState,
                        runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    inputs = state.inputs or {}
    marriott_valid, marriott_hmap = _valid_rows_for("marriott_backup", inputs)
    expenses = calculate_expenses(marriott_valid, marriott_hmap,
                                  state.trace_id, state.tenant)
    audit_refs, checkpoints = _record_checkpoint(state, "expenses")
    return {"expenses": expenses, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "expenses",
            "updated_at": utc_now()}


def _node_calc_nets(state: KitchenRevenueState,
                    runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    nets = calculate_nets(state.revenue, state.collections, state.expenses,
                          state.trace_id, state.tenant)
    audit_refs, checkpoints = _record_checkpoint(state, "nets")
    return {"nets": nets, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "nets",
            "updated_at": utc_now()}


def _node_build_state(state: KitchenRevenueState,
                      runtime: Runtime[KitchenRevenueContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    return {"workflow_state": ws, "status": "completed",
            "updated_at": utc_now()}


def _node_checkpoint(state: KitchenRevenueState,
                     runtime: Runtime[KitchenRevenueContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints.

    The InMemorySaver persists state after this node (§11)."""
    ctx = _ctx(runtime)
    _ = ctx
    audit_refs, checkpoints = _record_checkpoint(state, "kitchen_revenue")
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "updated_at": utc_now()}


def _node_respond(state: KitchenRevenueState,
                  runtime: Runtime[KitchenRevenueContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class KitchenRevenueFlowComponent(Component):
    # Bare class name as the canonical `name` (mirrors SupervisorAgentComponent).
    name = "KitchenRevenueFlowComponent"
    display_name = "Cosmic AR Kitchen Revenue Flow"
    description = (
        "Reads the four Cosmic Kitchen sheets (Menu Sales Analysis, Daily Sales, "
        "Detailed Check Payment, Marriott Backup), validates rows, calculates "
        "Revenue (Breakfast/Half Board segments), Collections, Expenses, Net "
        "Receivable, and Net Payable, and generates a Revenue JSON + Validation "
        "Report + Exception Report — with logging, retries, and checkpoints after "
        "every calculation (constitution §1/§4/§8/§9/§10/§11/§12/§15/§16/§20). The "
        "5th AR subflow; v1 is read-only compute + report (no posting). See "
        "ADR-0006."
    )
    icon = "Calculator"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="User Request",
            info="The natural-language request accompanying the kitchen-sheet upload (carries intent keywords).",
            required=False,
            tool_mode=True,
        ),
        HandleInput(
            name="files",
            display_name="Uploaded Kitchen Sheets",
            info="Uploaded Menu Sales Analysis / Daily Sales / Detailed Check Payment / "
                 "Marriott Backup Excel/CSV refs — either from the canvas File node "
                 "(Data) or carried on the ChatInput Message (.files) when files are "
                 "injected via the run API (the 'accept uploaded sheets' responsibility).",
            input_types=["Data", "Message"],
            is_list=True,
            required=False,
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
            name="kitchen_revenue_output",
            display_name="Kitchen Revenue Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=KitchenRevenueState,
                           context_schema=KitchenRevenueContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("read", _node_read)
        graph.add_node("validate", _node_validate)
        graph.add_node("classify_exceptions", _node_classify_exceptions)
        graph.add_node("calc_revenue", _node_calc_revenue)
        graph.add_node("calc_collections", _node_calc_collections)
        graph.add_node("calc_expenses", _node_calc_expenses)
        graph.add_node("calc_nets", _node_calc_nets)
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
                                     "classified": "calc_revenue"})
        graph.add_edge("calc_revenue", "calc_collections")
        graph.add_edge("calc_collections", "calc_expenses")
        graph.add_edge("calc_expenses", "calc_nets")
        graph.add_edge("calc_nets", "build_state")
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
            ctx: KitchenRevenueContext = {
                "user_input": user_input,
                "files": files,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_kitchen_revenue",
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = KitchenRevenueState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=kitchen_revenue.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=kitchen_revenue outcome={envelope.get('status')} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Kitchen revenue run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=kitchen_revenue.run outcome=error code=AR_UNEXPECTED")
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
        The supervisor merges ``data.audit_refs`` into ``AgentState``; revenue /
        collections / nets are NOT recognized ``data.totals`` keys, so they stay in
        ``data`` (ADR-0006 — no ``AgentState`` schema change). v1 is read-only compute
        + report, so ``data`` carries no financial ``totals{matched,outstanding,
        posted}`` (those stay ``"0.00"`` inside ``data.workflow_state``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        inputs = vals.get("inputs") or {}
        doc_count = (sum(len(rows) for rows in inputs.values()
                         if isinstance(rows, list))
                     if isinstance(inputs, dict) else 0)
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "revenue": vals.get("revenue") or {},
            "collections": vals.get("collections") or {},
            "nets": vals.get("nets") or {},
            "validation_report": vals.get("validation_report") or {},
            "exception_report": vals.get("exception_report") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "document_count": doc_count,
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "kitchen revenue failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err}
        return {"status": "ok", "code": "AR_OK", "trace_id": trace_id,
                "data": data}