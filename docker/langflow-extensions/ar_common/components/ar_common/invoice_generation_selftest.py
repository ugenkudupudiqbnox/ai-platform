#!/usr/bin/env python3
"""invoice_generation_selftest — offline stdlib-only tests for the Cosmic AR
Invoice Generation Flow's pure functions + end-to-end graph (constitution
§1/§4/§8/§9/§11/§14/§15/§16/§17).

Covers: numeric/date coercion (+ ``_add_days``); validated-JSON invoice-request
parsing (good / empty / malformed / non-object → AR_VALIDATION); payload
validation (no customer_ref / no line_items / no issue_date → hard AR_VALIDATION;
bad line_item qty|unit_price / bad currency / totals inconsistency → warnings);
exception classification (ValidationResult scoped to failures); InvoiceData
assembly (deterministic ``uuid5`` invoice_id / invoice_number shaped
``IG-<customer>-<8hex>``, ``status="draft"``, ``due_date = issue + 30``, 2dp
amounts, ``line_id`` uuid5s, inline ``_validate_invoice`` guard); Journal Entry
(balanced double-entry ``total_debit == total_credit``, debit AR / credit
Revenue + TaxPayable / debit Discounts, ``status="draft"``); Customer Statement
(``opening_balance="0.00"``, ``closing_balance=total``, ``payments:[]``); Zoho
Upload File (``format:"zoho-books-invoice-import"``, ``count:1``); Invoice
Metadata (deterministic ``content_hash``, ``source_refs``); PDF/Excel render-ready
specs (``render_ready:true``); WorkflowState snapshot (intent
``ar_invoice_generation``, totals ``"0.00"``); deterministic audit refs + the
per-generation checkpoints map (§11 — 8 labels); §14 envelope shape; and
end-to-end execution via ``run()`` (good payload → AR_OK + 8 artifacts + 8
checkpoints; malformed JSON → AR_VALIDATION; no line_items → AR_VALIDATION;
missing optional fields → still AR_OK). No network, no LangFlow, no Docker —
``python3 invoice_generation_selftest.py`` runs anywhere. Mirrors
calculation_selftest's harness (CLAUDE.md self-test convention): PASS/FAIL
counts, exits non-zero on any failure, so ``make test`` (via
``scripts/invoice-generation.selftest.sh``) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/invoice_generation_selftest.py
"""
import json
import os
import re as _re
import sys
import types
import uuid
from dataclasses import asdict

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so invoice_generation imports without the in-image venv.
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

# ar_common bundle root on sys.path (this flow has no cosmic_common dependency).
_AR_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_ROOT)

import components.ar_common.invoice_generation as c  # noqa: E402

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
    "customer_ref": "CUST-42",
    "issue_date": "2026-07-07",
    "currency": "SAR",
    "po_number": "PO-99",
    "salesperson_ref": "SP-1",
    "notes": "v1 draft",
    "line_items": [
        {"item_ref": "ITEM-A", "description": "Consulting",
         "qty": "10.00", "unit_price": "150.00"},
        {"item_ref": "ITEM-B", "description": "Licence",
         "qty": "2.00", "unit_price": "500.00"},
    ],
    "tax": "75.00",
    "discounts": "100.00",
}
# subtotal = 10*150 + 2*500 = 2500.00 ; total = 2500 + 75 - 100 = 2475.00
EXPECTED_SUBTOTAL = "2500.00"
EXPECTED_TOTAL = "2475.00"
EXPECTED_DUE = "2026-08-06"  # 2026-07-07 + 30 days


def _run(payload_text, layout_json=None):
    """Drive InvoiceGenerationFlowComponent.run() with a stub graph; return the envelope dict."""
    comp = c.InvoiceGenerationFlowComponent()
    comp.user_input = payload_text
    comp.layout = layout_json if layout_json is not None else c.LAYOUT_JSON
    comp.model_name = "glm-5.2:cloud"
    comp.session_id = "s1"
    return json.loads(comp.run().text)


