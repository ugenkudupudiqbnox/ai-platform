"""Calculation engine component (constitution §8).

Generic, reusable engine that runs a named financial calculation
(match / reconcile / aging / rounding) over inputs and emits a
``CalculationResult``. Scaffold only — pure Decimal logic at build phase;
amounts are 2-decimal strings (the project-wide rule). Never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class CalculationEngineComponent(Component):
    name = "CalculationEngineComponent"
    display_name = "Calculation Engine"
    description = (
        "Run a named financial calculation (match, reconcile, aging, or "
        "rounding) over inputs and return a CalculationResult. Call this to "
        "compute matched/outstanding/posted totals and aging buckets."
    )
    icon = "Calculator"

    inputs = [
        DropdownInput(
            name="calculation_type",
            display_name="Calculation",
            options=["match", "reconcile", "aging", "rounding"],
            value="match",
            info="The calculation to run (maps to the CalculationResult.calculation_type contract).",
            tool_mode=True,
        ),
        MultilineInput(
            name="inputs",
            display_name="Inputs (JSON)",
            info="JSON inputs for the calculation (e.g. a DocumentManifest of invoices + receipts).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="inputs_ref",
            display_name="Inputs Ref",
            info="Optional reference id of the input set (e.g. manifest_id), echoed in the result.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="currency",
            display_name="Currency",
            value="SAR",
            info="ISO-4217 currency for the computed totals.",
        ),
    ]

    outputs = [
        Output(
            name="calc_output",
            display_name="Calculation Result",
            method="calculate",
        ),
    ]

    def calculate(self) -> Message:
        calc_type = self.calculation_type or "match"
        # Placeholder — pure Decimal calculation is build phase. Emits the
        # CalculationResult contract. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"calculation_type":"{calc_type}","totals":{{}},"line_items":[]}}}}'
            )
        )