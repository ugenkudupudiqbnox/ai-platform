"""Business rule engine component (constitution §9, §17).

Generic, reusable engine that evaluates a **declarative rule set** against a
payload. Originally a boolean-assertion scaffold (``[{rule_id, field, op,
value}]`` → per-rule pass/fail); the Calculation Flow (ADR-0008) extends the
rule schema with **calculation** rule kinds so the engine is the single
calculator for the 9 Revenue/Discount/VAT/Municipality Tax/Royalty/Collections/
Expenses/Net Receivable/Net Payable figures — the flow component itself holds
**zero formulas** ("No hardcoded business rules. All calculations must use
Business Rule Engine." — ``prompts/P11_calculation_flow.md``).

Rule schema (``kind`` defaults to ``"assert"`` — backward compatible):

  - ``assert``   : ``{rule_id, kind:"assert", field, op, value}`` →
                   ``{rule_id, passed, message}``. ops ``== != < <= > >=
                   in not_in``. Evaluated AFTER all calculations, so an assert
                   may reference a computed ``outputs.<name>``.
  - ``sum``      : ``{rule_id, kind:"sum", inputs:[<dotted paths>], output}`` —
                   Σ inputs, 2dp ``ROUND_HALF_UP``; a missing input → ``0.00``.
  - ``pct_of``   : ``{rule_id, kind:"pct_of", base:<path>, rate:<literal|path|
                   $GV:NAME>, output}`` — ``(base * rate)`` 2dp ``ROUND_HALF_UP``.
                   ``rate`` is a decimal fraction (``"0.15"`` = 15%).
  - ``amount``   : ``{rule_id, kind:"amount", source:<path>, output}`` — copies
                   a single value (2dp).
  - ``formula``  : ``{rule_id, kind:"formula", expr:"...", output}`` — a
                   restricted recursive-descent parser over ``+ - * ( )`` +
                   unary ``-`` + decimal literals + named operands (NO ``/``,
                   NO ``eval``). Operand resolution order: outputs → facts →
                   parameters.

Paths: ``facts.<n>``, ``parameters.<n>``, ``outputs.<n>``, or a top-level
payload key. ``$GV:NAME`` resolves via ``payload["_global_variables"][NAME]``
(forward-compatible — populated at build phase from LangFlow Global Variables,
constitution §17; the Calculation Flow carries the seed rates in
``payload.parameters`` for v1).

Dependency ordering: a Kahn topological sort over ``outputs.*`` references — a
calc rule depends on every output name it reads. A cycle, a duplicate
``output``, a malformed rule, an unresolvable operand, or an unsafe expression
returns ``code=AR_VALIDATION`` (§9). In ``strict`` mode a failing ``assert``
returns ``code=AR_RULE_FAILED``; otherwise asserts are reported per-rule and the
overall result stays ``AR_OK``.

All evaluation logic lives in module-level **pure functions**
(``_evaluate_rules`` and friends) so it is testable without LangFlow/LangGraph;
``evaluate()`` is the thin lfx wrapper that parses the inputs, calls the pure
function, and wraps the dict in a ``Message``. The output method **never
raises** (§5/§9): malformed JSON / unexpected errors return ``AR_VALIDATION`` /
``AR_UNEXPECTED`` envelopes.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

from lfx.custom import Component
from lfx.io import BoolInput, MultilineInput, Output
from lfx.schema import Message

# --------------------------------------------------------------------------- #
#  Constants.
# --------------------------------------------------------------------------- #

CONTRACT_VERSION: str = "1.0.0"

_TWO_PLACES = Decimal("0.01")

# A path segment root or a bare formula operand. Identifiers are conservative:
# letter/underscore start, then word chars. ``$GV:NAME`` is handled separately.
_RE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Tokens pulled out of a formula expression for dependency analysis.
_RE_IDENTS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Valid ``assert`` operators.
_ASSERT_OPS = frozenset(("==", "!=", "<", "<=", ">", ">=", "in", "not_in"))

# Valid calculation rule kinds.
_CALC_KINDS = frozenset(("sum", "pct_of", "amount", "formula"))


class _RuleError(ValueError):
    """Raised by pure helpers to signal a malformed rule / unresolvable operand.

    ``_evaluate_rules`` converts this into an ``AR_VALIDATION`` envelope (§9) —
    it never escapes the output method.
    """


# --------------------------------------------------------------------------- #
#  Pure helpers (testable without LangFlow/LangGraph).
# --------------------------------------------------------------------------- #


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a numeric value/string to ``Decimal``; ``None`` on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
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


def _to_signed_2dp(value: Any) -> str:
    """Coerce a numeric to a signed 2dp string (allows negatives)."""
    d = _to_decimal(value)
    if d is None:
        return "0.00"
    return f"{d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)}"


def _quantize_2dp(d: Decimal) -> Decimal:
    return d.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _err(code: str, message: str, *, results: Optional[list] = None,
         calculations: Optional[dict] = None) -> dict[str, Any]:
    """Build an ``error`` engine envelope (§9)."""
    return {
        "status": "error",
        "code": code,
        "data": {
            "results": results or [],
            "calculations": calculations or {},
        },
        "error": {"message": message},
    }


_PATH_ROOTS = ("facts.", "parameters.", "outputs.", "_global_variables.")


def _is_path(token: Any) -> bool:
    """True for ``facts.x`` / ``parameters.x`` / ``outputs.x`` paths or ``$GV:NAME``.

    A bare decimal literal (``"0.15"``) is NOT a path — it has a ``.`` but no
    known root prefix, so it is treated as a numeric literal by callers.
    """
    if not isinstance(token, str) or not token:
        return False
    return token.startswith("$GV:") or any(token.startswith(r) for r in _PATH_ROOTS)


def _resolve_path(path: Any, payload: dict[str, Any]) -> Optional[Any]:
    """Resolve a dotted path against the payload.

    ``facts.<n>``, ``parameters.<n>``, ``outputs.<n>``, or a top-level payload
    key. Returns the raw value (caller coerces to ``Decimal``), or ``None`` when
    the path is absent / malformed.
    """
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("$GV:"):
        return _resolve_rate(path, payload)
    parts = path.split(".")
    if len(parts) == 1:
        return payload.get(parts[0])
    root = parts[0]
    if root not in ("facts", "parameters", "outputs", "_global_variables"):
        return None
    cur: Any = payload.get(root) if root != "outputs" else payload.get("outputs")
    if not isinstance(cur, dict):
        return None
    for seg in parts[1:]:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _resolve_rate(rate: Any, payload: dict[str, Any]) -> Decimal:
    """Resolve a rate literal / path / ``$GV:NAME`` to a ``Decimal``.

    A missing/unresolvable rate is a hard error (``_RuleError``) — a silent
    ``0%`` would produce wrong figures and violate the "no silent failures"
    fail-safe (§4). The Calculation Flow's ``resolve_parameters`` node supplies
    concrete rates (with a warning, not a hard fail) before the engine runs, so
    a missing rate here means the ruleset references an undefined parameter.
    """
    if rate is None or rate == "":
        raise _RuleError("rate is empty")
    if isinstance(rate, (int, float, Decimal)):
        d = _to_decimal(rate)
        if d is None:
            raise _RuleError(f"rate is not numeric: {rate!r}")
        return d
    s = str(rate).strip()
    if s.startswith("$GV:"):
        name = s[4:]
        gvars = payload.get("_global_variables") or {}
        if not isinstance(gvars, dict) or name not in gvars:
            raise _RuleError(f"global variable not found: {name}")
        d = _to_decimal(gvars[name])
        if d is None:
            raise _RuleError(f"global variable {name} is not numeric")
        return d
    if _is_path(s):
        val = _resolve_path(s, payload)
        d = _to_decimal(val)
        if d is None:
            raise _RuleError(f"rate path unresolved or non-numeric: {s}")
        return d
    # Literal decimal fraction, e.g. "0.15".
    d = _to_decimal(s)
    if d is None:
        raise _RuleError(f"rate is not numeric: {rate!r}")
    return d


def _resolve_operand(token: str, ctx: dict[str, Any]) -> Decimal:
    """Resolve a bare formula operand to a ``Decimal`` (formula parser).

    Resolution order: ``outputs`` → ``facts`` → ``parameters``. A numeric
    literal is parsed directly. An unknown identifier raises ``_RuleError``
    (→ ``AR_VALIDATION``) — never silently zero.
    """
    d = _to_decimal(token)
    if d is not None and not _is_path(token):
        # Numeric literal (and not a path like "facts.0").
        return d
    if not _RE_IDENT.match(token):
        raise _RuleError(f"invalid operand: {token!r}")
    for bucket in ("outputs", "facts", "parameters"):
        b = ctx.get(bucket)
        if isinstance(b, dict) and token in b:
            d = _to_decimal(b[token])
            if d is None:
                raise _RuleError(f"operand {token!r} is not numeric")
            return d
    raise _RuleError(f"unknown operand: {token!r}")


# --------------------------------------------------------------------------- #
#  Restricted recursive-descent formula parser (NO ``/``, NO ``eval``).
# --------------------------------------------------------------------------- #


class _FormulaParser:
    """``expr := term (('+'|'-') term)*`` / ``term := factor ('*' factor)*`` /
    ``factor := '-' factor | '(' expr ')' | number | operand``.

    No division operator is accepted — a ``/`` raises ``_RuleError`` (→
    ``AR_VALIDATION``). Operands resolve through ``_resolve_operand``.
    """

    def __init__(self, expr: str, ctx: dict[str, Any]) -> None:
        self._s = expr or ""
        self._i = 0
        self._ctx = ctx

    def parse(self) -> Decimal:
        self._skip_ws()
        if self._i >= len(self._s):
            raise _RuleError("empty formula")
        val = self._expr()
        self._skip_ws()
        if self._i != len(self._s):
            raise _RuleError(f"unexpected trailing input: {self._s[self._i:]!r}")
        return val

    def _skip_ws(self) -> None:
        while self._i < len(self._s) and self._s[self._i].isspace():
            self._i += 1

    def _peek(self) -> str:
        return self._s[self._i] if self._i < len(self._s) else ""

    def _expr(self) -> Decimal:
        val = self._term()
        while True:
            self._skip_ws()
            op = self._peek()
            if op == "+":
                self._i += 1
                val = val + self._term()
            elif op == "-":
                self._i += 1
                val = val - self._term()
            else:
                break
        return val

    def _term(self) -> Decimal:
        val = self._factor()
        while True:
            self._skip_ws()
            op = self._peek()
            if op == "*":
                self._i += 1
                val = val * self._factor()
            elif op == "/":
                raise _RuleError("division is not permitted in a formula")
            else:
                break
        return val

    def _factor(self) -> Decimal:
        self._skip_ws()
        c = self._peek()
        if c == "":
            raise _RuleError("unexpected end of formula")
        if c == "-":
            self._i += 1
            return -self._factor()
        if c == "+":
            self._i += 1
            return self._factor()
        if c == "(":
            self._i += 1
            val = self._expr()
            self._skip_ws()
            if self._peek() != ")":
                raise _RuleError("missing closing parenthesis")
            self._i += 1
            return val
        if c == ")":
            raise _RuleError("unexpected closing parenthesis")
        # number or operand
        start = self._i
        while self._i < len(self._s) and (self._s[self._i].isalnum()
                                          or self._s[self._i] in "._"):
            self._i += 1
        token = self._s[start:self._i]
        if not token:
            raise _RuleError(f"unexpected character: {c!r}")
        return _resolve_operand(token, self._ctx)


def _eval_formula(expr: str, ctx: dict[str, Any]) -> Decimal:
    return _FormulaParser(expr, ctx).parse()


# --------------------------------------------------------------------------- #
#  Calculation rule evaluation.
# --------------------------------------------------------------------------- #


def _eval_sum(rule: dict, payload: dict[str, Any]) -> Decimal:
    total = Decimal("0.00")
    inputs = rule.get("inputs")
    if not isinstance(inputs, list):
        raise _RuleError("sum requires an `inputs` list")
    for p in inputs:
        d = _to_decimal(_resolve_path(p, payload)) if isinstance(p, str) else None
        if d is None:
            d = Decimal("0.00")  # missing input → 0.00 (lenient, per spec)
        total += d
    return _quantize_2dp(total)


def _eval_pct_of(rule: dict, payload: dict[str, Any]) -> Decimal:
    base = _to_decimal(_resolve_path(rule.get("base"), payload))
    if base is None:
        base = Decimal("0.00")
    rate = _resolve_rate(rule.get("rate"), payload)
    return _quantize_2dp(base * rate)


def _eval_amount(rule: dict, payload: dict[str, Any]) -> Decimal:
    d = _to_decimal(_resolve_path(rule.get("source"), payload))
    if d is None:
        d = Decimal("0.00")
    return _quantize_2dp(d)


def _eval_calc(rule: dict, ctx: dict[str, Any]) -> Decimal:
    kind = rule.get("kind")
    payload = {  # _resolve_path reads facts/parameters/outputs off this view
        "facts": ctx.get("facts") or {},
        "parameters": ctx.get("parameters") or {},
        "outputs": ctx.get("outputs") or {},
        "_global_variables": ctx.get("_global_variables") or {},
    }
    if kind == "sum":
        return _eval_sum(rule, payload)
    if kind == "pct_of":
        return _eval_pct_of(rule, payload)
    if kind == "amount":
        return _eval_amount(rule, payload)
    if kind == "formula":
        return _eval_formula(rule.get("expr", ""), ctx)
    raise _RuleError(f"unknown calc kind: {kind!r}")


# --------------------------------------------------------------------------- #
#  Assert rule evaluation.
# --------------------------------------------------------------------------- #


def _coerce_pair(a: Any, b: Any) -> tuple[Any, Any]:
    """Coerce two values to a comparable pair: decimals if both numeric, else str."""
    da, db = _to_decimal(a), _to_decimal(b)
    if da is not None and db is not None:
        return da, db
    return str(a) if a is not None else "", str(b) if b is not None else ""


def _eval_assert(rule: dict, ctx: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate one ``assert`` rule → ``(passed, message)``."""
    field = rule.get("field")
    op = rule.get("op")
    expected = rule.get("value")
    if not isinstance(field, str) or not field:
        raise _RuleError("assert requires a `field` path")
    if op not in _ASSERT_OPS:
        raise _RuleError(f"assert op not permitted: {op!r}")
    payload = {
        "facts": ctx.get("facts") or {},
        "parameters": ctx.get("parameters") or {},
        "outputs": ctx.get("outputs") or {},
        "_global_variables": ctx.get("_global_variables") or {},
    }
    actual = _resolve_path(field, payload)
    rid = rule.get("rule_id", "")
    if op == "in":
        passed = isinstance(expected, list) and actual in expected
        return passed, f"{rid}: {field} in {expected!r} → {passed}"
    if op == "not_in":
        passed = isinstance(expected, list) and actual not in expected
        return passed, f"{rid}: {field} not_in {expected!r} → {passed}"
    a, b = _coerce_pair(actual, expected)
    if op == "==":
        passed = a == b
    elif op == "!=":
        passed = a != b
    elif op == "<":
        passed = a < b
    elif op == "<=":
        passed = a <= b
    elif op == ">":
        passed = a > b
    elif op == ">=":
        passed = a >= b
    else:  # pragma: no cover — guarded above
        raise _RuleError(f"assert op not permitted: {op!r}")
    return passed, f"{rid}: {field} {op} {expected!r} → {passed}"


