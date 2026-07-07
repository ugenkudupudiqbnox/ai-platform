#!/usr/bin/env python3
"""foodics_processing_selftest — offline stdlib-only tests for the Foodics
Processing Flow's pure functions.

Covers (constitution §1/§4/§8/§9/§10/§11/§14/§15/§16/§17/§19): export file-type
detection, file-ref normalization, role classification (filename keyword +
header fallback: order / order_items / order_payments), per-role row
validation (required columns + per-row rules: order_ref, item_ref, qty>0,
unit_price>0, payment_ref, amount>0, method enum, ISO date), exception
classification (valid vs exception split + missing-role warnings + all-rows-
fail), consolidated dataset build (order↔items join by order_ref, payments
attached, 2dp gross/payment totals), pivot refresh (by_item + by_payment_type
aggregations + totals), payment-type determination (METHOD_SYNONYMS → enum,
by_method, total_collected), discount application (BOTH in-file columns and
baked-in DISCOUNT_RULES, precedence in-file > baked-in > 0.00, discounts_total
2dp, line totals reduced), Sheet3 per-order net summary, Zoho Books upload
format (row shape: customer_ref/invoice_number/date/item_details/discount_total
/total/currency), one InvoiceData per order_ref (deterministic uuid5 ids, 2dp,
discounts applied, status="draft", due_date=issue+30), WorkflowState snapshot
shape, the per-calculation checkpoint (labeled audit refs + checkpoints map),
§14 envelope shape, §10 retry classification, and numeric/timestamp coercion.
No network, no LangFlow, no Docker, no openpyxl —
`python3 foodics_processing_selftest.py` runs anywhere. Mirrors
kitchen_revenue_selftest's harness (CLAUDE.md self-test convention): PASS/FAIL
counts, exits non-zero on any failure, so `make test` (via
scripts/foodics-processing.selftest.sh) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/foodics_processing_selftest.py
"""
import os
import re as _re
import sys
import types
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so foodics_processing imports without the in-image venv.
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

# Add the ar_common bundle root so `components.ar_common.foodics_processing`
# resolves (mirrors the in-image pip-installed editable bundle).
# HERE = .../ar_common/components/ar_common  → bundle root is 2 levels up.
_AR_BUNDLE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_BUNDLE_ROOT)

import components.ar_common.foodics_processing as fp  # noqa: E402

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
# [1] detect_type — export file-type identification by extension (§4)
# --------------------------------------------------------------------------- #
print("[1] detect_type")
eq(fp.detect_type("orders.xlsx"), "excel", "xlsx → excel")
eq(fp.detect_type("ORDERS.XLSX"), "excel", "case-insensitive ext")
eq(fp.detect_type("items.xls"), "excel", "xls → excel")
eq(fp.detect_type("payments.csv"), "csv", "csv → csv")
eq(fp.detect_type("orders.tsv"), "csv", "tsv → csv")
eq(fp.detect_type("note.pdf"), "unknown", "pdf → unknown")
eq(fp.detect_type("noext"), "unknown", "no extension → unknown")
eq(fp.detect_type(""), "unknown", "empty → unknown")

# --------------------------------------------------------------------------- #
# [2] _normalize_file — canvas File-node ref coercion
# --------------------------------------------------------------------------- #
print("[2] _normalize_file")
eq(fp._normalize_file("/tmp/foodics/orders.csv"),
   {"name": "orders.csv", "path": "/tmp/foodics/orders.csv"},
   "bare string → {name,path}")
eq(fp._normalize_file({"file_path": "/a/b.csv", "file_name": "b.csv"}),
   {"name": "b.csv", "path": "/a/b.csv"}, "dict with file_path/file_name")
eq(fp._normalize_file({"path": "/c/d.xlsx"}),
   {"name": "d.xlsx", "path": "/c/d.xlsx"}, "dict with path → basename name")


class _DataObj:
    def __init__(self, d):
        self.data = d


eq(fp._normalize_file(_DataObj({"file_path": "/e/f.xlsx"})),
   {"name": "f.xlsx", "path": "/e/f.xlsx"}, "Data obj with .data.file_path")
eq(fp._normalize_file(None), {"name": "", "path": ""}, "None → empty")
eq(fp._normalize_file({}), {"name": "", "path": ""}, "empty dict → empty")

