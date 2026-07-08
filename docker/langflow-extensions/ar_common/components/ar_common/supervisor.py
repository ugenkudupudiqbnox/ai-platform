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
  - route     : map intent → one of the nine subflow tools.           §5
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

import asyncio
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
#  Constants — the nine subflows, their tiers (architecture §4), and the
#  deterministic intent router. Tunables belong in Global Variables (§17) at
#  build phase; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

# The nine implemented subflows (architecture §4 rows 1-9). Seven reserved
# business-subflow slots (ar_fetch_invoices / ar_fetch_receipts /
# ar_match_payments / ar_reconcile / ar_dunning / ar_post_gl / ar_reporting)
# were never implemented and have been retired — see ADR-0013 (which renumbered
# §4 from the historical sixteen back to the nine implemented flows; the older
# ADRs retain their pre-renumber row numbering as immutable historical records).
SUBFLOWS: tuple[str, ...] = (
    "ar_issue_invoice",
    "ar_approval",
    "ar_file_intake",
    "ar_intercompany_sales",
    "ar_kitchen_revenue",
    "ar_foodics_processing",
    "ar_calculation",
    "ar_invoice_generation",
    "ar_audit",
)

# §19 tiers. read-only/auto proceed unattended; approval/dual-control pause.
TIER: dict[str, str] = {
    "ar_issue_invoice": "approval",
    "ar_approval": "approval",
    "ar_file_intake": "read-only",  # parses uploads → DocumentManifest; no mutation (ADR-0004)
    "ar_intercompany_sales": "approval",  # invoice production intent, but v1 is draft-only — gate dormant (ADR-0005)
    "ar_kitchen_revenue": "read-only",  # computes + reports Revenue/Collections/Expenses/Net Receivable/Net Payable; no posting (ADR-0006)
    "ar_foodics_processing": "approval",  # invoice production intent, but v1 is compute + draft only — gate dormant (ADR-0007)
    "ar_calculation": "read-only",  # computes + reports the 9 Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/Expenses/Net Receivable/Net Payable figures via the Business Rule Engine; no posting (ADR-0008)
    "ar_invoice_generation": "read-only",  # generates the 8 invoice artifacts — Invoice JSON/PDF/Excel/Journal Entry/Customer Statement/Zoho Upload File/Metadata + WorkflowState — as draft JSON-in-envelope; no posting; PDF/Excel binaries build-phase (ADR-0009)
    "ar_audit": "read-only",  # collects the run's artifacts → immutable §13 audit log (AuditRecords) + ExecutionSummary; no mutation (ADR-0012)
}