# --------------------------------------------------------------------------- #
#  Dependency analysis + Kahn topological sort.
# --------------------------------------------------------------------------- #


def _output_refs_in_path(path: Any, output_names: set[str]) -> set[str]:
    """Return the set of output names referenced by a dotted path.

    Any ``outputs.<name>`` path is collected unfiltered — a reference to an
    output no rule produces is caught by ``_toposort_rules`` as an unknown
    output (→ ``AR_VALIDATION``), so a typo'd output name is never silently 0.
    """
    _ = output_names  # kept for signature stability
    if not isinstance(path, str) or not path.startswith("outputs."):
        return set()
    return {path[len("outputs."):]}


def _calc_deps(rule: dict, output_names: set[str]) -> set[str]:
    """Collect the output names a calculation rule depends on."""
    refs: set[str] = set()
    kind = rule.get("kind")
    if kind == "sum":
        for p in (rule.get("inputs") or []):
            refs |= _output_refs_in_path(p, output_names)
    elif kind == "pct_of":
        refs |= _output_refs_in_path(rule.get("base"), output_names)
        refs |= _output_refs_in_path(rule.get("rate"), output_names)
    elif kind == "amount":
        refs |= _output_refs_in_path(rule.get("source"), output_names)
    elif kind == "formula":
        for tok in _RE_IDENTS.findall(rule.get("expr", "")):
            if tok in output_names:
                refs.add(tok)
    return refs