# --------------------------------------------------------------------------- #
# [3] _classify_input — role classification (filename keyword + header fallback)
# --------------------------------------------------------------------------- #
print("[3] _classify_input")
eq(fp._classify_input("Order Items.xlsx", []), "order_items",
   "filename 'item' → order_items")
eq(fp._classify_input("order_items.csv", []), "order_items",
   "filename underscore → order_items")
eq(fp._classify_input("Order Payments.xlsx", []), "order_payments",
   "filename 'payment' → order_payments")
eq(fp._classify_input("orders.csv", []), "order",
   "filename 'order' (not item/payment) → order")
# header fallback
eq(fp._classify_input("sheet1.xlsx", [{"item_ref": "I1", "qty": "2"}]),
   "order_items", "header item_ref+qty → order_items")
eq(fp._classify_input("sheet2.xlsx", [{"payment_ref": "P1",
                                         "payment_type": "cash"}]),
   "order_payments", "header payment_ref/payment_type → order_payments")
eq(fp._classify_input("sheet3.xlsx", [{"order_ref": "O1"}]),
   "order", "header order_ref → order")
eq(fp._classify_input("mystery.xlsx", [{"foo": "bar"}]), "unknown",
   "unrecognised → unknown")
# filename 'order' should NOT be beaten to order_items by an item header
eq(fp._classify_input("orders.xlsx", [{"item_ref": "I1", "qty": "2"}]),
   "order", "filename keyword wins over header fallback")

# --------------------------------------------------------------------------- #
# [4] _validate_role_rows — required columns + per-row rules (§9)
# --------------------------------------------------------------------------- #
print("[4] _validate_role_rows")
# order valid
orders = [
    {"order_ref": "O1", "posted_at": "2026-07-01", "customer_ref": "CUST-1"},
    {"order_ref": "O2", "posted_at": "2026-07-02"},
]
_oper, _oh, omiss = fp._validate_role_rows("order", orders)
eq(omiss, [], "order all required columns present")
eq(_oper[0], [], "order row 0 valid")
eq(_oper[1], [], "order row 1 valid (customer optional)")
# order missing required column
_oper2, _h, omiss2 = fp._validate_role_rows("order",
                                          [{"posted_at": "2026-07-01"}])
eq(omiss2, ["order_ref"], "order missing order_ref → reported")
# order_items valid
items = [
    {"order_ref": "O1", "item_ref": "I1", "qty": "2", "unit_price": "10.00"},
    {"order_ref": "O1", "item_ref": "I2", "qty": "1", "unit_price": "5.50",
     "discount_amount": "1.00"},
]
_iper, _ih, imiss = fp._validate_role_rows("order_items", items)
eq(imiss, [], "order_items all required present")
eq(_iper[0], [], "order_items row 0 valid")
eq(_iper[1], [], "order_items row 1 valid (discount optional)")
# order_items missing required column
_iper3, _ih3, imiss3 = fp._validate_role_rows("order_items",
                                             [{"order_ref": "O1",
                                               "item_ref": "I1"}])
eq(imiss3, ["qty", "unit_price"], "order_items missing qty+unit_price")
# per-row rule violations
bad_items = [{"order_ref": "", "item_ref": "", "qty": "0",
              "unit_price": "0"}]
_biper, _bih, _bim = fp._validate_role_rows("order_items", bad_items)
bids = {e["rule_id"] for e in _biper[0]}
truthy("fp.order_ref_required" in bids, "empty order_ref flagged")
truthy("fp.item_ref_required" in bids, "empty item_ref flagged")
truthy("fp.qty_positive" in bids, "qty 0 flagged")
truthy("fp.unit_price_positive" in bids, "unit_price 0 flagged")
# order_payments valid + method enum
pays = [{"payment_ref": "P1", "order_ref": "O1", "amount": "50.00",
         "method": "Check", "posted_at": "2026-07-01"}]
_pper, _ph, pmiss = fp._validate_role_rows("order_payments", pays)
eq(pmiss, [], "order_payments all required present")
eq(_pper[0], [], "order_payments valid (Check → bank_transfer accepted)")
# bad method
bad_pays = [{"payment_ref": "P1", "order_ref": "O1", "amount": "5",
             "method": "barter", "posted_at": "2026-07-01"}]
