#!/usr/bin/env python3
"""zoho_upload_flow_selftest — offline stdlib-only tests for the Cosmic AR Zoho
Upload Flow's pure functions + end-to-end graph (constitution §1/§8/§9/§10/§11/
§13/§14/§15/§16/§19).

Covers: numeric coercion + §10 backoff delay (deterministic parity jitter);
``ZohoUploadRequest`` wrapper parsing (good / single-via-array / missing
``approval_ref`` → AR_FORBIDDEN / bad ``approval_ref`` pattern → AR_FORBIDDEN /
missing or empty ``invoices`` / non-object / malformed → AR_VALIDATION);
``InvoiceData`` validation (good / missing each mandatory field / bad 2dp money
/ bad currency / bad date / empty ``line_items`` / bad ``status`` / empty
``customer_ref`` → per-field errors); request validation (per-invoice error
map); deterministic ``idempotency_key`` (``ar-idem:invoice_issue:<tenant>:
<uuid5(invoice_id)>``); ``_upload_one`` over the stub transport (success →
AR_OK + zoho_id + attempts=1; duplicate → AR_DUPLICATE + duplicate=true + no
retry; transient-then-success → attempts=2; hard 4xx → AR_VALIDATION +
attempts=1 + no retry; auth 401 → AR_AUTH; all-transient-exhausted → attempts=3
+ AR_UPSTREAM); canonical ``ZohoUploadResult`` (required keys, operation
``invoice_issue``, code enum, idempotency_key pattern, attempted_at ISO-Z,
attempts int ≥1, no ``rolled_back``); ``AuditRecord`` (create
``invoice.issue`` + rollback ``invoice.rollback``, ``source_system="zoho"``,
``append_only=true``, ``approval_ref`` link); ``WorkflowState`` (completed vs
failed, ``posted_total``=Σ non-rolled-back, ``idempotency_keys`` map,
``pending_approvals=[]``, ``intent="ar_issue_invoice"``); deterministic audit
refs + the checkpoint map (§11); §14 envelope shape; and end-to-end execution
via ``run()`` (8 scenarios: single success; batch all-success; partial →
rollback of created; all-failed no rollback; duplicate; missing approval_ref →
AR_FORBIDDEN; validation failure → AR_VALIDATION; malformed JSON →
AR_VALIDATION). No network, no LangFlow, no Docker — ``python3
zoho_upload_flow_selftest.py`` runs anywhere. Mirrors invoice_generation_selftest's
harness (CLAUDE.md self-test convention): PASS/FAIL counts, exits non-zero on
any failure, so ``make test`` (via ``scripts/zoho-upload-flow.selftest.sh``) and
CI pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/zoho_upload_flow_selftest.py
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
#  Stub lfx + langgraph so zoho_upload_flow imports without the in-image venv.
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

import components.ar_common.zoho_upload_flow as c  # noqa: E402

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


# --------------------------------------------------------------------------- #
#  Fixtures + scenario stub transport.
# --------------------------------------------------------------------------- #

APPROVAL_REF = "ar-approval-12345678-1234-1234-1234-123456789abc"


def _inv(iid="inv-1", total="100.00", customer="CUST-42"):
    """A valid InvoiceData dict (the Invoice JSON from ar_invoice_generation)."""
    return {
        "invoice_id": iid,
        "invoice_number": f"IG-{iid}",
        "customer_ref": customer,
        "tenant": "cosmic-vikings",
        "issue_date": "2026-07-07",
        "due_date": "2026-08-06",
        "line_items": [{
            "line_id": "l1", "item_ref": "X", "description": "d",
            "qty": "1.00", "unit_price": total, "amount": total,
        }],
        "subtotal": total,
        "total": total,
        "currency": "SAR",
        "status": "draft",
        "balance_due": total,
        "contract_version": "1.0.0",
    }


def _resp(code, http_status=201, zoho_id="zoho-inv-aaa", transient=False,
          duplicate=False):
    return {"ok": code in c.SUCCESS_CODES,
            "http_status": http_status, "code": code,
            "zoho_id": zoho_id, "zoho_ref": f"INV-{zoho_id[-3:]}" if zoho_id else "",
            "duplicate": duplicate, "transient": transient}


_TRANSIENT = _resp("AR_UPSTREAM", http_status=503, zoho_id="", transient=True)
_SUCCESS = _resp("AR_OK", http_status=201, zoho_id="zoho-inv-aaa")


class ScenarioStub:
    """Stub Zoho transport that maps invoice_id → a response or response list.

    A list is consumed in order (last repeats) so transient-then-success and
    all-transient-exhausted scenarios are exercisable. ``delete_invoice`` always
    succeeds (rollback best-effort is tested via the created-invoice count).
    """

    def __init__(self, scenarios):
        self.scenarios = scenarios
        self.calls = {}

    def create_invoice(self, invoice, idempotency_key):
        iid = invoice.get("invoice_id", "")
        seq = self.scenarios.get(iid)
        if seq is None:
            return c.StubZohoUpload().create_invoice(invoice, idempotency_key)
        if isinstance(seq, list):
            idx = self.calls.get(iid, 0)
            self.calls[iid] = idx + 1
            return seq[min(idx, len(seq) - 1)]
        return seq

    def delete_invoice(self, zoho_id):
        return {"ok": True, "http_status": 204, "code": c.CODE_OK,
                "transient": False}


def _run(payload_text, scenarios=None):
    """Drive ZohoUploadFlowComponent.run() with the stub graph; return envelope."""
    if scenarios is None:
        c.set_transport(c.StubZohoUpload())
    else:
        c.set_transport(ScenarioStub(scenarios))
    c._SLEEP = lambda s: None  # §10 backoff is instant under the offline test
    comp = c.ZohoUploadFlowComponent()
    comp.user_input = payload_text
    comp.model_name = "glm-5.2:cloud"
    comp.session_id = "s1"
    return json.loads(comp.run().text)


def _request(invoices, approval_ref=APPROVAL_REF, trace_id="t1"):
    return {"approval_ref": approval_ref, "trace_id": trace_id,
            "tenant": "cosmic-vikings", "invoices": invoices}


# --------------------------------------------------------------------------- #
# [1] numeric helpers + backoff
# --------------------------------------------------------------------------- #
print("[1] numeric helpers + §10 backoff")
eq(c._to_2dp("1234.5"), "1234.50", "half-up 2dp")
eq(c._to_2dp("-3.006"), "0.00", "negative clamped to 0.00 (non-neg)")
eq(c._to_signed_2dp("-3.006"), "-3.01", "signed half-up")
eq(c._sum_2dp(["1.10", "2.20", "-0.30"]), "3.00", "sum 2dp")
# §10 backoff: 1s·2^n capped 30s, ±25% parity jitter (deterministic).
eq(c._backoff_delay(0), 1.25, "attempt 0 → 1s +25% = 1.25s")
eq(c._backoff_delay(1), 1.5, "attempt 1 → 2s -25% = 1.5s")
eq(c._backoff_delay(2), 5.0, "attempt 2 → 4s +25% = 5.0s")
eq(c._backoff_delay(10), 30.0, "attempt 10 → capped at 30s")

# --------------------------------------------------------------------------- #
# [2] _parse_request
# --------------------------------------------------------------------------- #
print("[2] _parse_request")
req, err = c._parse_request(json.dumps(_request([_inv()])))
falsy(err, "good request → no error")
eq(len(req["invoices"]), 1, "single invoice via array")
eq(req["approval_ref"], APPROVAL_REF, "approval_ref parsed")
_, err = c._parse_request(json.dumps({"invoices": [_inv()]}))
eq(err["code"], "AR_FORBIDDEN", "missing approval_ref → AR_FORBIDDEN (§1)")
_, err = c._parse_request(json.dumps({"approval_ref": "ar-approval-not-a-uuid",
                                       "invoices": [_inv()]}))
eq(err["code"], "AR_FORBIDDEN", "bad approval_ref pattern → AR_FORBIDDEN")
_, err = c._parse_request(json.dumps({"approval_ref": APPROVAL_REF}))
eq(err["code"], "AR_VALIDATION", "missing invoices → AR_VALIDATION")
_, err = c._parse_request(json.dumps({"approval_ref": APPROVAL_REF,
                                       "invoices": []}))
eq(err["code"], "AR_VALIDATION", "empty invoices → AR_VALIDATION")
_, err = c._parse_request(json.dumps([1, 2, 3]))
eq(err["code"], "AR_VALIDATION", "non-object request → AR_VALIDATION")
_, err = c._parse_request("not json")
eq(err["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
_, err = c._parse_request("")
eq(err["code"], "AR_VALIDATION", "empty input → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [3] _validate_invoice
# --------------------------------------------------------------------------- #
print("[3] _validate_invoice")
eq(c._validate_invoice(_inv()), [], "good invoice → no errors")
truthy(any(e["path"] == "customer_ref" for e in c._validate_invoice(
    {**_inv(), "customer_ref": ""})), "empty customer_ref → error (§16)")
truthy(any(e["path"] == "invoice_id" for e in c._validate_invoice(
    {k: v for k, v in _inv().items() if k != "invoice_id"})),
    "missing invoice_id → error")
truthy(any(e["path"] == "total" for e in c._validate_invoice(
    {**_inv(), "total": "100"})), "bad 2dp money → error")
truthy(any(e["path"] == "currency" for e in c._validate_invoice(
    {**_inv(), "currency": "usd"})), "bad currency → error (lowercase fails ^[A-Z]{3}$)")
truthy(any(e["path"] == "issue_date" for e in c._validate_invoice(
    {**_inv(), "issue_date": "07-07-2026"})), "bad date → error")
truthy(any(e["path"] == "line_items" for e in c._validate_invoice(
    {**_inv(), "line_items": []})), "empty line_items → error")
truthy(any(e["path"] == "status" for e in c._validate_invoice(
    {**_inv(), "status": "unknown"})), "bad status enum → error")
truthy(any("unit_price" in e["path"] for e in c._validate_invoice(
    {**_inv(), "line_items": [{**_inv()["line_items"][0], "unit_price": "1"}]})),
    "bad line item 2dp → error")

# --------------------------------------------------------------------------- #
# [4] _validate_request
# --------------------------------------------------------------------------- #
print("[4] _validate_request")
report, err = c._validate_request(_request([_inv()]), "t1")
falsy(err, "good request → no error")
truthy(report["valid"], "good request valid=True")
eq(report["contract_name"], "ZohoUploadRequest", "contract_name")
report, err = c._validate_request(
    _request([_inv("inv-1"), {**_inv("inv-2"), "customer_ref": ""}]), "t1")
eq(err["code"], "AR_VALIDATION", "one bad invoice → AR_VALIDATION")
falsy(report["valid"], "bad request valid=False")
eq(len(report["per_invoice"]), 1, "per_invoice error map has 1 entry")
eq(report["per_invoice"][0]["invoice_id"], "inv-2", "per_invoice names the bad invoice")

# --------------------------------------------------------------------------- #
# [5] _build_idempotency_key
# --------------------------------------------------------------------------- #
print("[5] _build_idempotency_key")
key = c._build_idempotency_key("cosmic-vikings", "inv-1")
truthy(_re.match(c.IDEMPOTENCY_RE, key), "idempotency_key matches pattern")
truthy(key.startswith("ar-idem:invoice_issue:cosmic-vikings:"),
       "idempotency_key shaped ar-idem:invoice_issue:<tenant>:<h>")
eq(c._build_idempotency_key("cosmic-vikings", "inv-1"), key,
   "idempotency_key deterministic for same invoice_id")
falsy(c._build_idempotency_key("cosmic-vikings", "inv-1")
      == c._build_idempotency_key("cosmic-vikings", "inv-2"),
      "different invoice_id → different key")

# --------------------------------------------------------------------------- #
# [6] _upload_one (§10 retry over the stub transport)
# --------------------------------------------------------------------------- #
print("[6] _upload_one (§10 retry)")
c._SLEEP = lambda s: None

def _upload(iid, scenarios):
    c.set_transport(ScenarioStub(scenarios))
    return c._upload_one(_inv(iid), c._build_idempotency_key("cosmic-vikings", iid))

out = _upload("s1", {"s1": _SUCCESS})
eq(out["code"], "AR_OK", "success → AR_OK")
truthy(out["zoho_id"], "success → zoho_id set")
eq(out["attempts"], 1, "success → attempts=1")
out = _upload("s2", {"s2": _resp("AR_DUPLICATE", http_status=409, duplicate=True)})
eq(out["code"], "AR_DUPLICATE", "duplicate → AR_DUPLICATE")
eq(out["duplicate"], True, "duplicate → duplicate=true")
eq(out["attempts"], 1, "duplicate → no retry (attempts=1)")
out = _upload("s3", {"s3": [_TRANSIENT, _SUCCESS]})
eq(out["code"], "AR_OK", "transient-then-success → AR_OK")
eq(out["attempts"], 2, "transient-then-success → attempts=2")
out = _upload("s4", {"s4": _resp("AR_VALIDATION", http_status=400, zoho_id="")})
eq(out["code"], "AR_VALIDATION", "hard 4xx → AR_VALIDATION (no retry)")
eq(out["attempts"], 1, "hard 4xx → attempts=1")
falsy(out["zoho_id"], "hard 4xx → no zoho_id")
out = _upload("s5", {"s5": _resp("AR_AUTH", http_status=401, zoho_id="")})
eq(out["code"], "AR_AUTH", "auth 401 → AR_AUTH (no retry)")
eq(out["attempts"], 1, "auth 401 → attempts=1")
out = _upload("s6", {"s6": [_TRANSIENT, _TRANSIENT, _TRANSIENT]})
eq(out["code"], "AR_UPSTREAM", "all-transient-exhausted → AR_UPSTREAM")
eq(out["attempts"], 3, "all-transient-exhausted → attempts=3")
c.set_transport(c.StubZohoUpload())

# --------------------------------------------------------------------------- #
# [7] _build_upload_result (canonical ZohoUploadResult)
# --------------------------------------------------------------------------- #
print("[7] _build_upload_result (canonical ZohoUploadResult)")
outcome = {"code": "AR_OK", "http_status": 201, "zoho_id": "zoho-inv-aaa",
           "zoho_ref": "INV-aaa", "duplicate": False, "attempts": 1,
           "attempted_at": "2026-07-07T00:00:00Z",
           "idempotency_key": c._build_idempotency_key("cosmic-vikings", "inv-1")}
res = c._build_upload_result(outcome, "t1", "cosmic-vikings")
schema_keys = {"trace_id", "tenant", "operation", "http_status", "code",
               "idempotency_key", "contract_version", "zoho_id", "zoho_ref",
               "duplicate", "raw_response_ref", "attempted_at", "attempts"}
truthy(set(res.keys()) <= schema_keys, "canonical keys ⊆ schema (addlProps:false)")
falsy("rolled_back" in res, "canonical result has no rolled_back (rollback is audit-only)")
eq(res["operation"], "invoice_issue", "operation invoice_issue")
truthy(res["code"] in ("AR_OK", "AR_DUPLICATE", "AR_UPSTREAM", "AR_AUTH",
                       "AR_VALIDATION", "AR_FORBIDDEN", "AR_NOT_FOUND"),
       "code in enum")
truthy(_re.match(c.IDEMPOTENCY_RE, res["idempotency_key"]), "idempotency_key pattern")
truthy(_re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", res["attempted_at"]),
       "attempted_at ISO-Z")
eq(res["attempts"], 1, "attempts int")
eq(res["trace_id"], "t1", "trace_id set")
eq(res["tenant"], "cosmic-vikings", "tenant set")

# --------------------------------------------------------------------------- #
# [8] _build_audit_record
# --------------------------------------------------------------------------- #
print("[8] _build_audit_record")
create = c._build_audit_record(
    audit_id="a1", trace_id="t1", tenant="cosmic-vikings", actor="sub-1",
    action="invoice.issue", timestamp="2026-07-07T00:00:00Z",
    approval_ref=APPROVAL_REF,
    idempotency_key=c._build_idempotency_key("cosmic-vikings", "inv-1"),
    source_ref="zoho-inv-aaa", before={"status": "draft"},
    after={"zoho_id": "zoho-inv-aaa", "status": "sent"})
eq(create["action"], "invoice.issue", "create action invoice.issue")
eq(create["source_system"], "zoho", "source_system zoho")
eq(create["source_ref"], "zoho-inv-aaa", "source_ref = zoho_id")
eq(create["approval_ref"], APPROVAL_REF, "approval_ref link (§13)")
eq(create["append_only"], True, "append_only true")
eq(create["actor"], "sub-1", "actor = Keycloak sub")
eq(create["before"], {"status": "draft"}, "before delta")
eq(create["after"]["status"], "sent", "after status sent")
rollback = c._build_audit_record(
    audit_id="a2", trace_id="t1", tenant="cosmic-vikings", actor="sub-1",
    action="invoice.rollback", timestamp="2026-07-07T00:00:01Z",
    approval_ref=APPROVAL_REF, idempotency_key="", source_ref="zoho-inv-aaa",
    before={"zoho_id": "zoho-inv-aaa"}, after={"status": "voided"})
eq(rollback["action"], "invoice.rollback", "rollback action invoice.rollback")
eq(rollback["after"], {"status": "voided"}, "rollback after voided")
eq(rollback["append_only"], True, "rollback append_only true")
falsy("idempotency_key" in rollback, "rollback has no idempotency_key (empty omitted)")

# --------------------------------------------------------------------------- #
# [9] build_workflow_state
# --------------------------------------------------------------------------- #
print("[9] build_workflow_state")
ws = c.build_workflow_state("t1", c.FLOW_ID, "cosmic-vikings", "completed",
                            "300.00", {"invoice_issue:inv-1": "k1"}, ["ref1"],
                            "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
eq(ws["intent"], c.FLOW_ID, "intent ar_issue_invoice")
eq(ws["status"], "completed", "status completed")
eq(ws["posted_total"], "300.00", "posted_total reflected")
eq(ws["matched_amount"], "0.00", "matched_amount 0.00 (not a match flow)")
eq(ws["outstanding_balance"], "0.00", "outstanding_balance 0.00")
eq(ws["pending_approvals"], [], "pending_approvals [] (approval at boundary)")
eq(ws["idempotency_keys"], {"invoice_issue:inv-1": "k1"}, "idempotency_keys map")
eq(ws["audit_refs"], ["ref1"], "audit_refs reflected")
ws_fail = c.build_workflow_state("t1", c.FLOW_ID, "cosmic-vikings", "failed",
                                 "0.00", {}, ["ref1"], "", "")
eq(ws_fail["status"], "failed", "failed status reflected")

# --------------------------------------------------------------------------- #
# [10] _audit_ref + _record_checkpoint
# --------------------------------------------------------------------------- #
print("[10] audit ref + checkpoints")
eq(c._audit_ref("t1", "upload"), c._audit_ref("t1", "upload"),
   "audit ref deterministic for same trace+label")
falsy(c._audit_ref("t1", "upload") == c._audit_ref("t1", "store"),
      "different label → different ref")
st = c.ZohoUploadState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings")
audit_refs, checkpoints = c._record_checkpoint(st, "validate")
eq(checkpoints["validate"], c._audit_ref("t1", "validate"), "checkpoint map keyed by label")
audit_refs2, checkpoints2 = c._record_checkpoint(
    c.ZohoUploadState(trace_id="t1", flow_id=c.FLOW_ID, tenant="cosmic-vikings",
                     audit_refs=audit_refs, checkpoints=checkpoints), "upload")
eq(len(audit_refs2), 2, "second checkpoint appends a new ref")
truthy(set(checkpoints2.keys()) == {"validate", "upload"},
       "checkpoints map accumulates labels")

# --------------------------------------------------------------------------- #
# [11] end-to-end via run()
# --------------------------------------------------------------------------- #
print("[11] end-to-end run()")

# (a) single invoice + approval_ref + stub success
env = _run(json.dumps(_request([_inv("inv-1", "100.00")])))
eq(env["status"], "ok", "(a) single success → AR_OK")
eq(env["code"], "AR_OK", "(a) code AR_OK")
eq(len(env["data"]["upload_results"]), 1, "(a) 1 upload_result")
truthy(env["data"]["upload_results"][0]["zoho_id"], "(a) zoho_id set")
eq(env["data"]["batch_summary"]["posted_total"], "100.00", "(a) posted_total = total")
eq(env["data"]["workflow_state"]["status"], "completed", "(a) workflow_state completed")
eq(len(env["data"]["audit_records"]), 1, "(a) 1 create-audit")
eq(env["data"]["audit_records"][0]["action"], "invoice.issue", "(a) create audit action")
eq(len(env["data"]["rollback_results"]), 0, "(a) 0 rollback")
eq(env["data"]["approval_ref"], APPROVAL_REF, "(a) approval_ref echoed in envelope")

# (b) batch of 3 all succeed
env = _run(json.dumps(_request([_inv("a", "100.00"), _inv("b", "200.00"),
                                 _inv("c", "50.00")])))
eq(env["status"], "ok", "(b) batch all-success → AR_OK")
eq(len(env["data"]["upload_results"]), 3, "(b) 3 upload_results")
eq(env["data"]["batch_summary"]["posted_total"], "350.00", "(b) posted_total = Σ")
eq(env["data"]["workflow_state"]["status"], "completed", "(b) completed")
eq(len(env["data"]["audit_records"]), 3, "(b) 3 create-audit")
eq(len(env["data"]["zoho_upload_results"]), 3, "(b) 3 canonical ZohoUploadResult")

# (c) batch of 3, invoice 2 fails after retries, 1&3 succeed → rollback 1&3
env = _run(json.dumps(_request([_inv("c1", "100.00"), _inv("c2", "200.00"),
                                 _inv("c3", "50.00")])),
           scenarios={"c1": _SUCCESS, "c2": [_TRANSIENT, _TRANSIENT, _TRANSIENT],
                      "c3": _SUCCESS})
eq(env["status"], "error", "(c) partial → error envelope")
eq(env["code"], "AR_UPSTREAM", "(c) code AR_UPSTREAM (batch)")
eq(len(env["data"]["rollback_results"]), 2, "(c) rollback deletes 1&3 (created)")
ur = {r["invoice_id"]: r for r in env["data"]["upload_results"]}
eq(ur["c1"]["rolled_back"], True, "(c) c1 rolled_back")
eq(ur["c3"]["rolled_back"], True, "(c) c3 rolled_back")
eq(ur["c2"]["rolled_back"], False, "(c) c2 not rolled_back (never created)")
eq(ur["c2"]["code"], "AR_UPSTREAM", "(c) c2 code AR_UPSTREAM (exhausted)")
eq(env["data"]["workflow_state"]["posted_total"], "0.00", "(c) posted_total 0.00 (all rolled back/failed)")
eq(env["data"]["workflow_state"]["status"], "failed", "(c) workflow_state failed")
eq(len(env["data"]["audit_records"]), 5, "(c) 3 create-audit + 2 rollback-audit")
actions = [r["action"] for r in env["data"]["audit_records"]]
eq(actions.count("invoice.issue"), 3, "(c) 3 create audit records")
eq(actions.count("invoice.rollback"), 2, "(c) 2 rollback audit records")

# (d) batch of 2 both fail (hard) → no rollback, posted 0.00
env = _run(json.dumps(_request([_inv("d1", "100.00"), _inv("d2", "200.00")])),
           scenarios={"d1": _resp("AR_VALIDATION", http_status=400, zoho_id=""),
                      "d2": _resp("AR_VALIDATION", http_status=400, zoho_id="")})
eq(env["status"], "error", "(d) all-failed → error envelope")
eq(len(env["data"]["rollback_results"]), 0, "(d) no rollback (nothing created)")
eq(env["data"]["workflow_state"]["posted_total"], "0.00", "(d) posted_total 0.00")
eq(len(env["data"]["audit_records"]), 2, "(d) 2 create-audit (failed attempts)")
falsy(any(r["rolled_back"] for r in env["data"]["upload_results"]),
      "(d) none rolled_back")

# (e) duplicate (409) → AR_OK envelope, per-invoice AR_DUPLICATE
env = _run(json.dumps(_request([_inv("e1", "100.00")])),
           scenarios={"e1": _resp("AR_DUPLICATE", http_status=409,
                                  zoho_id="zoho-inv-e1", duplicate=True)})
eq(env["status"], "ok", "(e) duplicate → ok envelope")
eq(env["data"]["upload_results"][0]["code"], "AR_DUPLICATE", "(e) per-invoice AR_DUPLICATE")
eq(env["data"]["upload_results"][0]["duplicate"], True, "(e) duplicate=true")
truthy(env["data"]["upload_results"][0]["zoho_id"], "(e) zoho_id set (existing)")
eq(env["data"]["batch_summary"]["posted_total"], "100.00", "(e) posted_total = total (replay counted)")
eq(env["data"]["workflow_state"]["status"], "completed", "(e) completed")
eq(len(env["data"]["audit_records"]), 1, "(e) 1 create-audit")
eq(len(env["data"]["rollback_results"]), 0, "(e) 0 rollback")

# (f) missing approval_ref → AR_FORBIDDEN, no upload, no audit
env = _run(json.dumps({"invoices": [_inv("f1")], "trace_id": "t", "tenant": "cosmic-vikings"}))
eq(env["status"], "error", "(f) missing approval_ref → error")
eq(env["code"], "AR_FORBIDDEN", "(f) code AR_FORBIDDEN (§1)")
eq(len(env["data"]["upload_results"]), 0, "(f) no upload attempted")
eq(len(env["data"]["audit_records"]), 0, "(f) no audit")

# (g) validation failure (bad invoice) → AR_VALIDATION, no upload, no audit
env = _run(json.dumps(_request([{**_inv("g1"), "customer_ref": ""}])))
eq(env["status"], "error", "(g) bad invoice → error")
eq(env["code"], "AR_VALIDATION", "(g) code AR_VALIDATION")
eq(len(env["data"]["upload_results"]), 0, "(g) no upload attempted")
eq(len(env["data"]["audit_records"]), 0, "(g) no audit")
truthy(env["data"]["validation_report"]["per_invoice"], "(g) per_invoice error map present")

# (h) malformed JSON → AR_VALIDATION
env = _run("not json")
eq(env["status"], "error", "(h) malformed JSON → error")
eq(env["code"], "AR_VALIDATION", "(h) code AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [12] envelope shape
# --------------------------------------------------------------------------- #
print("[12] envelope shape")
env = _run(json.dumps(_request([_inv("inv-1", "100.00")])))
for k in ("upload_results", "zoho_upload_results", "rollback_results",
          "batch_summary", "validation_report", "workflow_state",
          "audit_records", "audit_refs", "checkpoints", "approval_ref",
          "flow_id", "tenant", "started_at", "ended_at", "contract_version"):
    truthy(k in env["data"], f"data has {k}")
eq(env["data"]["flow_id"], c.FLOW_ID, "flow_id in envelope data")
eq(env["data"]["contract_version"], c.CONTRACT_VERSION, "contract_version")
truthy(env["trace_id"], "envelope has a trace_id")
# canonical ZohoUploadResult objects respect additionalProperties:false
for zr in env["data"]["zoho_upload_results"]:
    truthy(set(zr.keys()) <= schema_keys, "canonical result keys ⊆ schema")
# checkpoint labels (success path: validate, upload, store, audit, state, ar_issue_invoice)
expected_labels = {"validate", "upload", "store", "audit", "state", c.FLOW_ID}
eq(set(env["data"]["checkpoints"].keys()), expected_labels,
   "checkpoint labels (success path — no rollback)")

# --------------------------------------------------------------------------- #
# [13] run() never raises (§5/§9)
# --------------------------------------------------------------------------- #
print("[13] run() never raises")
comp = c.ZohoUploadFlowComponent()  # no user_input set
comp.model_name = "glm-5.2:cloud"
comp.session_id = "s1"
env = json.loads(comp.run().text)
truthy(env["status"] in ("ok", "error"), "missing user_input still returns an envelope")
# truly broken input still returns an envelope, never raises
env = json.loads(c.ZohoUploadFlowComponent().run().text)
truthy(env["status"] in ("ok", "error"), "empty user_input returns an envelope")

print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)