def _toposort_rules(calcs: list[dict],
                    output_names: set[str]) -> list[dict]:
    """Kahn topological sort of calculation rules over ``outputs.*`` deps.

    Raises ``_RuleError`` on a cycle. Returns the rules in dependency order.
    """
    deps: dict[str, set[str]] = {}
    by_output: dict[str, dict] = {}
    for r in calcs:
        out = r["output"]
        by_output[out] = r
        deps[out] = _calc_deps(r, output_names)
    # adjacency: dep -> dependents
    indeg: dict[str, int] = {o: 0 for o in by_output}
    adj: dict[str, list[str]] = {o: [] for o in by_output}
    for out, ds in deps.items():
        for d in ds:
            if d not in by_output:
                # references an output that no rule produces → unresolved dep
                raise _RuleError(f"rule {_r_id(by_output[out])} references "
                                 f"unknown output: {d}")
            adj[d].append(out)
            indeg[out] += 1
    queue = [o for o in by_output if indeg[o] == 0]
    order: list[str] = []
    while queue:
        o = queue.pop(0)
        order.append(o)
        for nxt in adj[o]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(by_output):
        raise _RuleError("rule dependency cycle detected")
    return [by_output[o] for o in order]


def _r_id(rule: dict) -> str:
    return str(rule.get("rule_id", "?"))


