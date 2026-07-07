#!/usr/bin/env python3
"""calculation_selftest — offline stdlib-only tests for the Cosmic AR Calculation
Flow's pure functions + end-to-end graph (constitution §1/§4/§8/§9/§11/§14/§15/§17).

Covers: numeric/date coercion; validated-JSON payload parsing (good / empty /
malformed / non-object → AR_VALIDATION); parameter resolution (missing rates
default to ``0.00`` + a warning, §17); payload validation (no facts → hard
AR_VALIDATION; non-parseable fact / bad period / bad currency → warnings);
exception classification (ValidationResult scoped to failures); CalculationResult
assembly (``calculation_type="reconcile"``, the 9 signed-2dp totals keys,
``line_items`` with ``source_refs=[rule_id]``, default currency SAR); WorkflowState
snapshot (intent ``ar_calculation``, totals ``"0.00"``, status ``completed``);
deterministic audit refs + the per-calculation checkpoints map (§11); §14
envelope shape; and end-to-end execution via ``run()`` (good payload → AR_OK +
the 9 figures + 3 checkpoints; malformed JSON → AR_VALIDATION; no facts →
AR_VALIDATION; missing rate → warning + 0.00 figure). No network, no LangFlow,
no Docker — ``python3 calculation_selftest.py`` runs anywhere. Mirrors
kitchen_revenue_selftest's harness (CLAUDE.md self-test convention): PASS/FAIL
counts, exits non-zero on any failure, so ``make test`` (via
``scripts/calculation.selftest.sh``) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/calculation_selftest.py
"""
import json
import os
import re as _re
import sys
import types
from dataclasses import asdict

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so calculation imports without the in-image venv.
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

    def log(self, *a, **k):
        pass


class _Input:
    def __init__(self, *a, **k):
        pass


class _Message:
    def __init__(self, text=""):
        self.text = text


class _Runtime:
    pass


_stub("lfx")
_stub("lfx.custom", {"Component": _Component})
_stub("lfx.io", {"HandleInput": _Input, "MessageTextInput": _Input,
                 "Output": _Input, "DropdownInput": _Input,
                 "MultilineInput": _Input, "BoolInput": _Input})
_stub("lfx.schema", {"Message": _Message})
_stub("langgraph")
_stub("langgraph.checkpoint", {"memory": types.ModuleType("memory")})
_stub("langgraph.checkpoint.memory", {"InMemorySaver": object})
_g = _stub("langgraph.graph")
_g.START = "START"
_g.END = "END"


class _StateGraph:
    """Stub that records the graph topology and compiles a walker."""

    def __init__(self, *a, **k):
        self.nodes = {}
        self.edges = []  # static edges
        self.conds = {}  # node -> (router_fn, path_map)

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def add_edge(self, a, b):
        self.edges.append((a, b))

    def add_conditional_edges(self, name, fn, mapping):
        self.conds[name] = (fn, mapping)

    def compile(self, *a, **k):
        return _Compiled(self)


class _Compiled:
    """Walks the stub graph: conditional edges route on ``state.status``;
    static edges chain; unknown status falls back to ``respond``."""

    def __init__(self, sg):
        self.sg = sg
        self.state = {}

    def invoke(self, initial, config=None, context=None):
        st = initial
        rt = _Runtime()
        rt.context = context or {}
        static = {}
        for a, b in self.sg.edges:
            static.setdefault(a, b)
        cur = static.get("START")
        while cur is not None and cur != "END":
            fn = self.sg.nodes[cur]
            upd = fn(st, rt)
            d = asdict(st) if not isinstance(st, dict) else dict(st)
            if upd:
                d.update(upd)
            st = type(initial)(**d) if not isinstance(initial, dict) else d
            if cur in self.sg.conds:
                router, mapping = self.sg.conds[cur]
                nxt = router(st)
                cur = mapping.get(nxt, "respond")
            else:
                cur = static.get(cur, "END")
        self.state = asdict(st) if not isinstance(st, dict) else dict(st)

    def get_state(self, config):
        class _S:
            pass
        s = _S()
        s.values = self.state
        return s


_g.StateGraph = _StateGraph
_stub("langgraph.runtime", {"Runtime": _Runtime})
_stub("langgraph.types", {"Command": object, "interrupt": lambda *a, **k: None})

# Both bundle roots on sys.path: ar_common (this flow) + cosmic_common (the BRE
# the evaluate_rules node lazy-imports). Mirrors the in-image editable installs.
_AR_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
_CC_ROOT = os.path.abspath(os.path.join(_AR_ROOT, "..", "cosmic_common"))
sys.path.insert(0, _AR_ROOT)
sys.path.insert(0, _CC_ROOT)

