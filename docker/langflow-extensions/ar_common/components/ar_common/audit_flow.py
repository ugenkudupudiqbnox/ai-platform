"""Cosmic AR Agent — Audit Flow component (constitution §8/§9/§11/§13/§14/§15/§16,
architecture §4 row 9).

The Audit Flow is the **9th AR subflow** (ADR-0012) — it implements
``ar_audit`` (architecture §4 row 9, added by ADR-0012 which amends §4's
"Fifteen reusable subflows" to "Sixteen"). It is the run-level **audit
aggregator**: it collects a run's execution history, input files, validation
reports, calculation results, invoices, approvals, Zoho upload results,
execution time, errors, and warnings → **synthesizes an immutable §13 audit
log** (a list of append-only ``AuditRecord``s) → **generates an execution
summary** (the ``ExecutionSummary`` contract) → **returns the Audit JSON** (a
§14 envelope) and **updates WorkflowState**.

This implements ``prompts/P15_audit_flow.md`` verbatim:

  Collect [Execution History, Input Files, Validation Reports, Calculation
  Results, Invoices, Approvals, Zoho Upload Results, Execution Time, Errors,
  Warnings] → Generate immutable audit log → Generate execution summary →
  Return Audit JSON → Update Workflow State.

**Read-only audit emission — no §1 gate, no transport** (ADR-0012 §4, decided).
No money moves, so ``TIER["ar_audit"]="read-only"``, ``ar_audit`` is **not** in
``FINANCIAL_INTENTS``, there is **no §1 approval gate**, **no ``approval_ref``
required**, and **no idempotency key** (mirrors ``ar_invoice_generation`` /
``ar_calculation``, ADR-0008/0009). The flow is **pure compute** — deterministic
aggregation from the input wrapper, with no external calls and no side effects
(no ``set_transport`` seam, unlike ``ar_issue_invoice``). The "immutable audit
log" is generated in-memory and returned in the envelope (offline-testable);
persistence to the Postgres ``audit`` table / Langfuse is **build-phase**.

**Input = flow-internal ``AuditRequest`` wrapper** (ADR-0012 §3). The supervisor
routes an "audit"/"execution summary"/"run summary"/"audit trail" intent to
``ar_audit``, which parses the ``AuditRequest`` JSON from ``user_input``
(caller-assembled — exactly how ``ar_calculation`` /
``ar_invoice_generation`` / ``ar_issue_invoice`` take validated JSON as
``user_input``). The cross-subflow **auto**-accumulation (the supervisor
assembling the ``AuditRequest`` from multiple subflow envelopes across a run)
is a **v2 multi-subflow-run + AgentState-artifact build-phase feature** — not
exercisable in v1 (single-subflow-per-run); a manual/operator-assembled
``AuditRequest`` works now.

**No new contract schemas** (ADR-0012 §12). ``audit-record`` /
``execution-summary`` / ``workflow-state`` / ``envelope`` are reused as-is
(§15). The ``AuditRequest`` wrapper + ``audit_log`` + collected bundle are
flow-internal JSON (documented in the ADR + operational doc, not new schema
files — like ``ZohoUploadRequest``).

**``source_system`` enum handling** (ADR-0012 §7). ``audit-record.schema.json``
``source_system`` is ``["zoho","foodics"]`` only. The Audit Flow synthesizes
records for internal actions (validation/calculation/file-intake/
audit.summary) that are NOT zoho/foodics. **``source_system`` is set only on
zoho/foodics records** (a zoho upload result → ``"zoho"``; a foodics-derived
input file → ``"foodics"``); it is **omitted** on internal actions (optional in
the schema). No enum amendment. The ``AuditLoggerComponent``'s inconsistent
``cosmic-ar-agent|…`` dropdown is **not used**; the schema enum governs.

Responsibilities → LangGraph nodes:

  ingest → validate → collect → build_audit_log → build_execution_summary →
  build_state → checkpoint → respond

  - ingest                : parse the ``AuditRequest`` JSON from ``user_input``;
                            bind ``trace_id``/``flow_id``/``tenant``/``actor`` +
                            timestamps; carry ``model_name`` in **context** (§8).
                            Malformed JSON / non-object → ``AR_VALIDATION``.
                            status=``"created"``.                                  §9
  - validate              : hand-rolled validator for the ``AuditRequest`` wrapper
                            (stdlib): it is an object; each list field, if present,
                            must be a list; ``execution_time``, if present, must be
                            an object with ISO ``started_at``/``ended_at``. All
                            artifact lists are **optional** (an empty bundle audits
                            an empty/no-op run — still valid). Malformed →
                            ``AR_VALIDATION`` with a structured error map. Records
                            a checkpoint ``"validate"``. status=``"validated"``.    §9/§11
  - collect               : normalize the artifacts into state fields + compute
                            summary counts (n_invoices/n_approvals/n_zoho_uploads/
                            n_calc_results/n_val_reports/n_input_files/n_errors/
                            n_warnings/n_subflows). Derive ``subflows_invoked``
                            (unique ``flow_id``s from ``execution_history``). Records
                            a checkpoint ``"collect"``. status=``"collected"``.     §11
  - build_audit_log       : **Generate the immutable audit log (§13).** Synthesize
                            one ``AuditRecord`` per artifact + a terminal
                            ``audit.summary`` record (each ``append_only=true``,
                            ``actor``=Keycloak sub, deterministic uuid5 ``audit_id``,
                            ``correlation_id``=trace_id). ``before``/``after`` carry
                            **scalar-only** values (the ``state_delta`` contract
                            allows string/number/boolean/null — no nested objects or
                            arrays, so ``totals``/``execution_time``/``subflows_invoked``
                            are flattened to scalar keys). ``source_system`` only on
                            zoho/foodics records. Append all to ``audit_log`` +
                            ``audit_refs``. Records a checkpoint ``"audit_log"``.
                            status=``"audited"``.                                  §13/§11
  - build_execution_summary: build the ``ExecutionSummary`` contract: ``intent=
                            "ar_audit"``, ``status="ok"``, ``code="AR_OK"``,
                            ``totals`` (from collected totals or ``"0.00"``),
                            ``started_at``/``ended_at`` (from ``execution_time`` or
                            ``created_at``/``updated_at``), ``approvals`` (the
                            ``approval_ref``s), ``audit_refs``, ``checkpoint_id``,
                            ``subflows_invoked``, ``contract_version``. Records a
                            checkpoint ``"summary"``. status=``"summarized"``.       §14/§11
  - build_state           : build the ``WorkflowState`` snapshot: ``status=
                            "completed"``, ``intent="ar_audit"``, totals (or
                            ``"0.00"``), ``pending_approvals=[]``, ``idempotency_keys=
                            {}`` (read-only, no gate), ``audit_refs``,
                            ``contract_version``. Immutable (§8). Records a checkpoint
                            ``"state"``. status=``"stated"``.                       §8/§11
  - checkpoint            : append the final aggregate audit id; reflect ``audit_refs``
                            + ``checkpoints`` into the WorkflowState snapshot.
                            ``InMemorySaver`` persists state (§11 fallback — non-durable
                            v1). status=``"completed"``.                            §11
  - respond               : ``_finalize_envelope`` builds the §14 envelope. **No
                            pending branch** (no in-flow interrupt).                §14

**Checkpoints** after ``validate``/``collect``/``audit_log``/``summary``/
``state`` + the aggregate ``ar_audit`` (continues ADR-0006/0007/0008/0009/0010/
0011's stricter §11 pattern), persisted by ``InMemorySaver`` at each super-step.

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. No PII/secrets (§12/§16) — the bundle is
id-only references.
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
FLOW_ID: str = "ar_audit"
DEFAULT_TENANT: str = "cosmic-vikings"

# Envelope codes (§9/§14).
CODE_OK: str = "AR_OK"
CODE_VALIDATION: str = "AR_VALIDATION"
CODE_UNEXPECTED: str = "AR_UNEXPECTED"

# audit-record.source_system enum (audit-record.schema.json) — set only on
# zoho/foodics records; omitted on internal actions (ADR-0012 §7).
SOURCE_SYSTEMS: tuple[str, ...] = ("zoho", "foodics")

# ISO-Z timestamp + uuid patterns (the contracts' patterns).
RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_TWO_PLACES = Decimal("0.01")

# The artifact-list fields the AuditRequest wrapper may carry.
LIST_FIELDS: tuple[str, ...] = (
    "execution_history", "input_files", "validation_reports",
    "calculation_results", "invoices", "approvals",
    "zoho_upload_results", "errors", "warnings",
)


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class AuditFlowContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[AuditFlowContext]``.

    Durable, resumable state lives in ``AuditFlowState`` (checkpointed). These
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
class AuditFlowState:
    """The Audit Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|validated|collected|audited|summarized|stated|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    request: Optional[dict] = None  # the parsed AuditRequest wrapper
    actor: str = ""  # Keycloak sub (§13); "" when unattributed
    # The collected bundle (all optional lists — an empty bundle audits an empty run).
    execution_history: list = field(default_factory=list)
    input_files: list = field(default_factory=list)
    validation_reports: list = field(default_factory=list)
    calculation_results: list = field(default_factory=list)
    invoices: list = field(default_factory=list)
    approvals: list = field(default_factory=list)
    zoho_upload_results: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    execution_time: Optional[dict] = None
    summary_counts: Optional[dict] = None  # {n_invoices, n_approvals, …, n_subflows}
    subflows_invoked: list = field(default_factory=list)  # unique flow_ids, in order
    totals: Optional[dict] = None  # {matched, outstanding, posted} (2dp strings)
    audit_log: list = field(default_factory=list)  # synthesized AuditRecords (§13)
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<label>: audit_ref} (§11)
    execution_summary: Optional[dict] = None  # ExecutionSummary contract
    workflow_state: Optional[dict] = None  # WorkflowState snapshot


