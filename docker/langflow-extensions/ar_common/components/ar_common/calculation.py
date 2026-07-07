"""Cosmic AR Agent — Calculation Flow component (constitution §8, architecture §4 row 7).

The Calculation Flow is the 7th AR subflow (ADR-0008). It takes a **Validated
JSON** payload (the intended output of the P10 Validation Flow — not built here)
— an aggregated ``{trace_id, tenant, period, currency, facts, parameters}``
object — and computes the nine AR figures: **Revenue, Discount, VAT,
Municipality Tax, Royalty, Collections, Expenses, Net Receivable, Net Payable**.
It returns a structured ``CalculationResult`` + a Validation Report + an
Exception Report + a ``WorkflowState`` snapshot — with logging (§12) and
**checkpoints after every calculation** (§11).

The binding constraint (``prompts/P11_calculation_flow.md``): **"No hardcoded
business rules. All calculations must use Business Rule Engine."** This flow
holds **zero formulas** — every calculation is a declarative rule fed to the
shared ``BusinessRuleEngineComponent`` (cosmic_common). The seed ruleset ships
as the flow's ``rules`` input default (overridable); rates (VAT %, municipality
%, royalty %, discount %) are LangFlow Global Variables per §17, carried into
the engine via the payload's ``parameters`` block (``$GV:NAME`` injection is a
documented build-phase seam — v1 reads concrete rates from
``payload.parameters``).

v1 is **read-only compute + report** (mirrors ``ar_kitchen_revenue``): no
posting, no idempotency key, no ``pending_approval``, **not** in
``FINANCIAL_INTENTS``, §19 gate dormant — §1 north star preserved.

**Constitution §55 waiver** (ADR-0008): §55 lists "Tax filing, VAT/Saudi Zakat
calculation, and statutory returns" as out-of-scope. This flow computes
VAT/Municipality Tax/Royalty as **invoice/reconciliation figures** (not
statutory filing) — recorded as a written waiver + ADR-0008 per the
constitution's Authority note.

Responsibilities → LangGraph nodes:

  ingest → resolve_parameters → validate_payload → classify_exceptions →
  evaluate_rules → build_calculation_result → build_state → checkpoint → respond

  - ingest              : parse the validated-JSON payload from ``user_input``;
                          bind ``trace_id``/``flow_id``/``tenant`` + timestamps;
                          carry ``rules`` + ``model_name`` in **context** (§8).
                          Malformed JSON → ``AR_VALIDATION``.                  §9
  - resolve_parameters  : populate ``parameters`` from ``payload.parameters``;
                          a rate referenced by the rules but missing from the
                          payload defaults to ``"0.00"`` + a warning (not a hard
                          fail). ``$GV:NAME`` injection is build-phase.          §17
  - validate_payload    : inline hand-rolled validator for the input contract
                          (facts present + 2dp-parseable, period dates ISO,
                          currency ``^[A-Z]{3}$``). Hard fail (no facts) →
                          ``AR_VALIDATION``; per-fact/period/currency issues are
                          warnings. Builds the full ``ValidationResult``.       §9
  - classify_exceptions : split facts into valid vs exception; the Exception
                          Report = a ``ValidationResult`` scoped to failures.   §4
  - evaluate_rules      : **CORE** — build the engine payload ``{facts,
                          parameters, outputs:{}}`` and call the BRE's pure
                          ``_evaluate_rules`` directly (deliberate testability
                          choice — ADR-0008 #4; the lfx ``BusinessRuleEngineComponent``
                          wrapper remains for canvas/Flow-as-Tool use). On
                          ``AR_VALIDATION``/``AR_RULE_FAILED`` → ``failed``.
                          Extract ``data.calculations`` (the 9 figs). **Records a
                          checkpoint** ``"rules"``.                              §15
  - build_calculation_result : assemble the ``CalculationResult``
                          (``calculation_type="reconcile"``, ``totals`` = the 9
                          signed-2dp keys, ``line_items`` one per figure with
                          ``source_refs=[rule_id]``). **Records a checkpoint**
                          ``"calculation_result"``.                              §15
  - build_state         : ``WorkflowState`` snapshot (status="completed", totals
                          ``"0.00"`` — no money moved). Immutable (§8).
  - checkpoint          : append the final aggregate audit id; reflect
                          ``audit_refs`` + ``checkpoints`` into the snapshot.
                          ``InMemorySaver`` persists state.                     §11
  - respond             : ``_finalize_envelope`` builds the §14 envelope.       §14

**Checkpoints after every calculation** (the stricter §11 pattern from
ADR-0006/ADR-0007): ``evaluate_rules`` and ``build_calculation_result`` each
record a labeled ``_audit_ref`` into ``audit_refs`` and a ``checkpoints`` map
(``{rules, calculation_result}``), persisted by ``InMemorySaver`` at each
super-step.

Checkpointing uses the in-image ``InMemorySaver`` keyed by ``session_id`` (the
§11 fallback — non-durable). The supervisor's ``_node_invoke`` merges only
``data.totals{matched,outstanding,posted}`` and ``data.audit_refs`` into
``AgentState``; the 9 figures are NOT those keys → they stay in the envelope
``data`` (no ``AgentState`` schema change — mirrors ADR-0006 §7 / ADR-0007 §8).

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

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants & policy (v1). Tunables belong in Global Variables (§17) / the
#  overridable `rules` input; these defaults are the v1 policy.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"
DEFAULT_CURRENCY: str = "SAR"  # AR-bundle default (mirrors kitchen_revenue)

# 2dp / date / currency patterns (the contracts' patterns).
RE_2DP = re.compile(r"^-?\d+\.\d{2}$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_CURRENCY = re.compile(r"^[A-Z]{3}$")
_RE_IDENTS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_TWO_PLACES = Decimal("0.01")

# The 9 figures this flow produces (in CalculationResult.totals order).
FIGURE_KEYS: tuple[str, ...] = (
    "revenue", "discount", "vat", "municipality_tax", "royalty",
    "collections", "expenses", "net_receivable", "net_payable",
)

# Human-readable labels for the line items (one per figure).
FIGURE_LABELS: dict[str, str] = {
    "revenue": "Revenue",
    "discount": "Discount",
    "vat": "VAT",
    "municipality_tax": "Municipality Tax",
    "royalty": "Royalty",
    "collections": "Collections",
    "expenses": "Expenses",
    "net_receivable": "Net Receivable",
    "net_payable": "Net Payable",
}

# The seed ruleset — declarative, overridable via the `rules` input. The exact
# `net_receivable` / `net_payable` formulas are tunable data: an operator can
# change them without touching code. ``municipality_tax`` appears on both sides
# (billed to the customer → receivable; owed to the municipality → payable).
# Rates are decimal fractions carried in `payload.parameters` ("0.15" = 15%);
# at build phase they become LangFlow Global Variables referenced via `$GV:`.
SEED_RULESET: list[dict[str, Any]] = [
    {"rule_id": "R_REVENUE", "kind": "sum",
     "inputs": ["facts.gross_sales", "facts.returns", "facts.allowances"],
     "output": "revenue"},
    {"rule_id": "R_DISCOUNT", "kind": "pct_of", "base": "facts.gross_sales",
     "rate": "parameters.discount_rate", "output": "discount"},
    {"rule_id": "R_VAT", "kind": "pct_of", "base": "outputs.revenue",
     "rate": "parameters.vat_rate", "output": "vat"},
    {"rule_id": "R_MUNICIPALITY", "kind": "pct_of", "base": "outputs.revenue",
     "rate": "parameters.municipality_rate", "output": "municipality_tax"},
    {"rule_id": "R_ROYALTY", "kind": "pct_of", "base": "outputs.revenue",
     "rate": "parameters.royalty_rate", "output": "royalty"},
    {"rule_id": "R_COLLECTIONS", "kind": "sum",
     "inputs": ["facts.cash_collected", "facts.card_collected",
                "facts.bank_collected", "facts.online_collected",
                "facts.wallet_collected"],
     "output": "collections"},
    {"rule_id": "R_EXPENSES", "kind": "sum",
     "inputs": ["facts.expense_food", "facts.expense_labor",
                "facts.expense_overhead"],
     "output": "expenses"},
    {"rule_id": "R_NET_RECEIVABLE", "kind": "formula",
     "expr": "revenue - discount + vat + municipality_tax - collections",
     "output": "net_receivable"},
    {"rule_id": "R_NET_PAYABLE", "kind": "formula",
     "expr": "expenses + royalty + municipality_tax",
     "output": "net_payable"},
]
SEED_RULESET_JSON: str = json.dumps(SEED_RULESET, indent=2)


# --------------------------------------------------------------------------- #
#  Run-scoped context (NOT checkpointed — §8 keeps raw inputs out of state).
# --------------------------------------------------------------------------- #


class CalculationContext(TypedDict, total=False):
    """Per-run context passed to every node via ``Runtime[CalculationContext]``.

    Durable, resumable state lives in ``CalculationState`` (checkpointed).
    These are the transient inputs for one invocation; re-supplied on resume.
    """

    user_input: str
    rules: list  # parsed ruleset (the `rules` input default, overridable)
    actor: str  # Keycloak sub (§13); empty when unattributed
    session_id: str  # checkpoint thread id (adapter's conversationId)
    tenant: str
    flow_id: str
    model_name: str  # documented LLM hook (deterministic v1 ignores it)


# --------------------------------------------------------------------------- #
#  Typed state (constitution §8).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CalculationState:
    """The Calculation Flow's typed state (§8).

    Immutable dataclass — nodes return partial-update dicts; LangGraph merges.
    """

    trace_id: str
    flow_id: str
    tenant: str
    # created|resolved|validated|classified|evaluated|built|completed|failed
    status: str = "created"
    error: Optional[dict[str, str]] = None  # {"code": "AR_*", "message": "..."} (§9)
    created_at: str = ""
    updated_at: str = ""
    payload: Optional[dict] = None  # the parsed validated-JSON input
    parameters: dict = field(default_factory=dict)  # resolved rates (§17)
    facts: dict = field(default_factory=dict)
    parameter_warnings: list = field(default_factory=list)  # missing-rate warnings
    calculations: Optional[dict] = None  # the 9 figs (BRE output)
    calculation_result: Optional[dict] = None  # CalculationResult (reconcile)
    validation_report: Optional[dict] = None  # full ValidationResult
    exception_report: Optional[dict] = None  # ValidationResult scoped to failures
    workflow_state: Optional[dict] = None  # WorkflowState snapshot
    engine_error: Optional[dict] = None  # BRE error envelope (when failed)
    audit_refs: list = field(default_factory=list)
    checkpoints: dict = field(default_factory=dict)  # {<label>: audit_ref} (§11)


def _state_to_dict(state: Any) -> dict:
    """Coerce a ``CalculationState`` (or dict) snapshot to a plain dict."""
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


def _parse_date(value: str) -> Optional[str]:
    """Parse a date string to ``YYYY-MM-DD``; ``None`` when unparseable."""
    s = (value or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _audit_ref(trace_id: str, label: str) -> str:
    """Deterministic per-calculation audit record id (§11/§13)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"calculation-audit:{trace_id}:{label}"))


