"""Audit-record component (constitution §13, architecture §3/§13).

Scaffold only. At build phase this writes an immutable audit record for every
financial action: actor (Keycloak sub from the SSO session), action, before/after
state delta, UTC timestamp, approval_ref, trace_id, and idempotency_key. The
financial source system (Zoho Books / Foodics) remains the primary system of
record; this is the intent-and-attribution layer. For now it is a valid,
importable skeleton.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema import Message


class AuditRecordComponent(Component):
    name = "AuditRecordComponent"
    display_name = "Audit Record"
    description = (
        "Writes the immutable audit record for a financial action (§13): actor "
        "(Keycloak sub), action, before/after, timestamp, approval_ref, trace_id, "
        "idempotency_key. Append-only — correction is a compensating entry."
    )
    icon = "FileClock"

    inputs = [
        MessageTextInput(
            name="actor",
            display_name="Actor",
            info="Keycloak sub of the approver/actor of record (from the SSO session).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="action",
            display_name="Action",
            info="What was done (e.g. gl.post, invoice.issue, refund.issue).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="approval_ref",
            display_name="Approval Ref",
            info="The §19 approval that authorized this action.",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="audit_output",
            display_name="Audit Result",
            method="record",
        ),
    ]

    def record(self) -> Message:
        # Placeholder — real append-only audit write is build phase (§13).
        action = self.action or "action"
        return Message(text=f'{{"status":"ok","code":"AR_AUDITED","action":"{action}","audit_ref":"<placeholder>"}}')