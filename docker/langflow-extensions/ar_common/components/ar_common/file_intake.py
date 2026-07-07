"""Cosmic AR Agent — File Intake Flow component (constitution §8, architecture §4 row 3).

The File Intake Flow is the 3rd AR subflow. It accepts an uploaded
Excel/CSV/PDF, identifies its report type, extracts metadata, validates it,
builds a ``DocumentManifest``, updates workflow state, and returns structured
JSON — with logging (§12), retries (§10), and checkpoints (§11). It is the
**single stateful orchestrator** for file ingestion, mirroring the supervisor:
its responsibilities map to LangGraph nodes inside one ``lfx`` component.

Responsibilities → LangGraph nodes:

  ingest → detect_type → read (§10 retry) → extract_metadata → validate →
  build_manifest → checkpoint → respond

  - ingest          : bind ``trace_id``/``flow_id``/``tenant`` + timestamps;
                      carry uploaded-file refs in **context** (not state — §8).
  - detect_type     : dispatch by extension (.xlsx/.xls→excel, .csv→csv,
                      .pdf→pdf); unknown → ``AR_UNCERTAIN`` (§4 fail-safe).   §4
  - read            : instantiate the matching cosmic_common reader, call its
                      output method inside the §10 retry/backoff loop, parse its
                      §14 envelope. Read-only tier ⇒ exhausted → error (not
                      pending_approval).                                  §10/§9
  - extract_metadata: deterministic rules over rows/content → doc_type,
                      customer_ref (id-only, §16), amount (2dp), currency,
                      posted_at, source_ref. Classify via the reusable
                      DocumentClassifier; below ``MIN_CONFIDENCE`` →
                      ``AR_UNCERTAIN``.                                      §4/§15
  - validate        : per-document field validation via the reusable
                      ``validate_document`` (cosmic_common, §15). Invalid →
                      ``AR_VALIDATION`` with per-field errors.              §9
  - build_manifest  : assemble the ``DocumentManifest`` (manifest_id, documents,
                      totals{count,sum}, source_systems, period, generated_at,
                      contract_version). ``sum`` = Σ amounts to 2dp.       §8
  - checkpoint      : record the manifest id as the audit ref (the manifest is
                      the auditable artifact); InMemorySaver persists state. §11
  - respond         : build the §14 envelope carrying ``data.manifest`` +
                      ``data.audit_refs``.                                  §14

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id``. This
is the §11 **fallback**: non-durable (lost on worker recreate). Durable Postgres
checkpointing remains a documented build-phase step (see ADR-0004 and the
constitution §11 caveat — Langfuse tracing is currently off, so the checkpoint
is the source of truth for resume).

v1 notes (recorded in ADR-0004): detect_type/extract_metadata/validate are
deterministic (no LLM key required); ``model_name`` is a documented hook. The
manifest lives in the envelope ``data.manifest`` (NOT added to ``AgentState`` —
no schema change; the supervisor ignores unknown data keys). The output method
**never raises** (§5/§9): it catches at the boundary and returns an
``AR_UNEXPECTED`` envelope.
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

# §4 fail-safe threshold for the deterministic classifier.
MIN_CONFIDENCE: float = 0.6

# §10 retry policy (mirrors the supervisor).
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0
BACKOFF_CAP_S: float = 30.0

CONTRACT_VERSION: str = "1.0.0"

# Field-name aliases for deterministic metadata extraction (lowercased keys).
AMOUNT_KEYS = ("amount", "total", "grand_total", "balance_due", "total_amount",
               "amount_due", "net_total", "value")
CURRENCY_KEYS = ("currency", "curr", "ccy")
DATE_KEYS = ("posted_at", "invoice_date", "date", "posted_date", "txn_date",
             "transaction_date", "receipt_date")
CUSTOMER_KEYS = ("customer_ref", "customer_id", "customer", "cust_id",
                 "customer_no", "account_id")
REF_KEYS = ("source_ref", "invoice_number", "invoice_no", "receipt_no",
            "receipt_id", "reference", "ref", "doc_no", "document_no", "id")
STATUS_KEYS = ("status", "state", "invoice_status", "payment_status")
SOURCE_KEYS = ("source", "system", "source_system")

RE_AMOUNT_STR = re.compile(r"^-?\d+\.\d{2}$")


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class FileIntakeContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[FileIntakeContext]``.

    Durable, resumable state lives in ``FileIntakeState`` (checkpointed). These
    are the transient inputs for one invocation; they are re-supplied on resume.
    """

    user_input: str
    files: list[Any]  # uploaded-file refs from the canvas File node
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FileIntakeState:
    """The File Intake Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    The manifest is the auditable artifact; working file-plan/raw-docs are
    transient derived data (not raw inputs).
    """

    trace_id: str
    flow_id: str
    tenant: str
    status: str = "created"  # created|typed|read|extracted|validated|built|completed|failed
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    # Derived working data (per-file plan + raw reader output).
    file_plan: list = field(default_factory=list)  # [{name, path, kind}]
    raw_docs: list = field(default_factory=list)  # [{name, kind, content}]
    # Extracted documents conforming to DocumentManifest.document.
    documents: list = field(default_factory=list)
    manifest: Optional[dict] = None
    audit_refs: list = field(default_factory=list)