# --------------------------------------------------------------------------- #
#  Payload parse / parameter resolution / validation (pure).
# --------------------------------------------------------------------------- #


def _parse_payload(user_input: str) -> tuple[Optional[dict], Optional[dict]]:
    """Parse the validated-JSON payload from ``user_input``.

    Returns ``(payload, error)`` — exactly one is set. Malformed JSON →
    ``AR_VALIDATION`` (§9).
    """
    text = (user_input or "").strip()
    if not text:
        return None, {"code": "AR_VALIDATION",
                      "message": "no validated-JSON payload supplied"}
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, {"code": "AR_VALIDATION",
                      "message": f"payload JSON parse error: {exc}"}
    if not isinstance(obj, dict):
        return None, {"code": "AR_VALIDATION",
                      "message": "payload must be a JSON object"}
    return obj, None


def _output_names(rules: list) -> set[str]:
    """The set of output names produced by the calculation rules."""
    names: set[str] = set()
    for r in rules:
        if not isinstance(r, dict):
            continue
        out = r.get("output")
        if isinstance(out, str) and out:
            names.add(out)
    return names


def _referenced_parameters(rules: list, facts: dict) -> set[str]:
    """Collect parameter names referenced by the rules (for defaulting).

    Scans ``parameters.<n>`` dotted paths in sum/pct_of/amount, plus bare
    formula identifiers that are neither output names, known facts, nor numeric
    literals (those must be parameters). Used to default missing rates to
    ``"0.00"`` + a warning (§17 — not a hard fail).
    """
    refs: set[str] = set()
    out_names = _output_names(rules)
    for r in rules:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind", "assert")
        if kind == "sum":
            for p in (r.get("inputs") or []):
                if isinstance(p, str) and p.startswith("parameters."):
                    refs.add(p.split(".", 1)[1])
        elif kind == "pct_of":
            for k in ("base", "rate"):
                p = r.get(k)
                if isinstance(p, str) and p.startswith("parameters."):
                    refs.add(p.split(".", 1)[1])
        elif kind == "amount":
            p = r.get("source")
            if isinstance(p, str) and p.startswith("parameters."):
                refs.add(p.split(".", 1)[1])
        elif kind == "formula":
            for tok in _RE_IDENTS.findall(r.get("expr", "")):
                if tok in out_names or tok in facts:
                    continue
                if _to_decimal(tok) is not None:
                    continue
                refs.add(tok)
    return refs


