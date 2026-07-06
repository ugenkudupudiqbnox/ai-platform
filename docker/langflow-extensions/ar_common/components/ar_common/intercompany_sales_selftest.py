#!/usr/bin/env python3
"""intercompany_sales_selftest — offline stdlib-only tests for the Intercompany
Sales Flow's pure functions.

Covers (constitution §1/§4/§8/§9/§10/§11/§14/§15/§16): KOT file-type detection,
file-ref normalization, KOT-row validation (required columns + per-row rules),
exception classification, deterministic revenue calculation (qty × agreed_rate,
by-segment/by-customer grouping, period), draft InvoiceData assembly (one per
buyer, deterministic ids, status="draft"), WorkflowState snapshot shape, inline
InvoiceData validation, §14 envelope shape, §10 retry classification, and
numeric/timestamp coercion. No network, no LangFlow, no Docker, no openpyxl —
`python3 intercompany_sales_selftest.py` runs anywhere. Mirrors
file_intake_selftest's harness (CLAUDE.md self-test convention): PASS/FAIL
counts, exits non-zero on any failure, so `make test` (via
scripts/intercompany-sales.selftest.sh) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/intercompany_sales_selftest.py
"""
import os
import re as _re
import sys
import types
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so intercompany_sales imports without the in-image venv.
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

# Add the ar_common bundle root so `components.ar_common.intercompany_sales`
# resolves (mirrors the in-image pip-installed editable bundle).
# HERE = .../ar_common/components/ar_common  → bundle root is 2 levels up.
_AR_BUNDLE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_BUNDLE_ROOT)

import components.ar_common.intercompany_sales as ic  # noqa: E402

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
# [1] detect_type — KOT file-type identification by extension (§4)
# --------------------------------------------------------------------------- #
print("[1] detect_type")
eq(ic.detect_type("kot.xlsx"), "excel", "xlsx → excel")
eq(ic.detect_type("kot.XLSX"), "excel", "case-insensitive ext")
eq(ic.detect_type("kot.xls"), "excel", "xls → excel")
eq(ic.detect_type("kot.xlsm"), "excel", "xlsm → excel")
eq(ic.detect_type("kot.csv"), "csv", "csv → csv")
eq(ic.detect_type("kot.tsv"), "csv", "tsv → csv")
eq(ic.detect_type("note.pdf"), "unknown", "pdf → unknown (KOT is Excel/CSV)")
eq(ic.detect_type("noext"), "unknown", "no extension → unknown")
eq(ic.detect_type(""), "unknown", "empty → unknown")

# --------------------------------------------------------------------------- #
# [2] _normalize_file — canvas File-node ref coercion
# --------------------------------------------------------------------------- #
print("[2] _normalize_file")
eq(ic._normalize_file("/tmp/kot/x.csv"),
   {"name": "x.csv", "path": "/tmp/kot/x.csv"}, "bare string → {name,path}")
eq(ic._normalize_file({"file_path": "/a/b.csv", "file_name": "b.csv"}),
   {"name": "b.csv", "path": "/a/b.csv"}, "dict with file_path/file_name")
eq(ic._normalize_file({"path": "/c/d.xlsx"}),
   {"name": "d.xlsx", "path": "/c/d.xlsx"}, "dict with path → basename name")


class _DataObj:
    def __init__(self, d):
        self.data = d


eq(ic._normalize_file(_DataObj({"file_path": "/e/f.xlsx"})),
   {"name": "f.xlsx", "path": "/e/f.xlsx"}, "Data obj with .data.file_path")
eq(ic._normalize_file(None), {"name": "", "path": ""}, "None → empty")
eq(ic._normalize_file({}), {"name": "", "path": ""}, "empty dict → empty")

