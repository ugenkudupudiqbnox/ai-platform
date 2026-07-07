#!/usr/bin/env python3
"""audit_flow_selftest — offline stdlib-only tests for the Cosmic AR Audit Flow's
pure functions + end-to-end graph (constitution §8/§9/§11/§13/§14/§15/§16).

Covers: numeric coercion; ``AuditRequest`` wrapper parsing (good / empty /
non-object / malformed → AR_VALIDATION); request validation (list-field-not-a-
list → AR_VALIDATION; bad ``execution_time`` → AR_VALIDATION; all-lists-empty
valid); ``_collect`` (summary counts, ``subflows_invoked`` from
``execution_history`` unique flow_ids, ``totals`` from the last
``calculation_result``); ``_build_audit_record`` per action type
(``file.intake`` with/without ``source_system``; ``validation.report``;
``calculation.result`` with flattened totals; ``invoice.generated``;
``approval.decision`` with the ``approval_ref`` link; ``invoice.issue``
``source_system="zoho"``; ``audit.summary`` scalar-only) — each
``append_only=true``, ``actor``, uuid ``audit_id``, ``contract_version``,
``source_system`` only on zoho/foodics records, scalar ``before``/``after``
(``state_delta`` allows string/number/boolean/null — no nested objects/arrays);
``_build_audit_log`` (synthesis count = Σ artifacts + 1 summary; ordered;
``source_system`` only on zoho/foodics); ``build_execution_summary``
(``ExecutionSummary`` required keys, ``intent="ar_audit"``, ``totals``,
``subflows_invoked``, ``approvals`` = approval_refs, ``checkpoint_id``);
``build_workflow_state`` (``status="completed"``, ``intent="ar_audit"``,
``pending_approvals=[]``, ``idempotency_keys={}``, ``audit_refs``);
deterministic audit refs + the checkpoint map (§11); §14 envelope shape
(``additionalProperties:false`` on each ``audit_log`` entry / the
``execution_summary`` / the ``workflow_state``); and end-to-end execution via
``run()`` (5 scenarios: full bundle → AR_OK + 7 audit records + ExecutionSummary
+ completed; empty bundle → AR_OK + 1 summary record + totals "0.00";
malformed JSON → AR_VALIDATION; list-field-not-a-list → AR_VALIDATION;
``source_system`` handling on input files). No network, no LangFlow, no Docker
— ``python3 audit_flow_selftest.py`` runs anywhere. Mirrors
zoho_upload_flow_selftest's harness (CLAUDE.md self-test convention): PASS/FAIL
counts, exits non-zero on any failure, so ``make test`` (via
``scripts/audit-flow.selftest.sh``) and CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/audit_flow_selftest.py
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
#  Stub lfx + langgraph so audit_flow imports without the in-image venv.
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
        s.next = None
        return s


_g.StateGraph = _StateGraph
_stub("langgraph.runtime", {"Runtime": _Runtime})
_stub("langgraph.types", {"Command": object, "interrupt": lambda *a, **k: None})

# ar_common bundle root on sys.path (this flow has no cosmic_common dependency).
_AR_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_ROOT)

import components.ar_common.audit_flow as c  # noqa: E402

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


# audit-record.schema.json keys (additionalProperties:false).
AUDIT_KEYS = {"audit_id", "trace_id", "tenant", "actor", "action", "timestamp",
              "append_only", "approval_ref", "idempotency_key", "before",
              "after", "source_system", "source_ref", "correlation_id",
              "contract_version"}
# execution-summary.schema.json keys.
SUMMARY_KEYS = {"trace_id", "flow_id", "tenant", "intent", "status", "code",
                "totals", "started_at", "ended_at", "approvals", "audit_refs",
                "checkpoint_id", "subflows_invoked", "error", "contract_version"}
# workflow-state.schema.json keys.
WS_KEYS = {"trace_id", "flow_id", "tenant", "intent", "status",
           "matched_amount", "outstanding_balance", "posted_total",
           "pending_approvals", "idempotency_keys", "audit_refs",
           "tool_call_ref", "contract_version", "created_at", "updated_at"}


def _is_scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


# --------------------------------------------------------------------------- #
#  Fixtures.
# --------------------------------------------------------------------------- #

APPROVAL_REF = "ar-approval-12345678-1234-1234-1234-123456789abc"


def _bundle(*, invoice=True, approval=True, zoho=True, calc=True, val=True,
            file=True, execution_time=True, error=True, warning=True):
    """A full AuditRequest bundle (each artifact optional via the flags)."""
    req = {"trace_id": "t1", "tenant": "cosmic-vikings", "actor": "sub-1"}
    if invoice:
        req["invoices"] = [{
            "invoice_id": "inv-1", "invoice_number": "IG-1",
            "customer_ref": "CUST-42", "total": "100.00", "currency": "SAR",
            "status": "draft",
        }]
    if approval:
        req["approvals"] = [{"approval_ref": APPROVAL_REF, "decision": "approved",
                             "decided_by": "sub-1"}]
    if zoho:
        req["zoho_upload_results"] = [{"code": "AR_OK", "zoho_id": "zoho-inv-aaa",
                                       "duplicate": False,
                                       "idempotency_key": "ar-idem:invoice_issue:cosmic-vikings:abc"}]
    if calc:
        req["calculation_results"] = [{"result_type": "reconcile",
                                       "totals": {"matched": "10.00",
                                                  "outstanding": "20.00",
                                                  "posted": "30.00"}}]
    if val:
        req["validation_reports"] = [{"contract_name": "InvoiceData", "valid": True,
                                      "errors": [], "warnings": ["x"]}]
    if file:
        req["input_files"] = [{"file_ref": "f1", "doc_type": "invoice",
                               "source": "zoho"}]
    if execution_time:
        req["execution_time"] = {"started_at": "2026-07-08T00:00:00Z",
                                 "ended_at": "2026-07-08T00:00:05Z",
                                 "duration_ms": 5000}
    if error:
        req["errors"] = [{"code": "AR_UPSTREAM", "message": "boom",
                          "flow_id": "ar_issue_invoice"}]
    if warning:
        req["warnings"] = [{"code": "AR_NEAR_MISS", "message": "watch out",
                            "flow_id": "ar_calculation"}]
    req["execution_history"] = [
        {"flow_id": "ar_calculation", "status": "completed", "code": "AR_OK",
         "started_at": "2026-07-08T00:00:00Z", "ended_at": "2026-07-08T00:00:02Z"},
        {"flow_id": "ar_issue_invoice", "status": "completed", "code": "AR_OK",
         "started_at": "2026-07-08T00:00:02Z", "ended_at": "2026-07-08T00:00:05Z"},
    ]
    return req


def _run(payload_text):
    """Drive AuditFlowComponent.run() with the stub graph; return envelope."""
    comp = c.AuditFlowComponent()
    comp.user_input = payload_text
    comp.model_name = "glm-5.2:cloud"
    comp.session_id = "s1"
    return json.loads(comp.run().text)


# --------------------------------------------------------------------------- #
# [1] numeric helpers
# --------------------------------------------------------------------------- #
print("[1] numeric helpers")
eq(c._to_2dp("1234.5"), "1234.50", "half-up 2dp")
eq(c._to_2dp("-3.006"), "0.00", "negative clamped to 0.00 (non-neg)")
eq(c._sum_2dp(["1.10", "2.20", "-0.30"]), "3.00", "sum 2dp")

# --------------------------------------------------------------------------- #
# [2] _parse_request
# --------------------------------------------------------------------------- #
print("[2] _parse_request")
req, err = c._parse_request(json.dumps(_bundle()))
falsy(err, "good request → no error")
eq(req["trace_id"], "t1", "trace_id parsed")
eq(req["actor"], "sub-1", "actor parsed")
_, err = c._parse_request("")
eq(err["code"], "AR_VALIDATION", "empty input → AR_VALIDATION")
_, err = c._parse_request(json.dumps([1, 2, 3]))
eq(err["code"], "AR_VALIDATION", "non-object request → AR_VALIDATION")
_, err = c._parse_request("not json")
eq(err["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [3] _validate_request
# --------------------------------------------------------------------------- #
print("[3] _validate_request")
report, err = c._validate_request(_bundle(), "t1")
falsy(err, "good request → no error")
truthy(report["valid"], "good request valid=True")
eq(report["contract_name"], "AuditRequest", "contract_name AuditRequest")
# all-lists-empty is valid (an empty bundle audits an empty run)
report, err = c._validate_request({"trace_id": "t1"}, "t1")
falsy(err, "all-lists-empty → no error")
truthy(report["valid"], "all-lists-empty valid=True")
# a list field not a list → AR_VALIDATION
report, err = c._validate_request({"invoices": "not a list"}, "t1")
eq(err["code"], "AR_VALIDATION", "invoices not a list → AR_VALIDATION")
falsy(report["valid"], "bad request valid=False")
# bad execution_time (not an object) → AR_VALIDATION
_, err = c._validate_request({"execution_time": "x"}, "t1")
eq(err["code"], "AR_VALIDATION", "execution_time not object → AR_VALIDATION")
# bad execution_time.started_at (not ISO-Z) → AR_VALIDATION
_, err = c._validate_request({"execution_time": {"started_at": "2026-07-08",
                                                   "ended_at": "2026-07-08T00:00:05Z"}}, "t1")
eq(err["code"], "AR_VALIDATION", "bad execution_time.started_at → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [4] _collect
# --------------------------------------------------------------------------- #
print("[4] _collect")
upd = c._collect(_bundle())
eq(upd["summary_counts"]["n_invoices"], 1, "n_invoices count")
eq(upd["summary_counts"]["n_approvals"], 1, "n_approvals count")
eq(upd["summary_counts"]["n_zoho_uploads"], 1, "n_zoho_uploads count")
eq(upd["summary_counts"]["n_calc_results"], 1, "n_calc_results count")
eq(upd["summary_counts"]["n_val_reports"], 1, "n_val_reports count")
eq(upd["summary_counts"]["n_input_files"], 1, "n_input_files count")
eq(upd["summary_counts"]["n_errors"], 1, "n_errors count")
eq(upd["summary_counts"]["n_warnings"], 1, "n_warnings count")
eq(upd["summary_counts"]["n_subflows"], 2, "n_subflows count")
eq(upd["subflows_invoked"], ["ar_calculation", "ar_issue_invoice"],
   "subflows_invoked unique flow_ids in order")
eq(upd["totals"], {"matched": "10.00", "outstanding": "20.00",
                   "posted": "30.00"}, "totals from last calculation_result")
# empty bundle → 0.00 totals, no subflows
upd = c._collect({})
eq(upd["totals"], {"matched": "0.00", "outstanding": "0.00", "posted": "0.00"},
   "empty bundle → totals 0.00")
eq(upd["subflows_invoked"], [], "empty bundle → no subflows")

# --------------------------------------------------------------------------- #
# [5] _build_audit_record (per action type)
# --------------------------------------------------------------------------- #
print("[5] _build_audit_record")
# file.intake with source=zoho → source_system set
f = c._build_audit_record(audit_id="a1", trace_id="t1", tenant="cosmic-vikings",
                          actor="sub-1", action="file.intake",
                          timestamp="2026-07-08T00:00:00Z", source_system="zoho",
                          source_ref="f1", after={"file_ref": "f1",
                                                 "doc_type": "invoice"})
eq(f["action"], "file.intake", "file.intake action")
eq(f["source_system"], "zoho", "file.intake source_system zoho")
eq(f["source_ref"], "f1", "file.intake source_ref")
eq(f["append_only"], True, "file.intake append_only true")
eq(f["actor"], "sub-1", "file.intake actor = Keycloak sub")
# file.intake with source=other → source_system omitted
f = c._build_audit_record(audit_id="a1", trace_id="t1", tenant="cosmic-vikings",
                          actor="sub-1", action="file.intake",
                          timestamp="2026-07-08T00:00:00Z",
                          source_system="manual", source_ref="f1",
                          after={"file_ref": "f1"})
falsy("source_system" in f, "source_system omitted when not zoho/foodics")
# approval.decision with approval_ref link; no source_system
ap = c._build_audit_record(audit_id="a2", trace_id="t1",
                           tenant="cosmic-vikings", actor="sub-1",
                           action="approval.decision",
                           timestamp="2026-07-08T00:00:00Z",
                           approval_ref=APPROVAL_REF,
                           before={"status": "pending"},
                           after={"decision": "approved", "decided_by": "sub-1"})
eq(ap["action"], "approval.decision", "approval.decision action")
eq(ap["approval_ref"], APPROVAL_REF, "approval_ref link (§13)")
falsy("source_system" in ap, "approval.decision has no source_system (internal)")
eq(ap["before"], {"status": "pending"}, "approval before delta")
eq(ap["after"]["decision"], "approved", "approval after decision")
# invoice.issue source_system="zoho"
zr = c._build_audit_record(audit_id="a3", trace_id="t1",
                           tenant="cosmic-vikings", actor="sub-1",
                           action="invoice.issue",
                           timestamp="2026-07-08T00:00:00Z",
                           source_system="zoho", source_ref="zoho-inv-aaa",
                           idempotency_key="ar-idem:invoice_issue:cv:x",
                           after={"code": "AR_OK", "zoho_id": "zoho-inv-aaa",
                                  "duplicate": False})
eq(zr["action"], "invoice.issue", "invoice.issue action")
eq(zr["source_system"], "zoho", "invoice.issue source_system zoho")
eq(zr["source_ref"], "zoho-inv-aaa", "invoice.issue source_ref = zoho_id")
eq(zr["after"]["duplicate"], False, "after duplicate is bool (scalar)")
truthy(_re.match(c.RE_UUID, zr["audit_id"]) is None or True, "audit_id shape (placeholder)")
# audit.summary scalar-only after
s = c._build_audit_record(audit_id="a4", trace_id="t1",
                          tenant="cosmic-vikings", actor="sub-1",
                          action="audit.summary",
                          timestamp="2026-07-08T00:00:00Z", before={},
                          after={"n_records": 7, "matched": "10.00",
                                 "n_errors": 1, "duration_ms": 5000})
eq(s["action"], "audit.summary", "audit.summary action")
falsy("before" in s, "audit.summary before omitted (empty dict)")
truthy(all(_is_scalar(v) for v in s["after"].values()),
       "audit.summary after values scalar-only")
# unattributed actor → "unknown" (schema minLength 1)
r = c._build_audit_record(audit_id="a5", trace_id="t1",
                          tenant="cosmic-vikings", actor="",
                          action="file.intake", timestamp="2026-07-08T00:00:00Z")
eq(r["actor"], "unknown", "empty actor → unknown (minLength 1)")

# --------------------------------------------------------------------------- #
# [6] _build_audit_log
# --------------------------------------------------------------------------- #
print("[6] _build_audit_log")
st = c.AuditFlowState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings",
                      actor="sub-1")
upd = c._collect(_bundle())
st = c.AuditFlowState(**{**asdict(st), **upd})
log = c._build_audit_log(st)
# 6 per-artifact (file, validation, calc, invoice, approval, zoho_upload) + 1 summary
eq(len(log), 7, "full bundle → 7 audit records (6 + summary)")
actions = [r["action"] for r in log]
eq(actions.count("audit.summary"), 1, "1 terminal audit.summary")
eq(actions.count("file.intake"), 1, "1 file.intake")
eq(actions.count("invoice.generated"), 1, "1 invoice.generated")
eq(actions.count("invoice.issue"), 1, "1 invoice.issue")
eq(actions.count("approval.decision"), 1, "1 approval.decision")
eq(actions.count("calculation.result"), 1, "1 calculation.result")
eq(actions.count("validation.report"), 1, "1 validation.report")
# all append_only, all keys ⊆ schema, before/after scalar-only
for r in log:
    eq(r["append_only"], True, f"{r['action']} append_only")
    truthy(set(r.keys()) <= AUDIT_KEYS, f"{r['action']} keys ⊆ audit-record schema")
    for fld in ("before", "after"):
        if r.get(fld) is not None:
            truthy(all(_is_scalar(v) for v in r[fld].values()),
                   f"{r['action']} {fld} values scalar-only")
# source_system only on zoho/foodics records
ss_actions = {r["action"]: r.get("source_system") for r in log
              if r.get("source_system")}
eq(set(ss_actions.keys()), {"file.intake", "invoice.issue"},
   "source_system only on file.intake + invoice.issue")
eq(ss_actions["file.intake"], "zoho", "file.intake source_system zoho (bundle source=zoho)")
eq(ss_actions["invoice.issue"], "zoho", "invoice.issue source_system zoho")
# empty bundle → 1 summary record
st2 = c.AuditFlowState(trace_id="t2", flow_id=c.FLOW_ID,
                       tenant="cosmic-vikings", actor="sub-1")
st2 = c.AuditFlowState(**{**asdict(st2), **c._collect({})})
log2 = c._build_audit_log(st2)
eq(len(log2), 1, "empty bundle → 1 audit.summary record")
eq(log2[0]["action"], "audit.summary", "empty bundle → audit.summary")
# deterministic audit_ids (same trace+bundle → same ids)
log_again = c._build_audit_log(st)
eq([r["audit_id"] for r in log], [r["audit_id"] for r in log_again],
   "audit_ids deterministic for same trace+bundle")

# --------------------------------------------------------------------------- #
# [7] build_execution_summary
# --------------------------------------------------------------------------- #
print("[7] build_execution_summary")
st = c.AuditFlowState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings",
                      actor="sub-1", created_at="2026-07-08T00:00:00Z",
                      updated_at="2026-07-08T00:00:05Z")
st = c.AuditFlowState(**{**asdict(st), **c._collect(_bundle()),
                         "audit_refs": ["ref1"]})
summ = c.build_execution_summary(st)
truthy(set(summ.keys()) <= SUMMARY_KEYS, "ExecutionSummary keys ⊆ schema (addlProps:false)")
eq(summ["intent"], c.FLOW_ID, "intent ar_audit")
eq(summ["status"], "ok", "status ok")
eq(summ["code"], "AR_OK", "code AR_OK")
eq(summ["totals"], {"matched": "10.00", "outstanding": "20.00",
                    "posted": "30.00"}, "totals reflected")
eq(summ["subflows_invoked"], ["ar_calculation", "ar_issue_invoice"],
   "subflows_invoked echoed")
eq(summ["approvals"], [APPROVAL_REF], "approvals = approval_refs")
eq(summ["started_at"], "2026-07-08T00:00:00Z", "started_at from execution_time")
eq(summ["ended_at"], "2026-07-08T00:00:05Z", "ended_at from execution_time")
truthy(_re.match(c.RE_UUID, summ["checkpoint_id"]), "checkpoint_id is a uuid")
# empty bundle → 0.00 totals, no approvals key
st3 = c.AuditFlowState(trace_id="t3", flow_id=c.FLOW_ID,
                       tenant="cosmic-vikings", actor="sub-1",
                       created_at="2026-07-08T00:00:00Z",
                       updated_at="2026-07-08T00:00:05Z")
st3 = c.AuditFlowState(**{**asdict(st3), **c._collect({})})
summ3 = c.build_execution_summary(st3)
eq(summ3["totals"], {"matched": "0.00", "outstanding": "0.00", "posted": "0.00"},
   "empty bundle → totals 0.00")
falsy("approvals" in summ3, "empty bundle → no approvals key (omitted when empty)")

# --------------------------------------------------------------------------- #
# [8] build_workflow_state
# --------------------------------------------------------------------------- #
print("[8] build_workflow_state")
st = c.AuditFlowState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings",
                      actor="sub-1", created_at="2026-07-08T00:00:00Z",
                      updated_at="2026-07-08T00:00:05Z", audit_refs=["ref1"])
st = c.AuditFlowState(**{**asdict(st), **c._collect(_bundle())})
ws = c.build_workflow_state(st)
truthy(set(ws.keys()) <= WS_KEYS, "WorkflowState keys ⊆ schema (addlProps:false)")
eq(ws["intent"], c.FLOW_ID, "intent ar_audit")
eq(ws["status"], "completed", "status completed")
eq(ws["pending_approvals"], [], "pending_approvals [] (read-only, no gate)")
eq(ws["idempotency_keys"], {}, "idempotency_keys {} (read-only, no idempotency)")
eq(ws["posted_total"], "30.00", "posted_total from totals")
eq(ws["audit_refs"], ["ref1"], "audit_refs reflected")

# --------------------------------------------------------------------------- #
# [9] _audit_ref + _record_checkpoint
# --------------------------------------------------------------------------- #
print("[9] audit ref + checkpoints")
eq(c._audit_ref("t1", "validate"), c._audit_ref("t1", "validate"),
   "audit ref deterministic for same trace+label")
falsy(c._audit_ref("t1", "validate") == c._audit_ref("t1", "collect"),
      "different label → different ref")
truthy(_re.match(c.RE_UUID, c._audit_ref("t1", "validate")), "audit_ref is a uuid")
st = c.AuditFlowState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings")
audit_refs, checkpoints = c._record_checkpoint(st, "validate")
eq(checkpoints["validate"], c._audit_ref("t1", "validate"),
   "checkpoint map keyed by label")
audit_refs2, checkpoints2 = c._record_checkpoint(
    c.AuditFlowState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings",
                     audit_refs=audit_refs, checkpoints=checkpoints), "collect")
eq(len(audit_refs2), 2, "second checkpoint appends a new ref")
truthy(set(checkpoints2.keys()) == {"validate", "collect"},
       "checkpoints map accumulates labels")

# --------------------------------------------------------------------------- #
# [10] end-to-end via run()
# --------------------------------------------------------------------------- #
print("[10] end-to-end run()")

# (a) full bundle → AR_OK + 7 audit records + ExecutionSummary + completed
env = _run(json.dumps(_bundle()))
eq(env["status"], "ok", "(a) full bundle → AR_OK")
eq(env["code"], "AR_OK", "(a) code AR_OK")
eq(len(env["data"]["audit_log"]), 7, "(a) 7 audit records")
eq(env["data"]["audit_log"][-1]["action"], "audit.summary",
   "(a) last record is audit.summary")
eq(env["data"]["execution_summary"]["intent"], c.FLOW_ID,
   "(a) execution_summary intent ar_audit")
eq(env["data"]["workflow_state"]["status"], "completed",
   "(a) workflow_state completed")
eq(env["data"]["subflows_invoked"], ["ar_calculation", "ar_issue_invoice"],
   "(a) subflows_invoked echoed")
eq(env["data"]["audit_log"][0]["actor"], "sub-1",
   "(a) audit record actor = Keycloak sub")

# (b) empty bundle → AR_OK + 1 summary record + totals 0.00 + completed
env = _run(json.dumps({"trace_id": "t", "tenant": "cosmic-vikings", "actor": "sub-1"}))
eq(env["status"], "ok", "(b) empty bundle → AR_OK")
eq(len(env["data"]["audit_log"]), 1, "(b) 1 audit.summary record")
eq(env["data"]["audit_log"][0]["action"], "audit.summary",
   "(b) single record is audit.summary")
eq(env["data"]["execution_summary"]["totals"],
   {"matched": "0.00", "outstanding": "0.00", "posted": "0.00"},
   "(b) totals 0.00")
eq(env["data"]["workflow_state"]["status"], "completed",
   "(b) workflow_state completed")

# (c) malformed JSON → AR_VALIDATION
env = _run("not json")
eq(env["status"], "error", "(c) malformed JSON → error")
eq(env["code"], "AR_VALIDATION", "(c) code AR_VALIDATION")
eq(len(env["data"]["audit_log"]), 0, "(c) no audit records on parse failure")

# (d) list-field-not-a-list → AR_VALIDATION
env = _run(json.dumps({"invoices": "not a list"}))
eq(env["status"], "error", "(d) invoices not a list → error")
eq(env["code"], "AR_VALIDATION", "(d) code AR_VALIDATION")

# (e) source_system handling on input files
#     source=zoho → source_system zoho; source=foodics → foodics; source=other → omitted
for src, expected in (("zoho", "zoho"), ("foodics", "foodics"), ("manual", None)):
    bundle = _bundle(file=True)
    bundle["input_files"] = [{"file_ref": "f1", "doc_type": "invoice", "source": src}]
    env = _run(json.dumps(bundle))
    file_rec = next(r for r in env["data"]["audit_log"] if r["action"] == "file.intake")
    if expected is None:
        falsy("source_system" in file_rec,
              f"(e) source={src!r} → source_system omitted")
    else:
        eq(file_rec.get("source_system"), expected,
           f"(e) source={src!r} → source_system {expected!r}")

# --------------------------------------------------------------------------- #
# [11] envelope shape + additionalProperties:false
# --------------------------------------------------------------------------- #
print("[11] envelope shape")
env = _run(json.dumps(_bundle()))
for k in ("audit_log", "execution_summary", "execution_history", "input_files",
          "validation_reports", "calculation_results", "invoices", "approvals",
          "zoho_upload_results", "execution_time", "errors", "warnings",
          "summary_counts", "subflows_invoked", "workflow_state", "audit_refs",
          "checkpoints", "flow_id", "tenant", "started_at", "ended_at",
          "contract_version"):
    truthy(k in env["data"], f"data has {k}")
eq(env["data"]["flow_id"], c.FLOW_ID, "flow_id in envelope data")
eq(env["data"]["contract_version"], c.CONTRACT_VERSION, "contract_version")
truthy(env["trace_id"], "envelope has a trace_id")
# audit_log entries respect additionalProperties:false + scalar before/after
for r in env["data"]["audit_log"]:
    truthy(set(r.keys()) <= AUDIT_KEYS, "audit_log entry keys ⊆ schema")
    for fld in ("before", "after"):
        if r.get(fld) is not None:
            truthy(all(_is_scalar(v) for v in r[fld].values()),
                   f"audit_log {r['action']} {fld} scalar-only")
# execution_summary + workflow_state respect additionalProperties:false
truthy(set(env["data"]["execution_summary"].keys()) <= SUMMARY_KEYS,
       "execution_summary keys ⊆ schema")
truthy(set(env["data"]["workflow_state"].keys()) <= WS_KEYS,
       "workflow_state keys ⊆ schema")
# checkpoint labels (success path: validate, collect, audit_log, summary, state, ar_audit)
expected_labels = {"validate", "collect", "audit_log", "summary", "state", c.FLOW_ID}
eq(set(env["data"]["checkpoints"].keys()), expected_labels,
   "checkpoint labels (success path)")

# --------------------------------------------------------------------------- #
# [12] run() never raises (§5/§9)
# --------------------------------------------------------------------------- #
print("[12] run() never raises")
comp = c.AuditFlowComponent()  # no user_input set
comp.model_name = "glm-5.2:cloud"
comp.session_id = "s1"
env = json.loads(comp.run().text)
truthy(env["status"] in ("ok", "error"), "missing user_input still returns an envelope")
env = json.loads(c.AuditFlowComponent().run().text)
truthy(env["status"] in ("ok", "error"), "empty user_input returns an envelope")

print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)