def _resolve_parameters(rules: list, payload: dict) -> tuple[dict, list[dict]]:
    """Populate ``parameters`` from ``payload.parameters``; default missing rates.

    A rate referenced by the rules but absent from the payload defaults to
    ``"0.00"`` and emits a validation warning (§17 — not a hard fail).
    Returns ``(parameters, warnings)`` where each warning is a ValidationResult
    warning entry ``{path, code, message, rule_id?}``.
    """
    src = payload.get("parameters")
    params: dict[str, str] = {}
    if isinstance(src, dict):
        for k, v in src.items():
            params[str(k)] = _to_signed_2dp(v) if _to_decimal(v) is not None else str(v)
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    warnings: list[dict] = []
    for name in sorted(_referenced_parameters(rules, facts)):
        if name not in params:
            params[name] = "0.00"
            warnings.append({
                "path": f"parameters.{name}",
                "code": "AR_VALIDATION_MISSING_RATE",
                "message": f"rate {name} missing — defaulted to 0.00",
            })
    return params, warnings


def _build_validation_report(valid: bool, errors: list[dict],
                             warnings: list[dict],
                             trace_id: str) -> dict:
    """Build a ``ValidationResult`` (pure). ``contract_name="CalculationInputs"``."""
    return {
        "valid": valid,
        "contract_name": "CalculationInputs",
        "contract_version": CONTRACT_VERSION,
        "trace_id": trace_id,
        "errors": list(errors),
        "warnings": list(warnings),
    }


