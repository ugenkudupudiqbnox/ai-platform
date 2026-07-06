#!/usr/bin/env python3
"""kitchen_revenue_selftest — offline stdlib-only tests for the Cosmic Kitchen
Revenue Flow's pure functions.

Covers (constitution §1/§4/§8/§9/§10/§11/§14/§15/§16/§20): sheet file-type
detection, file-ref normalization, role classification (filename keyword +
header fallback), per-role row validation (required columns + per-row rules:
amount/qty×rate, segment, date, payment_id, method enum, category), exception
classification (valid vs exception split + missing-role warnings + all-rows-fail),
deterministic revenue calculation (Breakfast/Half Board by_segment grouping,
Menu Sales authoritative + Daily Sales cross-check divergence → warning),
CollectionData assembly (total_collected, by_method, match_status="unmatched"),
reported expense total + by_category (signed), the nets CalculationResult
(calculation_type="reconcile", totals = net_receivable/net_payable/...,
line_items), the per-calculation checkpoint (labeled audit refs + checkpoints
map), WorkflowState snapshot shape, §14 envelope shape, §10 retry
classification, and numeric/timestamp coercion. No network, no LangFlow, no
Docker, no openpyxl — `python3 kitchen_revenue_selftest.py` runs anywhere.
Mirrors intercompany_sales_selftest's harness (CLAUDE.md self-test convention):
PASS/FAIL counts, exits non-zero on any failure, so `make test` (via
scripts/kitchen-revenue.selftest.sh) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/kitchen_revenue_selftest.py
"""
import os
import re as _re
import sys
import types
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so kitchen_revenue imports without the in-image venv.
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
                 "MultilineInput": _Input, "FloatInput": _Input,
                 "BoolInput": _Input, "IntInput": _Input})
_stub("lfx.schema", {"Message": _Message})
_stub("langgraph")
_stub("langgraph.checkpoint", {"memory": types.ModuleType("memory")})
_stub("langgraph.checkpoint.memory", {"InMemorySaver": object})
_g = _stub("langgraph.graph")
_g.START = "START"
_g.END = "END"


class _StateGraph:
    def __init__(self, *a, **k):
        pass

    def add_node(self, *a, **k):
        pass

    def add_edge(self, *a, **k):
        pass

    def add_conditional_edges(self, *a, **k):
        pass

    def compile(self, *a, **k):
        return object()


_g.StateGraph = _StateGraph
_stub("langgraph.runtime", {"Runtime": _Runtime})
_stub("langgraph.types", {"Command": object, "interrupt": lambda *a, **k: None})

# Add the ar_common bundle root so `components.ar_common.kitchen_revenue`
# resolves (mirrors the in-image pip-installed editable bundle).
# HERE = .../ar_common/components/ar_common  → bundle root is 2 levels up.
_AR_BUNDLE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_BUNDLE_ROOT)

import components.ar_common.kitchen_revenue as kr  # noqa: E402

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


class _HTTPError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- #
# [1] detect_type — sheet file-type identification by extension (§4)
# --------------------------------------------------------------------------- #
print("[1] detect_type")
eq(kr.detect_type("menu_sales.xlsx"), "excel", "xlsx → excel")
eq(kr.detect_type("DAILY.XLSX"), "excel", "case-insensitive ext")
eq(kr.detect_type("backup.xls"), "excel", "xls → excel")
eq(kr.detect_type("check.csv"), "csv", "csv → csv")
eq(kr.detect_type("sales.tsv"), "csv", "tsv → csv")
eq(kr.detect_type("note.pdf"), "unknown", "pdf → unknown")
eq(kr.detect_type("noext"), "unknown", "no extension → unknown")
eq(kr.detect_type(""), "unknown", "empty → unknown")

# --------------------------------------------------------------------------- #
# [2] _normalize_file — canvas File-node ref coercion
# --------------------------------------------------------------------------- #
print("[2] _normalize_file")
eq(kr._normalize_file("/tmp/kitchen/menu.csv"),
   {"name": "menu.csv", "path": "/tmp/kitchen/menu.csv"},
   "bare string → {name,path}")
eq(kr._normalize_file({"file_path": "/a/b.csv", "file_name": "b.csv"}),
   {"name": "b.csv", "path": "/a/b.csv"}, "dict with file_path/file_name")
