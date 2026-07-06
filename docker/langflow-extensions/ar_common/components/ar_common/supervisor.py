"""Cosmic AR Agent supervisor component (constitution §8, architecture §5).

The supervisor is the single stateful orchestrator. It owns an explicit LangGraph
``StateGraph[AgentState]`` and drives the nine AR subflows, which are exposed to
it as LangChain tools (one ``RunFlow`` node per subflow on the supervisor flow's
canvas — see ``cosmic-ar/flows/supervisor.json``). Per §8 the durable,
checkpointed state is the typed ``AgentState`` (frozen dataclass, immutable
updates); raw per-run inputs (``user_input``, uploaded ``files``, the wired
``tools``, ``actor``, ``session_id``) are run-scoped **context**, not state, so
they are never persisted in a checkpoint.

Responsibilities → LangGraph nodes (architecture §5):

  ingest → classify → route → gate → invoke → respond
                                 ↑
                       §19 ``interrupt()`` pauses here for approval;
                       resumed with ``Command(resume=approval_ref)``.

  - ingest    : bind ``trace_id``/``flow_id``/``tenant`` + timestamps, carry
                file refs (the "accept uploaded files" + "build manifest"
                responsibility — refs only; parsing is the readers' job, in
                subflows). §8 / §2
  - classify  : deterministic intent + doc-type classification; low confidence
                → ``AR_UNCERTAIN`` (§4 fail-safe) → respond.               §4
  - route     : map intent → one of the nine subflow tools.               §5
  - gate      : §19 tier gate; ``read-only``/``auto`` proceed, ``approval``/
                ``dual-control`` call ``interrupt()`` to pause for a human. §19
  - invoke    : call the selected RunFlow tool inside the §10 retry/backoff
                loop, then **collect** the subflow's §14 envelope and merge its
                totals / audit_refs / pending_approvals into state. The
                supervisor NEVER computes financial amounts — totals come from
                the subflow envelope.                          §9/§10/§8
  - respond   : build the ``ExecutionSummary`` in the §14 envelope.        §14

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id`` (the
adapter forwards LibreChat's ``conversationId`` → LangFlow ``session_id``).
This is the §11 **fallback**: non-durable (lost on worker recreate). Durable
Postgres checkpointing (``langgraph-checkpoint-postgres`` into the ``ar_agent``
DB) remains a documented build-phase step — see ADR-0003 and the constitution
§11 caveat (Langfuse tracing is currently off, so the checkpoint is the source
of truth for resume).

v1 notes (recorded in ADR-0003): classify/route are deterministic (no LLM key
required); an LLM-driven classifier is a pluggable hook. ``invoke`` folds
``collect`` (single subflow per run in v1, so the subflow's totals are the run's
totals). Per-node ``self.log`` is deferred (nodes are functions without ``self``
— v1 logs at the ``run()`` boundary; full §12 per-node logging is build-phase).
The output method **never raises** (§5/§9): it catches at the boundary and
returns an ``AR_UNEXPECTED`` envelope.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from lfx.custom import Component
from lfx.io import HandleInput, MessageTextInput, Output
from lfx.schema import Message

from components.ar_common.agent_state import AgentState, Approval

# --------------------------------------------------------------------------- #
#  Constants — the ten subflows, their tiers (architecture §4), and the
#  deterministic intent router. Tunables belong in Global Variables (§17) at
#  build phase; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

# The ten subflows: nine business subflows + ar_file_intake (the File Intake Flow,
# added as the 10th — see ADR-0004; architecture §4's "Nine reusable subflows" is
# amended to "Ten" by that ADR).
SUBFLOWS: tuple[str, ...] = (
    "ar_fetch_invoices",
    "ar_fetch_receipts",
    "ar_match_payments",
    "ar_reconcile",
    "ar_dunning",
    "ar_post_gl",
    "ar_issue_invoice",
    "ar_reporting",
    "ar_approval",
    "ar_file_intake",
)

# §19 tiers. read-only/auto proceed unattended; approval/dual-control pause.
TIER: dict[str, str] = {
    "ar_fetch_invoices": "read-only",
    "ar_fetch_receipts": "read-only",
    "ar_match_payments": "auto",  # v1: below the auto-match ceiling (ceiling check is build-phase)
    "ar_reconcile": "auto",
    "ar_dunning": "auto",
    "ar_post_gl": "approval",
    "ar_issue_invoice": "approval",
    "ar_reporting": "read-only",
    "ar_approval": "approval",
    "ar_file_intake": "read-only",  # parses uploads → DocumentManifest; no mutation (ADR-0004)
}

# Intent → subflow routing keywords (deterministic v1 classifier).
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ar_fetch_invoices", ("invoice", "fetch invoice", "list invoice", "outstanding invoice")),
    ("ar_fetch_receipts", ("receipt", "pos", "foodics", "fetch receipt")),
    ("ar_match_payments", ("match", "apply payment", "payment matching")),
    ("ar_reconcile", ("reconcile", "reconciliation", "balance")),
    ("ar_dunning", ("dunning", "overdue", "remind", "reminder")),
    ("ar_post_gl", ("post", "gl", "general ledger", "payment received", "post payment")),
    ("ar_issue_invoice", ("issue invoice", "create invoice", "present invoice", "new invoice")),
    ("ar_reporting", ("report", "aging", "dashboard", "ar aging", "summary report")),
    ("ar_approval", ("approve", "approval")),  # also the resume path
    # File Intake: an explicit "intake/upload/parse" intent (optionally + "file")
    # clears MIN_CONFIDENCE and routes to the File Intake Flow. A bare file with
    # NO keyword falls through to the file-only branch below (→ ar_file_intake @
    # 0.4, below MIN_CONFIDENCE → AR_UNCERTAIN unless the user adds one). §4/ADR-0004.
    ("ar_file_intake", ("intake", "upload", "parse file", "parse this", "ingest")),
)

# §4 fail-safe threshold for the deterministic classifier.
MIN_CONFIDENCE: float = 0.6

# §10 retry policy.
MAX_ATTEMPTS: int = 3
BACKOFF_BASE_S: float = 1.0
BACKOFF_CAP_S: float = 30.0

# Approval-reference regex (matches the contracts' ar-approval-<uuid> shape).
APPROVAL_REF_RE = re.compile(
    r"ar-approval-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

# Intent values that perform a financial mutation → exhausted retry surfaces as
# pending_approval (§10), never a silent failure.
FINANCIAL_INTENTS: frozenset[str] = frozenset({"ar_post_gl", "ar_issue_invoice"})


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class SupervisorContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[SupervisorContext]``.

    Durable, resumable state lives in ``AgentState`` (checkpointed). These are
    the transient inputs for one invocation; they are re-supplied on resume.
    """

    user_input: str
    files: list[Any]
    tools: dict[str, Any]  # subflow id -> LangChain BaseTool
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str


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