# --------------------------------------------------------------------------- #
# [3] _validate_kot_rows — required columns + per-row rules (§9)
# --------------------------------------------------------------------------- #
print("[3] _validate_kot_rows")
rows = [
    {"customer_ref": "HYP", "item_ref": "MENU-01", "qty": "10",
     "agreed_rate": "12.50", "posted_at": "2026-07-01", "description": "Espresso"},
    {"customer_ref": "HYP", "item_ref": "MENU-02", "qty": "2",
     "agreed_rate": "30.00", "posted_at": "2026-07-02", "description": "Cappuccino"},
    {"customer_ref": "Upyard", "item_ref": "MENU-01", "qty": "5",
     "agreed_rate": "12.50", "posted_at": "2026-07-03", "description": "Espresso"},
]
per_row, hmap, missing = ic._validate_kot_rows(rows)
eq(missing, [], "all required columns present → no missing")
eq(per_row[0], [], "valid row → no errors")
eq(per_row[1], [], "second valid row → no errors")

# missing required column (no item_ref anywhere)
rows_missing = [{"customer_ref": "HYP", "qty": "1", "agreed_rate": "5.00",
                 "posted_at": "2026-07-01"}]
_per, _hmap, missing2 = ic._validate_kot_rows(rows_missing)
eq(missing2, ["item_ref"], "item_ref absent → reported missing")

# per-row rule violations
bad_rows = [
    {"customer_ref": "", "item_ref": "M", "qty": "0",
     "agreed_rate": "-5.00", "posted_at": "garbage"},
]
bper, _bh, _bm = ic._validate_kot_rows(bad_rows)
rule_ids = {e["rule_id"] for e in bper[0]}
truthy("kot.customer_ref_required" in rule_ids, "empty customer_ref flagged")
truthy("kot.qty_positive" in rule_ids, "qty 0 flagged (not positive)")
truthy("kot.rate_positive" in rule_ids, "negative agreed_rate flagged")
truthy("kot.date_iso" in rule_ids, "bad posted_at flagged")
# every issue is schema-conformant
for e in bper[0]:
    truthy(set(e.keys()) == {"path", "code", "message", "rule_id"},
           f"issue {e['rule_id']} has exactly 4 keys")
    truthy(bool(_re.match(r"^AR_VALIDATION(_[A-Z_]+)?$", e["code"])),
           f"issue {e['rule_id']} code matches AR_VALIDATION pattern")

# --------------------------------------------------------------------------- #
# [4] _classify_exceptions — valid vs exception split + Exception Report (§4/§15)
# --------------------------------------------------------------------------- #
print("[4] _classify_exceptions")
mixed = [
    {"customer_ref": "HYP", "item_ref": "M", "qty": "2",
     "agreed_rate": "10.00", "posted_at": "2026-07-01"},
    {"customer_ref": "", "item_ref": "M", "qty": "0",
     "agreed_rate": "5.00", "posted_at": "bad"},
]
mper, _mh, _mm = ic._validate_kot_rows(mixed)
valid, exception, report = ic._classify_exceptions(mixed, mper, "trace-1")
eq(len(valid), 1, "one valid row")
eq(len(exception), 1, "one exception row")
eq(valid[0]["customer_ref"], "HYP", "valid row is the HYP row")
falsy(report["valid"], "exception report valid=False when there are exceptions")
eq(report["contract_name"], "KOTrows", "exception report contract_name")
eq(report["contract_version"], "1.0.0", "exception report contract_version")
truthy(len(report["errors"]) == 3, "exception report carries the 3 row errors")
for e in report["errors"]:
    truthy(bool(e.get("rule_id")), "exception error has a rule_id")

# all-rows-fail: valid_rows empty (the node fails safe on this)
allbad = [{"customer_ref": "", "item_ref": "", "qty": "", "agreed_rate": "",
           "posted_at": ""}]
abper, _ah, _am = ic._validate_kot_rows(allbad)
v2, ex2, rep2 = ic._classify_exceptions(allbad, abper, "t")
eq(len(v2), 0, "all-bad → zero valid rows")
eq(len(ex2), 1, "all-bad → one exception row")
falsy(rep2["valid"], "all-bad report valid=False")

