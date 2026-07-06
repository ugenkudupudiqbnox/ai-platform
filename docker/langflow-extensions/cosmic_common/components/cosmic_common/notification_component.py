"""Notification component (constitution §12, §16).

Generic, reusable notifier across email / sms / chat / in-app channels. Channel
credentials come ONLY from Secret Global Variables via
``SecretStrInput(..., load_from_db=True)`` (§16). Recipients are referenced by id
(``recipient_ref``), never by inlined name/email/phone (§12 PII rule); the
message body is referenced (``body_ref``), not inlined, so flow JSON never holds
customer content. Scaffold only — at build phase it renders the template against
referenced content and dispatches; never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, IntInput, MessageTextInput, MultilineInput, Output, SecretStrInput
from lfx.schema import Message


class NotificationComponent(Component):
    name = "NotificationComponent"
    display_name = "Notification"
    description = (
        "Send a notification on a chosen channel (email/sms/chat/in-app) from a "
        "template + referenced content. Call this for dunning reminders, "
        "approval requests, and run summaries. Recipients and body are "
        "referenced, not inlined (§12/§16)."
    )
    icon = "Bell"

    inputs = [
        DropdownInput(
            name="channel",
            display_name="Channel",
            options=["email", "sms", "chat", "in_app"],
            value="email",
            info="Delivery channel; selects the credential to use (§16).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="recipient_ref",
            display_name="Recipient Ref",
            info="Stable id of the recipient (no email/phone inlined, §12).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="template",
            display_name="Template",
            info="Named template to render (resolved from the Configuration Loader, §17).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="approval_ref",
            display_name="Approval Ref",
            info="Optional approval_ref this notification requests/records (§19).",
            tool_mode=True,
        ),
        IntInput(
            name="dunning_level",
            display_name="Dunning Level",
            value=0,
            info="Dunning escalation level (0 = none, 1..3 escalating reminders).",
        ),
        MessageTextInput(
            name="subject_ref",
            display_name="Subject Ref",
            info="Reference to the rendered subject (email/chat), by id not content.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="body_ref",
            display_name="Body Ref",
            info="Reference to the rendered body content, by id (no PII inlined, §16).",
            tool_mode=True,
        ),
        SecretStrInput(
            name="channel_secret",
            display_name="Channel Credential (Secret)",
            info="Channel credential stored as a Secret Global Variable (e.g. SMTP_*), §16.",
            required=True,
            load_from_db=True,
        ),
    ]

    outputs = [
        Output(
            name="notification_output",
            display_name="Notification",
            method="send",
        ),
    ]

    def send(self) -> Message:
        channel = self.channel or "email"
        # Placeholder — template render + channel dispatch is build phase.
        # Emits the Notification contract. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"channel":"{channel}","delivered":false}}}}'
            )
        )