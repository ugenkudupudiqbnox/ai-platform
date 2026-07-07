"""Cosmic AR Agent — Zoho Upload Flow component (constitution §1/§8/§9/§10/§11/
§13/§14/§16/§19, architecture §4 row 1).

The Zoho Upload Flow is the **1st AR subflow** (ADR-0011) — it implements
``ar_issue_invoice`` (architecture §4 row 1), the row that **POSTs** an invoice
to Zoho Books. It is distinct from ``ar_invoice_generation`` (#15, ADR-0009),
which only *generates a draft "Zoho Upload File" artifact* — this flow *issues*
the invoice: it takes a **validated-JSON ``ZohoUploadRequest``** wrapper (a §1
``approval_ref`` + a batch of ``InvoiceData`` objects), **validates** each
invoice's mandatory fields, **uploads** each to Zoho Books with §10 retry,
**rolls back** (deletes) the already-created invoices when any invoice in the
batch fails after retries (all-or-nothing), **stores** the Zoho invoice id +
upload timestamp per invoice, **logs** an ``AuditRecord`` per create/rollback
(§13), **updates WorkflowState**, and returns a per-invoice ``ZohoUploadResult``
(+ batch summary) in the §14 envelope.

This implements ``prompts/P14_zoho_upload_flow.md`` verbatim:

  Input Invoice JSON → Validate mandatory fields → Upload invoices → Retry
  failures → Rollback failed uploads → Store [Zoho Invoice ID, Upload Timestamp]
  → Return Upload Result → Update Workflow State.

**§1 enforcement — ``approval_ref`` required at the boundary, NO in-flow
``interrupt``** (ADR-0011 §4, decided). The supervisor already has an *internal*
``_node_gate`` that captures §19 approval **before** delegating a financial
intent to this subflow (``ar_issue_invoice`` is pre-wired in
``FINANCIAL_INTENTS`` + ``TIER["approval"]`` + a ``RunFlow-ar07`` canvas node).
Per the standalone-surface precedent (ADR-0010), this flow does **not** pause
via ``interrupt()`` — instead it **requires an ``approval_ref``** in the input
wrapper (missing/invalid → ``AR_FORBIDDEN``), enforcing constitution §1 ("no
money moves without SSO-attributable approval") **at the flow boundary**. The
``approval_ref`` is echoed into every ``AuditRecord`` (§13 link). **No
``supervisor.py`` / ``supervisor.json`` edit this task** — the subflow is
already pre-wired; the resume-path / ``Flow-as-Tool`` live interaction is a
documented build-phase item.

**Batch-aware, all-or-nothing** (ADR-0011 §5, decided). A single invoice is a
1-element batch. Each invoice is uploaded with §10 retry; on **any** invoice
failing after retries, the already-created invoices are **rolled back** (deleted
best-effort) so the batch is observably failed — no partial batch is left in
Zoho. Rollback deletes are **audit-only** (there is no ``ZohoUploadResult``
operation enum value for delete); the rolled-back flag is carried in the
enriched per-invoice view + audit, not in the canonical contract.

**Deterministic stub transport v1** (ADR-0011 §6). A module-level
``_TRANSPORT = StubZohoUpload()`` + ``set_transport(t)`` abstraction: the stub
returns deterministic result dicts (``zoho_id = f"zoho-inv-<uuid5(invoice_id)>"``)
so the flow is **offline-testable with no live Zoho, no credentials**. The real
``ZohoBooksARTool.create_invoice`` / ``delete_invoice`` (OAuth + POST/DELETE +
401-retry, mirroring ``ZohoBooksAPTool``) is wired at **build-phase** via
``set_transport(RealZoho())``; the flow code is unchanged. This matches every
implemented flow (deterministic + offline-testable; live external calls are
build-phase).

Responsibilities → LangGraph nodes:

  ingest → validate → upload →(cond _after_upload) rollback → store → audit →
  build_state → checkpoint → respond

  - ingest       : parse the ``ZohoUploadRequest`` JSON from ``user_input``; bind
                   ``trace_id``/``flow_id``/``tenant``/``approval_ref`` +
                   timestamps; carry ``model_name`` in **context** (§8). **§1
                   gate:** missing/invalid ``approval_ref`` (not matching
                   ``^ar-approval-<uuid>$``) → ``AR_FORBIDDEN``. Malformed JSON /
                   non-object / missing or empty ``invoices`` → ``AR_VALIDATION``.
                                                                                   §9/§1
  - validate     : validate the wrapper + **each** ``InvoiceData`` against
                   ``invoice-data.schema.json`` mandatory fields (hand-rolled,
                   stdlib): required keys, ``customer_ref`` non-empty (id-only —
                   §16 no PII), money 2dp ``^\\d+\\.\\d{2}$``, ``currency``
                   ``^[A-Z]{3}$``, ``issue_date``/``due_date`` ISO, ``line_items``
                   non-empty with each line's 2dp fields, ``status`` enum. Any
                   error → ``AR_VALIDATION`` with a structured per-invoice error
                   map (no upload attempted). **Records a checkpoint**
                   ``"validate"``.                                              §9/§11
  - upload       : for each invoice, build the deterministic
                   ``idempotency_key = ar-idem:invoice_issue:<tenant>:<uuid5(
                   invoice_id)>`` (§10 replay-safe), then run ``_upload_one`` =
                   the §10 retry loop (≤3 attempts, exp backoff ``1s·2^n`` ±25%
                   parity-based jitter ≤30s; retry only on transient = 5xx/408/
                   429/conn-error; **no 4xx retry**; ``AR_OK``/``AR_DUPLICATE``
                   stop immediately — ``AR_DUPLICATE`` is a safe idempotent
                   replay). Capture ``attempted_at`` on the terminal attempt.
                   After all invoices: status=``"uploaded"`` (all succeeded) or
                   ``"partial"`` (≥1 failed after retries → sets ``error`` =
                   ``AR_UPSTREAM`` batch message). **Records a checkpoint**
                   ``"upload"``.                                              §10/§11
  - rollback     : only acts when status=``"partial"`` AND ≥1 invoice has a
                   ``zoho_id`` (was created). For each created invoice, call
                   ``_TRANSPORT.delete_invoice(zoho_id)`` (best-effort §10 retry;
                   a delete failure records ``rollback_code=AR_UPSTREAM`` but
                   still marks ``rolled_back=true`` so the batch is observably
                   failed). Mark those enriched results ``rolled_back=true``. No
                   ``zoho_id`` ⇒ no-op. **Records a checkpoint** ``"rollback"``.
                   status=``"rolled_back"``.                                   §10/§11
  - store        : build the canonical ``ZohoUploadResult`` per invoice
                   (``operation="invoice_issue"``, ``http_status``, ``code``,
                   ``idempotency_key``, ``zoho_id``, ``zoho_ref``, ``duplicate``,
                   ``attempted_at``, ``attempts``, ``trace_id``, ``tenant``,
                   ``contract_version``). Build the enriched per-invoice view
                   (canonical fields + ``invoice_id``/``invoice_number``/
                   ``customer_ref``/``total``/``currency`` + ``rolled_back``/
                   ``rollback_code`` + ``approval_ref`` echo). Build
                   ``batch_summary``. **Records a checkpoint** ``"store"``.
                   status=``"stored"``.                                        §11/§15
  - audit        : **Log (§13).** One ``AuditRecord`` per invoice **create**
                   (``action="invoice.issue"``, ``actor=ctx.actor``, ``approval_ref``
                   echo, ``idempotency_key``, ``source_system="zoho"``,
                   ``source_ref=zoho_id``, ``before={"status":"draft"}``,
                   ``after={"zoho_id":…,"status":"sent"}`` or the failure). One
                   ``AuditRecord`` per **rollback delete**
                   (``action="invoice.rollback"``, ``source_ref=zoho_id``,
                   ``before={"zoho_id":…}``, ``after={"status":"voided"}``). All
                   ``append_only=true``. **Records a checkpoint** ``"audit"``.
                   status=``"audited"``.                                       §13/§11
  - build_state  : ``WorkflowState`` snapshot: ``status="completed"`` (all
                   succeeded) / ``"failed"`` (partial→rollback or all failed);
                   ``intent="ar_issue_invoice"``; ``posted_total`` = Σ
                   non-rolled-back invoice totals (2dp; ``"0.00"`` if all rolled
                   back/failed); ``matched_amount``/``outstanding_balance=
                   "0.00"`` (not a match flow); ``idempotency_keys`` map;
                   ``pending_approvals=[]``; ``audit_refs``. Immutable (§8).
                   **Records a checkpoint** ``"state"``. status=``"stated"``/``"failed"``.
  - checkpoint   : append the final aggregate audit id; reflect ``audit_refs`` +
                   ``checkpoints`` into the snapshot. ``InMemorySaver`` persists
                   state (§11 fallback — non-durable v1).                   §11
  - respond      : ``_finalize_envelope`` builds the §14 envelope. **No pending
                   branch** (no in-flow interrupt).                          §14

**Checkpoints** after ``validate``/``upload``/``rollback``/``store``/``audit``/
``state`` + the aggregate ``ar_issue_invoice`` (continues ADR-0006/0007/0008/
0009/0010's stricter §11 pattern), persisted by ``InMemorySaver`` at each
super-step.

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. No PII/secrets (§12/§16) —
``customer_ref`` is a Zoho customer id, never customer PII.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1). Tunables belong in Global Variables (§17); these
#  defaults are the v1 policy.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors invoice_generation)
FLOW_ID: str = "ar_issue_invoice"
DEFAULT_TENANT: str = "cosmic-vikings"

# §10 retry policy (constitution §10).
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_SECS: float = 1.0  # 1s · 2^n
BACKOFF_CAP_SECS: float = 30.0
BACKOFF_JITTER: float = 0.25  # ±25% (deterministic parity-based)

# Canonical operation this flow performs (zoho-upload-result.operation enum).
OPERATION: str = "invoice_issue"

# ZohoUploadResult.code enum (zoho-upload-result.schema.json).
CODE_OK: str = "AR_OK"
CODE_DUPLICATE: str = "AR_DUPLICATE"
CODE_UPSTREAM: str = "AR_UPSTREAM"
CODE_AUTH: str = "AR_AUTH"
CODE_VALIDATION: str = "AR_VALIDATION"
CODE_FORBIDDEN: str = "AR_FORBIDDEN"
CODE_NOT_FOUND: str = "AR_NOT_FOUND"
# Codes that represent a successful terminal state (stop, no retry).
SUCCESS_CODES: tuple[str, ...] = (CODE_OK, CODE_DUPLICATE)
# Codes that are hard failures (no retry — §10 "no 4xx retry").
HARD_CODES: tuple[str, ...] = (CODE_AUTH, CODE_VALIDATION, CODE_FORBIDDEN,
                               CODE_NOT_FOUND)

# InvoiceData.status enum (invoice-data.schema.json).
INVOICE_STATUSES: tuple[str, ...] = ("draft", "sent", "open", "paid", "partial",
                                     "void", "overdue")

# Approval-reference regex (matches the contracts' ar-approval-<uuid> shape).
APPROVAL_REF_RE = re.compile(
    r"^ar-approval-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}$"
)
# Idempotency-key regex (zoho-upload-result.schema.json).
IDEMPOTENCY_RE = re.compile(r"^ar-idem:[a-z_]+:[a-z0-9_-]+:[a-z0-9_-]+$")

# 2dp / date / currency patterns (the contracts' patterns).
RE_MONEY = re.compile(r"^\d+\.\d{2}$")  # non-negative 2dp (invoice-data)
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")

_TWO_PLACES = Decimal("0.01")


# --------------------------------------------------------------------------- #
#  Deterministic stub transport (v1). The self-test injects a controllable
#  stub via ``set_transport``; build-phase swaps in a real ZohoBooksARTool
#  wrapper (OAuth + POST/DELETE + 401-retry) — the flow code is unchanged.
# --------------------------------------------------------------------------- #


def _stub_zoho_id(invoice_id: str) -> str:
    """Deterministic Zoho invoice id from the InvoiceData.invoice_id (uuid5)."""
    if not invoice_id:
        return ""
    return f"zoho-inv-{uuid.uuid5(uuid.NAMESPACE_URL, f'zoho-inv:{invoice_id}').hex[:12]}"


class StubZohoUpload:
    """Deterministic in-memory Zoho upload transport (v1, no network).

    ``create_invoice`` always succeeds (``AR_OK``) with a deterministic
    ``zoho_id``; ``delete_invoice`` always succeeds (``204``). The self-test
    injects a scenario stub via ``set_transport`` to exercise retry/rollback.
    """

    def create_invoice(self, invoice: dict, idempotency_key: str) -> dict:
        zid = _stub_zoho_id(str((invoice or {}).get("invoice_id", "")))
        return {
            "ok": True,
            "http_status": 201,
            "code": CODE_OK,
            "zoho_id": zid,
            "zoho_ref": f"INV-{zid[-8:]}" if zid else "",
            "duplicate": False,
            "transient": False,
        }

    def delete_invoice(self, zoho_id: str) -> dict:
        return {"ok": True, "http_status": 204, "code": CODE_OK,
                "transient": False}


_TRANSPORT: Any = StubZohoUpload()


def set_transport(transport: Any) -> None:
    """Swap the transport (self-test → scenario stub; build-phase → real Zoho)."""
    global _TRANSPORT
    _TRANSPORT = transport


# Sleep hook (so the §10 backoff is real in-image but instant under the
# offline self-test, which sets ``c._SLEEP = lambda s: None``).
_SLEEP = time.sleep


def _backoff_delay(attempt: int) -> float:
    """§10 exponential backoff with deterministic ±25% parity-based jitter.

    ``attempt`` is 0-indexed (the just-failed attempt). Base = ``1s·2^n`` capped
    at 30s; jitter is +25% on even ``attempt``, −25% on odd (deterministic — no
    ``Math.random`` / ``uuid4``, so the same attempt always yields the same
    delay; mirrors the calculation/kitchen-revenue backoff).
    """
    base = min(BACKOFF_CAP_SECS, BACKOFF_BASE_SECS * (2 ** attempt))
    jitter = BACKOFF_JITTER if (attempt % 2 == 0) else -BACKOFF_JITTER
    # The cap applies to the final delay (§10 "≤30s"), so jitter can't push it over.
    return min(BACKOFF_CAP_SECS, round(base * (1 + jitter), 3))


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class ZohoUploadContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[ZohoUploadContext]``.

    Durable, resumable state lives in ``ZohoUploadState`` (checkpointed). These
    are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZohoUploadState:
    """The Zoho Upload Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|validated|uploaded|partial|rolled_back|stored|audited|stated|
    # completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    request: Optional[dict] = None  # the parsed ZohoUploadRequest wrapper
    approval_ref: Optional[str] = None
    invoices: list = field(default_factory=list)  # validated InvoiceData list
    validation_report: Optional[dict] = None  # flow-internal validation report
    upload_results: list = field(default_factory=list)  # enriched per-invoice view
    zoho_upload_results: list = field(default_factory=list)  # canonical ZohoUploadResult
    rollback_results: list = field(default_factory=list)  # rollback outcomes
    batch_summary: Optional[dict] = None  # {total, succeeded, failed, rolled_back, posted_total, status}
    idempotency_keys: dict = field(default_factory=dict)  # {invoice_issue:<id>: key}
    audit_records: list = field(default_factory=list)
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<label>: audit_ref} (§11)
    workflow_state: Optional[dict] = None  # WorkflowState snapshot