# --------------------------------------------------------------------------- #
# [1] numeric / date helpers
# --------------------------------------------------------------------------- #
print("[1] numeric / date helpers")
eq(c._to_2dp("1234.5"), "1234.50", "half-up 2dp")
eq(c._to_2dp("-3.006"), "0.00", "negative clamped to 0.00 (non-neg)")
eq(c._to_signed_2dp("-3.006"), "-3.01", "signed half-up")
eq(c._sum_2dp(["1.10", "2.20", "-0.30"]), "3.00", "sum 2dp")
eq(c._parse_date("2026/07/07"), "2026-07-07", "slash date normalised")
falsy(c._parse_date("not a date"), "unparseable date → None")
eq(c._add_days("2026-07-07", 30), EXPECTED_DUE, "_add_days +30")
eq(c._add_days("2026-01-31", 1), "2026-02-01", "_add_days month rollover")
eq(c._add_days("bad", 30), "bad", "_add_days passthrough on unparseable")

# --------------------------------------------------------------------------- #
# [2] _parse_payload
# --------------------------------------------------------------------------- #
print("[2] _parse_payload")
p, err = c._parse_payload(json.dumps(GOOD_PAYLOAD))
falsy(err, "good payload → no error")
eq(p["customer_ref"], "CUST-42", "good payload parsed")
_, err = c._parse_payload("")
eq(err["code"], "AR_VALIDATION", "empty payload → AR_VALIDATION")
_, err = c._parse_payload("not json")
eq(err["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
_, err = c._parse_payload("[1,2,3]")
eq(err["code"], "AR_VALIDATION", "non-object payload → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [3] _validate_payload
# --------------------------------------------------------------------------- #
print("[3] _validate_payload")
report, _w, err = c._validate_payload(GOOD_PAYLOAD, "trace-1")
falsy(err, "good payload → no hard error")
truthy(report["valid"], "good payload valid=True")
eq(report["contract_name"], "InvoiceGenerationInputs", "contract_name")
# no customer_ref → hard fail
report, _w, err = c._validate_payload({"line_items": [{"item_ref": "X", "description": "d",
        "qty": "1.00", "unit_price": "1.00"}], "issue_date": "2026-07-07"}, "t1")
eq(err["code"], "AR_VALIDATION", "no customer_ref → AR_VALIDATION")
# no line_items → hard fail
report, _w, err = c._validate_payload({"customer_ref": "C", "issue_date": "2026-07-07"}, "t1")
eq(err["code"], "AR_VALIDATION", "no line_items → AR_VALIDATION")
# no issue_date → hard fail
report, _w, err = c._validate_payload({"customer_ref": "C",
        "line_items": [{"item_ref": "X", "description": "d", "qty": "1.00",
                        "unit_price": "1.00"}]}, "t1")
eq(err["code"], "AR_VALIDATION", "no issue_date → AR_VALIDATION")
# bad line_item qty/unit_price → warnings (not hard fail)
report, _w, err = c._validate_payload({"customer_ref": "C", "issue_date": "2026-07-07",
        "line_items": [{"item_ref": "X", "description": "d", "qty": "0", "unit_price": "0"}]},
        "t1")
falsy(err, "qty<=0 / price<=0 not a hard fail")
truthy(report["valid"], "bad qty/price → still valid=True")
truthy(any(w["rule_id"] == "ig.line_item_qty" for w in report["warnings"]),
       "qty<=0 warning recorded")
truthy(any(w["rule_id"] == "ig.line_item_unit_price" for w in report["warnings"]),
       "unit_price<=0 warning recorded")
# bad currency → warning
report, _w, err = c._validate_payload({"customer_ref": "C", "issue_date": "2026-07-07",
        "currency": "xyz", "line_items": [{"item_ref": "X", "description": "d",
        "qty": "1.00", "unit_price": "1.00"}]}, "t1")
falsy(err, "bad currency not a hard fail")
truthy(any(w["path"] == "currency" for w in report["warnings"]), "bad currency warning")
# totals inconsistency → warning
report, _w, err = c._validate_payload({"customer_ref": "C", "issue_date": "2026-07-07",
        "line_items": [{"item_ref": "X", "description": "d", "qty": "1.00",
                        "unit_price": "100.00"}],
        "totals": {"subtotal": "100.00", "tax": "0.00", "discounts": "0.00",
                   "total": "999.00"}}, "t1")
falsy(err, "totals inconsistency not a hard fail")
truthy(any(w["rule_id"] == "ig.totals_consistency" for w in report["warnings"]),
       "totals inconsistency warning")

# --------------------------------------------------------------------------- #
# [4] _classify_exceptions
# --------------------------------------------------------------------------- #
print("[4] _classify_exceptions")
rep = c._build_validation_report(True, [],
        [{"path": "line_items[0].qty", "code": "AR_VALIDATION_AMOUNT",
          "message": "qty must be > 0", "rule_id": "ig.line_item_qty"}], "t1")
exc = c._classify_exceptions(rep, "t1")
truthy(any(it["path"] == "line_items[0].qty" for it in exc["warnings"]),
       "exception report carries the failing-line warning")
rep_clean = c._build_validation_report(True, [], [], "t1")
exc_clean = c._classify_exceptions(rep_clean, "t1")
truthy(exc_clean["valid"], "clean input → exception report valid=True")

# --------------------------------------------------------------------------- #
# [5] _build_invoice
# --------------------------------------------------------------------------- #
print("[5] _build_invoice")
inv = c._build_invoice(GOOD_PAYLOAD, "t1", "cosmic-vikings")
for k in ("invoice_id", "invoice_number", "customer_ref", "tenant", "issue_date",
          "due_date", "line_items", "subtotal", "total", "currency", "status",
          "balance_due", "contract_version"):
    truthy(inv.get(k), f"invoice has {k}")
eq(inv["status"], "draft", "status draft")
eq(inv["subtotal"], EXPECTED_SUBTOTAL, "subtotal = Σ line amounts")
eq(inv["total"], EXPECTED_TOTAL, "total = subtotal + tax - discounts")
eq(inv["balance_due"], EXPECTED_TOTAL, "balance_due = total")
eq(inv["due_date"], EXPECTED_DUE, "due_date = issue + 30")
eq(inv["currency"], "SAR", "currency passed through")
eq(inv["po_number"], "PO-99", "po_number passed through (no PII)")
eq(inv["salesperson_ref"], "SP-1", "salesperson_ref passed through")
eq(inv["notes"], "v1 draft", "notes passed through")
# deterministic ids shaped IG-<customer>-<8hex>
expected_seed = f"invoice-gen:t1:CUST-42:2026-07-07"
expected_u = uuid.uuid5(uuid.NAMESPACE_URL, expected_seed)
eq(inv["invoice_id"], str(expected_u), "invoice_id deterministic uuid5")
eq(inv["invoice_number"], f"IG-CUST-42-{expected_u.hex[:8].upper()}",
   "invoice_number shaped IG-<customer>-<8hex>")
# same inputs → same ids (deterministic)
inv2 = c._build_invoice(GOOD_PAYLOAD, "t1", "cosmic-vikings")
eq(inv["invoice_id"], inv2["invoice_id"], "invoice_id reproducible")
# different trace → different ids
inv3 = c._build_invoice(GOOD_PAYLOAD, "t2", "cosmic-vikings")
falsy(inv["invoice_id"] == inv3["invoice_id"], "different trace → different invoice_id")
# line items: line_id uuid5, 2dp amounts
eq(len(inv["line_items"]), 2, "2 line items")
eq(inv["line_items"][0]["amount"], "1500.00", "line 0 amount = qty*price")
eq(inv["line_items"][1]["amount"], "1000.00", "line 1 amount = qty*price")
truthy(all(_re.match(r"^-?\d+\.\d{2}$", li["qty"]) for li in inv["line_items"]),
       "line qty 2dp")
truthy(all(_re.match(r"^-?\d+\.\d{2}$", li["amount"]) for li in inv["line_items"]),
       "line amount 2dp")
# _validate_invoice guard passes on the built invoice
errs = c._validate_invoice(inv)
eq(errs, [], "built invoice passes _validate_invoice")
# a truly malformed currency (not 3 alpha chars even after upper-casing) → SAR
inv_sar = c._build_invoice({"customer_ref": "C", "issue_date": "2026-07-07",
        "currency": "12 USD", "line_items": [{"item_ref": "X", "description": "d",
        "qty": "1.00", "unit_price": "1.00"}]}, "t1", "cosmic-vikings")
eq(inv_sar["currency"], "SAR", "malformed currency defaulted to SAR")
# lowercase 3-letter currency is upper-cased + passes through (not defaulted)
inv_norm = c._build_invoice({"customer_ref": "C", "issue_date": "2026-07-07",
        "currency": "sar", "line_items": [{"item_ref": "X", "description": "d",
        "qty": "1.00", "unit_price": "1.00"}]}, "t1", "cosmic-vikings")
eq(inv_norm["currency"], "SAR", "lowercase sar normalised to SAR")

# --------------------------------------------------------------------------- #
# [6] _build_journal_entry
# --------------------------------------------------------------------------- #
print("[6] _build_journal_entry")
je = c._build_journal_entry(inv, "t1")
eq(je["status"], "draft", "journal entry status draft (no POST — §1)")
eq(je["je_date"], inv["issue_date"], "je_date = issue_date")
eq(je["invoice_ref"], inv["invoice_number"], "invoice_ref")
eq(je["currency"], inv["currency"], "currency")
# balanced: total_debit == total_credit
eq(je["balanced"], True, "journal entry balanced")
eq(je["total_debit"], je["total_credit"], "total_debit == total_credit")
# debit AR = total, credit Revenue = subtotal, credit TaxPayable = tax, debit Discounts = discounts
lines = {ln["account"]: ln for ln in je["lines"]}
eq(lines["AR"]["debit"], EXPECTED_TOTAL, "debit AR = total")
eq(lines["Revenue"]["credit"], EXPECTED_SUBTOTAL, "credit Revenue = subtotal")
eq(lines["TaxPayable"]["credit"], "75.00", "credit TaxPayable = tax")
eq(lines["Discounts"]["debit"], "100.00", "debit Discounts = discounts")
# deterministic entry_id
expected_je = uuid.uuid5(uuid.NAMESPACE_URL,
                         f"invoice-gen-je:t1:{inv['invoice_id']}")
eq(je["entry_id"], str(expected_je), "entry_id deterministic uuid5")

# --------------------------------------------------------------------------- #
# [7] _build_customer_statement
# --------------------------------------------------------------------------- #
print("[7] _build_customer_statement")
cs = c._build_customer_statement(inv, "t1")
eq(cs["opening_balance"], "0.00", "opening_balance 0.00 (v1: no prior AR history)")
eq(cs["closing_balance"], EXPECTED_TOTAL, "closing_balance = total")
eq(cs["payments"], [], "payments [] (v1: none)")
eq(len(cs["invoices"]), 1, "one invoice listed")
eq(cs["invoices"][0]["invoice_number"], inv["invoice_number"], "invoice listed")
eq(cs["aging"]["current"], EXPECTED_TOTAL, "aging current = total")
eq(cs["aging"]["overdue"], "0.00", "aging overdue 0.00 (just-issued draft)")

# --------------------------------------------------------------------------- #
# [8] _build_zoho_upload
# --------------------------------------------------------------------------- #
print("[8] _build_zoho_upload")
zu = c._build_zoho_upload(inv, "t1")
eq(zu["format"], "zoho-books-invoice-import", "format zoho-books-invoice-import")
eq(zu["count"], 1, "count 1 (one invoice)")
eq(len(zu["rows"]), 1, "one row")
row = zu["rows"][0]
eq(row["customer_ref"], "CUST-42", "row customer_ref (id, no PII)")
eq(row["invoice_number"], inv["invoice_number"], "row invoice_number")
eq(row["date"], inv["issue_date"], "row date = issue_date")
eq(row["total"], EXPECTED_TOTAL, "row total")
eq(row["discount_total"], "100.00", "row discount_total = discounts")
eq(len(row["item_details"]), 2, "row item_details = 2 lines")
eq(row["item_details"][0]["item_ref"], "ITEM-A", "item_details item_ref")
eq(row["item_details"][0]["rate"], "150.00", "item_details rate = unit_price")

# --------------------------------------------------------------------------- #
# [9] _build_metadata
# --------------------------------------------------------------------------- #
print("[9] _build_metadata")
md = c._build_metadata(inv, "t1", c.FLOW_ID)
eq(md["invoice_id"], inv["invoice_id"], "metadata invoice_id")
eq(md["flow_id"], c.FLOW_ID, "metadata flow_id")
eq(md["line_item_count"], 2, "line_item_count = 2")
eq(md["source_refs"], ["build_invoice"], "source_refs")
eq(md["status"], "draft", "metadata status")
# content_hash deterministic
md2 = c._build_metadata(inv, "t1", c.FLOW_ID)
eq(md["content_hash"], md2["content_hash"], "content_hash deterministic")
# different invoice → different hash
md_other = c._build_metadata(inv3, "t1", c.FLOW_ID)
falsy(md["content_hash"] == md_other["content_hash"],
      "different invoice → different content_hash")

# --------------------------------------------------------------------------- #
# [10] _build_pdf_spec
# --------------------------------------------------------------------------- #
print("[10] _build_pdf_spec")
pdf = c._build_pdf_spec(inv, c.LAYOUT, "t1")
eq(pdf["format"], "invoice-pdf", "format invoice-pdf")
eq(pdf["render_ready"], True, "render_ready true (not a binary — build-phase)")
eq(pdf["data_ref"], inv["invoice_id"], "data_ref = invoice_id")
eq(pdf["page"]["size"], "A4", "page size A4")
names = [s["name"] for s in pdf["sections"]]
truthy("line_items_table" in names, "sections include line_items_table")
truthy("totals" in names, "sections include totals")
eq(pdf["layout"], c.LAYOUT, "layout carried through")

# --------------------------------------------------------------------------- #
# [11] _build_excel_spec
# --------------------------------------------------------------------------- #
print("[11] _build_excel_spec")
xls = c._build_excel_spec(inv, "t1")
eq(xls["format"], "invoice-excel", "format invoice-excel")
eq(xls["render_ready"], True, "render_ready true (not a binary — build-phase)")
eq(xls["data_ref"], inv["invoice_id"], "data_ref = invoice_id")
sheet_names = {s["name"] for s in xls["sheets"]}
truthy(sheet_names == {"Invoice", "Line Items"}, "two sheets: Invoice + Line Items")
inv_sheet = next(s for s in xls["sheets"] if s["name"] == "Invoice")
eq(len(inv_sheet["rows"]), 1, "Invoice sheet 1 row")
eq(inv_sheet["rows"][0]["total"], EXPECTED_TOTAL, "Invoice sheet total")
li_sheet = next(s for s in xls["sheets"] if s["name"] == "Line Items")
eq(len(li_sheet["rows"]), 2, "Line Items sheet 2 rows")

# --------------------------------------------------------------------------- #
# [12] build_workflow_state
# --------------------------------------------------------------------------- #
print("[12] build_workflow_state")
ws = c.build_workflow_state("t1", c.FLOW_ID, "cosmic-vikings",
                            ["ref1"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
eq(ws["intent"], c.FLOW_ID, "intent ar_invoice_generation")
eq(ws["status"], "completed", "status completed")
eq(ws["matched_amount"], "0.00", "no money moved — matched 0.00")
eq(ws["outstanding_balance"], "0.00", "outstanding 0.00")
eq(ws["posted_total"], "0.00", "posted 0.00")
eq(ws["idempotency_keys"], {}, "no POST → empty idempotency_keys")
eq(ws["tool_call_ref"], f"t1:{c.FLOW_ID}:0", "tool_call_ref")
eq(ws["audit_refs"], ["ref1"], "audit_refs reflected")

# --------------------------------------------------------------------------- #
# [13] _audit_ref + _record_checkpoint
# --------------------------------------------------------------------------- #
print("[13] audit ref + checkpoints")
ref1 = c._audit_ref("t1", "invoice")
ref2 = c._audit_ref("t1", "invoice")
eq(ref1, ref2, "audit ref deterministic for same trace+label")
ref3 = c._audit_ref("t1", "journal_entry")
falsy(ref1 == ref3, "different label → different ref")
st = c.InvoiceGenerationState(trace_id="t1", flow_id=c.FLOW_ID,
                              tenant="cosmic-vikings")
audit_refs, checkpoints = c._record_checkpoint(st, "invoice")
truthy(audit_refs[-1] == ref1, "checkpoint appends the labeled ref")
eq(checkpoints["invoice"], ref1, "checkpoints map keyed by label")
audit_refs2, checkpoints2 = c._record_checkpoint(
    c.InvoiceGenerationState(trace_id="t1", flow_id=c.FLOW_ID,
                             tenant="cosmic-vikings", audit_refs=audit_refs,
                             checkpoints=checkpoints), "journal_entry")
truthy(len(audit_refs2) == 2, "second checkpoint appends a new ref")
truthy(set(checkpoints2.keys()) == {"invoice", "journal_entry"},
       "checkpoints map accumulates labels")

# --------------------------------------------------------------------------- #
# [14] end-to-end via run()
# --------------------------------------------------------------------------- #
print("[14] end-to-end run()")
env = _run(json.dumps(GOOD_PAYLOAD))
eq(env["status"], "ok", "good payload → AR_OK")
eq(env["code"], "AR_OK", "code AR_OK")
eq(env["data"]["invoice"]["total"], EXPECTED_TOTAL, "e2e invoice total")
eq(env["data"]["invoice"]["due_date"], EXPECTED_DUE, "e2e due_date")
eq(env["data"]["invoice"]["status"], "draft", "e2e invoice status draft")
# 8 artifacts present
for k in ("invoice", "journal_entry", "customer_statement", "zoho_upload",
          "invoice_metadata", "invoice_pdf", "invoice_excel", "workflow_state"):
    truthy(env["data"].get(k), f"e2e data has {k}")
eq(env["data"]["artifact_count"], 8, "artifact_count = 8")
eq(env["data"]["line_item_count"], 2, "line_item_count = 2")
# 8 checkpoints: invoice, journal_entry, customer_statement, zoho_upload,
# invoice_metadata, invoice_pdf, invoice_excel, ar_invoice_generation
eq(len(env["data"]["checkpoints"]), 8, "8 checkpoints (7 build steps + aggregate)")
truthy(set(env["data"]["checkpoints"].keys()) ==
       {"invoice", "journal_entry", "customer_statement", "zoho_upload",
        "invoice_metadata", "invoice_pdf", "invoice_excel", c.FLOW_ID},
       "checkpoint labels")
eq(len(env["data"]["audit_refs"]), 8, "8 audit refs")
# journal entry balanced end-to-end
eq(env["data"]["journal_entry"]["balanced"], True, "e2e journal entry balanced")
# pdf/excel render-ready
eq(env["data"]["invoice_pdf"]["render_ready"], True, "e2e pdf render_ready")
eq(env["data"]["invoice_excel"]["render_ready"], True, "e2e excel render_ready")
eq(env["data"]["workflow_state"]["intent"], c.FLOW_ID, "e2e workflow_state intent")
# malformed JSON → AR_VALIDATION (ingest short-circuits)
env = _run("not json")
eq(env["status"], "error", "malformed JSON → error")
eq(env["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
# no line_items → AR_VALIDATION
env = _run(json.dumps({"customer_ref": "C", "issue_date": "2026-07-07"}))
eq(env["code"], "AR_VALIDATION", "no line_items → AR_VALIDATION")
# no customer_ref → AR_VALIDATION
env = _run(json.dumps({"issue_date": "2026-07-07",
        "line_items": [{"item_ref": "X", "description": "d", "qty": "1.00",
                        "unit_price": "1.00"}]}))
eq(env["code"], "AR_VALIDATION", "no customer_ref → AR_VALIDATION")
# missing optional fields → still AR_OK
minimal = {"customer_ref": "C", "issue_date": "2026-07-07",
           "line_items": [{"item_ref": "X", "description": "d",
                           "qty": "1.00", "unit_price": "10.00"}]}
env = _run(json.dumps(minimal))
eq(env["status"], "ok", "minimal payload (no tax/discounts/po) → AR_OK")
eq(env["data"]["invoice"]["total"], "10.00", "minimal total = subtotal (no tax/discounts)")
falsy("po_number" in env["data"]["invoice"], "minimal → no po_number key")

# --------------------------------------------------------------------------- #
# [15] envelope shape
# --------------------------------------------------------------------------- #
print("[15] envelope shape")
env = _run(json.dumps(GOOD_PAYLOAD))
for k in ("invoice", "journal_entry", "customer_statement", "zoho_upload",
          "invoice_metadata", "invoice_pdf", "invoice_excel", "validation_report",
          "exception_report", "workflow_state", "audit_refs", "checkpoints",
          "artifact_count", "line_item_count", "flow_id", "tenant", "started_at",
          "ended_at", "contract_version"):
    truthy(k in env["data"], f"data has {k}")
eq(env["data"]["flow_id"], c.FLOW_ID, "flow_id in envelope data")
eq(env["data"]["contract_version"], c.CONTRACT_VERSION, "contract_version")
truthy(env["trace_id"], "envelope has a trace_id")
eq(env["data"]["exception_report"]["contract_name"], "InvoiceGenerationInputs",
   "exception_report is a ValidationResult")

# --------------------------------------------------------------------------- #
# [16] run() never raises (§5/§9)
# --------------------------------------------------------------------------- #
print("[16] run() never raises")
comp = c.InvoiceGenerationFlowComponent()
comp.user_input = json.dumps(GOOD_PAYLOAD)
comp.layout = "not valid json"  # layout parse falls back to LAYOUT
comp.model_name = "glm-5.2:cloud"
comp.session_id = "s1"
env = json.loads(comp.run().text)
eq(env["status"], "ok", "bad layout JSON falls back to default layout → AR_OK")
# truly broken input still returns an envelope, never raises
env = json.loads(c.InvoiceGenerationFlowComponent().run().text)
truthy(env["status"] in ("ok", "error"), "missing user_input still returns an envelope")

# --------------------------------------------------------------------------- #
# [17] LAYOUT_JSON constant sanity
# --------------------------------------------------------------------------- #
print("[17] LAYOUT_JSON")
eq(c.LAYOUT_JSON, json.dumps(c.LAYOUT, indent=2), "LAYOUT_JSON matches LAYOUT")
eq(c.LAYOUT["page"]["size"], "A4", "default page A4")
truthy("line_items_table" in c.LAYOUT["sections"], "layout sections include line_items_table")
eq(c.ARTIFACT_KEYS, ("invoice", "journal_entry", "customer_statement", "zoho_upload",
                     "invoice_metadata", "invoice_pdf", "invoice_excel", "workflow_state"),
   "ARTIFACT_KEYS = 8 artifacts")

print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)