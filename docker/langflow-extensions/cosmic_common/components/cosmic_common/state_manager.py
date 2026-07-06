"""State manager component (constitution §8).

Generic, reusable manager over the typed ``AgentState``. Per §8, state is
immutable — `set`/`merge` return new snapshots rather than mutating in place.
This is the generic base the supervisor and sub-agents compose (the AR-specific
``AgentState`` lives in ``ar_common``); it deliberately carries no AR-specific
fields. Scaffold only — pure dict/datetime logic at build phase; never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class StateManagerComponent(Component):
    name = "StateManagerComponent"
    display_name = "State Manager"
    description = (
        "Get, set, merge, or snapshot the typed AgentState immutably (§8). Call "
        "this to evolve run state without in-place mutation; the result is a "
        "new state object the caller threads forward."
    )
    icon = "Workflow"

    inputs = [
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=["get", "set", "merge", "snapshot"],
            value="get",
            info="get = read a key; set = replace a key; merge = deep-merge a fragment; snapshot = copy whole state.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="state_ref",
            display_name="State Ref",
            info="Reference id of the current AgentState to read/evolve.",
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="fragment",
            display_name="Fragment (JSON)",
            info="JSON fragment to set/merge into the state (set replaces the named key; merge deep-merges).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="trace_id",
            display_name="Trace ID",
            info="Correlation id propagated into the new state (§12).",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="state_output",
            display_name="State Result",
            method="manage",
        ),
    ]

    def manage(self) -> Message:
        operation = self.operation or "get"
        # Placeholder — immutable state evolution is build phase (§8). Emits a
        # state envelope shaped like AgentState. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"operation":"{operation}","state":{{}}}}}}'
            )
        )