def _state_to_dict(state: Any) -> dict:
    """Coerce a ``ZohoUploadState`` (or dict) snapshot to a plain dict."""
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


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-upload audit record id (§11/§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"zoho-upload-audit:{trace_id}:{label}"))


def _record_checkpoint(state: ZohoUploadState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry for a step (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


# --------------------------------------------------------------------------- #
#  Request parse / validation (pure).
# --------------------------------------------------------------------------- #


def _parse_request(user_input: str) -> tuple[Optional[dict], Optional[dict]]:
    """Parse the ``ZohoUploadRequest`` wrapper from ``user_input`.

    Returns ``(request, error)`` — exactly one is set. The wrapper is
    ``{approval_ref, invoices:[InvoiceData,…], trace_id?, tenant?}``. Malformed
    JSON / non-object / missing-or-empty ``invoices`` → ``AR_VALIDATION`` (§9).
    **§1 gate:** missing/invalid ``approval_ref`` → ``AR_FORBIDDEN``.
    """
    text = (user_input or "").strip()
    if not text:
        return None, {"code": "AR_VALIDATION",
                      "message": "no zoho-upload request supplied"}
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, {"code": "AR_VALIDATION",
                      "message": f"request JSON parse error: {exc}"}
    if not isinstance(obj, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "request must be a JSON object"}
    approval_ref = obj.get("approval_ref")
    if not isinstance(approval_ref, str) or not APPROVAL_REF_RE.match(approval_ref):
        return None, {"code": "AR_FORBIDDEN",
                      "message": "approval_ref required to upload invoices (§1)"}
    invoices = obj.get("invoices")
    if not isinstance(invoices, list) or not invoices:
        return None, {"code": "AR_VALIDATION",
                      "message": "invoices must be a non-empty array"}
    return obj, None


def _build_validation_report(valid: bool, errors: list[dict],
                             per_invoice: list[dict],
                             trace_id: str) -> dict:
    """Build a flow-internal validation report (pure).

    ``contract_name="ZohoUploadRequest"``; ``per_invoice`` carries the
    per-invoice error map ``[{index, invoice_id, errors:[{path,message}]}]``.
    """
    return {
        "valid": valid,
        "contract_name": "ZohoUploadRequest",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": list(errors),
        "per_invoice": list(per_invoice),
    }


def _validate_line_item(li: Any, idx: int) -> list[dict]:
    """Validate one line item → list of {path, message} error entries."""
    errs: list[dict] = []
    if not isinstance(li, dict):
        errs.append({"path": f"line_items[{idx}]",
                     "message": f"line_items[{idx}] must be an object"})
        return errs
    for k in ("line_id", "item_ref", "description"):
        if not li.get(k):
            errs.append({"path": f"line_items[{idx}].{k}",
                         "message": f"line_items[{idx}] missing {k}"})
    for k in ("qty", "unit_price", "amount"):
        if not RE_MONEY.match(str(li.get(k, ""))):
            errs.append({"path": f"line_items[{idx}].{k}",
                         "message": f"line_items[{idx}].{k} must be a 2dp string "
                                    f"(^\\d+\\.\\d{{2}}$)"})
    return errs


def _validate_invoice(inv: Any) -> list[dict]:
    """Validate one ``InvoiceData`` → list of {path, message} error entries.

    Hand-rolled against ``invoice-data.schema.json`` mandatory fields (stdlib,
    no ``jsonschema`` dep — mirrors ``invoice_generation._validate_invoice``;
    ``ValidationEngineComponent`` wiring is build-phase).
    """
    errs: list[dict] = []
    if not isinstance(inv, dict):
        errs.append({"path": "", "message": "invoice must be a JSON object"})
        return errs
    required = ("invoice_id", "invoice_number", "customer_ref", "tenant",
                "issue_date", "due_date", "line_items", "subtotal", "total",
                "currency", "status", "balance_due", "contract_version")
    for k in required:
        v = inv.get(k)
        if v is None or v == "":
            errs.append({"path": k, "message": f"missing required field: {k}"})
    if not str(inv.get("customer_ref", "")).strip():
        errs.append({"path": "customer_ref",
                     "message": "customer_ref must be non-empty (Zoho id — §16)"})
    for k in ("subtotal", "total", "balance_due"):
        if not RE_MONEY.match(str(inv.get(k, ""))):
            errs.append({"path": k,
                         "message": f"{k} must be a 2dp string (^\\d+\\.\\d{{2}}$)"})
    if not RE_CURRENCY.match(str(inv.get("currency", ""))):
        errs.append({"path": "currency", "message": "currency must match ^[A-Z]{3}$"})
    for k in ("issue_date", "due_date"):
        if not RE_DATE.match(str(inv.get(k, ""))):
            errs.append({"path": k, "message": f"{k} must be YYYY-MM-DD"})
    if str(inv.get("status", "")) not in INVOICE_STATUSES:
        errs.append({"path": "status",
                     "message": f"status must be one of {list(INVOICE_STATUSES)}"})
    items = inv.get("line_items")
    if not isinstance(items, list) or not items:
        errs.append({"path": "line_items",
                     "message": "line_items must be a non-empty array"})
    else:
        for i, li in enumerate(items):
            errs.extend(_validate_line_item(li, i))
    return errs


def _validate_request(req: dict, trace_id: str) -> tuple[dict, Optional[dict]]:
    """Validate the wrapper + each invoice; build the validation report.

    Returns ``(validation_report, error)``. Any per-invoice error → ``error``
    (``AR_VALIDATION``) and the upload is not attempted. ``approval_ref`` is
    already checked by ``_parse_request`` (§1 gate).
    """
    invoices = req.get("invoices") or []
    errors: list[dict] = []
    per_invoice: list[dict] = []
    for i, inv in enumerate(invoices):
        inv_errs = _validate_invoice(inv)
        if inv_errs:
            iid = (inv.get("invoice_id", "") if isinstance(inv, dict) else "")
            entry = {"index": i, "invoice_id": iid, "errors": inv_errs}
            per_invoice.append(entry)
            errors.extend({"invoice_index": i, **e} for e in inv_errs)
    if errors:
        report = _build_validation_report(False, errors, per_invoice, trace_id)
        return report, {"code": "AR_VALIDATION",
                        "message": errors[0].get("message",
                                                 "invoice validation failed")}
    report = _build_validation_report(True, [], [], trace_id)
    return report, None


# --------------------------------------------------------------------------- #
#  Idempotency key + upload retry (pure over the transport — §10).
# --------------------------------------------------------------------------- #


def _build_idempotency_key(tenant: str, invoice_id: str) -> str:
    """Deterministic idempotency key: ``ar-idem:invoice_issue:<tenant>:<uuid5>``.

    ``uuid5(invoice_id)`` is reproducible (§4.3) — the same invoice always yields
    the same key, so a replay after a transient failure is safe (§10). Matches
    the ``zoho-upload-result.idempotency_key`` pattern.
    """
    h = uuid.uuid5(uuid.NAMESPACE_URL, f"zoho-idem:{invoice_id}").hex[:16]
    return f"ar-idem:invoice_issue:{tenant}:{h}"


def _is_transient(res: dict) -> bool:
    """Classify a transport result as transient (retryable) per §10.

    Transient = the transport flagged it, OR 408/429, OR 5xx. 4xx (except
    408/429) is hard (no retry); ``AR_AUTH``/``AR_VALIDATION``/``AR_FORBIDDEN``/
    ``AR_NOT_FOUND`` are hard codes.
    """
    if res.get("transient"):
        return True
    hs = int(res.get("http_status", 0) or 0)
    if hs in (408, 429) or 500 <= hs < 600:
        return True
    return False


def _retry_loop(invoice: dict, idempotency_key: str) -> tuple[dict, int, str, str]:
    """Run the §10 retry loop over ``_TRANSPORT.create_invoice``.

    Returns ``(final_result, attempts, attempted_at, final_code)``.
    ``AR_OK``/``AR_DUPLICATE`` stop immediately; transient results retry (≤3
    attempts, exp backoff ``1s·2^n`` ±25% parity jitter ≤30s); hard 4xx / hard
    codes stop immediately (no retry). Exhausted transient → ``AR_UPSTREAM``.
    """
    res: dict = {}
    attempts = 0
    for attempt in range(MAX_ATTEMPTS):
        attempts = attempt + 1
        res = _TRANSPORT.create_invoice(invoice, idempotency_key)
        code = str(res.get("code", CODE_UPSTREAM))
        if code in SUCCESS_CODES or res.get("ok"):
            return res, attempts, utc_now(), code
        if _is_transient(res):
            if attempt < MAX_ATTEMPTS - 1:
                try:
                    _SLEEP(_backoff_delay(attempt))
                except Exception:  # noqa: BLE001 — sleep must never break the loop
                    pass
                continue
            return res, attempts, utc_now(), CODE_UPSTREAM
        # Hard failure (4xx / hard code) — no retry (§10).
        return res, attempts, utc_now(), code
    return res, attempts, utc_now(), CODE_UPSTREAM


def _upload_one(invoice: dict, idempotency_key: str) -> dict:
    """Upload one invoice via the §10 retry loop → internal outcome dict.

    The outcome carries the canonical ``ZohoUploadResult`` fields plus the
    ``transient`` flag (used by the rollback/store nodes). ``attempted_at`` is
    the timestamp of the terminal attempt.
    """
    res, attempts, attempted_at, code = _retry_loop(invoice, idempotency_key)
    return {
        "code": code,
        "http_status": int(res.get("http_status", 0) or 0),
        "zoho_id": str(res.get("zoho_id") or ""),
        "zoho_ref": str(res.get("zoho_ref") or ""),
        "duplicate": bool(res.get("duplicate", False)),
        "attempts": attempts,
        "attempted_at": attempted_at,
        "transient": bool(res.get("transient", False)),
        "idempotency_key": idempotency_key,
    }


def _rollback_one(zoho_id: str) -> dict:
    """Best-effort delete of one created invoice → rollback outcome dict.

    A delete failure records ``code=AR_UPSTREAM`` but the caller still marks the
    invoice ``rolled_back=True`` so the batch is observably failed.
    """
    res, _attempts, _ts, code = _retry_loop_delete(zoho_id)
    return {
        "zoho_id": zoho_id,
        "code": code,
        "http_status": int(res.get("http_status", 0) or 0),
        "transient": bool(res.get("transient", False)),
    }


def _retry_loop_delete(zoho_id: str) -> tuple[dict, int, str, str]:
    """§10 retry loop over ``_TRANSPORT.delete_invoice`` (best-effort rollback)."""
    res: dict = {}
    attempts = 0
    for attempt in range(MAX_ATTEMPTS):
        attempts = attempt + 1
        res = _TRANSPORT.delete_invoice(zoho_id)
        code = str(res.get("code", CODE_UPSTREAM))
        if code in SUCCESS_CODES or res.get("ok"):
            return res, attempts, utc_now(), code
        if _is_transient(res):
            if attempt < MAX_ATTEMPTS - 1:
                try:
                    _SLEEP(_backoff_delay(attempt))
                except Exception:  # noqa: BLE001
                    pass
                continue
            return res, attempts, utc_now(), CODE_UPSTREAM
        return res, attempts, utc_now(), code
    return res, attempts, utc_now(), CODE_UPSTREAM


# --------------------------------------------------------------------------- #
#  Result / audit / workflow-state builders (pure).
# --------------------------------------------------------------------------- #


def _build_upload_result(outcome: dict, trace_id: str, tenant: str) -> dict:
    """Build the canonical ``ZohoUploadResult`` (§15) from an upload outcome.

    Only schema-known keys are emitted (``additionalProperties:false``).
    Optional keys (``zoho_id``/``zoho_ref``/``duplicate``/``attempted_at``/
    ``attempts``) are emitted when set. ``operation="invoice_issue"``.
    """
    res: dict[str, Any] = {
        "trace_id": trace_id,
        "tenant": tenant,
        "operation": OPERATION,
        "http_status": int(outcome.get("http_status", 0) or 0),
        "code": str(outcome.get("code", CODE_UPSTREAM)),
        "idempotency_key": str(outcome.get("idempotency_key", "")),
        "contract_version": CONTRACT_VERSION,
    }
    if outcome.get("zoho_id"):
        res["zoho_id"] = str(outcome["zoho_id"])
    if outcome.get("zoho_ref"):
        res["zoho_ref"] = str(outcome["zoho_ref"])
    res["duplicate"] = bool(outcome.get("duplicate", False))
    if outcome.get("attempted_at"):
        res["attempted_at"] = str(outcome["attempted_at"])
    res["attempts"] = int(outcome.get("attempts", 1) or 1)
    return res


def _build_enriched_result(outcome: dict, invoice: dict,
                           approval_ref: str) -> dict:
    """Build the enriched per-invoice view (flow-internal, not the contract).

    Canonical ``ZohoUploadResult`` fields + ``invoice_id``/``invoice_number``/
    ``customer_ref``/``total``/``currency`` + ``rolled_back``/``rollback_code`` +
    the ``approval_ref`` echo (§13 link). ``rolled_back`` defaults to ``False``;
    the rollback node flips it.
    """
    inv = invoice if isinstance(invoice, dict) else {}
    enr = _build_upload_result(outcome, "", "")  # canonical fields (trace/tenant filled by store)
    enr.update({
        "trace_id": "",  # filled by store (knows trace_id)
        "tenant": "",
        "invoice_id": str(inv.get("invoice_id", "")),
        "invoice_number": str(inv.get("invoice_number", "")),
        "customer_ref": str(inv.get("customer_ref", "")),
        "total": str(inv.get("total", "0.00")),
        "currency": str(inv.get("currency", "")),
        "approval_ref": approval_ref,
        "rolled_back": False,
        "rollback_code": None,
    })
    return enr


def _build_batch_summary(upload_results: list) -> dict:
    """Build the batch summary from the enriched per-invoice view."""
    total = len(upload_results)
    succeeded = sum(1 for r in upload_results
                    if r.get("code") in SUCCESS_CODES and not r.get("rolled_back"))
    failed = sum(1 for r in upload_results
                 if r.get("code") not in SUCCESS_CODES)
    rolled_back = sum(1 for r in upload_results if r.get("rolled_back"))
    posted = [str(r.get("total", "0.00")) for r in upload_results
              if r.get("code") in SUCCESS_CODES and not r.get("rolled_back")]
    status = "completed" if (failed == 0 and rolled_back == 0) else "failed"
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "rolled_back": rolled_back,
        "posted_total": _sum_2dp(posted),
        "status": status,
    }


def _build_audit_record(*, audit_id: str, trace_id: str, tenant: str,
                        actor: str, action: str, timestamp: str,
                        approval_ref: str, idempotency_key: str,
                        source_ref: str, before: dict,
                        after: dict) -> dict[str, Any]:
    """Build an append-only ``AuditRecord`` (§13).

    ``source_system="zoho"``; ``source_ref`` = the Zoho invoice id; ``actor`` =
    the Keycloak sub; ``approval_ref`` links the §19 approval that authorized
    the POST; ``append_only=true``. Used for both create (``invoice.issue``) and
    rollback (``invoice.rollback``).
    """
    rec: dict[str, Any] = {
        "audit_id": audit_id,
        "trace_id": trace_id,
        "tenant": tenant,
        "actor": actor or "unknown",
        "action": action,
        "timestamp": timestamp,
        "append_only": True,
        "source_system": "zoho",
        "contract_version": CONTRACT_VERSION,
    }
    if approval_ref:
        rec["approval_ref"] = approval_ref
    if idempotency_key:
        rec["idempotency_key"] = idempotency_key
    if source_ref:
        rec["source_ref"] = source_ref
    if before:
        rec["before"] = before
    if after:
        rec["after"] = after
    return rec


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         status: str, posted_total: str,
                         idempotency_keys: dict, audit_refs: list,
                         created_at: str, updated_at: str) -> dict[str, Any]:
    """Build a ``WorkflowState`` snapshot (§8, immutable).

    ``status="completed"`` (all succeeded) / ``"failed"`` (partial→rollback or
    all failed); ``posted_total`` = Σ non-rolled-back invoice totals (2dp);
    ``matched_amount``/``outstanding_balance="0.00"`` (not a match flow);
    ``pending_approvals=[]`` (approval captured at the boundary, not pending).
    """
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": FLOW_ID,
        "status": status,
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": posted_total,
        "pending_approvals": [],
        "idempotency_keys": dict(idempotency_keys),
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:{FLOW_ID}:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[ZohoUploadContext]) -> ZohoUploadContext:
    return runtime.context or {}