def _tools_by_name(tools: list[Any]) -> dict[str, Any]:
    """Index the wired RunFlow tools by their LangChain tool ``name``.

    A RunFlow tool's name is the target flow name (e.g. ``ar_fetch_invoices``).
    Tools that don't expose a usable name fall back to a substring match of the
    tool description against the known subflow ids; an empty dict means the
    canvas isn't wired yet.
    """
    indexed: dict[str, Any] = {}
    for tool in tools:
        name = getattr(tool, "name", None) or ""
        if name:
            indexed[name] = tool
            continue
        desc = (getattr(tool, "description", "") or "").lower()
        for sid in SUBFLOWS:
            if sid in desc:
                indexed[sid] = tool
                break
    return indexed


def detect_approval_ref(text: str) -> Optional[str]:
    """Return the first ``ar-approval-<uuid>`` in ``text``, or None."""
    if not text:
        return None
    match = APPROVAL_REF_RE.search(text)
    return match.group(0) if match else None


def classify_intent(user_input: str, files: list[Any]) -> tuple[str, float]:
    """Deterministic v1 intent classifier (constitution §4 design principle 3).

    Returns ``(intent, confidence)``. ``intent`` is a subflow id or ``""`` when
    nothing matches; confidence is 1.0 on a multi-word/long keyword hit, scaled
    down for short single-token hits. Below ``MIN_CONFIDENCE`` the supervisor
    fails safe (§4) with ``AR_UNCERTAIN``.
    """
    text = (user_input or "").lower()
    if not text and not files:
        return "", 0.0
    best_intent = ""
    best_score = 0.0
    for intent, keywords in INTENT_KEYWORDS:
        for kw in keywords:
            if kw in text:
                score = 1.0 if (" " in kw or len(kw) > 4) else 0.8
                if score > best_score:
                    best_intent, best_score = intent, score
    # File-only signal: an uploaded file with no recognisable intent routes to
    # the File Intake Flow so the upload is parsed into a DocumentManifest (low
    # confidence → AR_UNCERTAIN unless the user adds an "intake/upload" keyword,
    # preserving §4). Rerouted from ar_fetch_invoices per ADR-0004.
    if not best_intent and files:
        return "ar_file_intake", 0.4
    return best_intent, best_score