eq(kr._normalize_file({"path": "/c/d.xlsx"}),
   {"name": "d.xlsx", "path": "/c/d.xlsx"}, "dict with path → basename name")


class _DataObj:
    def __init__(self, d):
        self.data = d


eq(kr._normalize_file(_DataObj({"file_path": "/e/f.xlsx"})),
   {"name": "f.xlsx", "path": "/e/f.xlsx"}, "Data obj with .data.file_path")
eq(kr._normalize_file(None), {"name": "", "path": ""}, "None → empty")
eq(kr._normalize_file({}), {"name": "", "path": ""}, "empty dict → empty")

# --------------------------------------------------------------------------- #
# [3] _classify_input — role classification (filename keyword + header fallback)
# --------------------------------------------------------------------------- #
print("[3] _classify_input")
eq(kr._classify_input("Menu Sales Analysis.xlsx", []), "menu_sales",
   "filename 'menu' → menu_sales")
eq(kr._classify_input("daily_sales.csv", []), "daily_sales",
   "filename 'daily' → daily_sales")
eq(kr._classify_input("Detailed Check Payment.xlsx", []), "check_payment",
   "filename 'check' → check_payment")
eq(kr._classify_input("payments.csv", []), "check_payment",
   "filename 'payment' → check_payment")
eq(kr._classify_input("Marriott Backup.xlsx", []), "marriott_backup",
   "filename 'marriott' → marriott_backup")
eq(kr._classify_input("backup.csv", []), "marriott_backup",
   "filename 'backup' → marriott_backup")
# header fallback
eq(kr._classify_input("sheet1.xlsx", [{"payment_id": "P1", "method": "cash"}]),
   "check_payment", "header payment_id → check_payment")
eq(kr._classify_input("sheet2.xlsx", [{"meal_period": "Breakfast"}]),
   "menu_sales", "header meal_period → menu_sales")
eq(kr._classify_input("sheet3.xlsx", [{"expense_category": "Food"}]),
   "marriott_backup", "header expense_category → marriott_backup")
eq(kr._classify_input("mystery.xlsx", [{"foo": "bar"}]), "unknown",
   "unrecognised → unknown")

# --------------------------------------------------------------------------- #
# [4] _validate_role_rows — required columns + per-row rules (§9)
# --------------------------------------------------------------------------- #
print("[4] _validate_role_rows")
# menu_sales valid
menu = [
    {"meal_period": "Breakfast", "amount": "100.00", "date": "2026-07-01"},
    {"meal_period": "Half Board", "qty": "10", "rate": "12.50",
     "date": "2026-07-02"},
]
mper, mhmap, mmiss = kr._validate_role_rows("menu_sales", menu)
eq(mmiss, [], "menu_sales all required columns present")
eq(mper[0], [], "menu_sales row 0 valid")
eq(mper[1], [], "menu_sales row 1 (qty×rate) valid")
# missing required column (no segment)
mper2, _h, mmiss2 = kr._validate_role_rows("menu_sales",
                                          [{"amount": "1", "date": "2026-07-01"}])
eq(mmiss2, ["segment"], "menu_sales missing segment → reported")
# per-row rule violations
bad_menu = [{"meal_period": "", "amount": "0", "date": "garbage"}]
bper, _bh, _bm = kr._validate_role_rows("menu_sales", bad_menu)
rule_ids = {e["rule_id"] for e in bper[0]}
truthy("kr.segment_required" in rule_ids, "empty segment flagged")
truthy("kr.amount_positive" in rule_ids, "amount 0 flagged")
truthy("kr.date_iso" in rule_ids, "bad date flagged")
# check_payment valid + method enum
check = [{"payment_id": "P1", "amount": "50.00", "method": "Check",
          "date": "2026-07-01"}]
cper, _ch, cmiss = kr._validate_role_rows("check_payment", check)
eq(cmiss, [], "check_payment all required present")
eq(cper[0], [], "check_payment valid (Check → bank_transfer accepted)")
# bad method
bad_check = [{"payment_id": "P1", "amount": "5", "method": "barter",
              "date": "2026-07-01"}]
