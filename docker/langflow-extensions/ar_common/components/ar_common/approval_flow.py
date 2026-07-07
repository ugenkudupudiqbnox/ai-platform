"""Cosmic AR Agent — Human Approval Flow component (constitution §8/§19,
architecture §4 row 9).

The Human Approval Flow is the 9th AR subflow (ADR-0010). It is the
**presentational approval surface** for the AR Agent: an operator/upstream
caller submits a **validated-JSON review packet** (the Revenue/Expense/Invoice
summaries + the approval proposal), the flow **pauses** (constitution §19
``interrupt()``), **presents** the packet, **captures** an Approve / Reject /
Request-Changes decision on **resume**, **updates WorkflowState**, and **logs**
an ``AuditRecord`` (§13) — then returns the ``ApprovalResult`` in the §14
envelope.

This implements ``prompts/P13_approval_flow.md`` verbatim:

  Pause execution. Present [Revenue Summary / Expense Summary / Invoice Summary
  / Validation Report]. Allow [Approve / Reject / Request Changes]. Resume
  execution. Update Workflow State. Log all approvals.

It is the **single stateful orchestrator** for human approval, mirroring the
supervisor and the other AR subflows: its responsibilities map to LangGraph
nodes inside one ``lfx`` component, ``HumanApprovalFlowComponent``.

**Standalone presentational flow (ADR-0010 §2).** The supervisor already has an
*internal* ``_node_gate`` that calls ``interrupt()``/``Command(resume=...)`` for
approval-tier intents *mid-supervisor-run*. This subflow is a **separate,
direct-invocation surface** (its own ``flow_id`` ``ar_approval``) — it does not
edit ``supervisor.py`` / ``supervisor.json`` (which already pre-wire
``ar_approval``). The supervisor resume-path interaction (routing the resume
through this subflow vs the internal gate) is a documented build-phase
integration item needing live LangGraph ``Flow-as-Tool`` + ``interrupt``
propagation testing. This is the **first *subflow* to use
``Command``/``interrupt``** (previously supervisor-only).

Responsibilities → LangGraph nodes:

  ingest → assemble_packet → request_approval (interrupt) → update_state →
  audit → checkpoint → respond

  - ingest            : parse the review-packet JSON from ``user_input``; bind
                        ``trace_id``/``flow_id``/``tenant`` + timestamps; carry
                        ``tier``-override + ``model_name`` in **context** (§8).
                        Malformed JSON / non-object / missing ``action`` or
                        ``proposal`` → ``AR_VALIDATION``.                  §9
  - assemble_packet   : build the ``ApprovalRequest`` (contract) + the
                        presentation ``packet`` (the 4 summaries + proposal);
                        mint the deterministic ``approval_ref``. **Records a
                        checkpoint** ``"packet"``.                            §11
  - request_approval  : **PAUSE + CAPTURE (§19).** ``interrupt(payload)``
                        presents the packet + the 3 options. On first run the
                        graph suspends here → ``run()`` emits ``pending_approval``.
                        On resume ``interrupt()`` returns the decision (a dict
                        ``{decision, decided_by, reason}`` or a reply string) and
                        the node completes. Invalid/missing decision →
                        ``AR_FORBIDDEN``. **Records a checkpoint** ``"decision"``. §19
  - update_state      : build the ``ApprovalResult`` (``consumed=false`` — the
                        authorized POST is a separate flow's job, §19
                        non-reusable) + the ``WorkflowState`` snapshot
                        (status="completed" regardless of decision; totals
                        ``"0.00"`` — no money moves; ``pending_approvals=[]``).
                        Immutable (§8). **Records a checkpoint** ``"state"``.
  - audit             : **Log all approvals (§13).** One ``AuditRecord`` per
                        decision (approved/rejected/request_changes), actor =
                        ``decided_by`` (Keycloak sub), ``append_only=true``,
                        ``approval_ref`` link, ``before``/``after`` delta.
                        **Records a checkpoint** ``"audit"``.
  - checkpoint        : append the final aggregate audit id; reflect
                        ``audit_refs`` + ``checkpoints`` into the snapshot.
                        ``InMemorySaver`` persists state.                     §11
  - respond           : ``_finalize_envelope`` builds the §14 envelope (or
                        ``pending_approval`` when paused at the gate).       §14

**Checkpoints** after ``packet``/``decision``/``state``/``audit`` + aggregate
``ar_approval`` (§11 "after every human-approval gate"; the ``interrupt`` itself
persists via ``InMemorySaver``).

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id`` (the
§11 fallback — non-durable v1). The decision surfaces to the supervisor only
via the envelope (``data.approval_result`` + ``data.audit_refs``); **no
``AgentState`` schema change** (mirrors ADR-0006/0007/0008/0009).

The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope. No PII/secrets (§12/§16).
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
from langgraph.types import Command, interrupt

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1).
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors calculation)
FLOW_ID: str = "ar_approval"
DEFAULT_TENANT: str = "cosmic-vikings"

# The 3 capture options presented at the gate (§19 / prompt P13).
DECISIONS: tuple[str, ...] = ("approved", "rejected", "request_changes")
OPTIONS: tuple[str, ...] = ("approve", "reject", "request_changes")

# Approval-reference regex (matches the contracts' ar-approval-<uuid> shape).
APPROVAL_REF_RE = re.compile(
    r"ar-approval-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# 2dp / currency patterns (the contracts' patterns).
_TWO_PLACES = Decimal("0.01")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class ApprovalFlowContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[ApprovalFlowContext]``.

    Durable, resumable state lives in ``ApprovalFlowState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)
    tier: str  # tier override (packet.tier wins; mirrors ApprovalGateComponent)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApprovalFlowState:
    """The Human Approval Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|assembled|decided|state_updated|audited|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    packet: Optional[dict] = None  # the parsed review-packet input
    approval_request: Optional[dict] = None  # ApprovalRequest (contract)
    approval_ref: Optional[str] = None
    decision: Optional[str] = None  # approved | rejected | request_changes
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    reason: Optional[str] = None
    approval_result: Optional[dict] = None  # ApprovalResult (contract)
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    audit_records: list = field(default_factory=list)
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<label>: audit_ref} (§11)