_bpper, _bph, _bpm = fp._validate_role_rows("order_payments", bad_pays)
bbids = {e["rule_id"] for e in _bpper[0]}
truthy("fp.method_enum" in bbids, "unknown method → fp.method_enum")
# missing required column for a present role
_pays_no_ref = [{"payment_ref": "P1", "amount": "5", "method": "cash",
                 "posted_at": "2026-07-01"}]
_p2, _ph2, pm2 = fp._validate_role_rows("order_payments", _pays_no_ref)
eq(pm2, ["order_ref"], "order_payments missing order_ref")
# every issue is schema-conformant
for e in _biper[0] + _bpper[0]:
    truthy(set(e.keys()) == {"path", "code", "message", "rule_id"},
           f"issue {e['rule_id']} has exactly 4 keys")
    truthy(bool(_re.match(r"^AR_VALIDATION(_[A-Z_]+)?$", e["code"])),
           f"issue {e['rule_id']} code matches AR_VALIDATION pattern")

# --------------------------------------------------------------------------- #
# [5] _classify_exceptions — exception report + missing-role warnings (§4/§15)
# --------------------------------------------------------------------------- #
print("[5] _classify_exceptions")
# Only order_items present (order + order_payments missing → warnings)
inputs_partial = {
    "order": [],
    "order_items": [{"order_ref": "O1", "item_ref": "I1", "qty": "2",
                     "unit_price": "10.00"},
                    {"order_ref": "", "item_ref": "", "qty": "0",
                     "unit_price": "0"}],
    "order_payments": [],
}
report, row_exc = fp._classify_exceptions(inputs_partial, "trace-1")
falsy(report["valid"], "report valid=False (row exception + missing roles)")
eq(report["contract_name"], "FoodicsInputs", "report contract_name")
eq(report["contract_version"], "1.0.0", "report contract_version")
eq(row_exc, 1, "one row exception counted")
rule_ids = {e["rule_id"] for e in report["errors"]}
truthy("fp.order_missing" in rule_ids, "missing order warned")
truthy("fp.order_payments_missing" in rule_ids, "missing order_payments warned")
truthy("fp.qty_positive" in rule_ids, "row error carried into report")
# all three roles present + all rows valid → valid report
inputs_full = {
    "order": [{"order_ref": "O1", "posted_at": "2026-07-01"}],
    "order_items": [{"order_ref": "O1", "item_ref": "I1", "qty": "2",
                     "unit_price": "10.00"}],
    "order_payments": [{"payment_ref": "P1", "order_ref": "O1",
                        "amount": "20.00", "method": "cash",
                        "posted_at": "2026-07-01"}],
}
report2, row_exc2 = fp._classify_exceptions(inputs_full, "trace-1")
truthy(report2["valid"], "all roles present + all rows valid → valid=True")
eq(row_exc2, 0, "no row exceptions")
# all-rows-fail check (order_items only, all bad)
inputs_bad = {
    "order": [],
    "order_items": [{"order_ref": "", "item_ref": "", "qty": "0",
                     "unit_price": "0"}],
    "order_payments": [],
}
_rep3, re3 = fp._classify_exceptions(inputs_bad, "t")
eq(re3, 1, "all-bad order_items row counted as exception")

# --------------------------------------------------------------------------- #
# [6] build_consolidated — order↔items join, payments attached, 2dp totals (§8)
# --------------------------------------------------------------------------- #
print("[6] build_consolidated")
order_rows = [
    {"order_ref": "O1", "posted_at": "2026-07-01", "customer_ref": "CUST-1",
     "currency": "SAR"},
    {"order_ref": "O2", "posted_at": "2026-07-02", "customer_ref": "CUST-2"},
]
item_rows = [
    {"order_ref": "O1", "item_ref": "I1", "qty": "2", "unit_price": "10.00",
     "description": "Coffee"},
    {"order_ref": "O1", "item_ref": "I2", "qty": "1", "unit_price": "5.50"},
    {"order_ref": "O2", "item_ref": "I3", "qty": "3", "unit_price": "4.00"},
]
pay_rows = [
    {"payment_ref": "P1", "order_ref": "O1", "amount": "25.00",
     "method": "cash", "posted_at": "2026-07-01"},
]
ohmap = fp._header_map(order_rows)
cons = fp.build_consolidated(order_rows, item_rows, pay_rows, ohmap,
                             "trace-1", "cosmic-vikings")