# Intent → subflow routing keywords (deterministic v1 classifier).
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Invoice Generation precedes ar_issue_invoice: the classifier uses strict
    # `>` (first-match-wins on ties), and ar_issue_invoice's multi-word 1.0-score
    # "create/issue/present/new invoice" keywords would otherwise tie with this
    # flow's multi-word 1.0-score "generate/draft/build invoice" keywords.
    # Placing invoice-generation first lets "generate invoice" / "draft invoice"
    # / "invoice pdf" / "invoice excel" / "journal entry" / "customer statement"
    # win, while "issue/create/present/new invoice" still routes to ar_issue_invoice.
    # ADR-0009.
    ("ar_invoice_generation", ("generate invoice", "invoice generation",
                               "draft invoice", "build invoice",
                               "compose invoice", "invoice pdf",
                               "invoice excel", "journal entry",
                               "customer statement")),
    ("ar_issue_invoice", ("issue invoice", "create invoice", "present invoice", "new invoice")),
    ("ar_approval", ("approve", "approval")),  # also the resume path
    # File Intake: an explicit "intake/upload/parse" intent (optionally + "file")
    # clears MIN_CONFIDENCE and routes to the File Intake Flow. A bare file with
    # NO keyword falls through to the file-only branch below (→ ar_file_intake @
    # 0.4, below MIN_CONFIDENCE → AR_UNCERTAIN unless the user adds one). §4/ADR-0004.
    ("ar_file_intake", ("intake", "upload", "parse file", "parse this", "ingest")),
    # Intercompany Sales: KOT (Kitchen Order Ticket) Excel from intercompany buyer
    # restaurants (HYP, Upyard) → draft InvoiceData per buyer + Validation/Exception
    # reports. v1 is compute + draft only (no posting) — ADR-0005. Multi-word / long
    # keywords score 1.0 and clear MIN_CONFIDENCE.
    ("ar_intercompany_sales", ("intercompany sales", "inter-company", "intercompany",
                               "kot", "kitchen order ticket", "kitchen order")),
    # Cosmic Kitchen Revenue: the four daily kitchen sheets (Menu Sales Analysis,
    # Daily Sales, Detailed Check Payment, Marriott Backup) → Revenue (Breakfast/
    # Half Board segments), Collections, Expenses, Net Receivable, Net Payable +
    # Revenue JSON + Validation/Exception reports. v1 is read-only compute + report
    # (no posting) — ADR-0006. Multi-word / long keywords score 1.0 and clear
    # MIN_CONFIDENCE.
    ("ar_kitchen_revenue", ("kitchen revenue", "kitchen sales", "menu sales",
                            "daily sales", "check payment", "marriott backup",
                            "net receivable", "net payable", "kitchen")),
    # Foodics Processing: Foodics Order + Order Items + Order Payments (export
    # files or API) → consolidated dataset + pivot + payment-type breakdown +
    # discount rules + Zoho Books upload format + draft InvoiceData per order +
    # Validation/Exception reports. v1 is compute + draft only (no posting) —
    # ADR-0007. Multi-word / long keywords score 1.0 and clear MIN_CONFIDENCE.
    ("ar_foodics_processing", ("foodics processing", "foodics order",
                               "order items", "order payments",
                               "consolidated workbook", "refresh pivot",
                               "payment type", "discount rules", "sheet3",
                               "zoho upload", "zoho upload format", "foodics")),
    # Calculation: a Validated JSON payload (P10 Validation Flow output) → the
    # nine AR figures (Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/
    # Expenses/Net Receivable/Net Payable) computed via the Business Rule Engine
    # (no hardcoded formulas) + a CalculationResult + Validation/Exception
    # reports. v1 is read-only compute + report (no posting) — ADR-0008. §55
    # waiver: figures only, not statutory filing. Multi-word / long keywords
    # score 1.0 and clear MIN_CONFIDENCE.
    ("ar_calculation", ("calculation", "calculate revenue", "vat calculation",
                        "municipality tax", "royalty", "net receivable",
                        "net payable", "business rule engine", "calc flow")),
    # Audit: an explicit "audit / audit log / execution summary / run summary /
    # run history / audit trail" intent routes to the Audit Flow. It collects the
    # run's artifacts (execution history, input files, validation reports,
    # calculation results, invoices, approvals, Zoho upload results, execution
    # time, errors, warnings) from a caller-assembled AuditRequest wrapper,
    # synthesizes an immutable §13 audit log + an ExecutionSummary, and returns
    # the Audit JSON + WorkflowState. Read-only emission — no §1 gate (ADR-0012).
    ("ar_audit", ("audit", "audit log", "execution summary", "run summary",
                   "run history", "audit trail")),
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
FINANCIAL_INTENTS: frozenset[str] = frozenset({"ar_issue_invoice"})


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

    In lfx 1.10.1 a RunFlow tool's name is ``"<flow>_tool"`` (e.g.
    ``ar_calculation_tool`` — ``run_flow._get_tools`` passes
    ``tool_name=f"{flow_name_selected}_tool"`` to ``ComponentToolkit.get_tools``,
    which the single-output branch sets as ``tool.name``). ``_node_invoke``
    looks tools up by the **bare** flow id (``intent``, e.g. ``ar_calculation``),
    so we also index each ``"<flow>_tool"`` tool under the stripped bare name.
    Tools that don't expose a usable name fall back to a substring match of the
    tool description against the known subflow ids; an empty dict means the
    canvas isn't wired yet.
    """
    indexed: dict[str, Any] = {}
    for tool in tools:
        name = getattr(tool, "name", None) or ""
        if name:
            indexed[name] = tool
            # Bare-intent alias: _node_invoke does tools.get(intent) where
            # intent is the flow id ("ar_calculation"), but lfx names the tool
            # "ar_calculation_tool". Strip the 5-char "_tool" suffix so the
            # routable intent hits. The full "<flow>_tool" key is retained.
            if name.endswith("_tool"):
                indexed[name[:-5]] = tool
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
    # preserving §4). Routes to ar_file_intake per ADR-0004.
    if not best_intent and files:
        return "ar_file_intake", 0.4
    return best_intent, best_score


def utc_now() -> str:
    """UTC ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` (contracts' timestamp pattern)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mint_id() -> str:
    """A fresh lowercase uuid4 string (trace_id / approval_id / manifest_id)."""
    return str(uuid.uuid4())


def _to_agent_state(vals: Any) -> AgentState:
    """Reconstruct the typed ``AgentState`` from a checkpointer snapshot.

    LangGraph's ``graph.get_state(config).values`` returns a plain dict, not
    the typed dataclass (nodes receive the reconstructed dataclass, but the
    snapshot does not). ``_finalize_envelope`` reads typed fields
    (``trace_id``, ``matched_amount`` as Decimal, ``pending_approvals`` as a
    list of ``Approval`` objects), so rebuild the dataclass from the dict,
    filtering to known fields so a stray key never breaks construction.
    """
    if isinstance(vals, AgentState):
        return vals
    if isinstance(vals, dict):
        known = {f.name for f in dataclasses.fields(AgentState)}
        return AgentState(**{k: v for k, v in vals.items() if k in known})
    return vals  # type: ignore[return-value]


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
    """Conditional edge: failed → respond, else → route.

    Path-map keys are the node success statuses ("failed"/"routed"); returning
    state.status routes "failed"→respond and "routed"→route. Returning a node
    name here (e.g. "respond") would KeyError against the path map.
    """
    return state.status


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
    """Conditional edge: rejected → respond, approved/auto → invoke.

    Path-map keys are the node success statuses ("failed"/"executing"); returning
    state.status routes "failed"→respond and "executing"→invoke. Returning a
    node name here would KeyError against the path map.
    """
    return state.status


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


def _flow_tweak_schema(tool: Any) -> Optional[tuple[Any, str]]:
    """Return ``(InnerModel class, sub-field name)`` for a RunFlow tool's
    ``flow_tweak_data`` input, or ``None`` if the tool has no ``flow_tweak_data``.

    lfx 1.10.1 RunFlow tools expose ``args_schema = InputSchema`` with a single
    REQUIRED field ``flow_tweak_data`` whose annotation is an ``InnerModel``
    pydantic model; ``InnerModel``'s one sub-field is named after the subflow's
    ChatInput node (e.g. ``"ChatInput-ar001~input_value"``, type str). There is
    NO top-level ``input_value``. The sub-field name varies per subflow, so it is
    derived dynamically from ``tool.args_schema`` (V1-FLOW-TWEAK-DATA).
    """
    schema = getattr(tool, "args_schema", None)
    fields = getattr(schema, "model_fields", None)
    if not (isinstance(fields, dict) and "flow_tweak_data" in fields):
        return None
    inner = getattr(fields["flow_tweak_data"], "annotation", None)
    inner_fields = getattr(inner, "model_fields", None)
    if not (isinstance(inner_fields, dict) and inner_fields):
        return None
    return inner, next(iter(inner_fields))  # ("ChatInput-ar001~input_value",)


def _build_tool_payload(tool: Any, user_input: str) -> dict[str, Any]:
    """Build the ainvoke payload for a RunFlow StructuredTool (V1-FLOW-TWEAK-DATA).

    The correct ainvoke shape is ``{"flow_tweak_data": {<sub-field>: user_input}}``
    (no top-level ``input_value``). Tools without a ``flow_tweak_data`` field fall
    back to ``{"input_value": ...}``.
    """
    ft = _flow_tweak_schema(tool)
    if ft is not None:
        _inner_cls, sub_field = ft
        return {"flow_tweak_data": {sub_field: user_input}}
    return {"input_value": user_input}


def _extract_json_object(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` JSON-object substring in ``text``, or None.

    The user's chat message (as delivered by the OpenAI adapter) is natural
    language with an embedded JSON payload — e.g. ``"Calculate AR for January
    with this payload JSON: {\"trace_id\": ...}"``. The classifier matches NL
    keywords, but every JSON subflow ``json.loads`` its ``ChatInput`` directly
    and rejects the NL prefix, so the supervisor must hand the subflow the pure
    JSON object. Scans for the first ``'{'``, balances braces (respecting string
    literals / escapes), and returns the substring only if it parses to a JSON
    object (V1-PAYLOAD-EXTRACT).
    """
    s = text or ""
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            cand = s[start:end + 1]
            try:
                if isinstance(json.loads(cand), dict):
                    return cand
            except (TypeError, ValueError):
                pass
        start = s.find("{", start + 1)
    return None


def _subflow_input(user_input: str, intent: str) -> str:
    """Derive the string handed to a routed subflow tool (V1-PAYLOAD-EXTRACT).

    JSON-payload subflows ``json.loads`` their ``ChatInput`` directly and reject
    the NL prefix, so extract the embedded JSON object and pass only that.
    ``ar_approval`` consumes a natural-language decision reply (approval_ref +
    verb) and is passed through verbatim. When no JSON object is found for a
    JSON subflow, fall back to the raw message so the subflow returns its own
    graceful ``AR_VALIDATION`` (§9) rather than a tool-level error.
    """
    if intent == "ar_approval":
        return user_input
    obj = _extract_json_object(user_input)
    return obj if obj is not None else user_input


def _extract_runflow_component(tool: Any) -> Optional[Any]:
    """Recover the ORIGINAL RunFlow component a RunFlow tool wraps (V1-RUNFLOW-TOOL-INPUT).

    lfx 1.10.1's RunFlow tool builds its dynamic output resolver as
    ``MethodType(_dynamic_resolver, self)`` bound to the ORIGINAL RunFlow component
    at tool-build time (lfx/base/tools/run_flow.py ``_register_flow_output_method``).
    At invoke time lfx's ``output_function`` deepcopies that component and calls
    ``comp.set(flow_tweak_data=...)`` on the COPY (lfx/base/tools/component_tool.py),
    but the resolver still runs on the ORIGINAL — so the per-call ``flow_tweak_data``
    is ignored and results cache (``_last_run_outputs``) on the original. To pass
    per-call input to the subflow we must set ``flow_tweak_data`` on the ORIGINAL
    and reset its run cache. This walks the StructuredTool's ``coroutine``/``func``
    closure cells to find that original ``RunFlowBaseComponent``. Returns ``None``
    if not found (graceful — ``ainvoke`` still runs, input may not reach subflow).
    """
    try:
        from lfx.base.tools.run_flow import RunFlowBaseComponent
    except Exception:  # noqa: BLE001 — lfx layout drift → graceful fallback
        return None
    seen: set[int] = set()

    def _walk(fn: Any, depth: int = 0) -> Optional[Any]:
        if fn is None or id(fn) in seen or depth > 6:
            return None
        seen.add(id(fn))
        for cell in (getattr(fn, "__closure__", None) or ()):
            try:
                v = cell.cell_contents
            except ValueError:  # empty cell
                continue
            if isinstance(v, RunFlowBaseComponent):
                return v
            if callable(v):
                r = _walk(v, depth + 1)
                if r is not None:
                    return r
        return None

    for attr in ("coroutine", "func"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            r = _walk(fn)
            if r is not None:
                return r
    return None


def _call_tool(tool: Any, user_input: str) -> str:
    """Invoke an async-only RunFlow StructuredTool via a sync bridge (V1-FLOW-TWEAK-DATA).

    Two lfx 1.10.1 constraints shape this:

    1. RunFlow tools are async-only ``StructuredTools`` (sync ``invoke`` raises
       ``NotImplementedError``). The supervisor's output method runs SYNC under
       lfx (dispatched via ``asyncio.to_thread`` on a worker thread with NO running
       event loop), and lfx's custom-component loader only exposes SYNC module-level
       free functions to the component's methods — it filters on
       ``ast.FunctionDef`` (lfx/custom/validate.py), not ``ast.AsyncFunctionDef``,
       so the invoke chain MUST stay sync. We therefore run ``tool.ainvoke`` on a
       fresh loop via ``asyncio.run`` (safe — no running loop in this worker thread).

    2. lfx binds the RunFlow tool's output resolver to the ORIGINAL component, so
       ``flow_tweak_data`` set by ``tool.ainvoke`` (on the per-call deepcopy) never
       reaches the subflow and results cache on the original (V1-RUNFLOW-TOOL-INPUT).
       We set this call's ``flow_tweak_data`` on the ORIGINAL component and reset its
       ``_last_run_outputs`` cache before invoking, so the resolver reads fresh input.
       The supervisor runs one subflow at a time (sync graph), so mutating the shared
       original is race-free.
    """
    payload = _build_tool_payload(tool, user_input)
    ft = _flow_tweak_schema(tool)
    if ft is not None:
        inner_cls, sub_field = ft
        comp = _extract_runflow_component(tool)
        if comp is not None:
            try:
                comp.set(flow_tweak_data=inner_cls(**{sub_field: user_input}))
                comp._last_run_outputs = None  # type: ignore[attr-defined]  # force fresh run
            except Exception:  # noqa: BLE001 — best-effort; ainvoke still runs
                pass
    result = asyncio.run(tool.ainvoke(payload))
    return _to_str(result)


def _backoff_sleep(attempt: int) -> None:
    """§10 exponential backoff with ±25% jitter, capped at 30s.

    Uses attempt-parity jitter (no hidden randomness) so resume determinism (§8)
    holds: the same retry sequence reproduces the same waits. ``time.sleep`` is
    safe here: the supervisor runs SYNC (lfx dispatches the output method via
    ``asyncio.to_thread`` on a worker thread with no running event loop), so a
    blocking sleep cannot stall any loop.
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
    envelope = _invoke_with_retry(tool, _subflow_input(ctx.get("user_input", ""), intent),
                                  intent, state.trace_id)
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
        # Carry the subflow's §14 result payload through to the envelope so the
        # computed numbers reach the response under data.result (V1-RESULT-SURFACE).
        "result_data": data,
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
            value="glm-5.2:cloud",
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
        state: AgentState = _to_agent_state(snapshot.values)
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
            # Surface the routed subflow's §14 result payload under data.result
            # (V1-RESULT-SURFACE). Nested (not flat) so the deferred
            # data.execution_summary (V1-ENVELOPE-META) can be added later
            # without restructuring this. Overridden to {action, tier} on the
            # pending/awaiting branches below.
            "data": {"result": state.result_data} if state.result_data else {},
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
            err_env = {"message": err.get("message", "") if isinstance(err, dict) else str(err)}
            if isinstance(err, dict) and err.get("detail"):
                err_env["detail"] = err["detail"]
            env = dict(base)
            env.update({"status": "error", "code": err.get("code", "AR_UNEXPECTED"),
                        "error": err_env})
            return env
        # completed (default)
        env = dict(base)
        env.update({"status": "ok", "code": "AR_OK"})
        return env