def _state_to_dict(state: Any) -> dict:
    """Coerce a ``FileIntakeState`` (or dict) snapshot to a plain dict.

    ``graph.get_state().values`` is normally already a dict, but defend against
    a dataclass sneaking through (e.g. a future LangGraph version) so the
    envelope builder never raises on attribute-vs-key access.
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
    """A fresh lowercase uuid4 string (trace_id / manifest_id)."""
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
    """Identify the report type by file extension (architecture §4 row 3).

    Returns ``"excel"``/``"csv"``/``"pdf"``/``"unknown"``. Unknown extensions fail
    safe (§4) at the ``detect_type`` node → ``AR_UNCERTAIN``.
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm") or name.endswith(".xls"):
        return "excel"
    if name.endswith(".csv") or name.endswith(".tsv"):
        return "csv"
    if name.endswith(".pdf"):
        return "pdf"
    return "unknown"


def _basename(path: str) -> str:
    return os.path.basename(path or "") or path or "file"


def _normalize_file(ref: Any) -> dict[str, str]:
    """Coerce a canvas File-node ref to ``{name, path}``.

    Tolerant of the LangFlow File node's Data shape (a Data with ``.file`` /
    ``.data`` dicts, a plain dict, or a bare path string). Returns
    ``{name:"", path:""}`` when nothing usable is found — the caller fails safe.
    """
    if ref is None:
        return {"name": "", "path": ""}
    # Bare string → path.
    if isinstance(ref, str):
        return {"name": _basename(ref), "path": ref}
    # Data object (lfx) — try common attribute shapes.
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
    # Last resort: look for any string-valued key ending in _path/path on the obj.
    for attr in ("file_path", "path", "file"):
        v = getattr(ref, attr, None)
        if isinstance(v, str) and v:
            return {"name": _basename(v), "path": v}
    return {"name": "", "path": ""}


def _expand_files(files: Any) -> list:
    """Flatten a ``files`` input into individual refs.

    The ``files`` HandleInput may receive a list of Data/strings (from the
    canvas File node or an API upload) or a LangFlow ``Message`` carrying
    ``.files`` (when ChatInput routes the API-uploaded files onto the Message).
    Expand any Message into its ``.files`` list so the caller normalises one ref
    per uploaded file. None/empty entries are dropped.
    """
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
    """Resolve a LangFlow uploaded-file path to a real container filesystem path.

    LangFlow's file API stores uploads at ``{config_dir}/{flow_id}/{name}`` and
    returns the relative ``{flow_id}/{name}`` (a 2-segment storage path). The
    cosmic_common readers open a real path, so prefix relative storage paths
    with ``LANGFLOW_CONFIG_DIR``. Absolute paths and bare filenames pass through
    unchanged (the readers will surface ``AR_VALIDATION`` if they cannot open).
    """
    if not path or os.path.isabs(path):
        return path
    # LangFlow local-storage path shape: "<flow_id>/<filename>" (2 segments).
    if "/" in path and path.count("/") == 1:
        cfg = os.environ.get("LANGFLOW_CONFIG_DIR", "")
        if cfg:
            return os.path.join(cfg, path)
    return path