import components.ar_common.calculation as c  # noqa: E402

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


GOOD_PAYLOAD = {
    "trace_id": "trace-1", "tenant": "cosmic-vikings",
    "period": {"start": "2026-01-01", "end": "2026-01-31"}, "currency": "SAR",
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


def _run(payload_text, rules_json=None):
    """Drive CalculationFlowComponent.run() with a stub graph; return the envelope dict."""
    comp = c.CalculationFlowComponent()
    comp.user_input = payload_text
    comp.rules = rules_json if rules_json is not None else c.SEED_RULESET_JSON
    comp.model_name = "glm-5.2:cloud"
    comp.session_id = "s1"
    return json.loads(comp.run().text)


# --------------------------------------------------------------------------- #
# [1] numeric / date helpers
# --------------------------------------------------------------------------- #
print("[1] numeric / date helpers")
eq(c._to_signed_2dp("1234.5"), "1234.50", "half-up signed 2dp")
eq(c._to_signed_2dp("-3.006"), "-3.01", "negative half-up")
eq(c._to_signed_2dp(None), "0.00", "None → 0.00")
eq(c._sum_2dp(["1.10", "2.20", "-0.30"]), "3.00", "sum 2dp")
eq(c._parse_date("2026/01/05"), "2026-01-05", "slash date normalised")
falsy(c._parse_date("not a date"), "unparseable date → None")

# --------------------------------------------------------------------------- #
# [2] _parse_payload
# --------------------------------------------------------------------------- #
print("[2] _parse_payload")
p, err = c._parse_payload(json.dumps(GOOD_PAYLOAD))
falsy(err, "good payload → no error")
eq(p["facts"]["gross_sales"], "10000.00", "good payload parsed")
_, err = c._parse_payload("")
eq(err["code"], "AR_VALIDATION", "empty payload → AR_VALIDATION")
_, err = c._parse_payload("not json")
eq(err["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
_, err = c._parse_payload("[1,2,3]")
eq(err["code"], "AR_VALIDATION", "non-object payload → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [3] _resolve_parameters — missing rates default to 0.00 + warning
# --------------------------------------------------------------------------- #
print("[3] _resolve_parameters")
params, warns = c._resolve_parameters(c.SEED_RULESET, GOOD_PAYLOAD)
eq(params["vat_rate"], "0.15", "present rate passed through")
# strip all parameters → every referenced rate defaults to 0.00 + a warning
bare = {"facts": GOOD_PAYLOAD["facts"]}
params, warns = c._resolve_parameters(c.SEED_RULESET, bare)
for name in ("discount_rate", "vat_rate", "municipality_rate", "royalty_rate"):
    eq(params[name], "0.00", f"missing {name} defaulted to 0.00")
truthy(len(warns) == 4, "4 missing-rate warnings emitted")
truthy(all(w["code"] == "AR_VALIDATION_MISSING_RATE" for w in warns),
       "warnings carry AR_VALIDATION_MISSING_RATE code")

# --------------------------------------------------------------------------- #
# [4] _validate_payload
# --------------------------------------------------------------------------- #
print("[4] _validate_payload")
report, _w, err = c._validate_payload(GOOD_PAYLOAD, [], "trace-1")
falsy(err, "good payload → no hard error")
truthy(report["valid"], "good payload valid=True")
eq(report["contract_name"], "CalculationInputs", "contract_name")
# no facts → hard fail
report, _w, err = c._validate_payload({"facts": "x"}, [], "trace-1")
eq(err["code"], "AR_VALIDATION", "no facts dict → AR_VALIDATION")
falsy(report["valid"], "no facts → valid=False")
# non-parseable fact → warning (not hard fail)
report, _w, err = c._validate_payload({"facts": {"gross_sales": "abc"}},
                                      [], "trace-1")
falsy(err, "non-parseable fact is not a hard fail")
truthy(not report["valid"] is False and report["valid"],
       "non-parseable fact → still valid=True")
truthy(any(w["path"] == "facts.gross_sales" for w in report["warnings"]),
       "non-parseable fact recorded as a warning")
# bad period + bad currency → warnings
report, _w, err = c._validate_payload(
    {"facts": {"a": "1.00"}, "period": {"start": "01/2026"}, "currency": "xyz"},
    [], "trace-1")
falsy(err, "bad period/currency not a hard fail")
truthy(any(w["path"] == "period.start" for w in report["warnings"]),
       "bad period.start warning")
truthy(any(w["path"] == "currency" for w in report["warnings"]),
       "bad currency warning")

# --------------------------------------------------------------------------- #
# [5] _classify_exceptions
# --------------------------------------------------------------------------- #
print("[5] _classify_exceptions")
rep = c._build_validation_report(True, [], [{"path": "facts.x",
        "code": "AR_VALIDATION_AMOUNT", "message": "x not parseable"}], "t1")
exc = c._classify_exceptions(rep, "t1")
truthy(any(it["path"] == "facts.x" for it in exc["warnings"]),
       "exception report carries the failing-fact warning")
rep_clean = c._build_validation_report(True, [], [], "t1")
exc_clean = c._classify_exceptions(rep_clean, "t1")
truthy(exc_clean["valid"], "clean input → exception report valid=True")

# --------------------------------------------------------------------------- #
# [6] _build_calculation_result
# --------------------------------------------------------------------------- #
print("[6] _build_calculation_result")
calc = {
    "revenue": "9700.00", "discount": "500.00", "vat": "1455.00",
    "municipality_tax": "1358.00", "royalty": "194.00", "collections": "5000.00",
    "expenses": "4000.00", "net_receivable": "7013.00", "net_payable": "5552.00",
}
cr = c._build_calculation_result(calc, c.SEED_RULESET, "t1", "cosmic-vikings", "SAR")
eq(cr["calculation_type"], "reconcile", "calculation_type reconcile")
eq(cr["currency"], "SAR", "currency")
eq(cr["inputs_ref"], "t1", "inputs_ref = trace_id")
eq(cr["contract_version"], c.CONTRACT_VERSION, "contract_version")
for k in c.FIGURE_KEYS:
    truthy(k in cr["totals"], f"totals has {k}")
    truthy(_re.match(r"^-?\d+\.\d{2}$", cr["totals"][k]), f"{k} signed-2dp")
# line_items: one per figure, source_refs=[rule_id]
li = {item["label"]: item for item in cr["line_items"]}
eq(li["Revenue"]["amount"], "9700.00", "Revenue line item amount")
eq(li["Revenue"]["source_refs"], ["R_REVENUE"], "Revenue source_refs=[R_REVENCE]")
eq(li["Net Payable"]["source_refs"], ["R_NET_PAYABLE"], "Net Payable source_refs")
truthy(all(len(it["source_refs"]) >= 1 for it in cr["line_items"]),
       "every line_item has source_refs")
# default currency when payload omits it
cr2 = c._build_calculation_result(calc, c.SEED_RULESET, "t1", "cosmic-vikings", "")
eq(cr2["currency"], c.DEFAULT_CURRENCY, "default currency SAR when empty")

# --------------------------------------------------------------------------- #
# [7] build_workflow_state
# --------------------------------------------------------------------------- #
print("[7] build_workflow_state")
ws = c.build_workflow_state("t1", "ar_calculation", "cosmic-vikings",
                            ["ref1"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
eq(ws["intent"], "ar_calculation", "intent ar_calculation")
eq(ws["status"], "completed", "status completed")
eq(ws["matched_amount"], "0.00", "no money moved — matched 0.00")
eq(ws["outstanding_balance"], "0.00", "outstanding 0.00")
eq(ws["posted_total"], "0.00", "posted 0.00")
eq(ws["idempotency_keys"], {}, "no POST → empty idempotency_keys")
eq(ws["tool_call_ref"], "t1:ar_calculation:0", "tool_call_ref")
eq(ws["audit_refs"], ["ref1"], "audit_refs reflected")

# --------------------------------------------------------------------------- #
# [8] _audit_ref + _record_checkpoint
# --------------------------------------------------------------------------- #
print("[8] audit ref + checkpoints")
ref1 = c._audit_ref("t1", "rules")
ref2 = c._audit_ref("t1", "rules")
eq(ref1, ref2, "audit ref deterministic for same trace+label")
ref3 = c._audit_ref("t1", "calculation_result")
falsy(ref1 == ref3, "different label → different ref")
st = c.CalculationState(trace_id="t1", flow_id="ar_calculation",
                        tenant="cosmic-vikings")
audit_refs, checkpoints = c._record_checkpoint(st, "rules")
truthy(audit_refs[-1] == ref1, "checkpoint appends the labeled ref")
eq(checkpoints["rules"], ref1, "checkpoints map keyed by label")
audit_refs2, checkpoints2 = c._record_checkpoint(
    c.CalculationState(trace_id="t1", flow_id="ar_calculation",
                       tenant="cosmic-vikings", audit_refs=audit_refs,
                       checkpoints=checkpoints), "calculation_result")
truthy(len(audit_refs2) == 2, "second checkpoint appends a new ref")
truthy(set(checkpoints2.keys()) == {"rules", "calculation_result"},
       "checkpoints map accumulates labels")

# --------------------------------------------------------------------------- #
# [9] end-to-end via run()
# --------------------------------------------------------------------------- #
print("[9] end-to-end run()")
env = _run(json.dumps(GOOD_PAYLOAD))
eq(env["status"], "ok", "good payload → AR_OK")
eq(env["code"], "AR_OK", "code AR_OK")
totals = env["data"]["calculation_result"]["totals"]
eq(totals["revenue"], "9700.00", "e2e revenue")
eq(totals["vat"], "1455.00", "e2e vat")
eq(totals["net_receivable"], "7013.00", "e2e net_receivable")
eq(totals["net_payable"], "5552.00", "e2e net_payable")
eq(len(env["data"]["checkpoints"]), 3, "3 checkpoints (rules, calculation_result, ar_calculation)")
truthy(set(env["data"]["checkpoints"].keys()) ==
       {"rules", "calculation_result", "ar_calculation"}, "checkpoint labels")
eq(len(env["data"]["audit_refs"]), 3, "3 audit refs")
eq(env["data"]["workflow_state"]["intent"], "ar_calculation", "e2e workflow_state intent")
eq(env["data"]["rule_count"], 9, "rule_count = 9 line_items")
eq(env["data"]["fact_count"], 11, "fact_count = 11 facts (3 sales + 5 collections + 3 expenses)")
# malformed JSON → AR_VALIDATION (ingest short-circuits)
env = _run("not json")
eq(env["status"], "error", "malformed JSON → error")
eq(env["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
# no facts → AR_VALIDATION
env = _run(json.dumps({"facts": "x"}))
eq(env["code"], "AR_VALIDATION", "no facts → AR_VALIDATION")
# missing rate → warning + 0.00 figure (not a hard fail)
p2 = json.loads(json.dumps(GOOD_PAYLOAD))
p2["parameters"] = {"discount_rate": "0.05"}  # drop vat/mun/royalty
env = _run(json.dumps(p2))
eq(env["status"], "ok", "missing rates → still AR_OK (defaulted)")
eq(env["data"]["calculation_result"]["totals"]["vat"], "0.00", "missing vat_rate → vat 0.00")
truthy(any(w["code"] == "AR_VALIDATION_MISSING_RATE"
           for w in env["data"]["validation_report"]["warnings"]),
       "missing-rate warnings surface in the validation report")

# --------------------------------------------------------------------------- #
# [10] envelope shape
# --------------------------------------------------------------------------- #
print("[10] envelope shape")
env = _run(json.dumps(GOOD_PAYLOAD))
for k in ("calculation_result", "calculations", "validation_report",
          "exception_report", "workflow_state", "audit_refs", "checkpoints",
          "rule_count", "fact_count", "flow_id", "tenant", "started_at",
          "ended_at", "contract_version"):
    truthy(k in env["data"], f"data has {k}")
eq(env["data"]["flow_id"], "ar_calculation", "flow_id in envelope data")
eq(env["data"]["contract_version"], c.CONTRACT_VERSION, "contract_version")
truthy(env["trace_id"], "envelope has a trace_id")
# exception_report is a ValidationResult
eq(env["data"]["exception_report"]["contract_name"], "CalculationInputs",
   "exception_report is a ValidationResult")

# --------------------------------------------------------------------------- #
# [11] run() never raises (§5/§9)
# --------------------------------------------------------------------------- #
print("[11] run() never raises")
comp = c.CalculationFlowComponent()
comp.user_input = json.dumps(GOOD_PAYLOAD)
comp.rules = "not valid json"  # rules parse falls back to SEED_RULESET
comp.model_name = "glm-5.2:cloud"
comp.session_id = "s1"
env = json.loads(comp.run().text)
eq(env["status"], "ok", "bad rules JSON falls back to seed ruleset → AR_OK")

# --------------------------------------------------------------------------- #
# [12] seed ruleset constant sanity
# --------------------------------------------------------------------------- #
print("[12] seed ruleset")
eq(len(c.SEED_RULESET), 9, "seed ruleset has 9 rules")
eq(c.SEED_RULESET_JSON, json.dumps(c.SEED_RULESET, indent=2),
   "SEED_RULESET_JSON matches SEED_RULESET")
outs = {r["output"] for r in c.SEED_RULESET}
eq(outs, set(c.FIGURE_KEYS), "seed outputs are exactly the 9 figure keys")

print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)