def _node_ingest(state: ZohoUploadState,
                 runtime: Runtime[ZohoUploadContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    user_input = ctx.get("user_input", "")
    req, err = _parse_request(user_input)
    if err is not None:
        return {"status": "failed", "error": err,
                "request": None, "created_at": now, "updated_at": now}
    trace_id = str(req.get("trace_id") or state.trace_id or mint_id())
    tenant = str(req.get("tenant") or state.tenant
                 or ctx.get("tenant", DEFAULT_TENANT))
    return {
        "trace_id": trace_id,
        "flow_id": state.flow_id or ctx.get("flow_id", FLOW_ID),
        "tenant": tenant,
        "approval_ref": str(req.get("approval_ref", "")),
        "request": req,
        "invoices": list(req.get("invoices") or []),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_validate(state: ZohoUploadState,
                   runtime: Runtime[ZohoUploadContext]) -> dict:
    _ = _ctx(runtime)
    req = state.request or {}
    report, err = _validate_request(req, state.trace_id)
    if err is not None:
        return {"validation_report": report, "status": "failed",
                "error": err, "updated_at": utc_now()}
    audit_refs, checkpoints = _record_checkpoint(state, "validate")
    return {"validation_report": report, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "validated",
            "updated_at": utc_now()}


def _node_upload(state: ZohoUploadState,
                 runtime: Runtime[ZohoUploadContext]) -> dict:
    _ = _ctx(runtime)
    approval_ref = state.approval_ref or ""
    upload_results: list[dict] = []
    idem_keys: dict[str, str] = {}
    for inv in state.invoices:
        inv = inv if isinstance(inv, dict) else {}
        invoice_id = str(inv.get("invoice_id", ""))
        idem = _build_idempotency_key(state.tenant, invoice_id)
        idem_keys[f"invoice_issue:{invoice_id}"] = idem
        outcome = _upload_one(inv, idem)
        enr = _build_enriched_result(outcome, inv, approval_ref)
        upload_results.append(enr)
    # All succeeded ⇒ uploaded; ≥1 failed after retries ⇒ partial (→ rollback).
    failed_count = sum(1 for r in upload_results
                       if r.get("code") not in SUCCESS_CODES)
    audit_refs, checkpoints = _record_checkpoint(state, "upload")
    if failed_count:
        created = sum(1 for r in upload_results if r.get("zoho_id"))
        if created:
            msg = (f"{failed_count} of {len(upload_results)} invoice(s) failed "
                   f"after retries; {created} created invoice(s) rolled back")
        else:
            msg = f"all {len(upload_results)} invoice(s) failed; none created"
        return {"upload_results": upload_results,
                "idempotency_keys": idem_keys,
                "audit_refs": audit_refs, "checkpoints": checkpoints,
                "status": "partial",
                "error": {"code": CODE_UPSTREAM, "message": msg},
                "updated_at": utc_now()}
    return {"upload_results": upload_results,
            "idempotency_keys": idem_keys,
            "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "uploaded",
            "updated_at": utc_now()}


def _node_rollback(state: ZohoUploadState,
                   runtime: Runtime[ZohoUploadContext]) -> dict:
    """Roll back (delete) the already-created invoices on a partial batch.

    Best-effort §10 retry per delete; a delete failure still marks the invoice
    ``rolled_back=True`` (observably failed). No ``zoho_id`` ⇒ no-op. Records a
    checkpoint ``"rollback"``. Only reached when status=``"partial"`` (the
    ``_after_upload`` router skips it on ``"uploaded"``).
    """
    _ = _ctx(runtime)
    upload_results = [dict(r) for r in state.upload_results]
    rollback_results: list[dict] = []
    for r in upload_results:
        zid = str(r.get("zoho_id") or "")
        if not zid:
            continue
        rb = _rollback_one(zid)
        rollback_results.append({"invoice_id": r.get("invoice_id", ""),
                                 **rb})
        r["rolled_back"] = True
        r["rollback_code"] = rb.get("code", CODE_UPSTREAM)
    audit_refs, checkpoints = _record_checkpoint(state, "rollback")
    return {"upload_results": upload_results,
            "rollback_results": rollback_results,
            "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "rolled_back", "updated_at": utc_now()}


def _node_store(state: ZohoUploadState,
                runtime: Runtime[ZohoUploadContext]) -> dict:
    """Build the canonical ``ZohoUploadResult`` per invoice + batch summary."""
    _ = _ctx(runtime)
    zoho_upload_results: list[dict] = []
    for enr in state.upload_results:
        canonical = _build_upload_result(
            {k: enr.get(k) for k in ("code", "http_status", "zoho_id", "zoho_ref",
                                     "duplicate", "attempts", "attempted_at",
                                     "idempotency_key")},
            state.trace_id, state.tenant)
        zoho_upload_results.append(canonical)
        # Reflect trace/tenant back onto the enriched view.
        enr["trace_id"] = state.trace_id
        enr["tenant"] = state.tenant
    batch_summary = _build_batch_summary(state.upload_results)
    audit_refs, checkpoints = _record_checkpoint(state, "store")
    return {"zoho_upload_results": zoho_upload_results,
            "upload_results": list(state.upload_results),
            "batch_summary": batch_summary,
            "audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "stored", "updated_at": utc_now()}


def _node_audit(state: ZohoUploadState,
                runtime: Runtime[ZohoUploadContext]) -> dict:
    """Log one ``AuditRecord`` per create + one per rollback delete (§13)."""
    ctx = _ctx(runtime)
    actor = str(ctx.get("actor", "") or "")
    approval_ref = state.approval_ref or ""
    records: list[dict] = list(state.audit_records)
    for enr in state.upload_results:
        label = f"issue:{enr.get('invoice_id', '')}"
        audit_id = _audit_ref(state.trace_id, label)
        records.append(_build_audit_record(
            audit_id=audit_id, trace_id=state.trace_id, tenant=state.tenant,
            actor=actor, action="invoice.issue",
            timestamp=enr.get("attempted_at") or utc_now(),
            approval_ref=approval_ref,
            idempotency_key=str(enr.get("idempotency_key", "")),
            source_ref=str(enr.get("zoho_id", "")),
            before={"status": "draft"},
            after={"zoho_id": enr.get("zoho_id", ""),
                   "status": "sent" if enr.get("code") in SUCCESS_CODES
                   else "failed",
                   "code": enr.get("code", CODE_UPSTREAM)}))
    for rb in state.rollback_results:
        label = f"rollback:{rb.get('invoice_id', '') or rb.get('zoho_id', '')}"
        audit_id = _audit_ref(state.trace_id, label)
        records.append(_build_audit_record(
            audit_id=audit_id, trace_id=state.trace_id, tenant=state.tenant,
            actor=actor, action="invoice.rollback",
            timestamp=utc_now(), approval_ref=approval_ref,
            idempotency_key="",
            source_ref=str(rb.get("zoho_id", "")),
            before={"zoho_id": rb.get("zoho_id", "")},
            after={"status": "voided",
                   "rollback_code": rb.get("code", CODE_UPSTREAM)}))
    audit_refs, checkpoints = _record_checkpoint(state, "audit")
    return {"audit_records": records, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "audited",
            "updated_at": utc_now()}


def _node_build_state(state: ZohoUploadState,
                      runtime: Runtime[ZohoUploadContext]) -> dict:
    """Build the ``WorkflowState`` snapshot (§8)."""
    _ = _ctx(runtime)
    summary = _build_batch_summary(state.upload_results)
    ws_status = "completed" if summary["status"] == "completed" else "failed"
    idem_keys = {}
    for enr in state.upload_results:
        iid = str(enr.get("invoice_id", ""))
        if iid and enr.get("idempotency_key"):
            idem_keys[f"invoice_issue:{iid}"] = enr["idempotency_key"]
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              ws_status, summary["posted_total"], idem_keys,
                              state.audit_refs, state.created_at, state.updated_at)
    audit_refs, checkpoints = _record_checkpoint(state, "state")
    node_status = "stated" if ws_status == "completed" else "failed"
    return {"workflow_state": ws, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": node_status,
            "updated_at": utc_now()}


def _node_checkpoint(state: ZohoUploadState,
                     runtime: Runtime[ZohoUploadContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints."""
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, FLOW_ID)
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    # Preserve a failed status through the final checkpoint (the envelope reads
    # ``error`` / ``workflow_state.status`` to decide ok vs error).
    status = state.status if state.status == "failed" else "completed"
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "status": status,
            "updated_at": utc_now()}


def _node_respond(state: ZohoUploadState,
                  runtime: Runtime[ZohoUploadContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# Conditional routers (return state.status against status-keyed path maps).
def _after_ingest(state: ZohoUploadState) -> str:
    return state.status


def _after_validate(state: ZohoUploadState) -> str:
    return state.status


def _after_upload(state: ZohoUploadState) -> str:
    return state.status


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class ZohoUploadFlowComponent(Component):
    name = "ZohoUploadFlowComponent"
    display_name = "Cosmic AR Zoho Upload Flow"
    description = (
        "The Zoho Upload Flow for the Cosmic AR Agent (ar_issue_invoice, the 1st "
        "subflow): takes a validated-JSON ZohoUploadRequest "
        "({approval_ref, invoices:[InvoiceData,…]}) — §1 approval_ref required at "
        "the boundary (no in-flow interrupt) — validates each invoice's "
        "mandatory fields, uploads each to Zoho Books with §10 retry, rolls back "
        "(deletes) the already-created invoices when any invoice fails after "
        "retries (all-or-nothing), stores the Zoho invoice id + upload timestamp, "
        "logs an AuditRecord per create/rollback (§13), updates WorkflowState, "
        "and returns a per-invoice ZohoUploadResult + batch summary in the §14 "
        "envelope. Deterministic stub transport v1 (real ZohoBooksARTool "
        "create/delete POST build-phase). Constitution §1/§8/§9/§10/§11/§13/§14/"
        "§16/§19. See ADR-0011."
    )
    icon = "Upload"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="Zoho Upload Request (JSON)",
            info=(
                "The validated-JSON ZohoUploadRequest wrapper: {approval_ref "
                "(ar-approval-<uuid>, required — §1), invoices:[InvoiceData,…] "
                "(≥1; single = 1-element batch), trace_id?, tenant?}. Each "
                "InvoiceData is the Invoice JSON from ar_invoice_generation "
                "({invoice_id, invoice_number, customer_ref, tenant, issue_date, "
                "due_date, line_items, subtotal, total, currency, status, "
                "balance_due, contract_version}). PRIMARY input."
            ),
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic upload; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="zoho_upload_output",
            display_name="Upload Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=ZohoUploadState,
                           context_schema=ZohoUploadContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("validate", _node_validate)
        graph.add_node("upload", _node_upload)
        graph.add_node("rollback", _node_rollback)
        graph.add_node("store", _node_store)
        graph.add_node("audit", _node_audit)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _after_ingest,
                                    {"failed": "respond", "created": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond",
                                     "validated": "upload"})
        graph.add_conditional_edges("upload", _after_upload,
                                    {"partial": "rollback",
                                     "uploaded": "store"})
        # The remaining nodes are deterministic compute → static edges (unexpected
        # errors caught at the run() boundary → AR_UNEXPECTED).
        graph.add_edge("rollback", "store")
        graph.add_edge("store", "audit")
        graph.add_edge("audit", "build_state")
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
            ctx: ZohoUploadContext = {
                "user_input": user_input,
                "actor": actor,
                "session_id": session_id,
                "tenant": DEFAULT_TENANT,
                "flow_id": FLOW_ID,
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = ZohoUploadState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            posted = ""
            ws = envelope.get("data", {}).get("workflow_state") or {}
            if isinstance(ws, dict):
                posted = ws.get("posted_total", "")
            self.log(
                f"event=zoho_upload.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=issue_invoice posted_total={posted} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Zoho upload run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=zoho_upload.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state → §14 envelope (deterministic from state).

        ``graph.get_state(config).values`` is a plain dict (ADR-0003 §10), so
        fields are read by key. Failed = ``state.error`` set OR
        ``workflow_state.status=="failed"`` (partial→rollback or all failed).
        No ``pending_approval`` branch (no in-flow interrupt). The results
        surface via the envelope (``data.upload_results`` /
        ``data.zoho_upload_results`` + ``data.audit_refs``); no ``AgentState``
        schema change (ADR-0011 — mirrors ADR-0006/0007/0008/0009/0010).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        audit_refs = vals.get("audit_refs") or []
        ws = vals.get("workflow_state") or {}
        summary = vals.get("batch_summary") or {}
        data: dict[str, Any] = {
            "upload_results": vals.get("upload_results") or [],
            "zoho_upload_results": vals.get("zoho_upload_results") or [],
            "rollback_results": vals.get("rollback_results") or [],
            "batch_summary": summary,
            "validation_report": vals.get("validation_report") or {},
            "workflow_state": ws,
            "audit_records": vals.get("audit_records") or [],
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "approval_ref": vals.get("approval_ref", ""),
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        ws_status = ws.get("status") if isinstance(ws, dict) else ""
        failed = bool(vals.get("error")) or ws_status == "failed"
        if failed:
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "zoho upload failed"}
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