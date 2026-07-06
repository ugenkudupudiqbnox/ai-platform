#!/usr/bin/env python3
"""file_intake_selftest — offline stdlib-only tests for the File Intake Flow's
pure functions.

Covers (constitution §4/§8/§9/§10/§11/§14/§15): report-type detection by
extension, file-ref normalization, deterministic metadata extraction,
DocumentManifest assembly + totals cross-check, per-document + manifest
validation (reused from cosmic_common), §14 envelope shape, §10 retry
classification (transient vs hard), and the §4 fail-safe (AR_UNCERTAIN on low
classification confidence / unknown extension). No network, no LangFlow, no
Docker, no openpyxl/pdfplumber — `python3 file_intake_selftest.py` runs
anywhere. Mirrors adapter_selftest's harness (CLAUDE.md self-test convention):
PASS/FAIL counts, exits non-zero on any failure, so `make test` (via
scripts/file-intake.selftest.sh) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/file_intake_selftest.py
"""
import os
import sys
import types
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so file_intake imports without the in-image venv.
#  (The host has neither; the container has both. Compile/import must not
#  require them for pure-function testing.)
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

# Add both bundle roots so `components.ar_common.*` and `components.cosmic_common.*`
# resolve (mirrors the in-image pip-installed editable bundles). Each bundle root
# contains a `components/` package dir; putting the root on sys.path makes
# `components.ar_common.*` / `components.cosmic_common.*` importable.
# HERE = .../ar_common/components/ar_common  → bundle root is 2 levels up.
_AR_BUNDLE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
_COSMIC_BUNDLE_ROOT = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "cosmic_common"))
sys.path.insert(0, _AR_BUNDLE_ROOT)
sys.path.insert(0, _COSMIC_BUNDLE_ROOT)

import components.ar_common.file_intake as fi  # noqa: E402
from components.cosmic_common.validation_engine import (  # noqa: E402
    validate_document, validate_document_manifest,
)

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
# [1] detect_type — report-type identification by extension (§4)
# --------------------------------------------------------------------------- #
print("[1] detect_type")
eq(fi.detect_type("inv.xlsx"), "excel", "xlsx → excel")
eq(fi.detect_type("inv.XLSX"), "excel", "case-insensitive ext")
eq(fi.detect_type("inv.xls"), "excel", "xls → excel")
eq(fi.detect_type("inv.xlsm"), "excel", "xlsm → excel")
eq(fi.detect_type("data.csv"), "csv", "csv → csv")
eq(fi.detect_type("data.tsv"), "csv", "tsv → csv")
eq(fi.detect_type("scan.pdf"), "pdf", "pdf → pdf")
eq(fi.detect_type("note.docx"), "unknown", "docx → unknown")
eq(fi.detect_type("noext"), "unknown", "no extension → unknown")
eq(fi.detect_type(""), "unknown", "empty → unknown")

# --------------------------------------------------------------------------- #
# [2] _normalize_file — canvas File-node ref coercion
# --------------------------------------------------------------------------- #
print("[2] _normalize_file")
eq(fi._normalize_file("/tmp/x/inv.csv"),
   {"name": "inv.csv", "path": "/tmp/x/inv.csv"}, "bare string → {name,path}")
eq(fi._normalize_file({"file_path": "/a/b.csv", "file_name": "b.csv"}),
   {"name": "b.csv", "path": "/a/b.csv"}, "dict with file_path/file_name")
eq(fi._normalize_file({"path": "/c/d.xlsx"}),
   {"name": "d.xlsx", "path": "/c/d.xlsx"}, "dict with path → basename name")


class _DataObj:
    def __init__(self, d):
        self.data = d


eq(fi._normalize_file(_DataObj({"file_path": "/e/f.pdf"})),
   {"name": "f.pdf", "path": "/e/f.pdf"}, "Data obj with .data.file_path")
eq(fi._normalize_file(None), {"name": "", "path": ""}, "None → empty")
eq(fi._normalize_file({}), {"name": "", "path": ""}, "empty dict → empty")
eq(fi._normalize_file(_DataObj({})), {"name": "", "path": ""},
   "Data obj with empty data → empty")