def utc_now() -> str:
    """UTC ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` (contracts' timestamp pattern)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mint_id() -> str:
    """A fresh lowercase uuid4 string (trace_id / approval_id / manifest_id)."""
    return str(uuid.uuid4())


def derive_idempotency_key(action: str, entity_ref: str, nonce: str) -> str:
    """§10 idempotency key: ``ar-idem:<action>:<entity>:<nonce>``."""
    safe_action = re.sub(r"[^a-z0-9_]+", "_", (action or "").lower()) or "action"
    safe_entity = re.sub(r"[^a-z0-9_-]+", "_", (entity_ref or "").lower()) or "entity"
    safe_nonce = re.sub(r"[^a-z0-9_-]+", "_", (nonce or "").lower()) or "nonce"
    return f"ar-idem:{safe_action}:{safe_entity}:{safe_nonce}"


def parse_envelope(text: str) -> Optional[dict[str, Any]]:
    """Best-effort parse of a §14 envelope from a tool/flow output string."""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _envelope(status: str, code: str, data: Optional[dict] = None,
              error: Optional[dict] = None, trace_id: str = "",
              approval_ref: str = "") -> dict[str, Any]:
    """Build a §14 envelope dict."""
    env: dict[str, Any] = {"status": status, "code": code, "data": data or {},
                           "trace_id": trace_id, "approval_ref": approval_ref}
    if error:
        env["error"] = error
    return env


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Parse a 2dp-string amount into Decimal, or None on malformed input."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


# --------------------------------------------------------------------------- #
#  LangGraph nodes. Each takes ``(state: AgentState, runtime: Runtime)`` and
#  returns a partial-update dict (§8 immutable — LangGraph merges via replace).
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[SupervisorContext]) -> SupervisorContext:
    return runtime.context or {}


def _node_ingest(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    return {
        "trace_id": state.trace_id or mint_id(),
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_supervisor"),
        "tenant": state.tenant or ctx.get("tenant", "cosmic-vikings"),
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_classify(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    ctx = _ctx(runtime)
    user_input = ctx.get("user_input", "")
    files = ctx.get("files", [])
    intent, confidence = classify_intent(user_input, files)
    now = utc_now()
    if confidence < MIN_CONFIDENCE:
        # §4 fail-safe: stop and ask rather than guess.
        return {
            "intent": intent,
            "status": "failed",
            "error": {"code": "AR_UNCERTAIN",
                      "message": "Could not confidently classify the AR request."},
            "updated_at": now,
        }
    return {"intent": intent, "status": "routed", "updated_at": now}


def _after_classify(state: AgentState) -> str:
    """Conditional edge: failed → respond, else → route."""
    return "respond" if state.status == "failed" else "route"


def _node_route(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    """Map intent → the routed subflow (architecture §5).

    On the resume path the user's message carries an ``approval_ref``; route
    still records ``ar_approval`` so the gate's interrupt is reached and the
    pending checkpoint resumes.
    """
    ctx = _ctx(runtime)
    ref = detect_approval_ref(ctx.get("user_input", ""))
    if ref:
        return {"intent": "ar_approval", "updated_at": utc_now()}
    return {"updated_at": utc_now()}


def _node_gate(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    """§19 approval tier gate.

    ``read-only`` / ``auto`` proceed straight to ``invoke``. ``approval`` /
    ``dual-control`` call ``interrupt()`` to pause for a human; on resume the
    interrupt returns the ``approval_ref``, which is recorded (non-reusable, §19)
    and the run continues to ``invoke``.
    """
    ctx = _ctx(runtime)
    intent = state.intent
    tier = TIER.get(intent, "approval")  # unknown intent → safest tier
    if tier in ("read-only", "auto"):
        return {"status": "executing", "updated_at": utc_now()}
    # Mutation: pause for human approval (§19).
    approval = Approval(
        approval_id=mint_id(),
        action=intent,
        amount=Decimal("0"),  # amount is subflow-supplied at effect time (v1)
        requested_by=ctx.get("actor", "") or "unknown",
        requested_at=utc_now(),
    )
    payload = {
        "approval_ref": f"ar-approval-{approval.approval_id}",
        "action": intent,
        "tier": tier,
        "trace_id": state.trace_id,
    }
    # interrupt() pauses the graph; on resume it returns the Command(resume=...)
    # value (the approval_ref the human supplied).
    resumed = interrupt(payload)
    approved_ref = ""
    if isinstance(resumed, dict):
        approved_ref = resumed.get("approval_ref") or resumed.get("ref") or ""
    elif isinstance(resumed, str):
        approved_ref = resumed
    if not approved_ref or not APPROVAL_REF_RE.match(approved_ref):
        return {"status": "failed",
                "error": {"code": "AR_FORBIDDEN",
                          "message": "Approval was rejected or expired."},
                "updated_at": utc_now()}
    fulfilled = dataclasses.replace(approval, approval_ref=approved_ref)
    return {"status": "executing",
            "pending_approvals": [*state.pending_approvals, fulfilled],
            "updated_at": utc_now()}


def _after_gate(state: AgentState) -> str:
    """Conditional edge: rejected → respond, approved/auto → invoke."""
    return "respond" if state.status == "failed" else "invoke"


def _is_transient(exc: BaseException) -> bool:
    """§10: classify an exception as transient (retryable) vs hard."""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "temporary", "unreachable")):
        return True
    # urllib HTTPError: 5xx / 408 / 429 → transient; 401 handled by caller.
    code = getattr(exc, "code", None)
    if isinstance(code, int) and (code >= 500 or code in (408, 429)):
        return True
    return False


def _call_tool(tool: Any, user_input: str) -> str:
    """Invoke a LangChain BaseTool and coerce its result to a string."""
    try:
        result = tool.invoke({"input_value": user_input})
    except (TypeError, ValueError):
        result = tool.invoke(user_input)
    return _to_str(result)


def _backoff_sleep(attempt: int) -> None:
    """§10 exponential backoff with ±25% jitter, capped at 30s.

    Uses attempt-parity jitter (no hidden randomness) so resume determinism (§8)
    holds: the same retry sequence reproduces the same waits.
    """
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
    jitter = delay * 0.25
    slept = delay + (jitter if attempt % 2 else -jitter)
    time.sleep(max(0.0, slept))


def _invoke_with_retry(tool: Any, user_input: str, intent: str,
                       trace_id: str) -> dict[str, Any]:
    """Call a subflow tool inside the §10 retry/backoff loop.

    Returns a parsed §14 envelope dict. The subflow handles its own upstream
    retries (Zoho/Foodics); this loop only retries *transient LangFlow/infra*
    failures of the RunFlow invocation itself. A non-transient error or an
    exhausted *financial* retry surfaces as ``pending_approval`` (§10: never a
    silent failure for money).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _call_tool(tool, user_input)
            envelope = parse_envelope(raw) or _envelope(
                "error", "AR_UPSTREAM",
                error={"message": "Subflow returned no envelope."},
                trace_id=trace_id)
            return envelope
        except Exception as exc:  # noqa: BLE001 — classified below, never raised
            last_exc = exc
            if not _is_transient(exc):
                # Hard error: no retry (§10). Financial → pending_approval.
                if intent in FINANCIAL_INTENTS:
                    return _envelope("pending_approval", "AR_APPROVAL_REQUIRED",
                                     data={"action": intent,
                                           "reason": "subflow hard-failed"},
                                     trace_id=trace_id,
                                     approval_ref=f"ar-approval-{mint_id()}")
                return _envelope("error", "AR_UNEXPECTED",
                                 error={"message": f"subflow {intent} failed: {exc}"},
                                 trace_id=trace_id)
            # transient → backoff (full ±25% jitter, ≤30s window)
            if attempt < MAX_ATTEMPTS:
                _backoff_sleep(attempt)
    # exhausted transient retries
    if intent in FINANCIAL_INTENTS:
        return _envelope("pending_approval", "AR_APPROVAL_REQUIRED",
                         data={"action": intent,
                               "reason": "transient retries exhausted"},
                         trace_id=trace_id,
                         approval_ref=f"ar-approval-{mint_id()}")
    return _envelope("error", "AR_UPSTREAM",
                     error={"message": f"transient retries exhausted: {last_exc}"},
                     trace_id=trace_id)