eq(cons["count"], 2, "two orders")
eq(cons["contract_version"], "1.0.0", "consolidated contract_version pinned")
o1 = next(o for o in cons["orders"] if o["order_ref"] == "O1")
eq(o1["customer_ref"], "CUST-1", "customer_ref from order row")
eq(o1["posted_at"], "2026-07-01", "posted_at from order row")
eq(o1["currency"], "SAR", "currency from order row")
eq(len(o1["items"]), 2, "O1 has 2 items")
eq(o1["gross_total"], "25.50", "O1 gross = 2*10 + 1*5.50 = 25.50")
eq(o1["payment_total"], "25.00", "O1 payment = 25.00")
eq(len(o1["payments"]), 1, "O1 has 1 payment")
eq(o1["payments"][0]["amount"], "25.00", "payment amount 2dp")
o2 = next(o for o in cons["orders"] if o["order_ref"] == "O2")
eq(o2["gross_total"], "12.00", "O2 gross = 3*4 = 12.00")
eq(o2["payment_total"], "0.00", "O2 no payments → 0.00")
eq(o2["currency"], "SAR", "O2 default currency SAR")
# empty inputs → zero orders
cons0 = fp.build_consolidated([], [], [], {}, "trace-1", "cosmic-vikings")
eq(cons0["count"], 0, "no inputs → zero orders")
eq(cons0["orders"], [], "no inputs → empty orders list")
# items without an order header row still surface (order_ref carries them)
cons_orphan = fp.build_consolidated([], [{"order_ref": "OX", "item_ref": "I1",
                                          "qty": "1", "unit_price": "3.00"}],
                                    [], {}, "trace-1", "cosmic-vikings")
eq(cons_orphan["count"], 1, "orphan item order surfaces")
eq(cons_orphan["orders"][0]["gross_total"], "3.00", "orphan gross computed")

# --------------------------------------------------------------------------- #
# [7] refresh_pivot — by_item + by_payment_type aggregations + totals
# --------------------------------------------------------------------------- #
print("[7] refresh_pivot")
pivot = fp.refresh_pivot(cons)
eq(pivot["contract_version"], "1.0.0", "pivot contract_version pinned")
by_item = {b["item_ref"]: b for b in pivot["by_item"]}
eq(by_item["I1"]["qty"], "2.00", "I1 qty aggregated")
eq(by_item["I1"]["amount"], "20.00", "I1 amount aggregated")
eq(by_item["I2"]["amount"], "5.50", "I2 amount")
eq(pivot["totals"]["gross"], "37.50", "gross = 25.50 + 12.00")
eq(pivot["totals"]["collected"], "25.00", "collected = 25.00")
by_pt = {b["payment_type"]: b for b in pivot["by_payment_type"]}
eq(by_pt["cash"]["amount"], "25.00", "cash payment_type amount")
eq(by_pt["cash"]["count"], 1, "cash payment_type count")
# empty consolidated → zeros, empty aggregations
pivot0 = fp.refresh_pivot({"orders": []})
eq(pivot0["totals"]["gross"], "0.00", "empty pivot gross 0.00")
eq(pivot0["totals"]["collected"], "0.00", "empty pivot collected 0.00")
eq(pivot0["by_item"], [], "empty pivot → no by_item")

# --------------------------------------------------------------------------- #
# [8] determine_payment_type — METHOD_SYNONYMS → enum, by_method, total (§15)
# --------------------------------------------------------------------------- #
print("[8] determine_payment_type")
pay_rows2 = [
    {"payment_ref": "P1", "order_ref": "O1", "amount": "100.00",
     "method": "Check", "posted_at": "2026-07-01"},
    {"payment_ref": "P2", "order_ref": "O1", "amount": "50.00",
     "method": "Cash", "posted_at": "2026-07-02"},
    {"payment_ref": "P3", "order_ref": "O2", "amount": "30.00",
     "method": "barter", "posted_at": "2026-07-03"},
]
summary = fp.determine_payment_type(pay_rows2, "trace-1", "cosmic-vikings")
eq(summary["total_collected"], "180.00", "total_collected = Σ")
eq(summary["contract_version"], "1.0.0", "summary contract_version pinned")
bm = {m["method"]: m for m in summary["by_method"]}
eq(bm["bank_transfer"]["amount"], "100.00", "Check → bank_transfer")
eq(bm["cash"]["amount"], "50.00", "Cash → cash")
eq(bm["other"]["amount"], "30.00", "unknown method → other")
for m in summary["by_method"]:
    truthy(m["method"] in ("cash", "card", "bank_transfer", "online", "wallet",
                           "other"), f"by_method {m['method']} in enum")