def _validate_payload(payload: dict, parameter_warnings: list[dict],
                      trace_id: str) -> tuple[dict, list[dict], Optional[dict]]:
    """Validate the input contract; build the full ``ValidationResult``.

    Returns ``(validation_report, warnings, error)``. A hard failure (no facts
    dict) sets ``error`` (→ ``failed``); per-fact/period/currency issues are
    warnings. Parameter-default warnings are folded in.
    """
    errors: list[dict] = []
    warnings: list[dict] = list(parameter_warnings)

    facts = payload.get("facts")
    if not isinstance(facts, dict):
        errors.append({"path": "facts", "code": "AR_VALIDATION",
                       "message": "facts must be a JSON object of named 2dp fields"})
    else:
        for k, v in facts.items():
            if _to_decimal(v) is None:
                warnings.append({
                    "path": f"facts.{k}",
                    "code": "AR_VALIDATION_AMOUNT",
                    "message": f"fact {k} not parseable as a number — "
                               f"will contribute 0.00",
                })

    period = payload.get("period")
    if period is not None:
        if not isinstance(period, dict):
            warnings.append({"path": "period", "code": "AR_VALIDATION",
                             "message": "period must be an object {start,end}"})
        else:
            for k in ("start", "end"):
                v = period.get(k)
                if v and not _parse_date(str(v)):
                    warnings.append({
                        "path": f"period.{k}",
                        "code": "AR_VALIDATION_DATE",
                        "message": f"period.{k} not ISO YYYY-MM-DD",
                    })

    currency = payload.get("currency")
    if currency and not RE_CURRENCY.match(str(currency)):
        warnings.append({"path": "currency", "code": "AR_VALIDATION_CURRENCY",
                         "message": "currency must match ^[A-Z]{3}$"})

    if errors:
        report = _build_validation_report(False, errors, warnings, trace_id)
        return report, warnings, {"code": "AR_VALIDATION",
                                  "message": errors[0]["message"]}
    report = _build_validation_report(True, [], warnings, trace_id)
    return report, warnings, None


