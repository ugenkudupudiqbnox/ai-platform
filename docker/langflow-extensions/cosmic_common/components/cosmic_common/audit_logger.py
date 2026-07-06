"""Audit logger component (constitution §13).

Generic, reusable append-only audit writer. ``append_only`` is a constant true
(§13 — audit records are immutable once written). Records ``actor`` from the
Keycloak ``sub`` claim (§13); ``before``/``after`` reference state by id, never
inlining PII (§12/§16). Scaffold only — at build phase it persists an
``AuditRecord`` (Postgres `audit` table via SQLAlchemy, build-phase dep); never
raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class AuditLoggerComponent(Component):
    name = "AuditLoggerComponent"
    display_name = "Audit Logger"
    description = (
        "Write an append-only AuditRecord. Call this for every state-changing "
        "or financially-material action. Records are immutable (append_only=true, "
        "§13). The AR-specific logger composes this base."
    )
    icon = "FileClock"

    # §13: audit is append-only. Surfaced as a constant so flow JSON cannot
    # silently make it mutable.
    append_only = True

    inputs = [
        MessageTextInput(
            name="actor",
            display_name="Actor",
            info="Keycloak `sub` of the user/service performing the action (§13). No name/email.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="action",
            display_name="Action",
            info="Stable action name, e.g. ar.invoice.post, ar.match.commit, ar.dunning.send.",
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="before",
            display_name="Before (JSON)",
            info="State before the action, referenced by id (no PII/secrets inlined, §12/§16).",
            tool_mode=True,
        ),
        MultilineInput(
            name="after",
            display_name="After (JSON)",
            info="State after the action, referenced by id.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="approval_ref",
            display_name="Approval Ref",
            info="The non-reusable approval_ref that authorized this action (§19), if any.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="idempotency_key",
            display_name="Idempotency Key",
            info="Idempotency key of the action (§10), for replay-safe dedup.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="trace_id",
            display_name="Trace ID",
            info="Correlation id propagated into the AuditRecord (§12).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="tenant",
            display_name="Tenant",
            info="Tenant identifier for multi-tenant isolation.",
            tool_mode=True,
        ),
        DropdownInput(
            name="source_system",
            display_name="Source System",
            options=["cosmic-ar-agent", "librechat", "langflow", "manual", "scheduled"],
            value="cosmic-ar-agent",
            info="Originating system of the audited action.",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="audit_output",
            display_name="Audit Record",
            method="write",
        ),
    ]

    def write(self) -> Message:
        action = self.action or "<action>"
        # Placeholder — Postgres append-only insert is build phase. The
        # AuditRecord contract is emitted; append_only is enforced as a
        # constant above. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"action":"{action}","append_only":true}}}}'
            )
        )