def _rows_from_content(content: Any) -> list[dict]:
    """Extract a list of dict rows from a reader envelope's ``data``.

    Readers return dict rows (when ``has_header``) or list rows (otherwise);
    normalise to list[dict] for field lookup. PDFs (``pages``/``tables``) are
    flattened to single-row dicts keyed by a synthetic ``text`` field.
    """
    if not isinstance(content, dict):
        return []
    rows = content.get("rows")
    if isinstance(rows, list):
        out: list[dict] = []
        for r in rows:
            if isinstance(r, dict):
                out.append({str(k): str(v) for k, v in r.items()})
            elif isinstance(r, list):
                out.append({f"col{i}": ("" if c is None else str(c))
                            for i, c in enumerate(r)})
        return out
    pages = content.get("pages")
    if isinstance(pages, list):
        text = " ".join(str(p.get("text", "")) for p in pages
                        if isinstance(p, dict))
        return [{"text": text}] if text else []
    tables = content.get("tables")
    if isinstance(tables, list):
        out = []
        for tbl in tables:
            if not isinstance(tbl, dict):
                continue
            for row in tbl.get("rows", []):
                if isinstance(row, list):
                    out.append({f"col{i}": ("" if c is None else str(c))
                                for i, c in enumerate(row)})
        return out
    return []


def _text_from_content(content: Any) -> str:
    """Flatten reader content to a single lowercased text blob for classification."""
    if not isinstance(content, dict):
        return ""
    parts: list[str] = []
    rows = content.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict):
                parts.extend(str(v) for v in r.values())
            elif isinstance(r, list):
                parts.extend(str(c) for c in r)
            else:
                parts.append(str(r))
    for key in ("pages", "tables"):
        coll = content.get(key)
        if isinstance(coll, list):
            for item in coll:
                if isinstance(item, dict):
                    if "text" in item:
                        parts.append(str(item["text"]))
                    for row in item.get("rows", []) or []:
                        if isinstance(row, list):
                            parts.extend(str(c) for c in row)
    return " ".join(parts).lower()


def _first_value(rows: list[dict], keys: tuple[str, ...]) -> str:
    """Find the first non-empty value for any of ``keys`` across all rows.

    Keys are matched case-insensitively and also against normalised forms
    (spaces→underscores). Returns ``""`` when not found.
    """
    if not rows:
        return ""
    lowered = {k.lower() for k in keys}
    for row in rows:
        for rk, rv in row.items():
            if rk.lower().replace(" ", "_") in lowered:
                if rv and str(rv).strip():
                    return str(rv).strip()
    return ""


def _parse_ts(value: str) -> str:
    """Best-effort parse of a date/datetime string to ISO-8601 UTC.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM:SSZ``, ``YYYY/MM/DD``. Date-only
    inputs become midnight UTC. Unparseable/empty → ``utc_now()``.
    """
    s = (value or "").strip()
    if not s:
        return utc_now()
    s = s.replace("/", "-")
    # Already a full timestamp?
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}Z"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00Z"
    return utc_now()


def _to_2dp(value: Any) -> str:
    """Coerce a numeric string to a signed 2dp string; ``"0.00"`` on failure."""
    if value is None or value == "":
        return "0.00"
    s = str(value).strip().replace(",", "")  # strip thousands separators
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        # Try to extract a leading number (e.g. "SAR 1,234.50").
        m = re.search(r"-?\d+(\.\d+)?", s)
        if not m:
            return "0.00"
        try:
            d = Decimal(m.group(0))
        except (InvalidOperation, ValueError):
            return "0.00"
    return f"{d.quantize(Decimal('0.01'))}"