def _classify_exceptions(validation_report: dict,
                         trace_id: str) -> dict:
    """Build the Exception Report = a ``ValidationResult`` scoped to failures.

    Mirrors the kitchen-revenue / intercompany exception report: errors + the
    per-fact warnings that mean a fact contributes 0 (the "exceptions"). With no
    hard errors, the exception report carries the warnings (so the consumer sees
    which facts/parameters were defaulted).
    """
    errs = list((validation_report or {}).get("errors") or [])
    warns = list((validation_report or {}).get("warnings") or [])
    items = errs + [w for w in warns
                    if w.get("code") in ("AR_VALIDATION_AMOUNT",
                                         "AR_VALIDATION_MISSING_RATE")]
    valid = len(items) == 0
    return _build_validation_report(valid, errs, items, trace_id)


def _output_to_rule_id(rules: list) -> dict[str, str]:
    """Map each output name → its producing rule_id (for line_items source_refs)."""
    out: dict[str, str] = {}
    for r in rules:
        if isinstance(r, dict):
            o = r.get("output")
            if isinstance(o, str) and o:
                out[o] = str(r.get("rule_id", o))
    return out


def _build_calculation_result(calculations: dict, rules: list,
                              trace_id: str, tenant: str,
                              currency: str) -> dict:
    """Assemble the ``CalculationResult`` (``calculation_type="reconcile"``)."""
    calc = calculations or {}
    totals: dict[str, str] = {}
    line_items: list[dict] = []
    out_to_rule = _output_to_rule_id(rules)
    for key in FIGURE_KEYS:
        val = calc.get(key)
        if val is None:
            # A figure the ruleset did not produce → 0.00 (defensive, §4).
            val = "0.00"
        totals[key] = _to_signed_2dp(val)
        line_items.append({
            "label": FIGURE_LABELS.get(key, key),
            "amount": totals[key],
            "source_refs": [out_to_rule.get(key, key)],
        })
    # Include any extra outputs the ruleset produced beyond the 9 (forward-compat).
    for key, val in calc.items():
        if key in FIGURE_KEYS:
            continue
        totals[key] = _to_signed_2dp(val)
        line_items.append({
            "label": FIGURE_LABELS.get(key, key),
            "amount": totals[key],
            "source_refs": [out_to_rule.get(key, key)],
        })
    return {
        "trace_id": trace_id,
        "tenant": tenant,
        "calculation_type": "reconcile",
        "totals": totals,
        "line_items": line_items,
        "currency": currency or DEFAULT_CURRENCY,
        "inputs_ref": trace_id,
        "computed_at": utc_now(),
        "contract_version": CONTRACT_VERSION,
    }


def build_workflow_state(trace_id: str, flow_id: str, tenant: str,
                         audit_refs: list, created_at: str,
                         updated_at: str) -> dict:
    """Build a ``WorkflowState`` snapshot (pure). v1: read-only, no money moved."""
    return {
        "trace_id": trace_id,
        "flow_id": flow_id,
        "tenant": tenant,
        "intent": "ar_calculation",
        "status": "completed",
        "matched_amount": "0.00",
        "outstanding_balance": "0.00",
        "posted_total": "0.00",
        "pending_approvals": [],
        "idempotency_keys": {},
        "audit_refs": list(audit_refs),
        "tool_call_ref": f"{trace_id}:ar_calculation:0",
        "contract_version": CONTRACT_VERSION,
        "created_at": created_at or utc_now(),
        "updated_at": updated_at or utc_now(),
    }


def _record_checkpoint(state: CalculationState, label: str) -> tuple[list, dict]:
    """Append a labeled audit ref + checkpoints map entry for a calc (§11)."""
    ref = _audit_ref(state.trace_id, label)
    audit_refs = list(state.audit_refs)
    if ref not in audit_refs:
        audit_refs.append(ref)
    checkpoints = {**state.checkpoints, label: ref}
    return audit_refs, checkpoints


# --------------------------------------------------------------------------- #
#  LangGraph nodes.
# --------------------------------------------------------------------------- #


def _ctx(runtime: Runtime[CalculationContext]) -> CalculationContext:
    return runtime.context or {}


def _node_ingest(state: CalculationState,
                 runtime: Runtime[CalculationContext]) -> dict:
    ctx = _ctx(runtime)
    now = utc_now()
    user_input = ctx.get("user_input", "")
    payload, err = _parse_payload(user_input)
    if err is not None:
        return {"status": "failed", "error": err,
                "payload": None, "created_at": now, "updated_at": now}
    trace_id = str(payload.get("trace_id") or state.trace_id or mint_id())
    tenant = str(payload.get("tenant") or state.tenant
                 or ctx.get("tenant", "cosmic-vikings"))
    return {
        "trace_id": trace_id,
        "flow_id": state.flow_id or ctx.get("flow_id", "ar_calculation"),
        "tenant": tenant,
        "payload": payload,
        "status": "created",
        "created_at": state.created_at or now,
        "updated_at": now,
    }