# --------------------------------------------------------------------------- #
# [3] _extract_doc_fields — deterministic metadata extraction (§16 id-only)
# --------------------------------------------------------------------------- #
print("[3] _extract_doc_fields")
rows = {"rows": [
    {"invoice_number": "INV-1001", "customer_id": "C-42", "amount": "1234.5",
     "currency": "SAR", "date": "2026-07-01", "status": "open"}]}
doc = fi._extract_doc_fields({"name": "inv.csv", "content": rows}, "invoice")
eq(doc["doc_id"], "zoho:INV-1001", "doc_id = source:source_ref")
eq(doc["source_ref"], "INV-1001", "source_ref from invoice_number")
eq(doc["customer_ref"], "C-42", "customer_ref id-only (§16)")
eq(doc["amount"], "1234.50", "amount normalised to 2dp")
eq(doc["currency"], "SAR", "currency from column")
eq(doc["posted_at"], "2026-07-01T00:00:00Z", "date → ISO-8601 UTC midnight")
eq(doc["source"], "zoho", "default source zoho")
eq(doc["status"], "open", "status from column")
truthy(doc["fetched_at"].endswith("Z"), "fetched_at is ISO-8601 UTC")

# foodics detection via keyword
rows_foodics = {"rows": [{"text": "Foodics POS receipt", "amount": "10.00"}]}
doc2 = fi._extract_doc_fields({"name": "r.csv", "content": rows_foodics}, "receipt")
eq(doc2["source"], "foodics", "foodics keyword → source foodics")

# defaults when columns absent
rows_empty = {"rows": [{"x": "y"}]}
doc3 = fi._extract_doc_fields({"name": "file.xlsx", "content": rows_empty}, "credit_note")
eq(doc3["amount"], "0.00", "no amount column → 0.00")
eq(doc3["currency"], "USD", "no currency column → USD default")
eq(doc3["customer_ref"], "CUST-UNKNOWN", "no customer → CUST-UNKNOWN")
eq(doc3["source_ref"], "file", "no ref column → filename stem")
eq(doc3["doc_id"], "zoho:file", "doc_id defaults to source:stem")
eq(doc3["status"], "open", "no status → open default")

# thousands separators + currency prefix in amount
rows_amt = {"rows": [{"amount": "SAR 1,234.50"}]}
doc4 = fi._extract_doc_fields({"name": "a.csv", "content": rows_amt}, "invoice")
eq(doc4["amount"], "1234.50", "amount strips thousands sep + currency prefix")

# bad currency falls back to USD
rows_badcur = {"rows": [{"amount": "1.00", "currency": "riyals"}]}
doc5 = fi._extract_doc_fields({"name": "a.csv", "content": rows_badcur}, "invoice")
eq(doc5["currency"], "USD", "non-ISO currency → USD fallback")

# --------------------------------------------------------------------------- #
# [4] build_manifest — DocumentManifest assembly + totals (§8)
# --------------------------------------------------------------------------- #
print("[4] build_manifest")
d1 = {"doc_id": "zoho:A", "doc_type": "invoice", "source": "zoho",
      "source_ref": "A", "customer_ref": "C1", "amount": "100.00",
      "currency": "SAR", "posted_at": "2026-07-01T00:00:00Z", "status": "open",
      "fetched_at": "2026-07-06T00:00:00Z"}
d2 = {"doc_id": "foodics:B", "doc_type": "receipt", "source": "foodics",
      "source_ref": "B", "customer_ref": "C2", "amount": "-25.50",
      "currency": "SAR", "posted_at": "2026-07-03T00:00:00Z", "status": "paid",
      "fetched_at": "2026-07-06T00:00:00Z"}
man = fi.build_manifest([d1, d2], "trace-1", "cosmic-vikings")
eq(man["totals"]["count"], 2, "totals.count = number of docs")
eq(man["totals"]["sum"], "74.50", "totals.sum = Σ amounts (100.00 + -25.50)")
eq(sorted(man["source_systems"]), ["foodics", "zoho"], "source_systems distinct")
eq(man["period"], {"start": "2026-07-01", "end": "2026-07-03"},
   "period = min/max posted_at date")