def _sum_2dp(amounts: list[str]) -> str:
    """Sum a list of 2dp-string amounts to a 2dp string (producer-side check)."""
    total = Decimal("0.00")
    for a in amounts:
        try:
            total += Decimal(a)
        except (InvalidOperation, ValueError):
            continue
    return f"{total.quantize(Decimal('0.01'))}"


def _detect_source(text: str, rows: list[dict]) -> str:
    """Detect the source system: explicit column > keyword heuristic > default."""
    explicit = _first_value(rows, SOURCE_KEYS).lower()
    if explicit in ("zoho", "foodics"):
        return explicit
    if "foodics" in text or "pos receipt" in text:
        return "foodics"
    return "zoho"


def _extract_doc_fields(raw_doc: dict, doc_type: str) -> dict[str, str]:
    """Pure metadata extraction: build one ``DocumentManifest.document`` dict.

    Deterministic rules over the reader's rows/content. ``customer_ref`` is
    id-only (§16 — no PII). All fields are strings per the schema. Amounts are
    2dp strings. ``fetched_at`` is always ``utc_now()`` (the moment we read it).
    """
    content = raw_doc.get("content") if isinstance(raw_doc, dict) else None
    rows = _rows_from_content(content)
    text = _text_from_content(content)
    name = raw_doc.get("name", "") if isinstance(raw_doc, dict) else ""
    stem = os.path.splitext(_basename(name))[0] or "doc"
    source = _detect_source(text, rows)
    source_ref = _first_value(rows, REF_KEYS) or stem
    doc_id = f"{source}:{source_ref}"
    customer_ref = _first_value(rows, CUSTOMER_KEYS) or "CUST-UNKNOWN"
    currency = (_first_value(rows, CURRENCY_KEYS) or "USD").upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        currency = "USD"
    amount = _to_2dp(_first_value(rows, AMOUNT_KEYS))
    posted_at = _parse_ts(_first_value(rows, DATE_KEYS))
    status = _first_value(rows, STATUS_KEYS) or "open"
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "source": source,
        "source_ref": source_ref,
        "customer_ref": customer_ref,
        "amount": amount,
        "currency": currency,
        "posted_at": posted_at,
        "status": status,
        "fetched_at": utc_now(),
    }