def _node_resolve_parameters(state: CalculationState,
                             runtime: Runtime[CalculationContext]) -> dict:
    ctx = _ctx(runtime)
    rules = ctx.get("rules") or []
    payload = state.payload or {}
    params, warnings = _resolve_parameters(rules, payload)
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    return {"parameters": params, "facts": dict(facts),
            "parameter_warnings": warnings, "status": "resolved",
            "updated_at": utc_now()}


def _node_validate(state: CalculationState,
                   runtime: Runtime[CalculationContext]) -> dict:
    _ = _ctx(runtime)
    payload = state.payload or {}
    report, _warnings, err = _validate_payload(payload, state.parameter_warnings,
                                               state.trace_id)
    if err is not None:
        return {"validation_report": report, "status": "failed",
                "error": err, "updated_at": utc_now()}
    return {"validation_report": report, "status": "validated",
            "updated_at": utc_now()}


def _node_classify_exceptions(state: CalculationState,
                              runtime: Runtime[CalculationContext]) -> dict:
    _ = _ctx(runtime)
    report = state.validation_report or _build_validation_report(True, [], [],
                                                                 state.trace_id)
    exception_report = _classify_exceptions(report, state.trace_id)
    return {"exception_report": exception_report, "status": "classified",
            "updated_at": utc_now()}


def _node_evaluate_rules(state: CalculationState,
                         runtime: Runtime[CalculationContext]) -> dict:
    """CORE: delegate all calculations to the Business Rule Engine (pure fn)."""
    ctx = _ctx(runtime)
    rules = ctx.get("rules") or []
    payload = {
        "facts": state.facts or {},
        "parameters": state.parameters or {},
        "outputs": {},
        # $GV: injection is build-phase — v1 reads concrete rates from parameters.
        "_global_variables": (state.payload or {}).get("_global_variables") or {},
    }
    # Lazy import: the BRE lives in the cosmic_common bundle (shared). Mirrors
    # kitchen_revenue's lazy cosmic_common reader import (ADR-0002 §15 waiver).
    from components.cosmic_common.business_rule_engine import _evaluate_rules
    result = _evaluate_rules(rules, payload, strict=False)
    if result.get("status") != "ok":
        err = {"code": result.get("code", "AR_VALIDATION"),
               "message": (result.get("error") or {}).get("message",
                           "business rule engine failed")}
        return {"engine_error": result, "error": err, "status": "failed",
                "updated_at": utc_now()}
    calculations = (result.get("data") or {}).get("calculations") or {}
    audit_refs, checkpoints = _record_checkpoint(state, "rules")
    return {"calculations": calculations, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "evaluated",
            "updated_at": utc_now()}


def _node_build_calculation_result(state: CalculationState,
                                   runtime: Runtime[CalculationContext]) -> dict:
    _ = _ctx(runtime)
    currency = (state.payload or {}).get("currency") or DEFAULT_CURRENCY
    calc_result = _build_calculation_result(state.calculations or {},
                                            _ctx_rules(state, runtime),
                                            state.trace_id, state.tenant,
                                            currency)
    audit_refs, checkpoints = _record_checkpoint(state, "calculation_result")
    return {"calculation_result": calc_result, "audit_refs": audit_refs,
            "checkpoints": checkpoints, "status": "built",
            "updated_at": utc_now()}


def _ctx_rules(state: CalculationState,
               runtime: Runtime[CalculationContext]) -> list:
    """Fetch the ruleset from context (used by build_calculation_result)."""
    return _ctx(runtime).get("rules") or []


def _node_build_state(state: CalculationState,
                      runtime: Runtime[CalculationContext]) -> dict:
    _ = _ctx(runtime)
    ws = build_workflow_state(state.trace_id, state.flow_id, state.tenant,
                              state.audit_refs, state.created_at, state.updated_at)
    return {"workflow_state": ws, "status": "completed",
            "updated_at": utc_now()}