def _state_to_dict(state: Any) -> dict:
    """Coerce an ``ApprovalFlowState`` (or dict) snapshot to a plain dict."""
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
    """A fresh lowercase uuid4 string (approval_id / trace_id seed)."""
    return str(uuid.uuid4())


def detect_approval_ref(text: str) -> Optional[str]:
    """Return the first ``ar-approval-<uuid>`` in ``text``, or None."""
    if not text:
        return None
    match = APPROVAL_REF_RE.search(text)
    return match.group(0) if match else None


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


def _to_signed_2dp(value: Any) -> str:
    """Coerce a numeric to a signed 2dp string (allows negatives)."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    return f"{d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)}"


def _to_2dp(value: Any) -> str:
    """Coerce a numeric to a non-negative 2dp string; ``"0.00"`` on failure."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    q = d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if q < 0:
        q = Decimal("0.00")
    return f"{q}"


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-calculation audit record id (§11/§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"approval-audit:{trace_id}:{label}"))


# --------------------------------------------------------------------------- #
#  Packet parse / approval-request / packet / decision (pure).
# --------------------------------------------------------------------------- #


def _parse_packet(user_input: str) -> tuple[Optional[dict], Optional[dict]]:
    """Parse the review-packet JSON from ``user_input``.

    Returns ``(packet, error)`` — exactly one is set. Malformed JSON / non-object
    / missing ``action`` or ``proposal`` → ``AR_VALIDATION`` (§9).
    """
    text = (user_input or "").strip()
    if not text:
        return None, {"code": "AR_VALIDATION",
                      "message": "no review-packet JSON supplied"}
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, {"code": "AR_VALIDATION",
                      "message": f"review-packet JSON parse error: {exc}"}
    if not isinstance(obj, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "review packet must be a JSON object"}
    action = obj.get("action")
    if not isinstance(action, str) or not action.strip():
        return None, {"code": "AR_VALIDATION",
                      "message": "review packet missing required 'action'"}
    proposal = obj.get("proposal")
    if not isinstance(proposal, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "review packet missing required 'proposal' object"}
    return obj, None