def _state_to_dict(state: Any) -> dict:
    """Coerce an ``AuditFlowState`` (or dict) snapshot to a plain dict."""
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
    """Deterministic per-record audit id (§11/§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"audit-flow:{trace_id}:{label}"))


def _record_checkpoint(state: AuditFlowState, label: str) -> tuple[list, dict]:
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
    """Parse the ``AuditRequest`` wrapper from ``user_input``.

    Returns ``(request, error)`` — exactly one is set. The wrapper is an object
    carrying the collected run artifacts (all list fields optional — an empty
    bundle audits an empty/no-op run). Malformed JSON / non-object →
    ``AR_VALIDATION`` (§9). Field-shape checks (lists, ``execution_time``) run
    in ``_validate_request``.
    """
    text = (user_input or "").strip()
    if not text:
        return None, {"code": "AR_VALIDATION",
                      "message": "no audit request supplied"}
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, {"code": "AR_VALIDATION",
                      "message": f"request JSON parse error: {exc}"}
    if not isinstance(obj, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "request must be a JSON object"}
    return obj, None


def _validate_request(req: dict, trace_id: str) -> tuple[dict, Optional[dict]]:
    """Validate the ``AuditRequest`` wrapper shape; build the validation report.

    Returns ``(validation_report, error)``. Each list field, if present, must be a
    list; ``execution_time``, if present, must be an object with ISO
    ``started_at``/``ended_at``. All artifact lists are optional. Any error →
    ``AR_VALIDATION`` with a structured error map (no audit attempted).
    """
    errors: list[dict] = []
    for k in LIST_FIELDS:
        v = req.get(k)
        if v is None:
            continue
        if not isinstance(v, list):
            errors.append({"path": k, "message": f"{k} must be an array"})
    et = req.get("execution_time")
    if et is not None:
        if not isinstance(et, dict):
            errors.append({"path": "execution_time",
                           "message": "execution_time must be an object"})
        else:
            for k in ("started_at", "ended_at"):
                sv = et.get(k)
                if not isinstance(sv, str) or not RE_TS.match(sv):
                    errors.append({"path": f"execution_time.{k}",
                                   "message": f"execution_time.{k} must be "
                                              "ISO-8601 Z (YYYY-MM-DDTHH:MM:SSZ)"})
    report = {
        "valid": not errors,
        "contract_name": "AuditRequest",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": errors,
    }
    if errors:
        return report, {"code": "AR_VALIDATION",
                        "message": errors[0].get("message",
                                                 "audit request validation failed")}
    return report, None


# --------------------------------------------------------------------------- #
#  Collect (pure) — normalize the bundle into state fields + summary counts.
# --------------------------------------------------------------------------- #


def _collect(req: dict) -> dict:
    """Extract the artifact lists + ``execution_time`` + summary counts.

    Returns a partial-update dict for the ``collect`` node. ``subflows_invoked``
    = the unique ``flow_id``s from ``execution_history`` (in first-appearance
    order). ``totals`` = the ``totals`` of the last ``calculation_result`` that
    carries one (2dp-coerced), else ``{"matched":"0.00","outstanding":"0.00",
    "posted":"0.00"}``.
    """
    lists = {k: list(req.get(k) or []) for k in LIST_FIELDS}
    et = req.get("execution_time")
    et = et if isinstance(et, dict) else None
    subflows: list[str] = []
    for ev in lists["execution_history"]:
        if isinstance(ev, dict):
            fid = str(ev.get("flow_id", "") or "")
            if fid and fid not in subflows:
                subflows.append(fid)
    totals = {"matched": "0.00", "outstanding": "0.00", "posted": "0.00"}
    for cr in lists["calculation_results"]:
        if isinstance(cr, dict) and isinstance(cr.get("totals"), dict):
            t = cr["totals"]
            totals = {
                "matched": _to_2dp(t.get("matched", "0.00")),
                "outstanding": _to_2dp(t.get("outstanding", "0.00")),
                "posted": _to_2dp(t.get("posted", "0.00")),
            }
    counts = {
        "n_invoices": len(lists["invoices"]),
        "n_approvals": len(lists["approvals"]),
        "n_zoho_uploads": len(lists["zoho_upload_results"]),
        "n_calc_results": len(lists["calculation_results"]),
        "n_val_reports": len(lists["validation_reports"]),
        "n_input_files": len(lists["input_files"]),
        "n_errors": len(lists["errors"]),
        "n_warnings": len(lists["warnings"]),
        "n_subflows": len(subflows),
        "n_execution_history": len(lists["execution_history"]),
    }
    return {
        **lists,
        "execution_time": et,
        "subflows_invoked": subflows,
        "totals": totals,
        "summary_counts": counts,
    }


# --------------------------------------------------------------------------- #
#  Audit-record synthesis (pure, §13). before/after carry scalar-only values
#  (audit-record $defs/state_delta allows string/number/boolean/null).
# --------------------------------------------------------------------------- #


def _scalar_after(d: Any, keys: tuple[str, ...]) -> dict:
    """Project ``keys`` from ``d`` into a scalar-only ``after`` dict.

    Drops values that are not str/int/float/bool/None (the state_delta contract
    forbids nested objects/arrays), so ``totals``/``execution_time``/
    ``subflows_invoked`` must be flattened by the caller.
    """
    out: dict[str, Any] = {}
    if not isinstance(d, dict):
        return out
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if isinstance(v, bool) or v is None or isinstance(v, (int, float, str)):
            out[k] = v
    return out


def _build_audit_record(*, audit_id: str, trace_id: str, tenant: str,
                        actor: str, action: str, timestamp: str,
                        source_system: str = "", source_ref: str = "",
                        approval_ref: str = "", idempotency_key: str = "",
                        correlation_id: str = "", before: Optional[dict] = None,
                        after: Optional[dict] = None) -> dict[str, Any]:
    """Build an append-only ``AuditRecord`` (§13).

    ``append_only=true``; ``actor`` = the Keycloak sub (``"unknown"`` when
    unattributed — the schema requires minLength 1); ``source_system`` is set
    only when it is a zoho/foodics value; optional keys are emitted only when
    set. ``before``/``after`` carry **scalar-only** values.
    """
    rec: dict[str, Any] = {
        "audit_id": audit_id,
        "trace_id": trace_id,
        "tenant": tenant,
        "actor": actor or "unknown",
        "action": action,
        "timestamp": timestamp,
        "append_only": True,
        "contract_version": CONTRACT_VERSION,
    }
    if approval_ref:
        rec["approval_ref"] = approval_ref
    if idempotency_key:
        rec["idempotency_key"] = idempotency_key
    if source_system in SOURCE_SYSTEMS:
        rec["source_system"] = source_system
    if source_ref:
        rec["source_ref"] = str(source_ref)
    if correlation_id:
        rec["correlation_id"] = str(correlation_id)
    if before:
        rec["before"] = before
    if after:
        rec["after"] = after
    return rec


def _build_audit_log(state: AuditFlowState) -> list[dict]:
    """Synthesize the immutable audit log (§13) from the collected bundle.

    One ``AuditRecord`` per artifact (in a stable order: input_files,
    validation_reports, calculation_results, invoices, approvals,
    zoho_upload_results) + a terminal ``audit.summary`` record. Each has a
    deterministic uuid5 ``audit_id`` and ``correlation_id``=trace_id.
    ``before``/``after`` are scalar-only (state_delta). ``source_system`` is
    set only on zoho/foodics records.
    """
    tid = state.trace_id
    tenant = state.tenant
    actor = state.actor or ""
    ts = utc_now()
    correlation_id = tid
    records: list[dict] = []

    # input_files → file.intake (source_system only when the file's source is
    # zoho/foodics).
    for i, f in enumerate(state.input_files):
        f = f if isinstance(f, dict) else {}
        src = str(f.get("source", "") or "")
        file_ref = str(f.get("file_ref", "") or f.get("id", "") or "")
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"file:{i}:{file_ref}"),
            trace_id=tid, tenant=tenant, actor=actor, action="file.intake",
            timestamp=ts, correlation_id=correlation_id,
            source_system=src if src in SOURCE_SYSTEMS else "",
            source_ref=file_ref,
            before=None,
            after=_scalar_after(
                {**f, "file_ref": file_ref}, ("file_ref", "doc_type", "source")),
        ))

    # validation_reports → validation.report.
    for i, vr in enumerate(state.validation_reports):
        vr = vr if isinstance(vr, dict) else {}
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"validation:{i}"),
            trace_id=tid, tenant=tenant, actor=actor,
            action="validation.report", timestamp=ts,
            correlation_id=correlation_id, before=None,
            after=_scalar_after(
                {**vr, "n_errors": len(vr.get("errors", []) or []),
                 "n_warnings": len(vr.get("warnings", []) or [])},
                ("contract_name", "valid", "n_errors", "n_warnings")),
        ))

    # calculation_results → calculation.result (totals flattened to scalars).
    for i, cr in enumerate(state.calculation_results):
        cr = cr if isinstance(cr, dict) else {}
        t = cr.get("totals") if isinstance(cr.get("totals"), dict) else {}
        after = _scalar_after(cr, ("result_type",))
        after["matched"] = _to_2dp(t.get("matched", "0.00"))
        after["outstanding"] = _to_2dp(t.get("outstanding", "0.00"))
        after["posted"] = _to_2dp(t.get("posted", "0.00"))
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"calc:{i}"),
            trace_id=tid, tenant=tenant, actor=actor,
            action="calculation.result", timestamp=ts,
            correlation_id=correlation_id, before=None, after=after,
        ))

    # invoices → invoice.generated (internal; no source_system).
    for i, inv in enumerate(state.invoices):
        inv = inv if isinstance(inv, dict) else {}
        iid = str(inv.get("invoice_id", "") or "")
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"invoice:{i}:{iid}"),
            trace_id=tid, tenant=tenant, actor=actor,
            action="invoice.generated", timestamp=ts,
            correlation_id=correlation_id,
            before={"status": "draft"},
            after=_scalar_after(
                {**inv, "invoice_id": iid},
                ("invoice_id", "status", "total", "currency")),
        ))

    # approvals → approval.decision (approval_ref link; no source_system).
    for i, ap in enumerate(state.approvals):
        ap = ap if isinstance(ap, dict) else {}
        aref = str(ap.get("approval_ref", "") or "")
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"approval:{i}:{aref}"),
            trace_id=tid, tenant=tenant, actor=actor,
            action="approval.decision", timestamp=ts,
            approval_ref=aref, correlation_id=correlation_id,
            before={"status": "pending"},
            after=_scalar_after(ap, ("decision", "decided_by")),
        ))

    # zoho_upload_results → invoice.issue (source_system="zoho").
    for i, zr in enumerate(state.zoho_upload_results):
        zr = zr if isinstance(zr, dict) else {}
        zid = str(zr.get("zoho_id", "") or "")
        records.append(_build_audit_record(
            audit_id=_audit_ref(tid, f"zoho_upload:{i}:{zid}"),
            trace_id=tid, tenant=tenant, actor=actor,
            action="invoice.issue", timestamp=ts, correlation_id=correlation_id,
            source_system="zoho", source_ref=zid,
            idempotency_key=str(zr.get("idempotency_key", "") or ""),
            before=None,
            after=_scalar_after(zr, ("code", "zoho_id", "duplicate")),
        ))

    # Terminal audit.summary (before={}, after=scalar-only run metrics).
    counts = state.summary_counts or {}
    totals = state.totals or {"matched": "0.00", "outstanding": "0.00",
                              "posted": "0.00"}
    et = state.execution_time or {}
    summary_after: dict[str, Any] = {
        "n_records": len(records),
        "matched": _to_2dp(totals.get("matched", "0.00")),
        "outstanding": _to_2dp(totals.get("outstanding", "0.00")),
        "posted": _to_2dp(totals.get("posted", "0.00")),
        "n_errors": int(counts.get("n_errors", 0) or 0),
        "n_warnings": int(counts.get("n_warnings", 0) or 0),
        "subflows_count": int(counts.get("n_subflows", 0) or 0),
    }
    dm = et.get("duration_ms") if isinstance(et, dict) else None
    if isinstance(dm, (int, float)) and not isinstance(dm, bool):
        summary_after["duration_ms"] = int(dm)
    records.append(_build_audit_record(
        audit_id=_audit_ref(tid, "audit.summary"),
        trace_id=tid, tenant=tenant, actor=actor, action="audit.summary",
        timestamp=ts, correlation_id=correlation_id,
        before={}, after=summary_after,
    ))
    return records


# --------------------------------------------------------------------------- #
#  ExecutionSummary + WorkflowState builders (pure).
# --------------------------------------------------------------------------- #


def _run_window(state: AuditFlowState) -> tuple[str, str]:
    """Resolve ``(started_at, ended_at)`` from ``execution_time`` or state."""
    et = state.execution_time if isinstance(state.execution_time, dict) else {}
    started = str(et.get("started_at", "") or "")
    ended = str(et.get("ended_at", "") or "")
    if not RE_TS.match(started):
        started = state.created_at or utc_now()
    if not RE_TS.match(ended):
        ended = state.updated_at or utc_now()
    return started, ended


def build_execution_summary(state: AuditFlowState) -> dict[str, Any]:
    """Build the ``ExecutionSummary`` contract (§14).

    ``intent="ar_audit"``; ``status="ok"``; ``code="AR_OK"``; ``totals`` from
    collected totals or ``"0.00"``; ``approvals`` = the approval_refs;
    ``audit_refs``; ``checkpoint_id`` = the aggregate ``ar_audit`` ref;
    ``subflows_invoked``; ``contract_version``. Only schema-known keys are
    emitted (``additionalProperties:false``).
    """
    totals = state.totals or {"matched": "0.00", "outstanding": "0.00",
                              "posted": "0.00"}
    started, ended = _run_window(state)
    approvals: list[str] = []
    for ap in state.approvals:
        if isinstance(ap, dict):
            aref = str(ap.get("approval_ref", "") or "")
            if aref:
                approvals.append(aref)
    summary: dict[str, Any] = {
        "trace_id": state.trace_id,
        "flow_id": state.flow_id,
        "tenant": state.tenant,
        "intent": FLOW_ID,
        "status": "ok",
        "code": CODE_OK,
        "totals": {
            "matched": _to_2dp(totals.get("matched", "0.00")),
            "outstanding": _to_2dp(totals.get("outstanding", "0.00")),
            "posted": _to_2dp(totals.get("posted", "0.00")),
        },
        "started_at": started,
        "ended_at": ended,
        "audit_refs": list(state.audit_refs),
        "subflows_invoked": list(state.subflows_invoked),
        "contract_version": CONTRACT_VERSION,
    }
    if approvals:
        summary["approvals"] = approvals
    cp_ref = _audit_ref(state.trace_id, FLOW_ID)
    summary["checkpoint_id"] = cp_ref
    return summary


def build_workflow_state(state: AuditFlowState) -> dict[str, Any]:
    """Build a ``WorkflowState`` snapshot (§8, immutable).

    ``status="completed"``; ``intent="ar_audit"``; totals (or ``"0.00"``);
    ``pending_approvals=[]`` (read-only, no gate); ``idempotency_keys={}``
    (read-only, no idempotency); ``audit_refs``; ``contract_version``.
    """
    totals = state.totals or {"matched": "0.00", "outstanding": "0.00",
                              "posted": "0.00"}
    created_at, _ = _run_window(state)
    return {
        "trace_id": state.trace_id,
        "flow_id": state.flow_id,
        "tenant": state.tenant,
        "intent": FLOW_ID,
        "status": "completed",
        "matched_amount": _to_2dp(totals.get("matched", "0.00")),
        "outstanding_balance": _to_2dp(totals.get("outstanding", "0.00")),
        "posted_total": _to_2dp(totals.get("posted", "0.00")),
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(state.audit_refs),
        "tool_call_ref": f"{state.trace_id}:{FLOW_ID}:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": state.created_at or created_at,
        "updated_at": state.updated_at or utc_now(),
    }


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[AuditFlowContext]) -> AuditFlowContext:
    return runtime.context or {}


def _node_ingest(state: AuditFlowState,
                 runtime: Runtime[AuditFlowContext]) -> dict:
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
    actor = str(req.get("actor", "") or ctx.get("actor", "") or "")
    return {
        "trace_id": trace_id,
        "flow_id": state.flow_id or ctx.get("flow_id", FLOW_ID),
        "tenant": tenant,
        "actor": actor,
        "request": req,
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_validate(state: AuditFlowState,
                   runtime: Runtime[AuditFlowContext]) -> dict:
    _ = _ctx(runtime)
    req = state.request or {}
    report, err = _validate_request(req, state.trace_id)
    if err is not None:
        return {"status": "failed", "error": err, "updated_at": utc_now()}
    audit_refs, checkpoints = _record_checkpoint(state, "validate")
    return {"audit_refs": audit_refs, "checkpoints": checkpoints,
            "status": "validated", "updated_at": utc_now()}


def _node_collect(state: AuditFlowState,
                  runtime: Runtime[AuditFlowContext]) -> dict:
    _ = _ctx(runtime)
    upd = _collect(state.request or {})
    audit_refs, checkpoints = _record_checkpoint(state, "collect")
    upd.update({"audit_refs": audit_refs, "checkpoints": checkpoints,
                "status": "collected", "updated_at": utc_now()})
    return upd


def _node_build_audit_log(state: AuditFlowState,
                          runtime: Runtime[AuditFlowContext]) -> dict:
    _ = _ctx(runtime)
    records = _build_audit_log(state)
    audit_refs = list(state.audit_refs)
    for r in records:
        aid = r.get("audit_id")
        if aid and aid not in audit_refs:
            audit_refs.append(aid)
    refs, checkpoints = _record_checkpoint(
        dataclasses.replace(state, audit_refs=audit_refs), "audit_log")
    return {"audit_log": records, "audit_refs": refs,
            "checkpoints": checkpoints, "status": "audited",
            "updated_at": utc_now()}


def _node_build_execution_summary(state: AuditFlowState,
                                  runtime: Runtime[AuditFlowContext]) -> dict:
    _ = _ctx(runtime)
    summary = build_execution_summary(state)
    audit_refs, checkpoints = _record_checkpoint(state, "summary")
    return {"execution_summary": summary, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "summarized",
            "updated_at": utc_now()}


def _node_build_state(state: AuditFlowState,
                      runtime: Runtime[AuditFlowContext]) -> dict:
    _ = _ctx(runtime)
    ws = build_workflow_state(state)
    audit_refs, checkpoints = _record_checkpoint(state, "state")
    return {"workflow_state": ws, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "stated",
            "updated_at": utc_now()}


def _node_checkpoint(state: AuditFlowState,
                     runtime: Runtime[AuditFlowContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints."""
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, FLOW_ID)
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "status": "completed",
            "updated_at": utc_now()}