eq(man["contract_version"], "1.0.0", "contract_version pinned")
eq(man["trace_id"], "trace-1", "trace_id threaded through")
eq(man["tenant"], "cosmic-vikings", "tenant threaded through")
truthy(man["generated_at"].endswith("Z"), "generated_at is ISO-8601 UTC")
# manifest_id is a uuid
import re as _re
truthy(bool(_re.match(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    man["manifest_id"])), "manifest_id is a uuid")
# empty documents → no period
man_empty = fi.build_manifest([], "t", "tenant")
eq(man_empty["totals"], {"count": 0, "sum": "0.00"}, "empty → count 0 sum 0.00")
falsy("period" in man_empty, "empty → no period key")

# --------------------------------------------------------------------------- #
# [5] validation — per-document + full manifest (reused from cosmic_common, §15)
# --------------------------------------------------------------------------- #
print("[5] validate_document / validate_document_manifest")
errs, amts = validate_document(d1)
eq(errs, [], "valid document → no errors")
eq(amts, [Decimal("100.00")], "returns parsed amount for cross-check")

# missing required field
bad_doc = {"doc_id": "", "doc_type": "invoice", "source": "zoho",
           "source_ref": "A", "customer_ref": "C1", "amount": "100.00",
           "currency": "SAR", "posted_at": "2026-07-01T00:00:00Z",
           "status": "open", "fetched_at": "2026-07-06T00:00:00Z"}
b_errs, _ = validate_document(bad_doc)
truthy(any("doc_id" in e for e in b_errs), "empty doc_id flagged")

# bad amount format + bad currency + bad doc_type
ugly = {"doc_id": "x", "doc_type": "statement", "source": "zoho",
        "source_ref": "A", "customer_ref": "C1", "amount": "100.5",
        "currency": "sar", "posted_at": "2026-07-01T00:00:00Z",
        "status": "open", "fetched_at": "2026-07-06T00:00:00Z"}
u_errs, _ = validate_document(ugly)
truthy(any("doc_type" in e for e in u_errs), "bad doc_type (statement) flagged")
truthy(any("amount" in e for e in u_errs), "amount 100.5 (not 2dp) flagged")
truthy(any("currency" in e for e in u_errs), "currency 'sar' (lowercase) flagged")

# full manifest: valid passes; totals mismatch fails
m_valid = {
    "manifest_id": "12345678-1234-1234-1234-123456789012",
    "trace_id": "t", "tenant": "cv", "documents": [d1, d2],
    "totals": {"count": 2, "sum": "74.50"},
    "source_systems": ["foodics", "zoho"], "contract_version": "1.0.0",
    "generated_at": "2026-07-06T00:00:00Z"}
eq(validate_document_manifest(m_valid), [], "valid manifest → no errors")

m_badsum = {**m_valid, "totals": {"count": 2, "sum": "99.99"}}
truthy(any("cross-check" in e for e in validate_document_manifest(m_badsum)),
       "totals.sum cross-check mismatch flagged")

m_badcount = {**m_valid, "totals": {"count": 9, "sum": "74.50"}}
truthy(any("count" in e for e in validate_document_manifest(m_badcount)),
      "totals.count cross-check mismatch flagged")

m_extraprop = {**m_valid, "unexpected_key": "x"}
truthy(any("unexpected property" in e for e in validate_document_manifest(m_extraprop)),
       "additionalProperties:false flags unknown root key")

# --------------------------------------------------------------------------- #
# [6] _envelope shape (§14)
# --------------------------------------------------------------------------- #
print("[6] _envelope")
env = fi._envelope("ok", "AR_OK", data={"x": 1}, trace_id="t")
eq(env, {"status": "ok", "code": "AR_OK", "data": {"x": 1}, "trace_id": "t"},
   "ok envelope shape")
env_e = fi._envelope("error", "AR_VALIDATION", error={"message": "bad"},
                     trace_id="t")
truthy(env_e["error"] == {"message": "bad"}, "error envelope carries error")
truthy(env_e["trace_id"] == "t", "error envelope carries trace_id")

# --------------------------------------------------------------------------- #
# [7] _is_transient — §10 retry classification
# --------------------------------------------------------------------------- #
print("[7] _is_transient")
truthy(fi._is_transient(TimeoutError("x")),
       "TimeoutError → transient")