def _pick_tier(packet: dict, ctx_tier: str) -> str:
    """tier resolution: packet.tier > context override > default 'approval'."""
    t = packet.get("tier")
    if isinstance(t, str) and t.strip():
        return t.strip()
    if ctx_tier and ctx_tier.strip():
        return ctx_tier.strip()
    return "approval"


def _build_approval_request(packet: dict, trace_id: str, tenant: str,
                            ctx_actor: str, ctx_tier: str) -> dict[str, Any]:
    """Build the ``ApprovalRequest`` (contract) from the review packet.

    ``approval_ref = f"ar-approval-{approval_id}"`` (deterministic shape, uuid4
    mint — non-reusable per §19, mirroring the supervisor's gate).
    """
    approval_id = mint_id()
    currency = str(packet.get("currency") or DEFAULT_CURRENCY)
    if not RE_CURRENCY.match(currency):
        currency = DEFAULT_CURRENCY
    requested_by = (str(packet.get("requested_by") or ctx_actor or "unknown")
                    if (packet.get("requested_by") or ctx_actor) else "unknown")
    return {
        "approval_id": approval_id,
        "approval_ref": f"ar-approval-{approval_id}",
        "trace_id": trace_id,
        "tenant": tenant,
        "action": str(packet.get("action") or ""),
        "amount": _to_2dp(packet.get("amount")),
        "currency": currency,
        "tier": _pick_tier(packet, ctx_tier),
        "requested_by": requested_by,
        "requested_at": utc_now(),
        "proposal": packet.get("proposal") or {},
        "idempotency_key": packet.get("idempotency_key"),
        "second_approver_required": bool(packet.get("second_approver_required", False)),
        "contract_version": CONTRACT_VERSION,
    }


def _build_packet(packet: dict, approval_request: dict) -> dict[str, Any]:
    """Build the presentation packet (the 4 summaries + proposal) shown at the gate.

    Summaries are pass-through from the caller's review packet — the flow presents
    whatever the caller supplied (each optional).
    """
    summaries_src = packet.get("summaries")
    summaries = summaries_src if isinstance(summaries_src, dict) else {}
    return {
        "approval_ref": approval_request["approval_ref"],
        "action": approval_request["action"],
        "tier": approval_request["tier"],
        "amount": approval_request["amount"],
        "currency": approval_request["currency"],
        "proposal": approval_request["proposal"],
        "summaries": {
            "revenue_summary": summaries.get("revenue_summary"),
            "expense_summary": summaries.get("expense_summary"),
            "invoice_summary": summaries.get("invoice_summary"),
            "validation_report": summaries.get("validation_report"),
        },
    }


def _normalize_decision(raw: Any) -> Optional[str]:
    """Map a decision token to its canonical value; ``None`` if unrecognized."""
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(" ", "_")
    if s in ("approve", "approved", "accept", "accepted", "ok", "yes"):
        return "approved"
    if s in ("reject", "rejected", "deny", "denied", "no", "decline", "declined"):
        return "rejected"
    if s in ("request_changes", "requestchanges", "change", "changes",
             "revise", "request"):
        return "request_changes"
    return None


