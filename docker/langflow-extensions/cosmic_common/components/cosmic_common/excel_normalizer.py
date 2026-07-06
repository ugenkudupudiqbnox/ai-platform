"""Excel normalizer component (constitution §8).

Generic, reusable normalizer that turns messy spreadsheet rows into typed rows:
header mapping, date coercion to ISO-8601, and amount coercion to 2-decimal
strings (the project-wide amount rule, see cosmic-ar/contracts/README.md).
Scaffold only — pure logic at build phase.
"""

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema import Message


class ExcelNormalizerComponent(Component):
    name = "ExcelNormalizerComponent"
    display_name = "Excel Normalizer"
    description = (
        "Normalize messy spreadsheet rows into typed rows: map headers, coerce "
        "dates to ISO-8601, and coerce amounts to 2-decimal strings. Call this "
        "right after a reader before matching or validation."
    )
    icon = "Eraser"

    inputs = [
        MultilineInput(
            name="raw_rows",
            display_name="Raw Rows (JSON)",
            info="JSON array of row objects as produced by a reader.",
            required=True,
            tool_mode=True,
        ),
        MultilineInput(
            name="header_map",
            display_name="Header Map (JSON)",
            info='JSON object mapping source columns to canonical names, e.g. {"Amt": "amount"}.',
            tool_mode=True,
        ),
        MultilineInput(
            name="amount_columns",
            display_name="Amount Columns",
            info="One column name per line to coerce to 2-decimal strings.",
            tool_mode=True,
        ),
        MultilineInput(
            name="date_columns",
            display_name="Date Columns",
            info="One column name per line to coerce to ISO-8601 dates.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="currency",
            display_name="Currency",
            value="SAR",
            info="ISO-4217 currency to stamp on monetary rows.",
        ),
    ]

    outputs = [
        Output(
            name="normalizer_output",
            display_name="Normalized Rows",
            method="normalize",
        ),
    ]

    def normalize(self) -> Message:
        # Placeholder — pure coercion logic is build phase. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                '"data":{"rows":[]}}'
            )
        )