def _node_checkpoint(state: CalculationState,
                     runtime: Runtime[CalculationContext]) -> dict:
    """Record the final aggregate audit id + reflect audit_refs/checkpoints."""
    _ = _ctx(runtime)
    audit_refs, checkpoints = _record_checkpoint(state, "ar_calculation")
    ws = state.workflow_state or {}
    if isinstance(ws, dict):
        ws = {**ws, "audit_refs": audit_refs}
    return {"audit_refs": audit_refs, "workflow_state": ws,
            "checkpoints": checkpoints, "updated_at": utc_now()}


def _node_respond(state: CalculationState,
                  runtime: Runtime[CalculationContext]) -> dict:
    """Terminal marker; ``run()`` assembles the envelope from final state."""
    _ = runtime
    return {"updated_at": utc_now()}


# Conditional routers (return state.status against status-keyed path maps).
def _after_ingest(state: CalculationState) -> str:
    return state.status


def _after_resolve(state: CalculationState) -> str:
    return state.status


def _after_validate(state: CalculationState) -> str:
    return state.status


def _after_classify(state: CalculationState) -> str:
    return state.status


def _after_evaluate(state: CalculationState) -> str:
    return state.status


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class CalculationFlowComponent(Component):
    name = "CalculationFlowComponent"
    display_name = "Cosmic AR Calculation Flow"
    description = (
        "Reads a Validated JSON payload (P10 Validation Flow output — an "
        "aggregated {facts, parameters, period, currency} object) and computes "
        "the nine AR figures — Revenue, Discount, VAT, Municipality Tax, Royalty, "
        "Collections, Expenses, Net Receivable, Net Payable — via the Business "
        "Rule Engine (no hardcoded formulas), then emits a CalculationResult + "
        "Validation/Exception reports + a WorkflowState snapshot — with logging, "
        "and checkpoints after every calculation (constitution §1/§4/§8/§9/§10/"
        "§11/§12/§14/§15/§16/§17/§19). The 7th AR subflow; v1 is read-only "
        "compute + report (no posting). §55 waiver — figures only, not statutory "
        "filing. See ADR-0008."
    )
    icon = "Calculator"

    inputs = [
        MessageTextInput(
            name="user_input",
            display_name="Validated JSON",
            info=(
                "The Validated JSON payload (P10 Validation Flow output): "
                "{trace_id, tenant, period:{start,end}, currency, "
                "facts:{<named 2dp fields>}, parameters:{<named rate fields>}}. "
                "This is the primary input — carries the facts + rates the rules "
                "compute against."
            ),
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="rules",
            display_name="Rules (JSON)",
            info=(
                "Declarative ruleset (Business Rule Engine). Defaults to the "
                "seed ruleset computing the 9 figures; override to change "
                "formulas/rates without touching code. Kinds: sum, pct_of, "
                "amount, formula, assert."
            ),
            value=SEED_RULESET_JSON,
            required=False,
            tool_mode=True,
        ),
        MessageTextInput(
            name="model_name",
            display_name="Model",
            value="glm-5.2:cloud",
            info="LLM model hook (v1: deterministic calculate; LLM path is build-phase).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="calculation_output",
            display_name="Calculation Result",
            method="run",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Graph construction (compiled once, cached per instance).
    # ------------------------------------------------------------------ #
    def _build_graph(self) -> Any:
        graph = StateGraph(state_schema=CalculationState,
                           context_schema=CalculationContext)
        graph.add_node("ingest", _node_ingest)
        graph.add_node("resolve_parameters", _node_resolve_parameters)
        graph.add_node("validate", _node_validate)
        graph.add_node("classify_exceptions", _node_classify_exceptions)
        graph.add_node("evaluate_rules", _node_evaluate_rules)
        graph.add_node("build_calculation_result", _node_build_calculation_result)
        graph.add_node("build_state", _node_build_state)
        graph.add_node("checkpoint", _node_checkpoint)
        graph.add_node("respond", _node_respond)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _after_ingest,
                                    {"failed": "respond", "created": "resolve_parameters"})
        graph.add_conditional_edges("resolve_parameters", _after_resolve,
                                    {"failed": "respond", "resolved": "validate"})
        graph.add_conditional_edges("validate", _after_validate,
                                    {"failed": "respond",
                                     "validated": "classify_exceptions"})
        graph.add_conditional_edges("classify_exceptions", _after_classify,
                                    {"failed": "respond",
                                     "classified": "evaluate_rules"})
        graph.add_conditional_edges("evaluate_rules", _after_evaluate,
                                    {"failed": "respond",
                                     "evaluated": "build_calculation_result"})
        graph.add_edge("build_calculation_result", "build_state")
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
            rules_raw = _to_str(getattr(self, "rules", "")) or SEED_RULESET_JSON
            try:
                rules = json.loads(rules_raw)
            except (TypeError, ValueError):
                rules = SEED_RULESET
            ctx: CalculationContext = {
                "user_input": user_input,
                "rules": rules,
                "actor": actor,
                "session_id": session_id,
                "tenant": "cosmic-vikings",
                "flow_id": "ar_calculation",
                "model_name": model_name,
            }
            graph = self._get_graph()
            config = {"configurable": {"thread_id": session_id}}
            initial = CalculationState(
                trace_id=mint_id(),
                flow_id=ctx["flow_id"],
                tenant=ctx["tenant"],
            )
            graph.invoke(initial, config=config, context=ctx)
            envelope = self._finalize_envelope(graph, config)
            self.log(
                f"event=calculation.run outcome={envelope.get('status')} "
                f"trace_id={envelope.get('trace_id')} "
                f"flow_id={envelope.get('flow_id')} "
                f"ar_entity=calculation outcome={envelope.get('status')} "
                f"code={envelope.get('code')}")
            return Message(text=json.dumps(envelope))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            env = _envelope("error", "AR_UNEXPECTED",
                            error={"message": "Calculation run failed.",
                                   "detail": str(exc)[:500]},
                            trace_id="")
            try:
                self.log("event=calculation.run outcome=error code=AR_UNEXPECTED")
            except Exception:  # noqa: BLE001 — logging must never crash the boundary
                pass
            return Message(text=json.dumps(env))

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    def _finalize_envelope(self, graph: Any, config: dict) -> dict[str, Any]:
        """Read the final state → §14 envelope (deterministic from state).

        ``graph.get_state(config).values`` is a plain dict (ADR-0003 §10), so
        fields are read by key. The 9 figures are NOT ``data.totals{matched,
        outstanding, posted}`` keys, so the supervisor's ``_node_invoke`` does
        not recognize them — they stay in the envelope ``data`` (no ``AgentState``
        schema change — ADR-0008). v1 is read-only compute + report, so ``data``
        carries no financial ``totals{matched,outstanding,posted}`` (those stay
        ``"0.00"`` inside ``data.workflow_state``).
        """
        snapshot = graph.get_state(config)
        vals = snapshot.values if isinstance(snapshot.values, dict) \
            else _state_to_dict(snapshot.values)
        facts = vals.get("facts") or {}
        calc_result = vals.get("calculation_result") or {}
        # rule_count: the ruleset lives in the run context (not state), so derive
        # the count from the CalculationResult's line_items (one per output).
        line_items = (calc_result.get("line_items") or []) \
            if isinstance(calc_result, dict) else []
        rule_count = len(line_items)
        audit_refs = vals.get("audit_refs") or []
        data: dict[str, Any] = {
            "calculation_result": calc_result,
            "calculations": vals.get("calculations") or {},
            "validation_report": vals.get("validation_report") or {},
            "exception_report": vals.get("exception_report") or {},
            "workflow_state": vals.get("workflow_state") or {},
            "audit_refs": list(audit_refs) if isinstance(audit_refs, list) else [],
            "checkpoints": vals.get("checkpoints") or {},
            "rule_count": rule_count,
            "fact_count": len(facts) if isinstance(facts, dict) else 0,
            "flow_id": vals.get("flow_id", ""),
            "tenant": vals.get("tenant", ""),
            "started_at": vals.get("created_at") or utc_now(),
            "ended_at": vals.get("updated_at") or utc_now(),
            "contract_version": CONTRACT_VERSION,
        }
        trace_id = vals.get("trace_id", "")
        if vals.get("status") == "failed":
            err = vals.get("error") or {"code": "AR_UNEXPECTED",
                                         "message": "calculation failed"}
            code = err.get("code", "AR_UNEXPECTED") if isinstance(err, dict) \
                else "AR_UNEXPECTED"
            err_env = {"message": err.get("message", "") if isinstance(err, dict) else str(err)}
            if isinstance(err, dict) and err.get("detail"):
                err_env["detail"] = err["detail"]
            return {"status": "error", "code": code, "trace_id": trace_id,
                    "data": data, "error": err_env,
                    "flow_id": vals.get("flow_id", "")}
        return {"status": "ok", "code": "AR_OK", "trace_id": trace_id,
                "data": data, "flow_id": vals.get("flow_id", "")}