def _decision_from_text(text: str) -> tuple[Optional[str], str]:
    """Map a reply's leading verb phrase → ``(decision, reason)``.

    ``reason`` is the remainder after the verb phrase. ``(None, "")`` when no
    approval verb leads the reply. Multi-word ``"request changes"`` is matched
    before single-token verbs.
    """
    body = (text or "").strip()
    if not body:
        return None, ""
    low = body.lower()
    # Multi-word verb first so "request changes" is not split into "request".
    for verb in ("request changes", "request_changes", "requestchanges"):
        if low.startswith(verb):
            rest = body[len(verb):].strip()
            return "request_changes", rest
    parts = body.split(None, 1)
    first = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if first in ("approve", "approved", "accept", "accepted", "ok", "yes"):
        return "approved", rest
    if first in ("reject", "rejected", "deny", "denied", "no", "decline",
                 "declined"):
        return "rejected", rest
    if first in ("changes", "change", "revise", "request"):
        return "request_changes", rest
    return None, rest


def _parse_decision_reply(user_input: str, actor: str) -> dict[str, Any]:
    """Extract ``{decision, decided_by, reason}`` from a resume reply.

    The reply carries the ``approval_ref`` and a leading verb: e.g.
    ``"approve ar-approval-<uuid>"`` or ``"reject <ref> amount looks wrong"``.
    The ref is stripped before parsing so the verb leads and the reason is
    ref-free. ``decided_by`` is the run actor (Keycloak sub — §13).
    """
    text = (user_input or "").strip()
    ref = detect_approval_ref(text)
    body = text.replace(ref, "", 1).strip() if ref else text
    decision, reason = _decision_from_text(body)
    return {"decision": decision,
            "decided_by": actor or "unknown",
            "reason": (reason or "").strip()}


def _coerce_resume(resumed: Any, actor: str) -> tuple[Optional[str], str, str]:
    """Coerce the ``interrupt()`` resume value → ``(decision, decided_by, reason)``.

    Accepts a dict ``{decision, decided_by, reason}`` (the canonical resume
    shape) or a reply string (parsed via ``_parse_decision_reply``). A missing /
    invalid decision → ``(None, …)`` (the gate fails safe to ``AR_FORBIDDEN``).
    """
    if isinstance(resumed, dict):
        decision = _normalize_decision(resumed.get("decision"))
        decided_by = str(resumed.get("decided_by") or actor or "unknown")
        reason = str(resumed.get("reason") or "")
        return decision, decided_by, reason
    if isinstance(resumed, str):
        parsed = _parse_decision_reply(resumed, actor or "unknown")
        return parsed["decision"], parsed["decided_by"], parsed["reason"]
    return None, actor or "unknown", ""


def _build_approval_result(state: ApprovalFlowState) -> dict[str, Any]:
    """Build the ``ApprovalResult`` (contract) from the captured decision.

    ``consumed=false`` here — the authorized POST is a separate flow's job (§19
    non-reusable: one ``approval_ref`` = one idempotent action; ``consumed``
    flips on POST).
    """
    req = state.approval_request or {}
    return {
        "approval_id": req.get("approval_id", ""),
        "approval_ref": state.approval_ref or req.get("approval_ref", ""),
        "decision": state.decision,
        "decided_by": state.decided_by or "unknown",
        "decided_at": state.decided_at or utc_now(),
        "trace_id": state.trace_id,
        "tier": req.get("tier", "approval"),
        "idempotency_key": req.get("idempotency_key"),
        "reason": state.reason or "",
        "consumed": False,
        "contract_version": CONTRACT_VERSION,
    }


def _build_audit_record(state: ApprovalFlowState, audit_id: str) -> dict[str, Any]:
    """Build the append-only ``AuditRecord`` (§13) for one decision.

    ``actor = decided_by`` (Keycloak sub — §13); ``approval_ref`` links the §19
    approval that authorized it; ``before``/``after`` carry the decision delta.
    One record per decision (approved/rejected/request_changes all logged).
    """
    req = state.approval_request or {}
    action = req.get("action", "")
    return {
        "audit_id": audit_id,
        "trace_id": state.trace_id,
        "tenant": state.tenant,
        "actor": state.decided_by or "unknown",
        "action": f"approval.decision:{action}",
        "timestamp": state.decided_at or utc_now(),
        "append_only": True,
        "approval_ref": state.approval_ref or "",
        "idempotency_key": req.get("idempotency_key"),
        "before": {"status": "pending"},
        "after": {"decision": state.decision, "reason": state.reason or ""},
        "contract_version": CONTRACT_VERSION,
    }


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         audit_refs: list, created_at: str,
                         updated_at: str) -> dict[str, Any]:
    """Build a ``WorkflowState`` snapshot (§8, immutable).

    ``status="completed"`` regardless of decision (capturing a decision is
    terminal for the request). Totals ``"0.00"`` — no money moves (the flow
    captures a decision, it does not post). ``pending_approvals=[]`` (the
    approval is captured, not pending). ``intent="ar_approval"``.
    """
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": flow_id,
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:{flow_id}:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _record_checkpoint(state: ApprovalFlowState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[ApprovalFlowContext]) -> ApprovalFlowContext:
    return runtime.context or {}


