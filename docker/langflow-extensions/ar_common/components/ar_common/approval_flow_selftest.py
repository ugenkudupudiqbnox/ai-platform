#!/usr/bin/env python3
"""approval_flow_selftest — offline stdlib-only tests for the Cosmic AR Human
Approval Flow's pure functions + end-to-end pause/resume graph (constitution
§1/§8/§9/§11/§13/§14/§16/§19; ADR-0010).

Covers: numeric coercion; review-packet parsing (good / empty / malformed /
non-object / missing-action / missing-proposal → AR_VALIDATION);
``_build_approval_request`` (required contract keys, ``approval_ref`` shaped
``ar-approval-<uuid>``, tier packet > override > default, ``requested_by``
fallbacks, 2dp amount); ``_build_packet`` (4 summaries pass-through, 3 options);
``_normalize_decision`` (approve/reject/request changes + synonyms → canonical;
garbage → None); ``_parse_decision_reply`` (``"approve <ref>"``→approved,
``"reject <ref> …"``→rejected+reason, ``"request changes <ref>"``→request_changes,
no-verb → None); ``_build_approval_result`` (contract required keys,
``consumed=false``, decision echoed); ``_build_audit_record`` (``append_only=
true``, ``actor=decided_by``, ``approval_ref`` pattern, before/after delta,
``action="approval.decision:<action>"``); ``build_workflow_state``
(``status="completed"``, totals ``"0.00"``, ``pending_approvals=[]``,
``intent="ar_approval"``); deterministic audit refs + the per-gate checkpoints
map (§11: packet/decision/state/audit/ar_approval); and **end-to-end pause/
resume via ``run()``** — the custom walker models ``interrupt()`` pause/resume
(the base walker stubs ``interrupt`` → ``None`` which would fall through to
``AR_FORBIDDEN``): good packet, no resume → ``pending_approval`` + ref set +
3 options + packet present; resume ``"approve <ref>"`` → ``AR_OK`` +
``decision="approved"`` + 1 audit record + ``workflow_state.status=
"completed"``; resume ``"reject <ref>"`` → rejected; resume ``"request changes
<ref>"`` → request_changes; resume garbage (ref + no verb) → ``AR_FORBIDDEN``;
fresh run with malformed JSON → ``AR_VALIDATION``; envelope shape; ``run()``
never raises. No network, no LangFlow, no Docker — ``python3
approval_flow_selftest.py`` runs anywhere. Mirrors calculation_selftest's
harness (CLAUDE.md self-test convention): PASS/FAIL counts, exits non-zero on
any failure, so ``make test`` (via ``scripts/approval-flow.selftest.sh``) and CI
pick it up.

Run:  python3 docker/langflow-extensions/ar_common/components/ar_common/approval_flow_selftest.py
"""
import json
import os
import re as _re
import sys
import types
from dataclasses import asdict

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- #
#  Stub lfx + langgraph so approval_flow imports without the in-image venv.
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


# --------------------------------------------------------------------------- #
#  interrupt() pause/resume model — the key difference from the base walker.
# --------------------------------------------------------------------------- #


class _Pause(Exception):
    """Raised by the interrupt stub in 'pause' mode to suspend the graph."""

    def __init__(self, payload):
        super().__init__("interrupt")
        self.payload = payload


class _Command:
    """Stub langgraph.types.Command carrying a resume value."""

    def __init__(self, resume=None):
        self.resume = resume


# Module-level interrupt box: 'pause' mode raises _Pause; 'resume' mode returns
# the stored resume value. approval_flow's imported `interrupt` is bound to this
# function, so node calls are steered by the box state.
_INTERRUPT = {"mode": "pause", "resume_value": None}


def _interrupt(payload):
    if _INTERRUPT["mode"] == "pause":
        raise _Pause(payload)
    return _INTERRUPT["resume_value"]


