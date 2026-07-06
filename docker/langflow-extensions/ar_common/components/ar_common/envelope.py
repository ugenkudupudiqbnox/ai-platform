"""Canonical JSON envelope component (constitution §14, architecture §3).

Scaffold only. At build phase this formats every component/flow output into the
canonical envelope:
  {"status":"ok|error|pending_approval","code":"AR_*","data":{},
   "error":{"message":"","detail":""},"trace_id":"","approval_ref":""}
For now it is a valid, importable skeleton returning a placeholder Message.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema import Message


class JsonEnvelopeComponent(Component):
    name = "JsonEnvelopeComponent"
    display_name = "JSON Envelope"
    description = (
        "Wraps a component/flow result in the canonical AR JSON envelope "
        "(§14): status, code, data, error, trace_id, approval_ref. Secrets are "
        "masked; raw exceptions are never leaked."
    )
    icon = "Braces"

    inputs = [
        MessageTextInput(
            name="status",
            display_name="Status",
            value="ok",
            info="One of: ok, error, pending_approval.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="code",
            display_name="Code",
            value="AR_OK",
            info="Stable AR_* code the caller branches on (§9 table).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="payload",
            display_name="Payload / Error",
            info="The data payload (on ok) or error message (on error).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="envelope_output",
            display_name="Envelope",
            method="format",
        ),
    ]

    def format(self) -> Message:
        status = (self.status or "ok").strip() or "ok"
        code = (self.code or "AR_OK").strip() or "AR_OK"
        payload = self.payload or ""
        # Placeholder — real envelope construction (§14) is filled in at build phase.
        return Message(text=f'{{"status":"{status}","code":"{code}","data":{payload}}}')