truthy(fi._is_transient(_HTTPError(500)), "HTTP 500 → transient")
truthy(fi._is_transient(_HTTPError(408)), "HTTP 408 → transient")
truthy(fi._is_transient(_HTTPError(429)), "HTTP 429 → transient")
falsy(fi._is_transient(_HTTPError(404)), "HTTP 404 → hard (not transient)")
falsy(fi._is_transient(_HTTPError(401)), "HTTP 401 → hard")
falsy(fi._is_transient(ValueError("bad")), "ValueError → hard")

# --------------------------------------------------------------------------- #
# [8] _classify_doc — §4 fail-safe on low confidence
# --------------------------------------------------------------------------- #
print("[8] _classify_doc / AR_UNCERTAIN")
inv_content = {"rows": [{"text": "Tax Invoice Invoice number INV-1 amount 100.00"}]}
dt, conf = fi._classify_doc({"name": "i.csv", "content": inv_content}, 0.6)
eq(dt, "invoice", "invoice keyword → invoice")
truthy(conf >= 0.6, "invoice content clears MIN_CONFIDENCE")

# ambiguous / no keywords → unknown @ 0.0 → AR_UNCERTAIN
blank = {"rows": [{"x": "y"}]}
dt2, conf2 = fi._classify_doc({"name": "b.csv", "content": blank}, 0.6)
eq(dt2, "unknown", "no keywords → unknown")
falsy(conf2 >= 0.6, "no keywords below MIN_CONFIDENCE → AR_UNCERTAIN path")

# CSV/XLSX header convention: underscore-separated `invoice_number` /
# `invoice_date` + hyphenated `INV-1001` must still classify as invoice
# (the `\binvoice\b` / `\binv\s*#?\d` patterns alone miss these — see
# document_classifier RULES["invoice"]).
hdr = {"rows": [{"invoice_number": "INV-1001", "customer_id": "CUST-001",
                 "amount": "1000.00", "currency": "USD",
                 "invoice_date": "2026-06-01", "status": "open", "source": "zoho"}]}
dt3, conf3 = fi._classify_doc({"name": "invoice-1001.csv", "content": hdr}, 0.6)
eq(dt3, "invoice", "underscore header invoice_number → invoice")
truthy(conf3 >= 0.6, "underscore-header invoice clears MIN_CONFIDENCE")

# --------------------------------------------------------------------------- #
# [9] parse_envelope — reader/tool output parsing
# --------------------------------------------------------------------------- #
print("[9] parse_envelope")
eq(fi.parse_envelope('{"status":"ok","code":"AR_OK","data":{}}'),
   {"status": "ok", "code": "AR_OK", "data": {}}, "valid json dict")
eq(fi.parse_envelope("not json"), None, "non-json → None")
eq(fi.parse_envelope("[1,2,3]"), None, "json array → None")
eq(fi.parse_envelope(""), None, "empty → None")
eq(fi.parse_envelope(None), None, "None → None")

# --------------------------------------------------------------------------- #
# [10] _to_2dp / _sum_2dp / _parse_ts — numeric + timestamp coercion
# --------------------------------------------------------------------------- #
print("[10] numeric/timestamp coercion")
eq(fi._to_2dp("100.5"), "100.50", "100.5 → 100.50")
eq(fi._to_2dp("1,234.5"), "1234.50", "thousands sep stripped")
eq(fi._to_2dp("SAR 99.999"), "100.00", "prefix stripped, quantised to 2dp")
eq(fi._to_2dp(""), "0.00", "empty → 0.00")
eq(fi._to_2dp(None), "0.00", "None → 0.00")
eq(fi._sum_2dp(["100.00", "-25.50", "0.25"]), "74.75", "sum to 2dp")
eq(fi._sum_2dp([]), "0.00", "empty sum → 0.00")
eq(fi._parse_ts("2026-07-01"), "2026-07-01T00:00:00Z", "date → midnight UTC")
eq(fi._parse_ts("2026-07-01T12:30:45Z"), "2026-07-01T12:30:45Z",
   "full timestamp passthrough")
eq(fi._parse_ts("2026/07/01"), "2026-07-01T00:00:00Z", "slash date → normalised")
ts_now = fi._parse_ts("garbage")
truthy(ts_now.endswith("Z"), "garbage date → fallback to utc_now()")

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)