def build_manifest(documents: list[dict], trace_id: str, tenant: str) -> dict:
    """Assemble a ``DocumentManifest`` dict from validated documents (pure).

    ``totals.sum`` = Σ document amounts to 2dp (producer-side computation — the
    supervisor never computes financial amounts; intake does only this one sum
    because the manifest contract requires it). ``source_systems`` is the
    distinct set of sources. ``period`` is the min/max ``posted_at`` date.
    """
    amounts = [d.get("amount", "0.00") for d in documents
              if isinstance(d, dict)]
    count = len(documents)
    total_sum = _sum_2dp(amounts)
    source_systems = sorted({d.get("source", "") for d in documents
                            if isinstance(d, dict) and d.get("source")})
    dates = sorted(d.get("posted_at", "")[:10] for d in documents
                   if isinstance(d, dict) and d.get("posted_at"))
    dates = [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    period = {"start": dates[0], "end": dates[-1]} if dates else None
    manifest: dict[str, Any] = {
        "manifest_id": mint_id(),
        "trace_id": trace_id,
        "tenant": tenant,
        "documents": documents,
        "totals": {"count": count, "sum": total_sum},
        "source_systems": source_systems,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
    }
    if period:
        manifest["period"] = period
    return manifest


# --------------------------------------------------------------------------- #
#  §10 retry classification (mirrors supervisor._is_transient).
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

    Returns a §14 envelope dict (``data`` carries the reader's rows/pages).
    Reader error envelopes (``AR_VALIDATION`` / ``AR_NOT_IMPLEMENTED``) are HARD
    (file-not-found / corrupt / dep-missing) → no retry, surface as ``error``.
    A reader that *raises* a transient exception is retried; exhausted transient
    retries → ``error`` (intake is read-only, NOT ``pending_approval``).
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
    (openpyxl/pdfplumber may be absent). Returns None if the bundle/dep is
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
    if kind == "pdf":
        from components.cosmic_common.pdf_reader import PDFReaderComponent
        r = PDFReaderComponent()
        r.file_path = file_path
        r.extract_tables = False
        return r
    return None


def _classify_doc(raw_doc: dict, min_confidence: float) -> tuple[str, float]:
    """Classify a single document via the reusable DocumentClassifier (§15).

    Returns ``(doc_type, confidence)``. ``doc_type`` is one of
    invoice/receipt/credit_note/payment or ``unknown``. The classifier is lazy-
    imported; on import failure we fall back to ``unknown`` @ 0.0 (→ AR_UNCERTAIN).
    """
    content_ref = json.dumps(raw_doc.get("content") or {})
    document_ref = raw_doc.get("name", "doc")
    try:
        from components.cosmic_common.document_classifier import (
            DocumentClassifierComponent, _extract_text, RULES,
        )
    except ImportError:
        # Fall back to an inline keyword score so intake still fails safe.
        text = _text_from_content(raw_doc.get("content"))
        best, best_score = "unknown", 0
        import re as _re
        for cand, rules in RULES.items():
            score = sum(w for pat, w in rules if _re.search(pat, text))
            if score > best_score:
                best, best_score = cand, score
        conf = 1.0 if best_score > 0 and best != "unknown" else 0.0
        return best, conf
    try:
        clf = DocumentClassifierComponent()
        # Set every input the classifier reads (don't rely on lfx applying
        # input defaults — robust whether or not the base class populates them).
        clf.document_ref = document_ref
        clf.content_ref = content_ref
        clf.candidate_types = ""  # blank → default candidates (all four types)
        clf.rules_ref = ""
        clf.min_confidence = min_confidence
        env = parse_envelope(_to_str(clf.classify())) or {}
    except Exception:  # noqa: BLE001 — never raise from a node helper
        return "unknown", 0.0
    data = env.get("data") if isinstance(env, dict) else {}
    data = data if isinstance(data, dict) else {}
    doc_type = data.get("doc_type", "unknown")
    confidence = float(data.get("confidence", 0.0) or 0.0)
    return doc_type, confidence


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[FileIntakeContext]) -> FileIntakeContext:
    return runtime.context or {}


def _node_ingest(state: FileIntakeState,
                 runtime: Runtime[FileIntakeContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    return {
        "trace_id": state.trace_id or mint_id(),
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_file_intake"),
        "tenant": state.tenant or ctx.get("tenant", "cosmic-vikings"),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_detect_type(state: FileIntakeState,
                      runtime: Runtime[FileIntakeContext]) -> dict:
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
        # §4 fail-safe: unknown extension or no usable file → AR_UNCERTAIN.
        msg = "unknown file type" if unknowns else "no files supplied"
        if unknowns:
            msg = f"unknown file type for: {', '.join(unknowns)}"
        return {"file_plan": plan, "status": "failed",
                "error": {"code": "AR_UNCERTAIN", "message": msg},
                "updated_at": utc_now()}
    return {"file_plan": plan, "status": "typed", "updated_at": utc_now()}


def _after_detect(state: FileIntakeState) -> str:
    # Path-map keys are the node success statuses ("failed"/"typed"); returning
    # state.status routes "failed"→respond and "typed"→read. Returning a node
    # name here would KeyError against the path map.
    return state.status


def _node_read(state: FileIntakeState,
               runtime: Runtime[FileIntakeContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    raw_docs: list[dict] = []
    failures: list[str] = []
    for entry in state.file_plan:
        kind = entry.get("kind", "")
        path = entry.get("path", "")
        name = entry.get("name", "")
        try:
            reader = _make_reader(kind, path)
        except Exception as exc:  # noqa: BLE001 — dep/import failure is hard
            failures.append(f"{name}: reader unavailable ({exc})")
            continue
        if reader is None:
            failures.append(f"{name}: no reader for kind '{kind}'")
            continue
        envelope = _read_with_retry(reader, path, kind, state.trace_id)
        status = envelope.get("status")
        if status == "ok":
            data = envelope.get("data") if isinstance(envelope, dict) else {}
            raw_docs.append({"name": name, "kind": kind,
                             "content": data if isinstance(data, dict) else {}})
        else:
            err = envelope.get("error") or {}
            msg = err.get("message", "read failed") if isinstance(err, dict) \
                else "read failed"
            failures.append(f"{name}: {msg}")
    if failures or not raw_docs:
        msg = "; ".join(failures) if failures else "no files readable"
        return {"raw_docs": raw_docs, "status": "failed",
                "error": {"code": "AR_VALIDATION", "message": msg},
                "updated_at": utc_now()}
    return {"raw_docs": raw_docs, "status": "read", "updated_at": utc_now()}


def _after_read(state: FileIntakeState) -> str:
    # Path-map keys are node statuses ("failed"/"read"); see _after_detect note.
    return state.status


def _node_extract_metadata(state: FileIntakeState,
                           runtime: Runtime[FileIntakeContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    documents: list[dict] = []
    uncertain: list[str] = []
    for raw_doc in state.raw_docs:
        doc_type, confidence = _classify_doc(raw_doc, MIN_CONFIDENCE)
        if confidence < MIN_CONFIDENCE or doc_type == "unknown":
            uncertain.append(raw_doc.get("name", "doc"))
            continue
        doc = _extract_doc_fields(raw_doc, doc_type)
        documents.append(doc)
    if uncertain or not documents:
        msg = ("could not confidently classify: " + ", ".join(uncertain)
               if uncertain else "no documents extracted")
        return {"documents": documents, "status": "failed",
                "error": {"code": "AR_UNCERTAIN", "message": msg},
                "updated_at": utc_now()}
    return {"documents": documents, "status": "extracted",
            "updated_at": utc_now()}


def _after_extract(state: FileIntakeState) -> str:
    # Path-map keys are node statuses ("failed"/"extracted"); see _after_detect note.
    return state.status


def _node_validate(state: FileIntakeState,
                   runtime: Runtime[FileIntakeContext]) -> dict:
    """Per-document field validation via the reusable ``validate_document`` (§15).

    Lazy import keeps the module importable before the bundle is installed. The
    full-manifest cross-check (totals.sum == Σ amounts) is structurally
    guaranteed by ``build_manifest`` (sum computed from the documents), so per-
    document validation here is sufficient.
    """
    ctx = _ctx(runtime)
    _ = ctx
    try:
        from components.cosmic_common.validation_engine import validate_document
    except ImportError:
        return {"status": "failed",
                "error": {"code": "AR_NOT_IMPLEMENTED",
                          "message": "validation_engine unavailable"},
                "updated_at": utc_now()}
    all_errs: list[str] = []
    for i, doc in enumerate(state.documents):
        errs, _ = validate_document(doc, prefix=f"documents[{i}]")
        all_errs.extend(errs)
    if all_errs:
        return {"status": "failed",
                "error": {"code": "AR_VALIDATION",
                          "message": "; ".join(all_errs[:20])},
                "updated_at": utc_now()}
    return {"status": "validated", "updated_at": utc_now()}


def _after_validate(state: FileIntakeState) -> str:
    # Path-map keys are node statuses ("failed"/"validated"); see _after_detect note.
    return state.status


def _node_build_manifest(state: FileIntakeState,
                         runtime: Runtime[FileIntakeContext]) -> dict:
    ctx = _ctx(runtime)
    _ = ctx
    manifest = build_manifest(list(state.documents), state.trace_id, state.tenant)
    return {"manifest": manifest, "status": "built",
            "updated_at": utc_now()}


def _node_checkpoint(state: FileIntakeState,
                     runtime: Runtime[FileIntakeContext]) -> dict:
    """Record the manifest id as the audit ref (§11 — the manifest is the
    auditable artifact). The InMemorySaver persists state after this node."""
    ctx = _ctx(runtime)
    _ = ctx
    manifest = state.manifest or {}
    mid = manifest.get("manifest_id", "") if isinstance(manifest, dict) else ""
    audit_refs = list(state.audit_refs)
    if mid and mid not in audit_refs:
        audit_refs.append(mid)
    return {"audit_refs": audit_refs, "status": "completed",
            "updated_at": utc_now()}


def _node_respond(state: FileIntakeState,
                  runtime: Runtime[FileIntakeContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class FileIntakeFlowComponent(Component):
    # Bare class name as the canonical `name` (mirrors SupervisorAgentComponent).
    name = "FileIntakeFlowComponent"
    display_name = "Cosmic AR File Intake Flow"
    description = (
        "Accepts an uploaded Excel/CSV/PDF, identifies its report type, extracts "
        "metadata, validates it, and builds a DocumentManifest — with logging, "
        "retries, and checkpoints (constitution §4/§8/§9/§10/§11/§12). The 3rd "
        "AR subflow; called directly or routed to by the supervisor."
    )
    icon = "FileInput"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="User Request",
            info="The natural-language request accompanying the upload (carries intent keywords).",
            required=False,
            tool_mode=True,
        ),
        HandleInput(
            name="files",
            display_name="Uploaded Files",
            info="Uploaded file refs — either from the canvas File node (Data) or "
                 "carried on the ChatInput Message (.files) when files are injected via "
                 "the run API (the 'accept uploaded files' responsibility).",
            input_types=["Data", "Message"],
            is_list=True,
            required=False,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic detect/classify/extract; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="intake_output",
            display_name="Intake Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=FileIntakeState,
                           context_schema=FileIntakeContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("detect_type", _node_detect_type)
        graph.add_node("read", _node_read)
        graph.add_node("extract_metadata", _node_extract_metadata)
        graph.add_node("validate", _node_validate)
        graph.add_node("build_manifest", _node_build_manifest)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_edge("ingest", "detect_type")
        graph.add_conditional_edges("detect_type", _after_detect,
                                    {"failed": "respond", "typed": "read"})
        graph.add_conditional_edges("read", _after_read,
                                    {"failed": "respond", "read": "extract_metadata"})
        graph.add_conditional_edges("extract_metadata", _after_extract,
                                    {"failed": "respond", "extracted": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond", "validated": "build_manifest"})
        graph.add_edge("build_manifest", "checkpoint")
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
            ctx: FileIntakeContext = {
                "user_input": user_input,
                "files": files,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_file_intake",
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = FileIntakeState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=file_intake.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=intake outcome={envelope.get('status')} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "File intake run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=file_intake.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state → §14 envelope (deterministic from state).

        ``graph.get_state(config).values`` returns the merged channel values
        as a *plain dict* (not the typed ``FileIntakeState`` dataclass — nodes
        receive the reconstructed dataclass, but the snapshot does not), so we
        access fields by key here.

        The payload nests under ``data`` (§14: the top level is
        ``status|code|trace_id|data|error|approval_ref`` with
        ``additionalProperties:false``). The supervisor merges
        ``data.audit_refs`` into ``AgentState``; ``data.manifest`` carries the
        ``DocumentManifest``. Intake is read-only, so ``data`` carries no
        financial ``totals{matched,outstanding,posted}`` (the manifest's own
        ``totals{count,sum}`` live under ``data.manifest.totals``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        documents = vals.get("documents") or []
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "manifest": vals.get("manifest") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "document_count": len(documents) if isinstance(documents, list) else 0,
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "intake failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            err_env = {"message": err.get("message", "") if isinstance(err, dict) else str(err)}
            if isinstance(err, dict) and err.get("detail"):
                err_env["detail"] = err["detail"]
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err_env}
        return {"status": "ok", "code": "AR_OK", "trace_id": trace_id,
                "data": data}