# empty payments → zeros
summary0 = fp.determine_payment_type([], "trace-1", "cosmic-vikings")
eq(summary0["total_collected"], "0.00", "empty → 0.00")
eq(summary0["by_method"], [], "empty → no by_method")

# --------------------------------------------------------------------------- #
# [9] apply_discounts — in-file > baked-in > 0.00, discounts_total 2dp (§17)
# --------------------------------------------------------------------------- #
print("[9] apply_discounts")
# Row 1: in-file discount_amount wins (flat 1.00 on gross 10.00 → net 9.00)
# Row 2: baked-in rule (beverage 10% on gross 20.00 → disc 2.00 → net 18.00)
# Row 3: no discount column, no rule match → 0.00 → net = gross
disc_rows = [
    {"order_ref": "O1", "item_ref": "I1", "qty": "1", "unit_price": "10.00",
     "discount_amount": "1.00"},
    {"order_ref": "O1", "item_ref": "I2", "qty": "2", "unit_price": "10.00",
     "category": "Beverage"},
    {"order_ref": "O2", "item_ref": "I3", "qty": "1", "unit_price": "7.00",
     "category": "Main"},
]
adjusted, disc_total = fp.apply_discounts(disc_rows, "trace-1")
eq(adjusted["O1"]["I1"], "9.00", "in-file discount_amount → net 9.00")
eq(adjusted["O1"]["I2"], "18.00", "baked-in beverage 10% → net 18.00")
eq(adjusted["O2"]["I3"], "7.00", "no discount → net = gross")
eq(disc_total, "3.00", "discounts_total = 1.00 + 2.00 + 0.00")
# discount_pct (percentage of gross)
pct_rows = [{"order_ref": "O1", "item_ref": "I1", "qty": "1",
             "unit_price": "100.00", "discount_pct": "10"}]
adj_pct, _ = fp.apply_discounts(pct_rows, "trace-1")
eq(adj_pct["O1"]["I1"], "90.00", "discount_pct 10% of 100 → net 90.00")
# discount capped at gross
cap_rows = [{"order_ref": "O1", "item_ref": "I1", "qty": "1",
             "unit_price": "10.00", "discount_amount": "99.00"}]
adj_cap, _ = fp.apply_discounts(cap_rows, "trace-1")
eq(adj_cap["O1"]["I1"], "0.00", "discount capped at gross → net 0.00")
# baked-in amount rule (combo_meal flat 2.50)
combo_rows = [{"order_ref": "O1", "item_ref": "combo_meal", "qty": "1",
               "unit_price": "20.00"}]
adj_combo, _ = fp.apply_discounts(combo_rows, "trace-1")
eq(adj_combo["O1"]["combo_meal"], "17.50", "baked-in amount 2.50 → net 17.50")
# empty rows
adj0, dt0 = fp.apply_discounts([], "trace-1")
eq(adj0, {}, "empty → empty adjusted map")
eq(dt0, "0.00", "empty → discounts_total 0.00")

# --------------------------------------------------------------------------- #
# [10] populate_sheet3 — per-order net summary (§8)
# --------------------------------------------------------------------------- #
print("[10] populate_sheet3")
sheet3 = fp.populate_sheet3(cons, adjusted, "3.00")
eq(sheet3["contract_version"], "1.0.0", "sheet3 contract_version pinned")
eq(sheet3["count"], 2, "two order rows")
r1 = next(r for r in sheet3["rows"] if r["order_ref"] == "O1")
# O1 has I1 (9.00 net) + I2 (18.00 net) but `adjusted` here is from the
# discount test set, so O1 in `cons` items (I1,I2) — net = adjusted O1 I1+I2
# Note: adjusted above has O1 I1=9.00, O1 I2=18.00 → net 27.00
eq(r1["net"], "27.00", "O1 net = Σ adjusted line nets")
eq(r1["tax"], "0.00", "tax 0.00 (no tax engine v1)")
eq(r1["gross"], "25.50", "O1 gross from consolidated")
eq(r1["payment_type"], "cash", "O1 first payment method mapped")
r2 = next(r for r in sheet3["rows"] if r["order_ref"] == "O2")
eq(r2["payment_type"], "other", "O2 no payments → other")
# empty consolidated
sheet3_0 = fp.populate_sheet3({"orders": []}, {}, "0.00")
eq(sheet3_0["count"], 0, "empty → zero rows")
eq(sheet3_0["rows"], [], "empty → empty rows")