bcper, _bch, _bcm = kr._validate_role_rows("check_payment", bad_check)
bids = {e["rule_id"] for e in bcper[0]}
truthy("kr.method_enum" in bids, "unknown method → kr.method_enum")
# payment_id column present but empty → kr.payment_id_required (a row with
# no payment_id key at all hits the missing-required-column path instead)
nopid = [{"payment_id": "", "amount": "5", "method": "cash",
          "date": "2026-07-01"}]
nper, _nh, _nm = kr._validate_role_rows("check_payment", nopid)
truthy(any(e["rule_id"] == "kr.payment_id_required" for e in nper[0]),
       "empty payment_id value flagged")
# marriott_backup valid (signed amount allowed)
mar = [{"amount": "-25.00", "category": "Refund", "date": "2026-07-01"}]
mper3, _mh, mmiss3 = kr._validate_role_rows("marriott_backup", mar)
eq(mmiss3, [], "marriott_backup required present")
eq(mper3[0], [], "marriott_backup negative amount valid")
# every issue is schema-conformant
for e in bper[0] + bcper[0]:
    truthy(set(e.keys()) == {"path", "code", "message", "rule_id"},
           f"issue {e['rule_id']} has exactly 4 keys")
    truthy(bool(_re.match(r"^AR_VALIDATION(_[A-Z_]+)?$", e["code"])),
           f"issue {e['rule_id']} code matches AR_VALIDATION pattern")

# --------------------------------------------------------------------------- #
# [5] _classify_exceptions — exception report + missing-role warnings (§4/§15)
# --------------------------------------------------------------------------- #
print("[5] _classify_exceptions")
# Only menu_sales present (others missing → warnings)
inputs_partial = {
    "menu_sales": [{"meal_period": "Breakfast", "amount": "100.00",
                    "date": "2026-07-01"},
                   {"meal_period": "", "amount": "0", "date": "bad"}],
    "daily_sales": [],
    "check_payment": [],
    "marriott_backup": [],
}
report, row_exc = kr._classify_exceptions(inputs_partial, "trace-1")
falsy(report["valid"], "report valid=False (row exception + missing roles)")
eq(report["contract_name"], "KitchenRevenueInputs", "report contract_name")
eq(report["contract_version"], "1.0.0", "report contract_version")
truthy(row_exc == 1, "one row exception counted")
rule_ids = {e["rule_id"] for e in report["errors"]}
truthy("kr.daily_sales_missing" in rule_ids, "missing daily_sales warned")
truthy("kr.check_payment_missing" in rule_ids, "missing check_payment warned")
truthy("kr.marriott_backup_missing" in rule_ids, "missing marriott_backup warned")
truthy("kr.amount_positive" in rule_ids, "row error carried into report")
# all four roles present + all rows valid → valid report
inputs_full = {
    "menu_sales": [{"meal_period": "Breakfast", "amount": "10", "date": "2026-07-01"}],
    "daily_sales": [{"amount": "10", "date": "2026-07-01"}],
    "check_payment": [{"payment_id": "P1", "amount": "5", "method": "cash",
                       "date": "2026-07-01"}],
    "marriott_backup": [{"amount": "3", "date": "2026-07-01"}],
}
report2, row_exc2 = kr._classify_exceptions(inputs_full, "trace-1")
truthy(report2["valid"], "all roles present + all rows valid → valid=True")
eq(row_exc2, 0, "no row exceptions")
# all-rows-fail check (menu_sales only, all bad)
inputs_bad = {
    "menu_sales": [{"meal_period": "", "amount": "0", "date": "bad"}],
    "daily_sales": [], "check_payment": [], "marriott_backup": [],
}
_rep3, re3 = kr._classify_exceptions(inputs_bad, "t")
# 1 row, all bad → row_exceptions == total_rows
eq(re3, 1, "all-bad menu row counted as exception")

# --------------------------------------------------------------------------- #
# [6] calculate_revenue — Breakfast/Half Board segments, Menu authoritative,
#     Daily cross-check divergence → warning (§8/§4.3/§15)
# --------------------------------------------------------------------------- #
print("[6] calculate_revenue")
menu_rows = [
    {"meal_period": "Breakfast", "amount": "100.00", "date": "2026-07-01"},
    {"meal_period": "Half Board", "amount": "200.00", "date": "2026-07-02"},
    {"meal_period": "Breakfast", "qty": "5", "rate": "10.00",
     "date": "2026-07-03"},
]
daily_rows = [{"amount": "300.00", "date": "2026-07-01"}]
mhmap = kr._header_map(menu_rows)
dhmap = kr._header_map(daily_rows)
base_report = kr._build_validation_report([], "trace-1")
rev, exc = kr.calculate_revenue(menu_rows, daily_rows, mhmap, dhmap,
                                "trace-1", "cosmic-vikings", base_report)