def _node_respond(state: AuditFlowState,
                  runtime: Runtime[AuditFlowContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# Conditional routers (return state.status against status-keyed path maps).
def _after_ingest(state: AuditFlowState) -> str:
    return state.status


def _after_validate(state: AuditFlowState) -> str:
    return state.status


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class AuditFlowComponent(Component):
    name = "AuditFlowComponent"
    display_name = "Cosmic AR Audit Flow"
    description = (
        "The Audit Flow for the Cosmic AR Agent (ar_audit, the 9th subflow): "
        "collects a run's execution history, input files, validation reports, "
        "calculation results, invoices, approvals, Zoho upload results, "
        "execution time, errors, and warnings (a validated-JSON AuditRequest "
        "wrapper) → synthesizes an immutable §13 audit log (append-only "
        "AuditRecords) → generates an ExecutionSummary → returns the Audit "
        "JSON + updates WorkflowState in the §14 envelope. Read-only audit "
        "emission — no §1 approval gate, no transport (Postgres/Langfuse "
        "persistence build-phase). Constitution §8/§9/§11/§13/§14/§15/§16. "
        "See ADR-0012."
    )
    icon = "FileClock"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="Audit Request (JSON)",
            info=(
                "The validated-JSON AuditRequest wrapper: {trace_id?, tenant?, "
                "actor? (Keycloak sub — §13), execution_history?[], input_files?[], "
                "validation_reports?[], calculation_results?[], invoices?[], "
                "approvals?[], zoho_upload_results?[], execution_time? "
                "{started_at, ended_at, duration_ms?}, errors?[], warnings?[]}. "
                "All artifact lists are optional — an empty bundle audits an "
                "empty/no-op run. PRIMARY input."
            ),
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic aggregation; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="audit_output",
            display_name="Audit Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=AuditFlowState,
                           context_schema=AuditFlowContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("validate", _node_validate)
        graph.add_node("collect", _node_collect)
        graph.add_node("build_audit_log", _node_build_audit_log)
        graph.add_node("build_execution_summary", _node_build_execution_summary)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _after_ingest,
                                    {"failed": "respond", "created": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond",
                                     "validated": "collect"})
        # The remaining nodes are deterministic compute → static edges (unexpected
        # errors caught at the run() boundary → AR_UNEXPECTED).
        graph.add_edge("collect", "build_audit_log")
        graph.add_edge("build_audit_log", "build_execution_summary")
        graph.add_edge("build_execution_summary", "build_state")
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
            ctx: AuditFlowContext = {
                "user_input": user_input,
                "actor": actor,
                "session_id": session_id,
                "tenant": DEFAULT_TENANT,
                "flow_id": FLOW_ID,
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = AuditFlowState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            n_records = ""
            log = envelope.get("data", {}).get("audit_log")
            if isinstance(log, list):
                n_records = len(log)
            self.log(
                f"event=audit.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=audit n_records={n_records} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", CODE_UNEXPECTED,
                            error={"message": "Audit flow run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id=mint_id())
            try:
                self.log("event=audit.run outcome=error code=AR_UNEXPECTED")
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
        ``workflow_state.status=="failed"``. No ``pending_approval`` branch (no
        in-flow interrupt). The audit log + collected bundle surface via the
        envelope (``data.audit_log`` + ``data.*`` echoes); no ``AgentState``
        schema change (ADR-0012 — mirrors ADR-0006/0007/0008/0009/0010/0011).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        audit_refs = vals.get("audit_refs") or []
        ws = vals.get("workflow_state") or {}
        data: dict[str, Any] = {
            "audit_log": vals.get("audit_log") or [],
            "execution_summary": vals.get("execution_summary") or {},
            "execution_history": vals.get("execution_history") or [],
            "input_files": vals.get("input_files") or [],
            "validation_reports": vals.get("validation_reports") or [],
            "calculation_results": vals.get("calculation_results") or [],
            "invoices": vals.get("invoices") or [],
            "approvals": vals.get("approvals") or [],
            "zoho_upload_results": vals.get("zoho_upload_results") or [],
            "execution_time": vals.get("execution_time") or {},
            "errors": vals.get("errors") or [],
            "warnings": vals.get("warnings") or [],
            "summary_counts": vals.get("summary_counts") or {},
            "subflows_invoked": vals.get("subflows_invoked") or [],
            "workflow_state": ws,
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
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
            err = vals.get("error") or {"code": CODE_UNEXPECTED,
                                         "message": "audit flow failed"}
            code = err.get("code", CODE_UNEXPECTED) if isinstance(err, dict) \
                else CODE_UNEXPECTED
            err_env = {"message": err.get("message", "") if isinstance(err, dict) else str(err)}
            if isinstance(err, dict) and err.get("detail"):
                err_env["detail"] = err["detail"]
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err_env,
                    "flow_id": vals.get("flow_id", "")}
        return {"status": "ok", "code": CODE_OK, "trace_id": trace_id,
                "data": data, "flow_id": vals.get("flow_id", "")}


# Guard so importing the module (for the self-test) does not execute main logic.
if __name__ == "__main__":  # pragma: no cover
    dataclasses  # noqa: B018 — keep the import live for tooling that prunes
    raise SystemExit("This is a LangFlow component module; import it, do not run it.")