# --------------------------------------------------------------------------- #
#  The pure entry point.
# --------------------------------------------------------------------------- #


def _evaluate_rules(rules: Any, payload: Any, strict: bool = False) -> dict[str, Any]:
    """Evaluate a declarative ruleset against a payload (pure).

    Returns an engine envelope dict:
      - ok                  → ``{"status":"ok","code":"AR_OK","data":{...}}``
      - strict + failed     → ``{"status":"error","code":"AR_RULE_FAILED",...}``
      - malformed/cycle     → ``{"status":"error","code":"AR_VALIDATION",...}``

    Never raises (§9): internal ``_RuleError``s become ``AR_VALIDATION``.
    """
    if not isinstance(rules, list):
        return _err("AR_VALIDATION", "rules must be a JSON array")
    if not isinstance(payload, dict):
        return _err("AR_VALIDATION", "payload must be a JSON object")

    output_names: set[str] = set()
    calcs: list[dict] = []
    asserts: list[dict] = []
    for r in rules:
        if not isinstance(r, dict) or "rule_id" not in r:
            return _err("AR_VALIDATION", "each rule must be an object with rule_id")
        kind = r.get("kind", "assert")
        out = r.get("output")
        if out is not None:
            if not isinstance(out, str) or not out:
                return _err("AR_VALIDATION", f"rule {_r_id(r)}: output must be a non-empty string")
            if out in output_names:
                return _err("AR_VALIDATION", f"duplicate output: {out}")
            if kind not in _CALC_KINDS:
                return _err("AR_VALIDATION", f"rule {_r_id(r)}: unknown calc kind {kind!r}")
            output_names.add(out)
            calcs.append(r)
        else:
            if kind != "assert":
                return _err("AR_VALIDATION", f"rule {_r_id(r)}: kind {kind!r} requires an output")
            asserts.append(r)

    # Topologically order the calculation rules.
    try:
        ordered = _toposort_rules(calcs, output_names)
    except _RuleError as e:
        return _err("AR_VALIDATION", str(e))

    ctx: dict[str, Any] = {
        "outputs": {},
        "facts": payload.get("facts") or {},
        "parameters": payload.get("parameters") or {},
        "_global_variables": payload.get("_global_variables") or {},
    }
    results: list[dict] = []

    for r in ordered:
        try:
            val = _eval_calc(r, ctx)
        except _RuleError as e:
            return _err("AR_VALIDATION", f"rule {_r_id(r)}: {e}",
                        results=results, calculations=ctx["outputs"])
        except Exception as e:  # noqa: BLE001 — defend the pure fn (§9)
            return _err("AR_VALIDATION", f"rule {_r_id(r)}: {e}",
                        results=results, calculations=ctx["outputs"])
        s = _to_signed_2dp(val)
        ctx["outputs"][r["output"]] = s
        results.append({"rule_id": r.get("rule_id"), "passed": True,
                        "message": f"{r['output']}={s}"})

    failed: list[str] = []
    for r in asserts:
        try:
            passed, message = _eval_assert(r, ctx)
        except _RuleError as e:
            return _err("AR_VALIDATION", f"rule {_r_id(r)}: {e}",
                        results=results, calculations=ctx["outputs"])
        except Exception as e:  # noqa: BLE001 — defend the pure fn (§9)
            return _err("AR_VALIDATION", f"rule {_r_id(r)}: {e}",
                        results=results, calculations=ctx["outputs"])
        results.append({"rule_id": r.get("rule_id"), "passed": passed,
                        "message": message})
        if not passed:
            failed.append(str(r.get("rule_id")))

    if strict and failed:
        return {
            "status": "error",
            "code": "AR_RULE_FAILED",
            "data": {"results": results, "calculations": ctx["outputs"]},
            "error": {"failed_rule_ids": failed},
        }
    return {
        "status": "ok",
        "code": "AR_OK",
        "data": {"results": results, "calculations": ctx["outputs"]},
    }