eq(rev["total"], "350.00", "total = Σ (100 + 200 + 50)")
eq(rev["currency"], "SAR", "default currency SAR")
eq(rev["period"], {"start": "2026-07-01", "end": "2026-07-03"},
   "period = min/max date")
eq(rev["contract_version"], "1.0.0", "revenue contract_version pinned")
eq(rev["trace_id"], "trace-1", "trace_id threaded through")
eq(rev["tenant"], "cosmic-vikings", "tenant threaded through")
eq(rev["by_invoice"], [], "by_invoice empty (no invoices in kitchen flow)")
seg = {s["segment"]: s for s in rev["by_segment"]}
eq(seg["breakfast"]["amount"], "150.00", "breakfast = 100.00 + 50.00")
eq(seg["breakfast"]["count"], 2, "breakfast count = 2 rows")
eq(seg["half_board"]["amount"], "200.00", "half_board = 200.00")
eq(seg["half_board"]["count"], 1, "half_board count = 1 row")
# by_segment entries schema-conformant
for s in rev["by_segment"]:
    truthy(set(s.keys()) == {"segment", "amount", "count"},
           "segment has exactly {segment,amount,count}")
    truthy(bool(_re.match(r"^\d+\.\d{2}$", s["amount"])),
           f"segment amount {s['amount']} non-negative 2dp")
# Daily cross-check: 300 vs 350 → divergence 50 → warning appended
truthy(not exc["valid"], "cross-check divergence → exception report valid=False")
cc = [e for e in exc["errors"] if e["rule_id"] == "kr.revenue_cross_check"]
truthy(len(cc) == 1, "one revenue_cross_check warning appended")
# Cross-check reconciles → no warning
daily_ok = [{"amount": "350.00", "date": "2026-07-01"}]
dhmap2 = kr._header_map(daily_ok)
rev2, exc2 = kr.calculate_revenue(menu_rows, daily_ok, mhmap, dhmap2,
                                  "trace-1", "cosmic-vikings",
                                  kr._build_validation_report([], "trace-1"))
cc2 = [e for e in exc2["errors"] if e["rule_id"] == "kr.revenue_cross_check"]
eq(cc2, [], "reconciled cross-check → no warning")
# No menu rows → Daily Sales fallback (segment = daily_summary)
rev3, exc3 = kr.calculate_revenue([], daily_ok, {}, dhmap2,
                                   "trace-1", "cosmic-vikings",
                                   kr._build_validation_report([], "trace-1"))
eq(rev3["total"], "350.00", "Daily fallback total")
eq(rev3["by_segment"][0]["segment"], "daily_summary",
   "Daily fallback segment = daily_summary")
# No sales at all → at least one by_segment entry (RevenueData minItems:1)
rev4, _ = kr.calculate_revenue([], [], {}, {}, "trace-1", "cosmic-vikings",
                                kr._build_validation_report([], "trace-1"))
eq(rev4["total"], "0.00", "no sales → total 0.00")
truthy(len(rev4["by_segment"]) >= 1, "no sales → ≥1 by_segment entry")
eq(rev4["by_segment"][0]["amount"], "0.00", "empty by_segment amount 0.00")

