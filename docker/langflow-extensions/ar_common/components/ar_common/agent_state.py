"""Typed state schema for the Cosmic AR Agent (constitution §8).

This is the single, typed, resumable representation of an in-flight AR run,
owned by the supervisor's LangGraph graph. Nodes return *fragments* (immutable
updates); financial running totals are explicit named fields; pure tool
Components are stateless. This file defines the schema only — no logic.

See docs/cosmic-ar-architecture.md §7 for the field table and the resume-
determinism rule (reads are re-fetched on resume, never cached across runs).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Approval:
    """A structured pending-approval record (§8/§19).

    Stored in ``AgentState.pending_approvals`` as a structured record, never as
    free text. An approval is non-reusable (§19): one ``approval_id`` authorizes
    exactly one idempotent action.
    """

    approval_id: str
    action: str
    amount: Decimal
    requested_by: str
    requested_at: str
    approval_ref: Optional[str] = None


@dataclass(frozen=True)
class AgentState:
    """The supervisor's typed state (§8).

    Immutable dataclass — nodes build new copies via ``dataclasses.replace``;
    they never mutate shared state in place. Financial totals are explicit named
    fields (no hidden running totals in component closures).
    """

    trace_id: str
    flow_id: str
    tenant: str
    intent: str
    matched_amount: Decimal = Decimal("0")
    outstanding_balance: Decimal = Decimal("0")
    posted_total: Decimal = Decimal("0")
    pending_approvals: list[Approval] = field(default_factory=list)
    idempotency_keys: dict[str, str] = field(default_factory=dict)
    tool_call_ref: Optional[str] = None
    audit_refs: list[str] = field(default_factory=list)
    # --- Orchestration fields (architecture §7 state lifecycle, §9 error node). ---
    # The scaffold originally omitted these; the supervisor needs them to drive
    # conditional graph edges and to build the ExecutionSummary. All defaulted so
    # existing positional construction (trace_id, flow_id, tenant, intent) and
    # every prior reader are unaffected (backward compatible). See ADR-0003.
    status: str = "created"  # created| routed| executing| awaiting_approval| completed| failed
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""  # ISO-8601 UTC; ExecutionSummary.started_at (§12)
    updated_at: str = ""  # ISO-8601 UTC; last node touch (§12)
    # The routed subflow's envelope ``data`` (its §14 result payload), surfaced
    # by the supervisor under ``data.result`` so the computed numbers actually
    # reach the response. None when no subflow ran (AR_NOT_FOUND) or before
    # invoke. Additive/defaulted — backward compatible with prior construction
    # and readers (V1-RESULT-SURFACE). ``data.execution_summary`` conformance
    # (matched/outstanding/posted run-metadata) is the deferred V1-ENVELOPE-META.
    result_data: Optional[dict] = None