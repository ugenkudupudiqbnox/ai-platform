"""Human-approval gate component (constitution §19, architecture §3).

Scaffold only. At build phase this captures/fulfills approvals: any financial
mutation requires tier >= `approval`; `dual-control` above ceilings; an approval
is non-reusable (one approval_ref -> exactly one idempotent action). It returns
`pending_approval` in the §14 envelope and writes a checkpoint (§11). For now it
is a valid, importable skeleton.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output
from lfx.schema import Message


class ApprovalGateComponent(Component):
    name = "ApprovalGateComponent"
    display_name = "Approval Gate"
    description = (
        "Gates financial mutations behind human approval (§19). Returns "
        "pending_approval until fulfilled; captures the approver's Keycloak sub "
        "as the actor of record. Tiers: read-only, auto, approval, dual-control."
    )
    icon = "ShieldCheck"

    inputs = [
        DropdownInput(
            name="tier",
            display_name="Approval Tier",
            options=["read-only", "auto", "approval", "dual-control"],
            value="approval",
            info="Action tier per §19. Any financial mutation is at least `approval`.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="action",
            display_name="Action",
            info="The action being authorized (e.g. gl.post, invoice.issue).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="amount",
            display_name="Amount",
            info="Financial amount of the action (drives dual-control ceiling).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="approval_ref",
            display_name="Approval Ref",
            info="Fulfilled approval reference on resume; empty on first request.",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="approval_output",
            display_name="Approval Result",
            method="request_approval",
        ),
    ]

    def request_approval(self) -> Message:
        # Placeholder — real tier gating / checkpoint / non-reuse is build phase (§19).
        return Message(
            text=(
                '{"status":"pending_approval","code":"AR_APPROVAL_REQUIRED",'
                f'"approval_ref":"","action":"{self.action or ""}"}}'
            )
        )