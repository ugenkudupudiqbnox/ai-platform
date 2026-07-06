"""Checkpoint manager component (constitution §11).

Generic, reusable checkpoint save/load/list over the agent state. Per §11,
checkpoints must record enough state to resume WITHOUT relying on Langfuse
spans (tracing is currently OFF — ``LANGFLOW_DEACTIVATE_TRACING=true``). The
build-phase backend is a Postgres saver (``langgraph-checkpoint-postgres``, a
build-phase Dockerfile dep) against the ``ar_agent`` database; MemorySaver is the
documented fallback for local/dev. The AR-specific checkpoint composes this base
(see ``ar_common.CheckpointComponent``). Scaffold only; never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class CheckpointManagerComponent(Component):
    name = "CheckpointManagerComponent"
    display_name = "Checkpoint Manager"
    description = (
        "Save, load, or list agent-state checkpoints so a run can resume after "
        "interruption. Call this at every durable boundary (§11). Checkpoints "
        "are self-sufficient — they do not depend on tracing spans."
    )
    icon = "DatabaseBackup"

    inputs = [
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=["save", "load", "list"],
            value="save",
            info="save = persist current state; load = resume from a checkpoint_id; list = enumerate checkpoints.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="checkpoint_id",
            display_name="Checkpoint ID",
            info="Checkpoint id to load; omit for save/list to generate/enumerate.",
            tool_mode=True,
        ),
        MultilineInput(
            name="agent_state",
            display_name="Agent State (JSON)",
            info="Full AgentState JSON to persist (save). Must be self-sufficient (§11).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="trace_id",
            display_name="Trace ID",
            info="Correlation id propagated into the checkpoint record (§12).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="checkpoint_output",
            display_name="Checkpoint Result",
            method="manage",
        ),
    ]

    def manage(self) -> Message:
        operation = self.operation or "save"
        # Placeholder — Postgres saver (langgraph-checkpoint-postgres, build
        # phase) or MemorySaver fallback. Emits a checkpoint envelope. Never
        # raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"operation":"{operation}","checkpoint_id":null}}}}'
            )
        )