# --------------------------------------------------------------------------- #
# [11] build_zoho_upload — Zoho Books invoice-import row shape (§15/§16)
# --------------------------------------------------------------------------- #
print("[11] build_zoho_upload")
zoho = fp.build_zoho_upload(cons, adjusted, "trace-1")
eq(zoho["format"], "zoho-books-invoice-import", "format pinned")
eq(zoho["count"], 2, "two rows")
eq(zoho["contract_version"], "1.0.0", "zoho contract_version pinned")
z1 = next(z for z in zoho["rows"] if z["invoice_number"] == "FP-O1")
eq(z1["customer_ref"], "CUST-1", "customer_ref (Zoho id, no PII)")
eq(z1["date"], "2026-07-01", "date = order posted_at")
eq(z1["currency"], "SAR", "currency threaded")
eq(len(z1["item_details"]), 2, "O1 item_details has 2 entries")
id0 = z1["item_details"][0]
eq(set(id0.keys()), {"item_ref", "qty", "rate", "amount", "discount"},
   "item_detail has exactly 5 fields")
eq(id0["amount"], "9.00", "discount-adjusted amount")
eq(z1["total"], "27.00", "O1 total = Σ net")
# empty
zoho0 = fp.build_zoho_upload({"orders": []}, {}, "trace-1")
eq(zoho0["count"], 0, "empty → zero rows")
eq(zoho0["rows"], [], "empty → empty rows")

# --------------------------------------------------------------------------- #
# [12] build_invoices — one InvoiceData per order_ref, deterministic ids (§15)
# --------------------------------------------------------------------------- #
print("[12] build_invoices")
# Use a consistent adjusted map that matches `cons`'s items so the discount
# math is clean: O1 I1 gross 20.00 → net 18.00 (disc 2.00); O1 I2 gross 5.50 →
# net 5.00 (disc 0.50); O2 I3 gross 12.00 → net 12.00 (no disc).
adj_inv = {"O1": {"I1": "18.00", "I2": "5.00"}, "O2": {"I3": "12.00"}}
invoices = fp.build_invoices(cons, adj_inv, "2.50", "trace-1", "cosmic-vikings")
eq(len(invoices), 2, "one invoice per order")
inv1 = next(i for i in invoices if i["customer_ref"] == "CUST-1")
eq(set(inv1.keys()), {"invoice_id", "invoice_number", "customer_ref",
                      "tenant", "issue_date", "due_date", "line_items",
                      "subtotal", "discounts", "total", "balance_due",
                      "currency", "status", "contract_version"},
   "invoice has exactly the InvoiceData fields")
eq(inv1["status"], "draft", "status = draft (no posting)")
eq(inv1["currency"], "SAR", "currency SAR")
eq(inv1["issue_date"], "2026-07-01", "issue_date = order posted_at")
eq(inv1["due_date"], "2026-07-31", "due_date = issue + 30 days")
eq(inv1["subtotal"], "25.50", "subtotal = Σ gross (20.00 + 5.50)")
eq(inv1["discounts"], "2.50", "O1 discounts = 2.00 + 0.50")
eq(inv1["total"], "23.00", "total = subtotal - discounts (25.50 - 2.50)")
eq(inv1["balance_due"], "23.00", "balance_due = total")
eq(len(inv1["line_items"]), 2, "2 line items")
li0 = inv1["line_items"][0]
eq(set(li0.keys()), {"line_id", "item_ref", "description", "qty", "unit_price",
                    "amount"}, "line_item has exactly 6 fields")
eq(li0["amount"], "18.00", "line amount = discount-adjusted net")
truthy(_re.match(r"^FP-O1-[0-9A-F]{8}$", inv1["invoice_number"]),
       "invoice_number shaped FP-{order_ref}-{8hex}")
# deterministic ids
inv_again = fp.build_invoices(cons, adj_inv, "2.50", "trace-1",
                              "cosmic-vikings")
eq(inv_again[0]["invoice_id"], invoices[0]["invoice_id"],
   "invoice_id deterministic for same trace+order")