# --------------------------------------------------------------------------- #
# [5] calculate_revenue — qty × agreed_rate, grouping, period (§8/§4.3)
# --------------------------------------------------------------------------- #
print("[5] calculate_revenue")
valid_rows = [
    {"customer_ref": "HYP", "item_ref": "MENU-01", "qty": "10",
     "agreed_rate": "12.50", "posted_at": "2026-07-01"},
    {"customer_ref": "HYP", "item_ref": "MENU-02", "qty": "2",
     "agreed_rate": "30.00", "posted_at": "2026-07-02"},
    {"customer_ref": "Upyard", "item_ref": "MENU-01", "qty": "5",
     "agreed_rate": "12.50", "posted_at": "2026-07-03"},
]
_per5, hmap5, _miss5 = ic._validate_kot_rows(valid_rows)
vr5, _ex5, _rep5 = ic._classify_exceptions(valid_rows, _per5, "trace-1")
rev = ic.calculate_revenue(vr5, hmap5, "trace-1", "cosmic-vikings")
eq(rev["total"], "247.50", "total = Σ (125.00 + 60.00 + 62.50)")
eq(rev["currency"], "SAR", "default currency SAR")
eq(rev["period"], {"start": "2026-07-01", "end": "2026-07-03"},
   "period = min/max posted_at")
eq(rev["contract_version"], "1.0.0", "revenue contract_version pinned")
eq(rev["trace_id"], "trace-1", "trace_id threaded through")
eq(rev["tenant"], "cosmic-vikings", "tenant threaded through")
eq(rev["by_invoice"], [], "by_invoice empty before build_invoices")
# by_segment groups by customer_ref
seg = {s["segment"]: s for s in rev["by_segment"]}
eq(seg["HYP"]["amount"], "185.00", "HYP segment amount = 125.00 + 60.00")
eq(seg["HYP"]["count"], 2, "HYP segment count = 2 rows")
eq(seg["Upyard"]["amount"], "62.50", "Upyard segment amount = 62.50")
eq(seg["Upyard"]["count"], 1, "Upyard segment count = 1 row")
# by_customer_ref mirrors by_segment
cust = {c["customer_ref"]: c for c in rev["by_customer_ref"]}
eq(cust["HYP"]["amount"], "185.00", "by_customer_ref HYP amount")
eq(cust["Upyard"]["count"], 1, "by_customer_ref Upyard count")
# segment issue shape: exactly {segment, amount, count}
for s in rev["by_segment"]:
    truthy(set(s.keys()) == {"segment", "amount", "count"},
           "segment has exactly {segment,amount,count}")

# --------------------------------------------------------------------------- #
# [6] build_invoices — one InvoiceData per buyer, deterministic, draft (§15/§16)
# --------------------------------------------------------------------------- #
print("[6] build_invoices")
invoices = ic.build_invoices(vr5, hmap5, rev, "trace-1", "cosmic-vikings")
eq(len(invoices), 2, "two buyers → two invoices")
by_cust = {inv["customer_ref"]: inv for inv in invoices}
eq(set(by_cust.keys()), {"HYP", "Upyard"}, "one invoice per buyer customer_ref")
hyp = by_cust["HYP"]
eq(len(hyp["line_items"]), 2, "HYP invoice has 2 line items")
eq(hyp["subtotal"], "185.00", "HYP subtotal = 185.00")
eq(hyp["total"], "185.00", "HYP total = subtotal (no tax/discount v1)")
eq(hyp["balance_due"], "185.00", "HYP balance_due = total (draft, unpaid)")
eq(hyp["status"], "draft", "invoice status = draft")
eq(hyp["currency"], "SAR", "invoice currency SAR")
eq(hyp["issue_date"], "2026-07-01", "HYP issue_date = earliest posted_at")
eq(hyp["due_date"], "2026-07-31", "HYP due_date = issue + 30 days")
eq(hyp["contract_version"], "1.0.0", "invoice contract_version pinned")
up = by_cust["Upyard"]
eq(len(up["line_items"]), 1, "Upyard invoice has 1 line item")
eq(up["subtotal"], "62.50", "Upyard subtotal = 62.50")
eq(up["issue_date"], "2026-07-03", "Upyard issue_date = its posted_at")
eq(up["due_date"], "2026-08-02", "Upyard due_date = issue + 30 days")
# line_item shape + 2dp amounts
li = hyp["line_items"][0]
eq(set(li.keys()), {"line_id", "item_ref", "description", "qty", "unit_price",
                    "amount"}, "line_item has exactly the 6 schema fields")