# --------------------------------------------------------------------------- #
#  The lfx Component.
# --------------------------------------------------------------------------- #


class BusinessRuleEngineComponent(Component):
    name = "BusinessRuleEngineComponent"
    display_name = "Business Rule Engine"
    description = (
        "Evaluate a declarative rule set against a payload. Supports boolean "
        "asserts (==, !=, <, <=, >, >=, in, not_in) and calculation rule kinds "
        "(sum, pct_of, amount, formula) so this engine is the single calculator "
        "for the AR Calculation Flow — the flow holds no hardcoded formulas. "
        "Rates may be literals, dotted paths, or $GV:NAME Global Variables. "
        "Malformed rules return AR_VALIDATION; a failed assert in strict mode "
        "returns AR_RULE_FAILED. Never raises (constitution §5/§9)."
    )
    icon = "ListChecks"

    inputs = [
        MultilineInput(
            name="rules",
            display_name="Rules (JSON)",
            info=(
                'JSON array of rules, e.g. '
                '[{"rule_id":"R_REVENUE","kind":"sum","inputs":["facts.gross_sales",'
                '"facts.returns"],"output":"revenue"}, '
                '{"rule_id":"R_VAT","kind":"pct_of","base":"outputs.revenue",'
                '"rate":"parameters.vat_rate","output":"vat"}, '
                '{"rule_id":"A1","kind":"assert","field":"outputs.vat",'
                '"op":">=","value":"0.00"}].'
            ),
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="payload",
            display_name="Payload (JSON)",
            info=(
                "JSON object to evaluate the rules against: "
                "{facts:{...}, parameters:{...}, outputs:{...}, "
                "_global_variables:{...}}."
            ),
            required=True,
            tool_mode=True,
        ),
        BoolInput(
            name="strict",
            display_name="Strict",
            value=False,
            info="If true, any failing assert makes the overall result an error (AR_RULE_FAILED).",
        ),
    ]

    outputs = [
        Output(
            name="engine_output",
            display_name="Rule Results",
            method="evaluate",
        ),
    ]

    def evaluate(self) -> Message:
        """Evaluate the ruleset and return the engine envelope as a Message.

        Never raises (§5/§9): malformed JSON → ``AR_VALIDATION``; an unexpected
        error → ``AR_UNEXPECTED``.
        """
        try:
            try:
                rules = json.loads(self.rules) if self.rules else []
            except (TypeError, ValueError) as exc:
                return Message(text=json.dumps(
                    _err("AR_VALIDATION", f"rules JSON parse error: {exc}")
                ))
            try:
                payload = json.loads(self.payload) if self.payload else {}
            except (TypeError, ValueError) as exc:
                return Message(text=json.dumps(
                    _err("AR_VALIDATION", f"payload JSON parse error: {exc}")
                ))
            strict = bool(self.strict)
            result = _evaluate_rules(rules, payload, strict=strict)
            return Message(text=json.dumps(result))
        except Exception as exc:  # noqa: BLE001 — §5: never raise out of the output method
            return Message(text=json.dumps(
                _err("AR_UNEXPECTED", f"engine failed: {exc}")
            ))