# --------------------------------------------------------------------------- #
# [7] calculate_collections — CollectionData, unmatched (§15)
# --------------------------------------------------------------------------- #
print("[7] calculate_collections")
check_rows = [
    {"payment_id": "P1", "amount": "100.00", "method": "Check",
     "date": "2026-07-01", "customer_ref": "G1"},
    {"payment_id": "P2", "amount": "50.00", "method": "Cash",
     "date": "2026-07-02"},
]
chmap = kr._header_map(check_rows)
coll = kr.calculate_collections(check_rows, chmap, "trace-1", "cosmic-vikings")
eq(coll["total_collected"], "150.00", "total_collected = Σ")
eq(coll["currency"], "SAR", "default currency SAR")
eq(coll["matched_amount"], "0.00", "matched_amount 0.00 (no invoice list v1)")
eq(coll["unmatched_amount"], "150.00", "unmatched_amount = total_collected")
eq(coll["contract_version"], "1.0.0", "collections contract_version pinned")
eq(len(coll["payments"]), 2, "2 payments")
p0 = coll["payments"][0]
eq(set(p0.keys()), {"payment_id", "customer_ref", "amount", "method",
                    "posted_at", "match_status"}, "payment has the 6 schema fields")
eq(p0["method"], "bank_transfer", "Check → bank_transfer")
eq(p0["match_status"], "unmatched", "match_status = unmatched (v1)")
eq(p0["customer_ref"], "G1", "customer_ref from column")
truthy(bool(_re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", p0["posted_at"])),
       "posted_at ISO-8601 UTC datetime")
eq(p0["posted_at"], "2026-07-01T00:00:00Z", "date-only → midnight UTC")
eq(p0["amount"], "100.00", "amount 2dp")
bm = {m["method"]: m for m in coll["by_method"]}
eq(bm["bank_transfer"]["amount"], "100.00", "by_method bank_transfer amount")
eq(bm["cash"]["count"], 1, "by_method cash count")
for m in coll["by_method"]:
    truthy(m["method"] in ("cash", "card", "bank_transfer", "online", "wallet",
                           "other"), f"by_method {m['method']} in enum")
# empty check_payment → zeros, no payments
coll2 = kr.calculate_collections([], {}, "trace-1", "cosmic-vikings")
eq(coll2["total_collected"], "0.00", "empty collections → 0.00")
eq(coll2["payments"], [], "empty collections → no payments")
eq(coll2["unmatched_amount"], "0.00", "empty collections unmatched 0.00")

# --------------------------------------------------------------------------- #
# [8] calculate_expenses — reported total + by_category, signed (§20)
# --------------------------------------------------------------------------- #
print("[8] calculate_expenses")
mar_rows = [
    {"amount": "200.00", "category": "Food Cost", "date": "2026-07-01"},
    {"amount": "-25.00", "category": "Refund", "date": "2026-07-02"},
    {"amount": "75.00", "category": "Food Cost", "date": "2026-07-03"},
]
mrhmap = kr._header_map(mar_rows)
exp = kr.calculate_expenses(mar_rows, mrhmap, "trace-1", "cosmic-vikings")
eq(exp["total"], "250.00", "total = Σ (200 - 25 + 75)")
eq(exp["currency"], "SAR", "expense currency SAR")
fc = {c["category"]: c for c in exp["by_category"]}
eq(fc["food_cost"]["amount"], "275.00", "food_cost = 200 + 75")
eq(fc["food_cost"]["count"], 2, "food_cost count = 2")
eq(fc["refund"]["amount"], "-25.00", "refund negative amount kept (signed)")
# empty marriott → zeros
exp2 = kr.calculate_expenses([], {}, "trace-1", "cosmic-vikings")
eq(exp2["total"], "0.00", "empty expenses → 0.00")
eq(exp2["by_category"], [], "empty expenses → no categories")

# --------------------------------------------------------------------------- #
# [9] calculate_nets — CalculationResult reconcile (§15)
# --------------------------------------------------------------------------- #
print("[9] calculate_nets")
nets = kr.calculate_nets(rev, coll, exp, "trace-1", "cosmic-vikings")
eq(nets["calculation_type"], "reconcile", "calculation_type = reconcile")
eq(nets["currency"], "SAR", "nets currency SAR")
eq(nets["inputs_ref"], "trace-1", "inputs_ref = trace_id")
eq(nets["contract_version"], "1.0.0", "nets contract_version pinned")
t = nets["totals"]
eq(t["total_revenue"], "350.00", "total_revenue from RevenueData.total")
eq(t["total_collections"], "150.00", "total_collections from CollectionData")
eq(t["total_expenses"], "250.00", "total_expenses from expense total")
eq(t["net_receivable"], "200.00", "net_receivable = revenue − collections")
eq(t["net_payable"], "250.00", "net_payable = total expenses")
for k, v in t.items():
    truthy(bool(_re.match(r"^-?\d+\.\d{2}$", v)), f"totals {k} signed 2dp")
# line_items: 5 top-level + per-category
li_labels = {li["label"] for li in nets["line_items"]}
truthy("Net Receivable" in li_labels, "line_items has Net Receivable")
truthy("Net Payable" in li_labels, "line_items has Net Payable")
truthy("Expense: food_cost" in li_labels, "line_items has per-category expense")
for li in nets["line_items"]:
    truthy(set(li.keys()) == {"label", "amount", "source_refs"},
           f"line_item {li['label']} has exactly 3 fields")
    truthy(bool(_re.match(r"^-?\d+\.\d{2}$", li["amount"])),
           f"line_item {li['label']} amount signed 2dp")
    truthy(isinstance(li["source_refs"], list) and li["source_refs"],
           f"line_item {li['label']} has source_refs")
# net_receivable can go negative (collections > revenue)
nets_neg = kr.calculate_nets({"total": "100.00", "currency": "SAR"},
                             {"total_collected": "250.00"}, {"total": "0.00"},
                             "trace-1", "cosmic-vikings")
eq(nets_neg["totals"]["net_receivable"], "-150.00",
   "negative net_receivable (collections > revenue)")

# --------------------------------------------------------------------------- #
# [10] build_workflow_state — WorkflowState snapshot (no money moved)
# --------------------------------------------------------------------------- #
print("[10] build_workflow_state")
ws = kr.build_workflow_state("trace-1", "ar_kitchen_revenue", "cosmic-vikings",
                             ["audit-1"], "2026-07-01T00:00:00Z",
                             "2026-07-01T00:01:00Z")
eq(ws["status"], "completed", "workflow status = completed (report built)")
eq(ws["intent"], "ar_kitchen_revenue", "intent = the subflow id")
eq(ws["matched_amount"], "0.00", "matched_amount 0.00 (no money moved)")
eq(ws["outstanding_balance"], "0.00", "outstanding_balance 0.00")
eq(ws["posted_total"], "0.00", "posted_total 0.00 (no posting)")
eq(ws["pending_approvals"], [], "no pending approvals (read-only v1)")
eq(ws["idempotency_keys"], {}, "no idempotency keys (no POST)")
eq(ws["audit_refs"], ["audit-1"], "audit_refs threaded through")
truthy(ws["tool_call_ref"].startswith("trace-1:ar_kitchen_revenue:"),
       "tool_call_ref shaped trace_id:intent:index")
eq(ws["contract_version"], "1.0.0", "workflow state contract_version pinned")

# --------------------------------------------------------------------------- #
# [11] _audit_ref + checkpoint map — deterministic per-calc (§11)
# --------------------------------------------------------------------------- #
print("[11] _audit_ref / checkpoint")
r1 = kr._audit_ref("trace-1", "revenue")
r2 = kr._audit_ref("trace-1", "revenue")
eq(r1, r2, "audit_ref deterministic for same trace+label")
r3 = kr._audit_ref("trace-1", "collections")
falsy(r1 == r3, "different label → different audit_ref")
# The four per-calc labels + the final aggregate are all distinct
labels = ("revenue", "collections", "expenses", "nets", "kitchen_revenue")
refs = [kr._audit_ref("trace-1", lb) for lb in labels]
eq(len(set(refs)), len(labels), "all per-calc + final audit refs distinct")

# --------------------------------------------------------------------------- #
# [12] _envelope shape (§14)
# --------------------------------------------------------------------------- #
print("[12] _envelope")
env = kr._envelope("ok", "AR_OK", data={"x": 1}, trace_id="t")
eq(env, {"status": "ok", "code": "AR_OK", "data": {"x": 1}, "trace_id": "t"},
   "ok envelope shape")
env_e = kr._envelope("error", "AR_VALIDATION", error={"message": "bad"},
                     trace_id="t")
truthy(env_e["error"] == {"message": "bad"}, "error envelope carries error")
truthy(env_e["trace_id"] == "t", "error envelope carries trace_id")

# --------------------------------------------------------------------------- #
# [13] _is_transient — §10 retry classification
# --------------------------------------------------------------------------- #
print("[13] _is_transient")
truthy(kr._is_transient(TimeoutError("x")), "TimeoutError → transient")
truthy(kr._is_transient(_HTTPError(500)), "HTTP 500 → transient")
truthy(kr._is_transient(_HTTPError(408)), "HTTP 408 → transient")
truthy(kr._is_transient(_HTTPError(429)), "HTTP 429 → transient")
falsy(kr._is_transient(_HTTPError(404)), "HTTP 404 → hard")
falsy(kr._is_transient(_HTTPError(401)), "HTTP 401 → hard")
falsy(kr._is_transient(ValueError("bad")), "ValueError → hard")

# --------------------------------------------------------------------------- #
# [14] parse_envelope — reader/tool output parsing
# --------------------------------------------------------------------------- #
print("[14] parse_envelope")
eq(kr.parse_envelope('{"status":"ok","code":"AR_OK","data":{}}'),
   {"status": "ok", "code": "AR_OK", "data": {}}, "valid json dict")
eq(kr.parse_envelope("not json"), None, "non-json → None")
eq(kr.parse_envelope("[1,2,3]"), None, "json array → None")
eq(kr.parse_envelope(""), None, "empty → None")
eq(kr.parse_envelope(None), None, "None → None")

# --------------------------------------------------------------------------- #
# [15] numeric/timestamp/token coercion
# --------------------------------------------------------------------------- #
print("[15] numeric/timestamp/token coercion")
eq(kr._to_2dp("10.5"), "10.50", "10.5 → 10.50")
eq(kr._to_2dp("1,234.5"), "1234.50", "thousands sep stripped")
eq(kr._to_2dp("SAR 99.999"), "100.00", "prefix stripped, quantised to 2dp")
eq(kr._to_2dp(""), "0.00", "empty → 0.00")
eq(kr._to_2dp(None), "0.00", "None → 0.00")
eq(kr._to_2dp("-5"), "0.00", "negative clamped to 0.00 (non-negative)")
eq(kr._to_signed_2dp("-25.50"), "-25.50", "signed 2dp keeps negative")
eq(kr._sum_2dp(["100.00", "-25.50", "0.25"]), "74.75", "sum to 2dp")
eq(kr._sum_2dp([]), "0.00", "empty sum → 0.00")
eq(kr._parse_date("2026-07-01"), "2026-07-01", "ISO date passthrough")
eq(kr._parse_date("2026/07/01"), "2026-07-01", "slash date normalised")
eq(kr._parse_date("garbage"), None, "garbage → None")
eq(kr._parse_date(""), None, "empty → None")
eq(kr._to_iso_datetime("2026-07-01"), "2026-07-01T00:00:00Z",
   "date-only → midnight UTC")
eq(kr._to_iso_datetime("garbage"), kr.utc_now(), "unparseable → utc_now")
eq(kr._norm_token("Half Board"), "half_board", "Half Board → half_board")
eq(kr._norm_token("Breakfast"), "breakfast", "Breakfast → breakfast")
eq(kr._norm_token("  Food-Cost!! "), "food_cost", "punctuation normalised")
eq(kr._norm_token(""), "", "empty token → empty (caller defaults)")
eq(kr._map_method("Check"), "bank_transfer", "Check → bank_transfer")
eq(kr._map_method("CASH"), "cash", "case-insensitive method")
eq(kr._map_method("barter"), "", "unknown method → '' (flagged by validator)")
eq(kr._map_method(""), "", "empty method → ''")

# --------------------------------------------------------------------------- #
# [16] _valid_rows_for — per-role valid-row extraction
# --------------------------------------------------------------------------- #
print("[16] _valid_rows_for")
v, _h = kr._valid_rows_for("menu_sales",
                           {"menu_sales": menu + [{"meal_period": "", "amount": "0",
                                                   "date": "bad"}]})
eq(len(v), 2, "2 valid menu rows (1 bad excluded)")
v2, _h2 = kr._valid_rows_for("menu_sales", {"menu_sales": []})
eq(v2, [], "absent role → no valid rows")
v3, _h3 = kr._valid_rows_for("check_payment",
                             {"check_payment": [{"amount": "5", "method": "cash",
                                                 "date": "2026-07-01"}]})  # missing payment_id
eq(v3, [], "missing required column → zero valid rows")

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)