eq(li["qty"], "10.00", "qty quantised to 2dp")
eq(li["unit_price"], "12.50", "unit_price = agreed_rate 2dp")
eq(li["amount"], "125.00", "amount = qty × rate 2dp")
eq(li["item_ref"], "MENU-01", "item_ref from menu column")
# deterministic ids: same trace+buyer → same ids
id1, num1 = ic._deterministic_invoice_id("trace-1", "HYP")
id2, num2 = ic._deterministic_invoice_id("trace-1", "HYP")
eq((id1, num1), (id2, num2), "invoice ids deterministic for same inputs")
truthy(num1.startswith("IC-HYP-"), "invoice_number shaped IC-<cust>-<hex>")
# build_invoices backfilled revenue.by_invoice
eq(len(rev["by_invoice"]), 2, "by_invoice backfilled with 2 entries")
bi = {b["invoice_ref"]: b for b in rev["by_invoice"]}
eq(bi[num1]["customer_ref"], "HYP", "by_invoice entry links invoice→customer")
eq(bi[num1]["amount"], "185.00", "by_invoice entry amount = invoice subtotal")

# --------------------------------------------------------------------------- #
# [7] _validate_invoice — inline InvoiceData guard
# --------------------------------------------------------------------------- #
print("[7] _validate_invoice")
eq(ic._validate_invoice(hyp), [], "valid invoice → no errors")
bad_inv = {**hyp, "subtotal": "100.5", "currency": "sar", "issue_date": "07/01/26",
           "line_items": []}
errs = ic._validate_invoice(bad_inv)
truthy(any("subtotal" in e for e in errs), "subtotal not 2dp flagged")
truthy(any("currency" in e for e in errs), "bad currency flagged")
truthy(any("issue_date" in e for e in errs), "bad issue_date flagged")
truthy(any("line_items" in e for e in errs), "empty line_items flagged")

# --------------------------------------------------------------------------- #
# [8] build_workflow_state — WorkflowState snapshot (no money moved)
# --------------------------------------------------------------------------- #
print("[8] build_workflow_state")
ws = ic.build_workflow_state("trace-1", "ar_intercompany_sales", "cosmic-vikings",
                             ["audit-1"], "2026-07-01T00:00:00Z",
                             "2026-07-01T00:01:00Z")
eq(ws["status"], "completed", "workflow status = completed (draft built)")
eq(ws["intent"], "ar_intercompany_sales", "intent = the subflow id")
eq(ws["matched_amount"], "0.00", "matched_amount 0.00 (no money moved)")
eq(ws["outstanding_balance"], "0.00", "outstanding_balance 0.00")
eq(ws["posted_total"], "0.00", "posted_total 0.00 (no posting)")
eq(ws["pending_approvals"], [], "no pending approvals (gate dormant v1)")
eq(ws["idempotency_keys"], {}, "no idempotency keys (no POST)")
eq(ws["audit_refs"], ["audit-1"], "audit_refs threaded through")
truthy(ws["tool_call_ref"].startswith("trace-1:ar_intercompany_sales:"),
       "tool_call_ref shaped trace_id:intent:index")
eq(ws["contract_version"], "1.0.0", "workflow state contract_version pinned")

# --------------------------------------------------------------------------- #
# [9] _envelope shape (§14)
# --------------------------------------------------------------------------- #
print("[9] _envelope")
env = ic._envelope("ok", "AR_OK", data={"x": 1}, trace_id="t")
eq(env, {"status": "ok", "code": "AR_OK", "data": {"x": 1}, "trace_id": "t"},
   "ok envelope shape")
env_e = ic._envelope("error", "AR_VALIDATION", error={"message": "bad"},
                     trace_id="t")