# different trace → different id
inv_other = fp.build_invoices(cons, adj_inv, "2.50", "trace-2",
                             "cosmic-vikings")
falsy(inv_other[0]["invoice_id"] == invoices[0]["invoice_id"],
      "different trace → different invoice_id")

# --------------------------------------------------------------------------- #
# [13] _validate_invoice — inline InvoiceData guard
# --------------------------------------------------------------------------- #
print("[13] _validate_invoice")
eq(fp._validate_invoice(inv1), [], "valid invoice → no errors")
bad_inv = {"invoice_id": "", "invoice_number": "", "customer_ref": "",
           "tenant": "", "issue_date": "bad", "due_date": "bad",
           "line_items": [], "subtotal": "x", "total": "y",
           "balance_due": "z", "currency": "us", "status": "",
           "contract_version": ""}
errs = fp._validate_invoice(bad_inv)
truthy(any("missing required field" in e for e in errs), "missing fields flagged")
truthy(any("must be a 2dp string" in e for e in errs), "non-2dp totals flagged")
truthy(any("must be ^[A-Z]{3}$" in e for e in errs), "bad currency flagged")
truthy(any("must be YYYY-MM-DD" in e for e in errs), "bad date flagged")
truthy(any("line_items must be a non-empty array" in e for e in errs),
       "empty line_items flagged")

# --------------------------------------------------------------------------- #
# [14] build_workflow_state — WorkflowState snapshot (no money moved)
# --------------------------------------------------------------------------- #
print("[14] build_workflow_state")
ws = fp.build_workflow_state("trace-1", "ar_foodics_processing", "cosmic-vikings",
                             ["audit-1"], "2026-07-01T00:00:00Z",
                             "2026-07-01T00:01:00Z")
eq(ws["status"], "completed", "workflow status = completed (draft built)")
eq(ws["intent"], "ar_foodics_processing", "intent = the subflow id")
eq(ws["matched_amount"], "0.00", "matched_amount 0.00 (no money moved)")
eq(ws["outstanding_balance"], "0.00", "outstanding_balance 0.00")
eq(ws["posted_total"], "0.00", "posted_total 0.00 (no posting)")
eq(ws["pending_approvals"], [], "no pending approvals (gate dormant)")
eq(ws["idempotency_keys"], {}, "no idempotency keys (no POST)")
eq(ws["audit_refs"], ["audit-1"], "audit_refs threaded through")
truthy(ws["tool_call_ref"].startswith("trace-1:ar_foodics_processing:"),
      "tool_call_ref shaped trace_id:intent:index")
eq(ws["contract_version"], "1.0.0", "workflow state contract_version pinned")

# --------------------------------------------------------------------------- #
# [15] _audit_ref + checkpoint map — deterministic per-calc (§11)
# --------------------------------------------------------------------------- #
print("[15] _audit_ref / checkpoint")
r1 = fp._audit_ref("trace-1", "consolidated")
r2 = fp._audit_ref("trace-1", "consolidated")
eq(r1, r2, "audit_ref deterministic for same trace+label")
r3 = fp._audit_ref("trace-1", "pivot")
falsy(r1 == r3, "different label → different audit_ref")
# The 7 per-calc labels + the final aggregate are all distinct
labels = ("consolidated", "pivot", "payment_type", "discounts", "sheet3",
          "zoho_upload", "invoice", "foodics_processing")
refs = [fp._audit_ref("trace-1", lb) for lb in labels]
eq(len(set(refs)), len(labels), "all per-calc + final audit refs distinct")

# --------------------------------------------------------------------------- #
# [16] _envelope shape (§14)
# --------------------------------------------------------------------------- #
print("[16] _envelope")
env = fp._envelope("ok", "AR_OK", data={"x": 1}, trace_id="t")
eq(env, {"status": "ok", "code": "AR_OK", "data": {"x": 1}, "trace_id": "t"},
   "ok envelope shape")
env_e = fp._envelope("error", "AR_VALIDATION", error={"message": "bad"},
                     trace_id="t")
truthy(env_e["error"] == {"message": "bad"}, "error envelope carries error")
truthy(env_e["trace_id"] == "t", "error envelope carries trace_id")

