"""Checkpoint component (constitution §11, architecture §3/§11).

Scaffold only. At build phase this wraps a thin custom ``BaseCheckpointSaver``
over SQLAlchemy, writing the full ``AgentState`` + intended next action +
idempotency key + tool-call ref to the ``ar_checkpoints`` table in the
``ar_agent`` Postgres DB. It is the source of truth for resume because Langfuse
tracing is currently disabled (``LANGFLOW_DEACTIVATE_TRACING=true``, §11 caveat).
For now it is a valid, importable skeleton.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, Output
from lfx.schema import Message


class CheckpointComponent(Component):
    name = "CheckpointComponent"
    display_name = "Checkpoint"
    description = (
        "Saves/loads AgentState at the §11 boundaries (after approval gates, "
        "before any financial POST, after each reconciled batch) to a "
        "Postgres-backed checkpointer — the resume source of truth while "
        "Langfuse tracing is gated (§11 caveat)."
    )
    icon = "Save"

    inputs = [
        MessageTextInput(
            name="mode",
            display_name="Mode",
            value="save",
            info="save (write a checkpoint) or load (resume from a checkpoint_id).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="checkpoint_id",
            display_name="Checkpoint ID",
            info="On load: the resume handle. On save: ignored (a new id is minted).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="checkpoint_output",
            display_name="Checkpoint Result",
            method="handle",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Build-phase wiring (placeholder — NOT implemented here)
    # ------------------------------------------------------------------ #
    # class _PostgresCheckpointSaver(BaseCheckpointSaver):
    #     """SQLAlchemy-backed saver into ar_agent.ar_checkpoints. Falls back to
    #     langgraph.checkpoint.memory.MemorySaver if the DB is unavailable."""
    #     ...

    def handle(self) -> Message:
        mode = (self.mode or "save").strip() or "save"
        # Placeholder — real saver/loader is build phase (§11).
        return Message(text=f'{{"status":"ok","code":"AR_CHECKPOINT_{mode.upper()}","checkpoint_id":"<placeholder>"}}')