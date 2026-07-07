#!/usr/bin/env python3
"""business_rule_engine_selftest — offline stdlib-only tests for the Business
Rule Engine's pure functions (constitution §4/§9/§15/§17).

Covers: decimal coercion + signed-2dp quantisation (ROUND_HALF_UP); the four
calculation rule kinds (sum, pct_of, amount, formula); the restricted
recursive-descent formula parser (precedence, parentheses, unary minus, NO
``/``, NO ``eval``, unknown operand → AR_VALIDATION); Kahn topological sort
over ``outputs.*`` refs (cycle → AR_VALIDATION, duplicate output →
AR_VALIDATION, unknown output ref → AR_VALIDATION); assert rules (==, !=, <,
<=, >, >=, in, not_in) evaluated AFTER calculations (can assert on computed
outputs); strict mode (failed assert → AR_RULE_FAILED) vs non-strict (AR_OK with
per-rule results); malformed-rule handling (non-list, non-object, missing
rule_id, unknown kind, calc kind without output → AR_VALIDATION); rate
resolution (literal decimal | dotted path | ``$GV:NAME``); and the seed
ruleset end-to-end (the 9 AR figures). No network, no LangFlow, no LangGraph,
no Docker — ``python3 business_rule_engine_selftest.py`` runs anywhere.
Mirrors the AR bundle self-test harness (CLAUDE.md self-test convention):
PASS/FAIL counts, exits non-zero on any failure, so ``make test`` (via
``scripts/business-rule-engine.selftest.sh``) and CI pick it up.

Run:  python3 docker/langflow-extensions/cosmic_common/components/cosmic_common/business_rule_engine_selftest.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx so business_rule_engine imports without the in-image venv.
# --------------------------------------------------------------------------- #


def _stub(name, attrs=None):
    m = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m


class _Component:
    def __init__(self, *a, **k):
        pass


class _Input:
    def __init__(self, *a, **k):
        pass


class _Message:
    def __init__(self, text=""):
        self.text = text


_stub("lfx")
_stub("lfx.custom", {"Component": _Component})
_stub("lfx.io", {"BoolInput": _Input, "MultilineInput": _Input, "Output": _Input})
_stub("lfx.schema", {"Message": _Message})

# Add the cosmic_common bundle root so `components.cosmic_common.business_rule_engine`
# resolves (mirrors the in-image pip-installed editable bundle).
# HERE = .../cosmic_common/components/cosmic_common → bundle root is 2 levels up.
_CC_BUNDLE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _CC_BUNDLE_ROOT)

import components.cosmic_common.business_rule_engine as bre  # noqa: E402

PASS = 0
FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  \033[32mPASS\033[0m {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  \033[31mFAIL\033[0m {name}" + (f" — {detail}" if detail else ""))


def eq(got, expected, name):
    if got == expected:
        ok(name)
    else:
        bad(name, f"expected {expected!r}, got {got!r}")


def truthy(value, name):
    ok(name) if value else bad(name, f"expected truthy, got {value!r}")


def falsy(value, name):
    ok(name) if not value else bad(name, f"expected falsy, got {value!r}")


# A canonical seed-ruleset payload used by several sections. The seed ruleset is
# the Calculation Flow's `rules` default (calculation.py); inlined here so this
# self-test stays within the cosmic_common bundle (no cross-bundle import).
SEED = [
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
SEED_JSON = __import__("json").dumps(SEED, indent=2)
PAYLOAD = {
    "facts": {
        "gross_sales": "10000.00", "returns": "-200.00", "allowances": "-100.00",
        "cash_collected": "3000.00", "card_collected": "2000.00",
        "bank_collected": "0", "online_collected": "0", "wallet_collected": "0",
        "expense_food": "1500.00", "expense_labor": "2000.00",
        "expense_overhead": "500.00",
    },
    "parameters": {
        "discount_rate": "0.05", "vat_rate": "0.15",
        "municipality_rate": "0.14", "royalty_rate": "0.02",
    },
}


# --------------------------------------------------------------------------- #
# [1] decimal coercion + signed-2dp quantisation
# --------------------------------------------------------------------------- #
print("[1] _to_decimal / _to_signed_2dp")
eq(bre._to_signed_2dp("1234.5"), "1234.50", "half-up quantise 1234.5 → 1234.50")
eq(bre._to_signed_2dp("1234.555"), "1234.56", "half-up quantise 1234.555 → 1234.56")
eq(bre._to_signed_2dp("-3.004"), "-3.00", "negative quantise")
eq(bre._to_signed_2dp(None), "0.00", "None → 0.00")
eq(bre._to_signed_2dp(""), "0.00", "empty → 0.00")
eq(bre._to_signed_2dp("SAR 1,234.50"), "1234.50", "strip currency + thousands sep")
eq(bre._to_signed_2dp(True), "0.00", "bool rejected → 0.00")
truthy(bre._to_decimal("0.15") is not None, "decimal literal parsed")

# --------------------------------------------------------------------------- #
# [2] sum rule
# --------------------------------------------------------------------------- #
print("[2] sum rule")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "sum",
      "inputs": ["facts.a", "facts.b", "facts.c"], "output": "x"}],
    {"facts": {"a": "10.00", "b": "20.00", "c": "-5.00"}})
eq(r["status"], "ok", "sum ok")
eq(r["data"]["calculations"]["x"], "25.00", "10+20-5 = 25.00")
# missing input → 0.00 (lenient)
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "sum", "inputs": ["facts.a", "facts.missing"],
      "output": "x"}], {"facts": {"a": "10.00"}})
eq(r["data"]["calculations"]["x"], "10.00", "missing input contributes 0.00")

# --------------------------------------------------------------------------- #
# [3] pct_of rule — literal / path / $GV rate; missing rate → AR_VALIDATION
# --------------------------------------------------------------------------- #
print("[3] pct_of rule")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.gross",
      "rate": "0.15", "output": "v"}], {"facts": {"gross": "1000.00"}})
eq(r["data"]["calculations"]["v"], "150.00", "literal rate 0.15 → 150.00")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.gross",
      "rate": "parameters.vat", "output": "v"}],
    {"facts": {"gross": "1000.00"}, "parameters": {"vat": "0.15"}})
eq(r["data"]["calculations"]["v"], "150.00", "path rate → 150.00")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.gross",
      "rate": "$GV:VAT", "output": "v"}],
    {"facts": {"gross": "1000.00"}, "_global_variables": {"VAT": "0.15"}})
eq(r["data"]["calculations"]["v"], "150.00", "$GV rate → 150.00")
# half-up rounding on pct: 1.00 * 0.005 = 0.005 → 0.01 (HALF_UP); 1.00*0.004 → 0.00
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.g",
      "rate": "0.005", "output": "v"}], {"facts": {"g": "1.00"}})
eq(r["data"]["calculations"]["v"], "0.01", "half-up pct 1.00*0.005 → 0.01")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.g",
      "rate": "0.004", "output": "v"}], {"facts": {"g": "1.00"}})
eq(r["data"]["calculations"]["v"], "0.00", "half-down pct 1.00*0.004 → 0.00")
# missing rate → AR_VALIDATION (never silent 0)
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.g",
      "rate": "parameters.missing", "output": "v"}], {"facts": {"g": "1000.00"}})
eq(r["code"], "AR_VALIDATION", "missing rate → AR_VALIDATION")
# missing $GV → AR_VALIDATION
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "pct_of", "base": "facts.g",
      "rate": "$GV:NOPE", "output": "v"}], {"facts": {"g": "1000.00"}})
eq(r["code"], "AR_VALIDATION", "missing $GV → AR_VALIDATION")
# base referencing a computed output (dependency)
r = bre._evaluate_rules([
    {"rule_id": "R1", "kind": "amount", "source": "facts.g", "output": "base"},
    {"rule_id": "R2", "kind": "pct_of", "base": "outputs.base",
     "rate": "0.10", "output": "v"}], {"facts": {"g": "200.00"}})
eq(r["data"]["calculations"]["v"], "20.00", "pct_of on computed output → 20.00")

# --------------------------------------------------------------------------- #
# [4] amount rule
# --------------------------------------------------------------------------- #
print("[4] amount rule")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "amount", "source": "facts.x", "output": "y"}],
    {"facts": {"x": "42.50"}})
eq(r["data"]["calculations"]["y"], "42.50", "amount copies value")
r = bre._evaluate_rules(
    [{"rule_id": "R1", "kind": "amount", "source": "facts.missing", "output": "y"}],
    {"facts": {}})
eq(r["data"]["calculations"]["y"], "0.00", "missing source → 0.00")

# --------------------------------------------------------------------------- #
# [5] formula — precedence, parens, unary, no division, no eval, unknown operand
# --------------------------------------------------------------------------- #
print("[5] formula parser")
def f(expr, payload=None):
    return bre._evaluate_rules(
        [{"rule_id": "R", "kind": "formula", "expr": expr, "output": "o"}],
        payload or {})
eq(f("2 + 3 * 4")["data"]["calculations"]["o"], "14.00", "precedence 2+3*4 = 14")
eq(f("(2 + 3) * 4")["data"]["calculations"]["o"], "20.00", "parens (2+3)*4 = 20")
eq(f("-(2 + 3) * 4")["data"]["calculations"]["o"], "-20.00", "unary minus -(5)*4 = -20")
eq(f("10 - 3 - 2")["data"]["calculations"]["o"], "5.00", "left-assoc subtraction = 5")
eq(f("2 * 3 + 4 * 5")["data"]["calculations"]["o"], "26.00", "6+20 = 26")
eq(f("  1 +  2 ")["data"]["calculations"]["o"], "3.00", "whitespace tolerated")
eq(f("a + b", {"facts": {"a": "1.50", "b": "2.50"}})["data"]["calculations"]["o"],
   "4.00", "bare fact operands")
eq(f("x * 2", {"parameters": {"x": "3.00"}})["data"]["calculations"]["o"],
   "6.00", "bare parameter operand (after facts/outputs miss)")
# operand resolution order: outputs shadow facts
r = bre._evaluate_rules([
    {"rule_id": "A", "kind": "amount", "source": "facts.x", "output": "dup"},
    {"rule_id": "B", "kind": "formula", "expr": "dup + 1", "output": "o"}],
    {"facts": {"x": "10.00", "dup": "999.00"}})
eq(r["data"]["calculations"]["o"], "11.00", "outputs shadow facts (10+1, not 999+1)")
# division rejected
eq(f("a / b", {"facts": {"a": "10", "b": "2"}})["code"], "AR_VALIDATION",
   "division → AR_VALIDATION")
# unknown operand → AR_VALIDATION
eq(f("zzz")["code"], "AR_VALIDATION", "unknown operand → AR_VALIDATION")
# trailing garbage
eq(f("1 + 2 abc")["code"], "AR_VALIDATION", "trailing input → AR_VALIDATION")
# empty formula
eq(f("")["code"], "AR_VALIDATION", "empty formula → AR_VALIDATION")
# unbalanced parens
eq(f("(1 + 2")["code"], "AR_VALIDATION", "unclosed paren → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [6] topological sort — cycle, duplicate output, unknown output ref
# --------------------------------------------------------------------------- #
print("[6] dependency ordering / toposort")
# correct ordering: VAT depends on revenue → revenue computed first
r = bre._evaluate_rules([
    {"rule_id": "R_VAT", "kind": "pct_of", "base": "outputs.revenue",
     "rate": "0.15", "output": "vat"},
    {"rule_id": "R_REV", "kind": "sum", "inputs": ["facts.a"], "output": "revenue"}],
    {"facts": {"a": "100.00"}})
eq(r["status"], "ok", "topo-ordered deps resolve")
eq(r["data"]["calculations"]["vat"], "15.00", "vat after revenue = 15.00")
# cycle → AR_VALIDATION
r = bre._evaluate_rules([
    {"rule_id": "A", "kind": "formula", "expr": "b", "output": "a"},
    {"rule_id": "B", "kind": "formula", "expr": "a", "output": "b"}], {})
eq(r["code"], "AR_VALIDATION", "cycle → AR_VALIDATION")
# duplicate output → AR_VALIDATION
r = bre._evaluate_rules([
    {"rule_id": "A", "kind": "amount", "source": "facts.x", "output": "y"},
    {"rule_id": "B", "kind": "amount", "source": "facts.x", "output": "y"}],
    {"facts": {"x": "1.00"}})
eq(r["code"], "AR_VALIDATION", "duplicate output → AR_VALIDATION")
# unknown output ref → AR_VALIDATION
r = bre._evaluate_rules([
    {"rule_id": "A", "kind": "pct_of", "base": "outputs.nope",
     "rate": "0.1", "output": "v"}], {})
eq(r["code"], "AR_VALIDATION", "unknown output ref → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [7] assert rules — ops, evaluated after calcs
# --------------------------------------------------------------------------- #
print("[7] assert rules")
def ass(field, op, value, payload, strict=False):
    return bre._evaluate_rules(
        [{"rule_id": "A", "kind": "amount", "source": "facts.x", "output": "y"},
         {"rule_id": "C", "kind": "assert", "field": field, "op": op, "value": value}],
        payload, strict=strict)
p = {"facts": {"x": "10.00"}}
eq(ass("facts.x", "==", "10.00", p)["status"], "ok", "assert == passes")
eq(ass("facts.x", "!=", "5.00", p)["status"], "ok", "assert != passes")
eq(ass("facts.x", ">", "5.00", p)["status"], "ok", "assert > passes")
eq(ass("facts.x", "<=", "10.00", p)["status"], "ok", "assert <= passes")
eq(ass("facts.x", ">=", "10.00", p)["status"], "ok", "assert >= passes")
eq(ass("facts.x", "<", "5.00", p)["status"], "ok", "assert < fails but non-strict ok")
# assert on a computed output (after calcs)
r = bre._evaluate_rules([
    {"rule_id": "A", "kind": "sum", "inputs": ["facts.a", "facts.b"], "output": "sum"},
    {"rule_id": "C", "kind": "assert", "field": "outputs.sum", "op": "==", "value": "30.00"}],
    {"facts": {"a": "10.00", "b": "20.00"}})
eq(r["status"], "ok", "assert on computed output passes")
# in / not_in
r = bre._evaluate_rules(
    [{"rule_id": "C", "kind": "assert", "field": "facts.ccy",
      "op": "in", "value": ["SAR", "USD"]}], {"facts": {"ccy": "SAR"}})
eq(r["status"], "ok", "assert in passes")
r = bre._evaluate_rules(
    [{"rule_id": "C", "kind": "assert", "field": "facts.ccy",
      "op": "not_in", "value": ["EUR"]}], {"facts": {"ccy": "SAR"}})
eq(r["status"], "ok", "assert not_in passes")
# invalid op → AR_VALIDATION
r = bre._evaluate_rules(
    [{"rule_id": "C", "kind": "assert", "field": "facts.x", "op": "~", "value": "1"}],
    {"facts": {"x": "1.00"}})
eq(r["code"], "AR_VALIDATION", "invalid assert op → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [8] strict mode
# --------------------------------------------------------------------------- #
print("[8] strict mode")
r = ass("facts.x", ">", "100.00", p, strict=True)
eq(r["code"], "AR_RULE_FAILED", "strict failed assert → AR_RULE_FAILED")
truthy("C" in (r.get("error") or {}).get("failed_rule_ids", []),
       "failed_rule_ids lists the failing rule")
# non-strict: failing assert reported but overall AR_OK
r = ass("facts.x", ">", "100.00", p, strict=False)
eq(r["code"], "AR_OK", "non-strict failing assert → AR_OK")
res = {x["rule_id"]: x["passed"] for x in r["data"]["results"]}
falsy(res.get("C"), "failing assert recorded as passed=False")

# --------------------------------------------------------------------------- #
# [9] malformed rules
# --------------------------------------------------------------------------- #
print("[9] malformed rules → AR_VALIDATION")
eq(bre._evaluate_rules("not a list", {})["code"], "AR_VALIDATION", "non-list rules")
eq(bre._evaluate_rules([{"no_rule_id": True}], {})["code"], "AR_VALIDATION",
   "rule missing rule_id")
eq(bre._evaluate_rules([{"rule_id": "A", "kind": "sum", "inputs": ["facts.a"]}],
                       {})["code"], "AR_VALIDATION", "calc rule without output")
eq(bre._evaluate_rules([{"rule_id": "A", "kind": "bogus", "output": "x"}],
                       {})["code"], "AR_VALIDATION", "unknown calc kind")
eq(bre._evaluate_rules([{"rule_id": "A", "kind": "sum", "inputs": "notlist",
                         "output": "x"}], {})["code"], "AR_VALIDATION",
   "sum inputs not a list")
eq(bre._evaluate_rules([], {})["status"], "ok", "empty ruleset → AR_OK (no calcs, no asserts)")
eq(bre._evaluate_rules([], "notdict")["code"], "AR_VALIDATION", "non-dict payload")

# --------------------------------------------------------------------------- #
# [10] seed ruleset end-to-end (the 9 AR figures)
# --------------------------------------------------------------------------- #
print("[10] seed ruleset end-to-end")
r = bre._evaluate_rules(SEED, PAYLOAD)
eq(r["status"], "ok", "seed ruleset ok")
calc = r["data"]["calculations"]
eq(calc["revenue"], "9700.00", "revenue = 10000-200-100")
eq(calc["discount"], "500.00", "discount = 10000*0.05")
eq(calc["vat"], "1455.00", "vat = 9700*0.15")
eq(calc["municipality_tax"], "1358.00", "municipality = 9700*0.14")
eq(calc["royalty"], "194.00", "royalty = 9700*0.02")
eq(calc["collections"], "5000.00", "collections = 3000+2000")
eq(calc["expenses"], "4000.00", "expenses = 1500+2000+500")
eq(calc["net_receivable"], "7013.00", "net_receivable = 9700-500+1455+1358-5000")
eq(calc["net_payable"], "5552.00", "net_payable = 4000+194+1358")
eq(len(r["data"]["results"]), 9, "9 rule results")
truthy(all(x["passed"] for x in r["data"]["results"]), "all 9 seed rules pass")
# every calculation is a signed-2dp string
import re as _re
truthy(all(_re.match(r"^-?\d+\.\d{2}$", v) for v in calc.values()),
       "all calculations signed-2dp")

# --------------------------------------------------------------------------- #
# [11] evaluate() wrapper — JSON parse errors → AR_VALIDATION; never raises
# --------------------------------------------------------------------------- #
print("[11] evaluate() lfx wrapper")
class _FakeComp(bre.BusinessRuleEngineComponent):
    def __init__(self, rules, payload, strict=False):
        self.rules = rules
        self.payload = payload
        self.strict = strict
import json as _json
msg = _FakeComp(SEED_JSON, _json.dumps(PAYLOAD)).evaluate()
env = _json.loads(msg.text)
eq(env["status"], "ok", "evaluate() ok with seed + payload")
eq(env["data"]["calculations"]["net_receivable"], "7013.00", "evaluate() computes net_receivable")
msg = _FakeComp("not json", "{}").evaluate()
eq(_json.loads(msg.text)["code"], "AR_VALIDATION", "bad rules JSON → AR_VALIDATION")
msg = _FakeComp("[]", "not json").evaluate()
eq(_json.loads(msg.text)["code"], "AR_VALIDATION", "bad payload JSON → AR_VALIDATION")
# never raises even on pathological input
msg = _FakeComp(None, None).evaluate()
truthy(msg is not None, "evaluate() on None inputs returns a Message")

# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)