class _StateGraph:
    """Stub that records the graph topology and compiles a pause/resume walker."""

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
    """Walks the stub graph modeling interrupt() pause/resume.

    Conditional edges route on ``state.status``; static edges chain; unknown
    status falls back to ``respond``. A fresh ``invoke(initial)`` runs in
    'pause' mode; when a node raises ``_Pause`` (the interrupt), the walker
    records the paused node + persists state + sets ``state.next=[paused_node]``.
    ``invoke(Command(resume=v))`` switches to 'resume' mode (interrupt returns
    ``v``) and re-runs from the paused node → ``state.next=[]`` on completion.
    """

    def __init__(self, sg):
        self.sg = sg
        self.state = {}
        self.state_type = None
        self.paused_node = None
        self.paused_payload = None
        self.next = []

    def _static(self):
        static = {}
        for a, b in self.sg.edges:
            static.setdefault(a, b)
        return static

    def _materialize(self, st, initial):
        if isinstance(st, dict):
            return st
        return self.state_type(**asdict(st)) if self.state_type else st

    def invoke(self, initial, config=None, context=None):
        rt = _Runtime()
        rt.context = context or {}
        static = self._static()
        if isinstance(initial, _Command):
            # Resume: re-run from the paused node with interrupt returning v.
            _INTERRUPT["mode"] = "resume"
            _INTERRUPT["resume_value"] = initial.resume
            st = self.state_type(**self.state) if self.state_type else dict(self.state)
            cur = self.paused_node
        else:
            # Fresh run: pause mode; interrupt raises _Pause at the gate.
            _INTERRUPT["mode"] = "pause"
            _INTERRUPT["resume_value"] = None
            self.state_type = type(initial)
            st = initial
            cur = static.get("START")
        while cur is not None and cur != "END":
            fn = self.sg.nodes[cur]
            try:
                upd = fn(st, rt)
            except _Pause as p:
                self.paused_node = cur
                self.paused_payload = p.payload
                self.state = asdict(st) if not isinstance(st, dict) else dict(st)
                self.next = [cur]
                return
            d = asdict(st) if not isinstance(st, dict) else dict(st)
            if upd:
                d.update(upd)
            st = self.state_type(**d) if self.state_type else d
            if cur in self.sg.conds:
                router, mapping = self.sg.conds[cur]
                nxt = router(st)
                cur = mapping.get(nxt, "respond")
            else:
                cur = static.get(cur, "END")
        self.state = asdict(st) if not isinstance(st, dict) else dict(st)
        self.next = []

    def get_state(self, config):
        class _S:
            pass
        s = _S()
        s.values = self.state
        s.next = self.next
        s.tasks = []
        s.config = {}
        return s


_g.StateGraph = _StateGraph
_stub("langgraph.runtime", {"Runtime": _Runtime})
_stub("langgraph.types", {"Command": _Command, "interrupt": _interrupt})

# ar_common bundle root on sys.path (this flow only — no cosmic_common import).
_AR_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, _AR_ROOT)

import components.ar_common.approval_flow as c  # noqa: E402

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
    if not value:
        ok(name)
    else:
        bad(name, f"expected falsy, got {value!r}")


GOOD_PACKET = {
    "trace_id": "trace-1", "tenant": "cosmic-vikings", "currency": "SAR",
    "action": "ar_issue_invoice", "amount": "1250.00", "tier": "approval",
    "requested_by": "keycloak-sub-abc",
    "proposal": {
        "operation": "post", "target": "GL:4000",
        "amount": "1250.00", "currency": "SAR",
        "details": {"narration": "Post intercompany receivable"},
    },
    "idempotency_key": "ar-idem:trace-1:post",
    "summaries": {
        "revenue_summary": {"total": "10000.00"},
        "expense_summary": {"total": "4000.00"},
        "invoice_summary": {"invoice_id": "INV-001", "total": "1250.00"},
        "validation_report": {"valid": True, "errors": []},
    },
}


def _new_comp(user_input, actor="keycloak-sub-tester"):
    """Fresh component wired for one run() call."""
    comp = c.HumanApprovalFlowComponent()
    comp.user_input = user_input
    comp.tier = "approval"
    comp.model_name = "glm-5.2:cloud"
    comp.session_id = "s1"
    comp.actor = actor
    return comp


def _run_pause(packet_text, actor="keycloak-sub-tester"):
    """First turn: pause at the gate. Returns (comp, envelope)."""
    comp = _new_comp(packet_text, actor)
    return comp, json.loads(comp.run().text)


def _run_resume(comp, reply, actor=None):
    """Resume turn on the SAME component (preserves the checkpoint)."""
    comp.user_input = reply
    if actor is not None:
        comp.actor = actor
    return json.loads(comp.run().text)


