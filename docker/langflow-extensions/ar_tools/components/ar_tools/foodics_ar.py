"""FOODICS accounts-receivable tool.

Packaged as part of the `ar_tools` Extension Bundle so it is a server-trusted
component (recognized on the public playground path) rather than inline custom
code. The API token input uses ``load_from_db=True`` so it resolves from a
LangFlow Secret Global Variable selected in the UI — the secret value never
lives in the flow JSON.

Scaffold only — no business logic. At build phase this will mirror
``FoodicsAPTool``'s request/retry behavior for the AR operations below (POS
receipts and sales, used to match against Zoho Books invoices).
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output, SecretStrInput
from lfx.schema import Message


class FoodicsARTool(Component):
    # Plain class name as the canonical `name` so the component is addressable
    # both as the bundle address (ext:ar_tools:FoodicsARTool@extra) AND by the
    # bare class name used by existing flow nodes for `data.type`.
    name = "FoodicsARTool"
    display_name = "FOODICS AR Tool"
    description = (
        "Fetches accounts receivable data from FOODICS — specifically POS "
        "receipts and sales used to match against Zoho Books invoices. Call "
        "this tool when the user asks about POS receipts, daily sales, or any "
        "accounts receivable query that requires live FOODICS data."
    )
    icon = "Receipt"

    inputs = [
        SecretStrInput(
            name="foodics_api_token",
            display_name="FOODICS API Token",
            info="API token for authenticating with the FOODICS API. Select the FOODICS_API_TOKEN Secret Global Variable.",
            required=True,
            load_from_db=True,
        ),
        MessageTextInput(
            name="foodics_api_url",
            display_name="FOODICS API URL",
            value="https://api.foodics.com/v2/",
            info="Base URL for the FOODICS API (e.g. https://api.foodics.com/v2/).",
            required=True,
        ),
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=[
                "list_receipts",
                "get_receipt",
                "list_sales",
            ],
            value="list_receipts",
            info="The FOODICS AR operation to perform. Use list_* to browse all or get_* to fetch a specific receipt by ID.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_id",
            display_name="Entity ID",
            info="ID of a specific receipt (required for get_receipt).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="query_params",
            display_name="Query Parameters",
            info="Optional filter parameters as key=value pairs separated by '&' (e.g. 'per_page=10&page=1').",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="ar_tool_output",
            display_name="AR Tool Result",
            method="fetch_foodics_data",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Build-phase wiring (placeholder — NOT implemented here)
    # ------------------------------------------------------------------ #
    # def _build_headers(self) -> dict: ...
    # def _make_request(self, endpoint: str) -> dict: ...
    # def _format_results(self, data: dict, operation: str) -> str: ...

    def fetch_foodics_data(self) -> Message:
        operation = self.operation
        entity_id = self.entity_id.strip() if self.entity_id else ""

        if operation and operation.startswith("get_") and not entity_id:
            return Message(
                text=f"Error: An Entity ID is required for the '{operation}' operation."
            )

        # Placeholder — real FOODICS AR calls (with §10 retry) are build phase.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"operation":"{operation}","entity_id":"{entity_id}"}}}}'
            )
        )