# --------------------------------------------------------------------------- #
# [17] _is_transient — §10 retry classification
# --------------------------------------------------------------------------- #
print("[17] _is_transient")
truthy(fp._is_transient(TimeoutError("x")), "TimeoutError → transient")
truthy(fp._is_transient(_HTTPError(500)), "HTTP 500 → transient")
truthy(fp._is_transient(_HTTPError(408)), "HTTP 408 → transient")
truthy(fp._is_transient(_HTTPError(429)), "HTTP 429 → transient")
falsy(fp._is_transient(_HTTPError(404)), "HTTP 404 → hard")
falsy(fp._is_transient(_HTTPError(401)), "HTTP 401 → hard")
falsy(fp._is_transient(ValueError("bad")), "ValueError → hard")

# --------------------------------------------------------------------------- #
# [18] parse_envelope — reader/tool output parsing
# --------------------------------------------------------------------------- #
print("[18] parse_envelope")
eq(fp.parse_envelope('{"status":"ok","code":"AR_OK","data":{}}'),
   {"status": "ok", "code": "AR_OK", "data": {}}, "valid json dict")
eq(fp.parse_envelope("not json"), None, "non-json → None")
eq(fp.parse_envelope("[1,2,3]"), None, "json array → None")
eq(fp.parse_envelope(""), None, "empty → None")
eq(fp.parse_envelope(None), None, "None → None")

# --------------------------------------------------------------------------- #
# [19] numeric/timestamp/token coercion
# --------------------------------------------------------------------------- #
print("[19] numeric/timestamp/token coercion")
eq(fp._to_2dp("10.5"), "10.50", "10.5 → 10.50")
eq(fp._to_2dp("1,234.5"), "1234.50", "thousands sep stripped")
eq(fp._to_2dp("SAR 99.999"), "100.00", "prefix stripped, quantised to 2dp")
eq(fp._to_2dp(""), "0.00", "empty → 0.00")
eq(fp._to_2dp(None), "0.00", "None → 0.00")
eq(fp._to_2dp("-5"), "0.00", "negative clamped to 0.00 (non-negative)")
eq(fp._to_signed_2dp("-25.50"), "-25.50", "signed 2dp keeps negative")
eq(fp._sum_2dp(["100.00", "-25.50", "0.25"]), "74.75", "sum to 2dp")
eq(fp._sum_2dp([]), "0.00", "empty sum → 0.00")
eq(fp._parse_date("2026-07-01"), "2026-07-01", "ISO date passthrough")
eq(fp._parse_date("2026/07/01"), "2026-07-01", "slash date normalised")
eq(fp._parse_date("garbage"), None, "garbage → None")
eq(fp._parse_date(""), None, "empty → None")
eq(fp._to_iso_datetime("2026-07-01"), "2026-07-01T00:00:00Z",
   "date-only → midnight UTC")
eq(fp._to_iso_datetime("garbage"), fp.utc_now(), "unparseable → utc_now")
eq(fp._add_days("2026-07-01", 30), "2026-07-31", "add_days 30 → 2026-07-31")
eq(fp._add_days("2026-01-31", 1), "2026-02-01", "add_days crosses month")
eq(fp._norm_token("Beverage"), "beverage", "Beverage → beverage")
eq(fp._norm_token("  Food-Cost!! "), "food_cost", "punctuation normalised")
eq(fp._norm_token(""), "", "empty token → empty (caller defaults)")
eq(fp._map_method("Check"), "bank_transfer", "Check → bank_transfer")
eq(fp._map_method("CASH"), "cash", "case-insensitive method")
eq(fp._map_method("barter"), "", "unknown method → '' (flagged by validator)")
eq(fp._map_method(""), "", "empty method → ''")

# --------------------------------------------------------------------------- #
# [20] _valid_rows_for — per-role valid-row extraction
# --------------------------------------------------------------------------- #
print("[20] _valid_rows_for")
v, _h = fp._valid_rows_for("order_items",
                           {"order_items": items + [{"order_ref": "", "item_ref": "",
                                                     "qty": "0", "unit_price": "0"}]})
eq(len(v), 2, "2 valid order_items rows (1 bad excluded)")
v2, _h2 = fp._valid_rows_for("order_items", {"order_items": []})
eq(v2, [], "absent role → no valid rows")
v3, _h3 = fp._valid_rows_for("order_payments",
                             {"order_payments": [{"amount": "5", "method": "cash",
                                                 "posted_at": "2026-07-01"}]})
eq(v3, [], "missing required column → zero valid rows")

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)