def _node_invoke(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    """Invoke the routed subflow (+ §10 retry) and **collect** its envelope.

    The supervisor NEVER computes financial amounts (constitution §1 north star
    + §8): the matched/outstanding/posted totals, audit_refs and any pending
    approval come straight from the subflow's §14 envelope and are merged here.
    """
    ctx = _ctx(runtime)
    intent = state.intent
    tools = ctx.get("tools", {}) or {}
    tool = tools.get(intent)
    now = utc_now()
    if tool is None:
        return {"status": "failed",
                "error": {"code": "AR_NOT_FOUND",
                          "message": f"Subflow '{intent}' is not wired on the canvas."},
                "tool_call_ref": f"{state.trace_id}:{intent}:0",
                "updated_at": now}
    envelope = _invoke_with_retry(tool, ctx.get("user_input", ""), intent, state.trace_id)
    data = envelope.get("data") if isinstance(envelope, dict) else {}
    data = data if isinstance(data, dict) else {}
    # Idempotency key for any financial mutation (§10) — recorded so a resume
    # replays the same key; the subflow honours it at build phase.
    idem_keys = dict(state.idempotency_keys)
    if intent in FINANCIAL_INTENTS:
        idem_keys[intent] = derive_idempotency_key(intent, intent, state.trace_id)
    updates: dict[str, Any] = {
        "idempotency_keys": idem_keys,
        "tool_call_ref": f"{state.trace_id}:{intent}:0",
        "updated_at": now,
    }
    status = envelope.get("status") if isinstance(envelope, dict) else None
    if status == "pending_approval":
        updates["status"] = "awaiting_approval"
        ref = envelope.get("approval_ref", "")
        if ref:
            updates["pending_approvals"] = [
                *state.pending_approvals,
                Approval(approval_id=mint_id(), action=intent, amount=Decimal("0"),
                         requested_by=ctx.get("actor", "") or "unknown",
                         requested_at=utc_now(), approval_ref=ref),
            ]
        return updates
    if status == "error":
        updates["status"] = "failed"
        updates["error"] = envelope.get("error") or {"code": "AR_UNEXPECTED",
                                                     "message": "subflow error"}
        return updates
    # success — merge the subflow's reported totals (v1: single subflow ⇒ set).
    updates["status"] = "completed"
    totals = data.get("totals") if isinstance(data, dict) else None
    if isinstance(totals, dict):
        matched = _to_decimal(totals.get("matched"))
        if matched is not None:
            updates["matched_amount"] = matched
        outstanding = _to_decimal(totals.get("outstanding"))
        if outstanding is not None:
            updates["outstanding_balance"] = outstanding
        posted = _to_decimal(totals.get("posted"))
        if posted is not None:
            updates["posted_total"] = posted
    audit = data.get("audit_refs") or data.get("audit_ref")
    if isinstance(audit, list):
        updates["audit_refs"] = [*state.audit_refs, *[str(a) for a in audit]]
    elif isinstance(audit, str) and audit:
        updates["audit_refs"] = [*state.audit_refs, audit]
    return updates


def _approval_refs(approvals: list[Approval]) -> list[str]:
    return [a.approval_ref for a in approvals if a.approval_ref]


def _node_respond(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state.

    The ExecutionSummary is built deterministically from the merged ``AgentState``
    in ``_finalize_envelope`` (run side), so this node only refreshes the
    ``updated_at`` timestamp captured as ``ended_at`` for the checkpoint.
    """
    _ = runtime  # context not needed here; envelope is state-derived
    return {"updated_at": utc_now()}


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class SupervisorAgentComponent(Component):
    # Bare class name as the canonical `name` so the component is addressable
    # both as the bundle address (ext:ar_common:SupervisorAgentComponent@extra)
    # AND by the bare class name used by existing flow nodes for `data.type`.
    name = "SupervisorAgentComponent"
    display_name = "Cosmic AR Supervisor"
    description = (
        "Orchestrates the Cosmic AR Agent: classifies the AR intent, routes to "
        "the right reusable LangFlow subflow, gates financial mutations behind "
        "human approval, and checkpoints state for resumable runs. Call this "
        "for any accounts-receivable request."
    )
    icon = "Network"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="User Request",
            info="The natural-language AR request from the user (via the OpenAI adapter / LibreChat).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="gpt-4o-mini",
            info="LLM model used for intent classification and routing (v1: deterministic; LLM hook is build-phase).",
            tool_mode=True,
        ),
        HandleInput(
            name="files",
            display_name="Uploaded Files",
            info="Uploaded file refs from the canvas File / ChatInput node (the 'accept uploaded files' responsibility).",
            input_types=["Data"],
            is_list=True,
            required=False,
        ),
        HandleInput(
            name="tools",
            display_name="Subflow Tools",
            info="The nine RunFlow subflow-as-tool outputs wired in from the canvas.",
            input_types=["Tool"],
            is_list=True,
            required=False,
        ),
    ]

    outputs = [
        Output(
            name="supervisor_output",
            display_name="Supervisor Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=AgentState, context_schema=SupervisorContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("classify", _node_classify)
        graph.add_node("route", _node_route)
        graph.add_node("gate", _node_gate)
        graph.add_node("invoke", _node_invoke)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_edge("ingest", "classify")
        graph.add_conditional_edges("classify", _after_classify,
                                    {"failed": "respond", "routed": "route"})
        graph.add_edge("route", "gate")
        graph.add_conditional_edges("gate", _after_gate,
                                    {"failed": "respond", "executing": "invoke"})
        graph.add_edge("invoke", "respond")
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
            tools = _tools_by_name(_as_list(getattr(self, "tools", None)))
            files = _as_list(getattr(self, "files", None))
            session_id = _to_str(getattr(self, "session_id", "")) or mint_id()
            actor = _to_str(getattr(self, "actor", ""))
            ctx: SupervisorContext = {
                "user_input": user_input,
                "files": files,
                "tools": tools,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_supervisor",
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            approval_ref = detect_approval_ref(user_input)
            # Resume path: a pending checkpoint exists and the user supplied an
            # approval_ref → continue past the gate's interrupt (§19/§11).
            if approval_ref and self._has_pending_checkpoint(graph, config):
                graph.invoke(Command(resume=approval_ref), config=config, context=ctx)
            else:
                initial = AgentState(
                    trace_id=mint_id(),
                    flow_id=ctx["flow_id"],
                    tenant=ctx["tenant"],
                    intent="",
                )
                graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config, ctx)
            self.log(f"event=supervisor.run outcome={envelope.get('status')} "
                     f"trace_id={envelope.get('trace_id')} intent={envelope.get('intent')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Supervisor run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=supervisor.run outcome=error code=AR_UNEXPECTED")
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

    def _finalize_envelope(self, graph: Any, config: dict,
                            ctx: SupervisorContext) -> dict[str, Any]:
        """Read the final state (and any pending interrupt) → §14 envelope.

        Deterministic from state: status drives the envelope status/code;
        totals/approvals/audit_refs come from the merged AgentState. Paused-at-
        gate (interrupt) is detected via ``snapshot.next``.
        """
        _ = ctx  # envelope is state-derived; context already merged into state
        snapshot = graph.get_state(config)
        state: AgentState = snapshot.values  # type: ignore[assignment]
        pending = getattr(snapshot, "next", None)
        base = {
            "trace_id": state.trace_id, "flow_id": state.flow_id, "tenant": state.tenant,
            "intent": state.intent,
            "totals": {"matched": f"{state.matched_amount:.2f}",
                       "outstanding": f"{state.outstanding_balance:.2f}",
                       "posted": f"{state.posted_total:.2f}"},
            "started_at": state.created_at or utc_now(),
            "ended_at": state.updated_at or utc_now(),
            "approvals": _approval_refs(state.pending_approvals),
            "audit_refs": list(state.audit_refs),
            "checkpoint_id": self._checkpoint_id(graph, config),
            "subflows_invoked": [state.intent] if state.intent else [],
            "contract_version": "1.0.0",
        }
        # Paused at the gate ⇒ pending approval (§19).
        if pending:
            ref = ""
            action = state.intent
            tier = "approval"
            for task in getattr(snapshot, "tasks", []) or []:
                for it in (getattr(task, "interrupts", []) or []):
                    payload = getattr(it, "value", {}) or {}
                    if isinstance(payload, dict):
                        ref = payload.get("approval_ref", "") or ref
                        action = payload.get("action", action) or action
                        tier = payload.get("tier", tier) or tier
            env = dict(base)
            env.update({"status": "pending_approval", "code": "AR_APPROVAL_REQUIRED",
                        "approval_ref": ref, "data": {"action": action, "tier": tier}})
            return env
        if state.status == "awaiting_approval":
            ref = ""
            for ap in reversed(state.pending_approvals):
                if ap.approval_ref:
                    ref = ap.approval_ref
                    break
            env = dict(base)
            env.update({"status": "pending_approval", "code": "AR_APPROVAL_REQUIRED",
                        "approval_ref": ref,
                        "data": {"action": state.intent, "tier": TIER.get(state.intent, "approval")}})
            return env
        if state.status == "failed":
            err = state.error or {"code": "AR_UNEXPECTED", "message": "run failed"}
            env = dict(base)
            env.update({"status": "error", "code": err.get("code", "AR_UNEXPECTED"),
                        "approval_ref": "", "error": err})
            return env
        # completed (default)
        env = dict(base)
        env.update({"status": "ok", "code": "AR_OK", "approval_ref": ""})
        return env