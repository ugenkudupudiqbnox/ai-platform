"""Idempotency-key component (constitution §10, architecture §3/§10).

Scaffold only. At build phase this derives a stable idempotency key for any
financial POST and replays the same key on retry/resume so the upstream
deduplicates. It also drives the retry/backoff loop (3 attempts, exp backoff
±25% jitter, <=30s window, no 4xx retry except 408/429, 401 re-credential once;
exhausted financial retry -> pending_approval, never silent). For now it is a
valid, importable skeleton.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema import Message


class IdempotencyKeyComponent(Component):
    name = "IdempotencyKeyComponent"
    display_name = "Idempotency Key"
    description = (
        "Derives and replays a stable idempotency key for any financial POST "
        "(§10) so retries are deduplicated by the upstream, and drives the "
        "retry/backoff loop with full jitter."
    )
    icon = "Key"

    inputs = [
        MessageTextInput(
            name="action",
            display_name="Action",
            info="The action being made idempotent (e.g. gl.post, invoice.issue).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_ref",
            display_name="Entity Reference",
            info="Stable reference (invoice id + amount) that seeds the key.",
            required=True,
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="idempotency_output",
            display_name="Idempotency Key",
            method="derive_key",
        ),
    ]

    def derive_key(self) -> Message:
        # Placeholder — real key derivation (hash of action+entity_ref+tenant) is build phase (§10).
        action = self.action or "action"
        entity = self.entity_ref or "entity"
        return Message(text=f"ar-idem:{action}:{entity}:<placeholder>")