# --------------------------------------------------------------------------- #
# [1] numeric helpers
# --------------------------------------------------------------------------- #
print("[1] numeric helpers")
eq(c._to_signed_2dp("1234.5"), "1234.50", "half-up signed 2dp")
eq(c._to_signed_2dp("-3.006"), "-3.01", "negative half-up")
eq(c._to_signed_2dp(None), "0.00", "None → 0.00")
eq(c._to_2dp("-5.00"), "0.00", "non-negative 2dp clamps negative")
eq(c._to_2dp("12.345"), "12.35", "non-negative half-up")

# --------------------------------------------------------------------------- #
# [2] _parse_packet
# --------------------------------------------------------------------------- #
print("[2] _parse_packet")
p, err = c._parse_packet(json.dumps(GOOD_PACKET))
falsy(err, "good packet → no error")
eq(p["action"], "ar_issue_invoice", "good packet parsed")
_, err = c._parse_packet("")
eq(err["code"], "AR_VALIDATION", "empty packet → AR_VALIDATION")
_, err = c._parse_packet("not json")
eq(err["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
_, err = c._parse_packet("[1,2,3]")
eq(err["code"], "AR_VALIDATION", "non-object packet → AR_VALIDATION")
_, err = c._parse_packet(json.dumps({"proposal": {"operation": "x"}}))
eq(err["code"], "AR_VALIDATION", "missing action → AR_VALIDATION")
_, err = c._parse_packet(json.dumps({"action": "x"}))
eq(err["code"], "AR_VALIDATION", "missing proposal → AR_VALIDATION")
_, err = c._parse_packet(json.dumps({"action": "x", "proposal": "notobj"}))
eq(err["code"], "AR_VALIDATION", "non-object proposal → AR_VALIDATION")

# --------------------------------------------------------------------------- #
# [3] _build_approval_request
# --------------------------------------------------------------------------- #
print("[3] _build_approval_request")
req = c._build_approval_request(GOOD_PACKET, "trace-1", "cosmic-vikings",
                                 "keycloak-sub-fallback", "approval")
for k in ("approval_id", "approval_ref", "trace_id", "tenant", "action",
          "amount", "currency", "tier", "requested_by", "requested_at",
          "proposal", "contract_version"):
    truthy(k in req, f"request has {k}")
truthy(_re.match(c.APPROVAL_REF_RE.pattern + r"$", req["approval_ref"]),
       "approval_ref shaped ar-approval-<uuid>")
eq(req["approval_ref"], f"ar-approval-{req['approval_id']}", "ref = ar-approval-{id}")
eq(req["amount"], "1250.00", "amount 2dp")
eq(req["tier"], "approval", "tier from packet")
eq(req["requested_by"], "keycloak-sub-abc", "requested_by from packet")
# tier fallback chain: no packet tier → ctx override
pkt_no_tier = dict(GOOD_PACKET); pkt_no_tier.pop("tier")
req2 = c._build_approval_request(pkt_no_tier, "t", "cv", "act", "dual-control")
eq(req2["tier"], "dual-control", "tier falls back to ctx override")
# no packet tier, no ctx override → default approval
req3 = c._build_approval_request(pkt_no_tier, "t", "cv", "act", "")
eq(req3["tier"], "approval", "tier default approval")
# requested_by fallback: no packet field → ctx actor
pkt_no_by = dict(GOOD_PACKET); pkt_no_by.pop("requested_by")
req4 = c._build_approval_request(pkt_no_by, "t", "cv", "keycloak-ctx-actor", "")
eq(req4["requested_by"], "keycloak-ctx-actor", "requested_by falls back to ctx actor")
# bad currency → default SAR
pkt_bad_ccy = dict(GOOD_PACKET); pkt_bad_ccy["currency"] = "xyz"
req5 = c._build_approval_request(pkt_bad_ccy, "t", "cv", "act", "")
eq(req5["currency"], "SAR", "bad currency → default SAR")

# --------------------------------------------------------------------------- #
# [4] _build_packet
# --------------------------------------------------------------------------- #
print("[4] _build_packet")
pkt = c._build_packet(GOOD_PACKET, req)
for k in ("revenue_summary", "expense_summary", "invoice_summary",
          "validation_report"):
    truthy(k in pkt["summaries"], f"packet summaries has {k}")
eq(pkt["approval_ref"], req["approval_ref"], "packet carries approval_ref")
eq(pkt["action"], "ar_issue_invoice", "packet carries action")
# summaries pass-through (whatever the caller supplied)
eq(pkt["summaries"]["revenue_summary"], {"total": "10000.00"},
   "revenue_summary passed through")
# missing summaries → None entries (present whatever supplied)
pkt_sparse = c._build_packet({"action": "x", "proposal": {}}, req)
falsy(pkt_sparse["summaries"]["revenue_summary"],
      "missing summary → None (present nothing)")

# --------------------------------------------------------------------------- #
# [5] _normalize_decision
# --------------------------------------------------------------------------- #
print("[5] _normalize_decision")
eq(c._normalize_decision("approve"), "approved", "approve → approved")
eq(c._normalize_decision("Approved"), "approved", "Approved → approved")
eq(c._normalize_decision("accept"), "approved", "accept → approved")
eq(c._normalize_decision("reject"), "rejected", "reject → rejected")
eq(c._normalize_decision("denied"), "rejected", "denied → rejected")
eq(c._normalize_decision("decline"), "rejected", "decline → rejected")
eq(c._normalize_decision("request changes"), "request_changes",
   "request changes → request_changes")
eq(c._normalize_decision("request_changes"), "request_changes",
   "request_changes → request_changes")
eq(c._normalize_decision("changes"), "request_changes", "changes → request_changes")
eq(c._normalize_decision("revise"), "request_changes", "revise → request_changes")
falsy(c._normalize_decision("maybe"), "garbage → None")
falsy(c._normalize_decision(None), "None → None")

# --------------------------------------------------------------------------- #
# [6] _parse_decision_reply
# --------------------------------------------------------------------------- #
print("[6] _parse_decision_reply")
ref = "ar-approval-12345678-1234-1234-1234-1234567890ab"
d = c._parse_decision_reply(f"approve {ref}", "act")
eq(d["decision"], "approved", "approve <ref> → approved")
eq(d["decided_by"], "act", "decided_by = actor")
d = c._parse_decision_reply(f"reject {ref} amount looks wrong", "act")
eq(d["decision"], "rejected", "reject <ref> … → rejected")
eq(d["reason"], "amount looks wrong", "reject reason = remainder")
d = c._parse_decision_reply(f"request changes {ref} need narration", "act")
eq(d["decision"], "request_changes", "request changes <ref> → request_changes")
eq(d["reason"], "need narration", "request_changes reason = remainder")
d = c._parse_decision_reply(f"{ref} hello there", "act")
falsy(d["decision"], "no leading verb → None decision")
d = c._parse_decision_reply("approve", "")
eq(d["decision"], "approved", "approve with no ref → approved")
eq(d["decided_by"], "unknown", "empty actor → unknown")

# --------------------------------------------------------------------------- #
# [7] _build_approval_result + _build_audit_record
# --------------------------------------------------------------------------- #
print("[7] _build_approval_result + _build_audit_record")
st = c.ApprovalFlowState(trace_id="t1", flow_id="ar_approval",
                         tenant="cosmic-vikings", decision="approved",
                         decided_by="keycloak-sub-1", decided_at="2026-01-01T00:00:00Z",
                         reason="ok", approval_ref=ref,
                         approval_request={"approval_id": "aid", "approval_ref": ref,
                                           "tier": "approval", "action": "ar_issue_invoice",
                                           "idempotency_key": "ar-idem:1"})
ar = c._build_approval_result(st)
for k in ("approval_id", "approval_ref", "decision", "decided_by", "decided_at",
          "trace_id", "tier", "idempotency_key", "reason", "consumed",
          "contract_version"):
    truthy(k in ar, f"result has {k}")
eq(ar["decision"], "approved", "result decision echoed")
eq(ar["consumed"], False, "consumed=false (POST is a separate flow's job)")
eq(ar["contract_version"], c.CONTRACT_VERSION, "result contract_version")
aid = c._audit_ref("t1", "audit")
rec = c._build_audit_record(st, aid)
eq(rec["append_only"], True, "audit append_only=true (§13)")
eq(rec["actor"], "keycloak-sub-1", "audit actor = decided_by (§13)")
eq(rec["action"], "approval.decision:ar_issue_invoice", "audit action shape")
eq(rec["approval_ref"], ref, "audit approval_ref link")
eq(rec["before"], {"status": "pending"}, "audit before delta")
eq(rec["after"], {"decision": "approved", "reason": "ok"}, "audit after delta")
eq(rec["audit_id"], aid, "audit_id passed through")

# --------------------------------------------------------------------------- #
# [8] build_workflow_state + _audit_ref + _record_checkpoint
# --------------------------------------------------------------------------- #
print("[8] workflow_state + audit ref + checkpoints")
ws = c.build_workflow_state("t1", "ar_approval", "cosmic-vikings", ["ref1"],
                            "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
eq(ws["intent"], "ar_approval", "intent ar_approval")
eq(ws["status"], "completed", "status completed regardless of decision")
eq(ws["matched_amount"], "0.00", "no money moved — matched 0.00")
eq(ws["outstanding_balance"], "0.00", "outstanding 0.00")
eq(ws["posted_total"], "0.00", "posted 0.00")
eq(ws["pending_approvals"], [], "pending_approvals empty (captured, not pending)")
eq(ws["idempotency_keys"], {}, "no POST → empty idempotency_keys")
eq(ws["tool_call_ref"], "t1:ar_approval:0", "tool_call_ref")
ref1 = c._audit_ref("t1", "packet")
ref2 = c._audit_ref("t1", "packet")
eq(ref1, ref2, "audit ref deterministic for same trace+label")
falsy(ref1 == c._audit_ref("t1", "decision"), "different label → different ref")
st2 = c.ApprovalFlowState(trace_id="t1", flow_id="ar_approval",
                          tenant="cosmic-vikings")
ar_refs, cps = c._record_checkpoint(st2, "packet")
eq(cps["packet"], ref1, "checkpoints map keyed by label")
ar_refs2, cps2 = c._record_checkpoint(
    c.ApprovalFlowState(trace_id="t1", flow_id="ar_approval",
                        tenant="cosmic-vikings", audit_refs=ar_refs,
                        checkpoints=cps), "decision")
truthy(len(ar_refs2) == 2, "second checkpoint appends a new ref")
truthy(set(cps2.keys()) == {"packet", "decision"}, "checkpoints accumulate labels")

# --------------------------------------------------------------------------- #
# [9] end-to-end pause/resume via run()
# --------------------------------------------------------------------------- #
print("[9] end-to-end pause/resume run()")
# (1) good packet, no resume → pending_approval
comp, env = _run_pause(json.dumps(GOOD_PACKET))
eq(env["status"], "pending_approval", "first run → pending_approval")
eq(env["code"], "AR_APPROVAL_REQUIRED", "code AR_APPROVAL_REQUIRED")
truthy(_re.match(c.APPROVAL_REF_RE.pattern + r"$", env["approval_ref"]),
       "pending envelope has approval_ref")
eq(len(env["data"]["options"]), 3, "3 options presented (approve/reject/request_changes)")
eq(set(env["data"]["options"]), {"approve", "reject", "request_changes"}, "options set")
truthy(env["data"]["packet"], "pending envelope carries the presentation packet")
eq(env["data"]["action"], "ar_issue_invoice", "pending data.action")
ref = env["approval_ref"]

# (2) resume "approve <ref>" → AR_OK + approved
env = _run_resume(comp, f"approve {ref}")
eq(env["status"], "ok", "approve resume → AR_OK")
eq(env["code"], "AR_OK", "code AR_OK")
eq(env["data"]["decision"], "approved", "decision approved")
eq(env["data"]["approval_result"]["decision"], "approved",
   "approval_result.decision approved")
eq(env["data"]["approval_result"]["consumed"], False, "consumed false on capture")
eq(len(env["data"]["audit_records"]), 1, "1 audit record logged (§13)")
eq(env["data"]["audit_records"][0]["actor"], "keycloak-sub-tester",
   "audit actor = run actor")
eq(env["data"]["audit_records"][0]["action"], "approval.decision:ar_issue_invoice",
   "audit action shape")
eq(env["data"]["audit_records"][0]["append_only"], True, "audit append_only")
eq(env["data"]["workflow_state"]["status"], "completed",
   "workflow_state.status completed regardless of decision")
eq(env["data"]["workflow_state"]["intent"], "ar_approval", "workflow_state intent")
eq(env["approval_ref"], ref, "ok envelope echoes approval_ref")
truthy({"packet", "decision", "state", "audit", "ar_approval"}
       .issubset(set(env["data"]["checkpoints"].keys())),
       "5 checkpoint labels (packet/decision/state/audit/ar_approval)")

# (3) fresh pause + resume "reject <ref> …" → rejected
comp, env = _run_pause(json.dumps(GOOD_PACKET))
ref = env["approval_ref"]
env = _run_resume(comp, f"reject {ref} amount disputed")
eq(env["status"], "ok", "reject resume → AR_OK")
eq(env["data"]["decision"], "rejected", "decision rejected")
eq(env["data"]["approval_result"]["decision"], "rejected", "result rejected")
eq(len(env["data"]["audit_records"]), 1, "reject logs 1 audit record")

# (4) fresh pause + resume "request changes <ref> …" → request_changes
comp, env = _run_pause(json.dumps(GOOD_PACKET))
ref = env["approval_ref"]
env = _run_resume(comp, f"request changes {ref} need narration line")
eq(env["status"], "ok", "request_changes resume → AR_OK")
eq(env["data"]["decision"], "request_changes", "decision request_changes")
eq(env["data"]["approval_result"]["decision"], "request_changes",
   "result request_changes")
eq(len(env["data"]["audit_records"]), 1, "request_changes logs 1 audit record")

# (5) fresh pause + resume garbage (ref present, no verb) → AR_FORBIDDEN
comp, env = _run_pause(json.dumps(GOOD_PACKET))
ref = env["approval_ref"]
env = _run_resume(comp, f"{ref} this makes no sense")
eq(env["status"], "error", "garbage resume → error")
eq(env["code"], "AR_FORBIDDEN", "garbage resume → AR_FORBIDDEN")

# (6) fresh run with malformed JSON → AR_VALIDATION (ingest short-circuits)
comp, env = _run_pause("not json")
eq(env["status"], "error", "malformed JSON → error")
eq(env["code"], "AR_VALIDATION", "malformed JSON → AR_VALIDATION")
falsy(env.get("approval_ref", ""), "malformed JSON → no approval_ref")

# --------------------------------------------------------------------------- #
# [10] envelope shape
# --------------------------------------------------------------------------- #
print("[10] envelope shape")
comp, env = _run_pause(json.dumps(GOOD_PACKET))
env = _run_resume(comp, f"approve {env['approval_ref']}")
for k in ("approval_result", "workflow_state", "packet", "audit_records",
          "audit_refs", "checkpoints", "decision", "flow_id", "tenant",
          "started_at", "ended_at", "contract_version"):
    truthy(k in env["data"], f"data has {k}")
eq(env["data"]["flow_id"], "ar_approval", "flow_id in envelope data")
eq(env["data"]["contract_version"], c.CONTRACT_VERSION, "contract_version")
eq(env["contract_version"], c.CONTRACT_VERSION, "base contract_version")
truthy(env["trace_id"], "envelope has a trace_id")
# pending envelope shape
comp, env = _run_pause(json.dumps(GOOD_PACKET))
for k in ("action", "tier", "packet", "options", "checkpoint_id"):
    truthy(k in env["data"], f"pending data has {k}")

# --------------------------------------------------------------------------- #
# [11] run() never raises (§5/§9)
# --------------------------------------------------------------------------- #
print("[11] run() never raises")
comp = c.HumanApprovalFlowComponent()
comp.user_input = None  # _to_str(None) = "" → AR_VALIDATION envelope, not a raise
comp.tier = "approval"
comp.model_name = "glm-5.2:cloud"
comp.session_id = "s1"
env = json.loads(comp.run().text)
eq(env["code"], "AR_VALIDATION", "None input → AR_VALIDATION envelope (no raise)")
# a deeply broken input still returns an envelope, never raises
env = json.loads(c.HumanApprovalFlowComponent().run().text)
truthy(env["status"] in ("error", "pending_approval", "ok"),
       "no-attribute run returns an envelope, not a raise")

# --------------------------------------------------------------------------- #
# [12] constants sanity
# --------------------------------------------------------------------------- #
print("[12] constants")
eq(c.FLOW_ID, "ar_approval", "FLOW_ID")
eq(c.DEFAULT_CURRENCY, "SAR", "DEFAULT_CURRENCY")
eq(c.DECISIONS, ("approved", "rejected", "request_changes"), "DECISIONS tuple")
eq(c.OPTIONS, ("approve", "reject", "request_changes"), "OPTIONS tuple")
eq(c.CONTRACT_VERSION, "1.0.0", "CONTRACT_VERSION")

print(f"\n== results: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)