truthy(env_e["error"] == {"message": "bad"}, "error envelope carries error")
truthy(env_e["trace_id"] == "t", "error envelope carries trace_id")

# --------------------------------------------------------------------------- #
# [10] _is_transient — §10 retry classification
# --------------------------------------------------------------------------- #
print("[10] _is_transient")
truthy(ic._is_transient(TimeoutError("x")), "TimeoutError → transient")
truthy(ic._is_transient(_HTTPError(500)), "HTTP 500 → transient")
truthy(ic._is_transient(_HTTPError(408)), "HTTP 408 → transient")
truthy(ic._is_transient(_HTTPError(429)), "HTTP 429 → transient")
falsy(ic._is_transient(_HTTPError(404)), "HTTP 404 → hard")
falsy(ic._is_transient(_HTTPError(401)), "HTTP 401 → hard")
falsy(ic._is_transient(ValueError("bad")), "ValueError → hard")

# --------------------------------------------------------------------------- #
# [11] parse_envelope — reader/tool output parsing
# --------------------------------------------------------------------------- #
print("[11] parse_envelope")
eq(ic.parse_envelope('{"status":"ok","code":"AR_OK","data":{}}'),
   {"status": "ok", "code": "AR_OK", "data": {}}, "valid json dict")
eq(ic.parse_envelope("not json"), None, "non-json → None")
eq(ic.parse_envelope("[1,2,3]"), None, "json array → None")
eq(ic.parse_envelope(""), None, "empty → None")
eq(ic.parse_envelope(None), None, "None → None")

# --------------------------------------------------------------------------- #
# [12] _to_2dp / _sum_2dp / _to_signed_2dp / _parse_date / _add_days
# --------------------------------------------------------------------------- #
print("[12] numeric/timestamp coercion")
eq(ic._to_2dp("10.5"), "10.50", "10.5 → 10.50")
eq(ic._to_2dp("1,234.5"), "1234.50", "thousands sep stripped")
eq(ic._to_2dp("SAR 99.999"), "100.00", "prefix stripped, quantised to 2dp")
eq(ic._to_2dp(""), "0.00", "empty → 0.00")
eq(ic._to_2dp(None), "0.00", "None → 0.00")
eq(ic._to_2dp("-5"), "0.00", "negative clamped to 0.00 (non-negative)")
eq(ic._to_signed_2dp("-25.50"), "-25.50", "signed 2dp keeps negative")
eq(ic._sum_2dp(["100.00", "-25.50", "0.25"]), "74.75", "sum to 2dp")
eq(ic._sum_2dp([]), "0.00", "empty sum → 0.00")
eq(ic._parse_date("2026-07-01"), "2026-07-01", "ISO date passthrough")
eq(ic._parse_date("2026/07/01"), "2026-07-01", "slash date normalised")
eq(ic._parse_date("garbage"), None, "garbage → None")
eq(ic._parse_date(""), None, "empty → None")
eq(ic._add_days("2026-07-01", 30), "2026-07-31", "issue + 30 days")
eq(ic._add_days("2026-07-03", 30), "2026-08-02", "crosses month boundary")
eq(ic._add_days("2026-01-31", 1), "2026-02-01", "january roll-over")

# --------------------------------------------------------------------------- #
# [13] _build_validation_report — full ValidationResult over all rows
# --------------------------------------------------------------------------- #
print("[13] _build_validation_report")
clean_rows = [
    {"customer_ref": "HYP", "item_ref": "M", "qty": "1",
     "agreed_rate": "10.00", "posted_at": "2026-07-01"}
]
cper, _ch, _cm = ic._validate_kot_rows(clean_rows)
rep = ic._build_validation_report(clean_rows, cper, "trace-1")
truthy(rep["valid"], "clean sheet → valid=True")
eq(rep["errors"], [], "clean sheet → no errors")
eq(rep["contract_name"], "KOTrows", "report contract_name")
eq(rep["contract_version"], "1.0.0", "report contract_version")
truthy(bool(_re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                     rep["validated_at"])), "validated_at ISO-8601 UTC")

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)