def _node_ingest(state: ApprovalFlowState,
                 runtime: Runtime[ApprovalFlowContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    user_input = ctx.get("user_input", "")
    packet, err = _parse_packet(user_input)
    if err is not None:
        return {"status": "failed", "error": err,
                "packet": None, "created_at": now, "updated_at": now}
    trace_id = str(packet.get("trace_id") or state.trace_id or mint_id())
    tenant = str(packet.get("tenant") or state.tenant
                 or ctx.get("tenant", DEFAULT_TENANT))
    return {
        "trace_id": trace_id,
        "flow_id": state.flow_id or ctx.get("flow_id", FLOW_ID),
        "tenant": tenant,
        "packet": packet,
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_assemble_packet(state: ApprovalFlowState,
                          runtime: Runtime[ApprovalFlowContext]) -> dict:
    ctx = _ctx(runtime)
    packet = state.packet or {}
    ctx_tier = str(ctx.get("tier") or "")
    ctx_actor = str(ctx.get("actor") or "")
    approval_request = _build_approval_request(packet, state.trace_id,
                                               state.tenant, ctx_actor, ctx_tier)
    presentation = _build_packet(packet, approval_request)
    audit_refs, checkpoints = _record_checkpoint(state, "packet")
    return {
        "approval_request": approval_request,
        "approval_ref": approval_request["approval_ref"],
        "packet": presentation,
        "audit_refs": audit_refs,
        "checkpoints": checkpoints,
        "status": "assembled",
        "updated_at": utc_now(),
    }


def _node_request_approval(state: ApprovalFlowState,
                           runtime: Runtime[ApprovalFlowContext]) -> dict:
    """PAUSE + CAPTURE (§19). ``interrupt()`` presents the packet + 3 options.

    On first run the graph suspends here. On resume ``interrupt()`` returns the
    decision and the node completes. Invalid/missing decision → ``AR_FORBIDDEN``.
    """
    ctx = _ctx(runtime)
    req = state.approval_request or {}
    presentation = state.packet or {}
    payload = {
        "approval_ref": state.approval_ref or req.get("approval_ref", ""),
        "action": req.get("action", ""),
        "tier": req.get("tier", "approval"),
        "trace_id": state.trace_id,
        "packet": presentation,
        "options": list(OPTIONS),
    }
    resumed = interrupt(payload)
    actor = str(ctx.get("actor") or "")
    decision, decided_by, reason = _coerce_resume(resumed, actor)
    now = utc_now()
    if decision is None:
        err = {"code": "AR_FORBIDDEN",
               "message": "Approval decision missing or invalid."}
        audit_refs, checkpoints = _record_checkpoint(state, "decision")
        return {"status": "failed", "error": err,
                "decision": None, "decided_by": decided_by, "reason": reason,
                "audit_refs": audit_refs, "checkpoints": checkpoints,
                "updated_at": now}
    audit_refs, checkpoints = _record_checkpoint(state, "decision")
    return {
        "status": "decided",
        "decision": decision,
        "decided_by": decided_by,
        "decided_at": now,
        "reason": reason,
        "audit_refs": audit_refs,
        "checkpoints": checkpoints,
        "updated_at": now,
    }


def _node_update_state(state: ApprovalFlowState,
                       runtime: Runtime[ApprovalFlowContext]) -> dict:
    _ = _ctx(runtime)
    approval_result = _build_approval_result(state)
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    audit_refs, checkpoints = _record_checkpoint(state, "state")
    return {
        "approval_result": approval_result,
        "workflow_state": ws,
        "audit_refs": audit_refs,
        "checkpoints": checkpoints,
        "status": "state_updated",
        "updated_at": utc_now(),
    }


def _node_audit(state: ApprovalFlowState,
                runtime: Runtime[ApprovalFlowContext]) -> dict:
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, "audit")
    audit_id = checkpoints.get("audit", _audit_ref(state.trace_id, "audit"))
    record = _build_audit_record(state, audit_id)
    return {
        "audit_records": [*state.audit_records, record],
        "audit_refs": audit_refs,
        "checkpoints": checkpoints,
        "status": "audited",
        "updated_at": utc_now(),
    }


def _node_checkpoint(state: ApprovalFlowState,
                     runtime: Runtime[ApprovalFlowContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints."""
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, FLOW_ID)
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "status": "completed",
            "updated_at": utc_now()}


def _node_respond(state: ApprovalFlowState,
                  runtime: Runtime[ApprovalFlowContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# Conditional routers (return state.status against status-keyed path maps).
def _after_ingest(state: ApprovalFlowState) -> str:
    return state.status


def _after_request_approval(state: ApprovalFlowState) -> str:
    return state.status


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class HumanApprovalFlowComponent(Component):
    name = "HumanApprovalFlowComponent"
    display_name = "Cosmic AR Human Approval Flow"
    description = (
        "The presentational Human Approval Flow for the Cosmic AR Agent: takes a "
        "validated-JSON review packet (Revenue/Expense/Invoice summaries + the "
        "approval proposal), PAUSES via §19 interrupt, PRESENTS the packet, "
        "CAPTURES an Approve / Reject / Request-Changes decision on resume, "
        "UPDATES WorkflowState, and LOGS an audit record (§13) — then returns "
        "the ApprovalResult in the §14 envelope. The 9th AR subflow; standalone "
        "direct-invocation surface (no supervisor change); v1 uses InMemorySaver "
        "(non-durable). Constitution §1/§8/§9/§11/§13/§14/§16/§19. See ADR-0010."
    )
    icon = "ShieldCheck"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="Review Packet (JSON)",
            info=(
                "The validated-JSON review packet: {trace_id?, tenant?, action, "
                "amount?, currency?, tier?, requested_by?, proposal:{operation, "
                "target, amount?, currency?, details?}, idempotency_key?, "
                "summaries:{revenue_summary?, expense_summary?, "
                "invoice_summary?, validation_report?}}. PRIMARY input — carries "
                "the 4 summaries + the approval proposal. On the resume turn, "
                "carry the approval_ref + a leading verb "
                "(approve|reject|request changes)."
            ),
            required=True,
            tool_mode=True,
        ),
        DropdownInput(
            name="tier",
            display_name="Tier",
            options=["read-only", "auto", "approval", "dual-control"],
            value="approval",
            info=(
                "Approval-tier override (packet.tier wins). Mirrors "
                "ApprovalGateComponent. v1 uses single-approver; dual-control "
                "second-approver enforcement is build-phase."
            ),
            required=False,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic capture; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="approval_output",
            display_name="Approval Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=ApprovalFlowState,
                           context_schema=ApprovalFlowContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("assemble_packet", _node_assemble_packet)
        graph.add_node("request_approval", _node_request_approval)
        graph.add_node("update_state", _node_update_state)
        graph.add_node("audit", _node_audit)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _after_ingest,
                                    {"failed": "respond",
                                     "created": "assemble_packet"})
        graph.add_edge("assemble_packet", "request_approval")
        graph.add_conditional_edges("request_approval", _after_request_approval,
                                    {"failed": "respond",
                                     "decided": "update_state"})
        graph.add_edge("update_state", "audit")
        graph.add_edge("audit", "checkpoint")
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
            tier_override = _to_str(getattr(self, "tier", "")) or "approval"
            ctx: ApprovalFlowContext = {
                "user_input": user_input,
                "actor": actor,
                "session_id": session_id,
                "tenant": DEFAULT_TENANT,
                "flow_id": FLOW_ID,
                "model_name": model_name,
                "tier": tier_override,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            approval_ref = detect_approval_ref(user_input)
            # Resume path: a pending checkpoint exists and the user supplied an
            # approval_ref → continue past the gate's interrupt (§19/§11) with
            # the parsed decision payload.
            if approval_ref and self._has_pending_checkpoint(graph, config):
                resume_value = _parse_decision_reply(user_input, actor)
                graph.invoke(Command(resume=resume_value),
                             config=config, context=ctx)
            else:
                initial = ApprovalFlowState(
                    trace_id=mint_id(),
                    flow_id=ctx["flow_id"],
                    tenant=ctx["tenant"],
                )
                graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            decision = (envelope.get("data") or {}).get("decision", "")
            self.log(
                f"event=approval.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=approval decision={decision} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Approval run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=approval.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _has_pending_checkpoint(self, graph: Any, config: dict) -> bool:
        try:
            snapshot = graph.get_state(config)
            return bool(getattr(snapshot, "next", None))
        except Exception:  # noqa: BLE001 — no checkpoint ⇒ fresh run
            return False

    def _checkpoint_id(self, graph: Any, config: dict) -> str:
        """Best-effort read of the latest checkpoint id (§11) for the envelope."""
        try:
            snapshot = graph.get_state(config)
            cfg = getattr(snapshot, "config", None) or {}
            configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
            cid = configurable.get("checkpoint_id")
            if isinstance(cid, str) and cid:
                return cid
            parent = configurable.get("checkpoint_parent_id")
            return parent if isinstance(parent, str) else ""
        except Exception:  # noqa: BLE001
            return ""

    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state (and any pending interrupt) → §14 envelope.

        Deterministic from state. Paused-at-gate (interrupt) is detected via
        ``snapshot.next`` → ``pending_approval``. ``graph.get_state(config).values``
        is a plain dict (ADR-0003 §10), so fields are read by key. The decision
        surfaces via the envelope (``data.approval_result`` + ``data.audit_refs``);
        no ``AgentState`` schema change (ADR-0010).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        pending = getattr(snapshot, "next", None)
        audit_refs = vals.get("audit_refs") or []
        base: dict[str, Any] = {
            "trace_id": vals.get("trace_id", ""),
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoint_id": self._checkpoint_id(graph, config),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        # Paused at the gate ⇒ pending approval (§19).
        if pending:
            req = vals.get("approval_request") or {}
            presentation = vals.get("packet") or {}
            ref = vals.get("approval_ref", "") or req.get("approval_ref", "")
            env = dict(base)
            env.update({
                "status": "pending_approval",
                "code": "AR_APPROVAL_REQUIRED",
                "approval_ref": ref,
                "data": {
                    "action": req.get("action", ""),
                    "tier": req.get("tier", "approval"),
                    "packet": presentation,
                    "options": list(OPTIONS),
                    "checkpoint_id": self._checkpoint_id(graph, config),
                },
            })
            return env
        # Completed (ok) or failed.
        data: dict[str, Any] = {
            "approval_result": vals.get("approval_result") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "packet": vals.get("packet") or {},
            "audit_records": vals.get("audit_records") or [],
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "decision": vals.get("decision"),
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "approval failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            env = dict(base)
            env.update({"status": "error", "code": code,
                        "approval_ref": vals.get("approval_ref", ""),
                        "data": data, "error": err})
            return env
        env = dict(base)
        env.update({"status": "ok", "code": "AR_OK",
                    "approval_ref": vals.get("approval_ref", ""),
                    "data": data})
        return env