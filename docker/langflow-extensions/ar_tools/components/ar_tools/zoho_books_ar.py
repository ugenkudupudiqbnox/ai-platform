"""Zoho Books accounts-receivable tool.

Packaged as part of the `ar_tools` Extension Bundle so it is a server-trusted
component (recognized on the public playground path) rather than inline custom
code. Credential inputs use ``load_from_db=True`` so they resolve from LangFlow
Secret Global Variables selected in the UI — the secret values never live in the
flow JSON.

Scaffold only — no business logic. At build phase this will mirror
``ZohoBooksAPTool``'s OAuth refresh-on-401 and request/retry behavior for the AR
operations below.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output, SecretStrInput
from lfx.schema import Message


class ZohoBooksARTool(Component):
    # Plain class name as the canonical `name` so the component is addressable
    # both as the bundle address (ext:ar_tools:ZohoBooksARTool@extra) AND by the
    # bare class name used by existing flow nodes for `data.type`.
    name = "ZohoBooksARTool"
    display_name = "Zoho Books AR Tool"
    description = (
        "Fetches accounts receivable data from Zoho Books — including invoices, "
        "customer contacts, and customer payments. Call this tool when the user "
        "asks about outstanding invoices, customer balances, payment status, or "
        "any accounts receivable query that requires live Zoho Books data."
    )
    icon = "BookOpen"

    inputs = [
        SecretStrInput(
            name="zoho_client_id",
            display_name="Zoho Client ID",
            info="Client ID of your Zoho OAuth app. Select the ZOHO_CLIENT_ID Secret Global Variable.",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_client_secret",
            display_name="Zoho Client Secret",
            info="Client secret of your Zoho OAuth app. Select the ZOHO_CLIENT_SECRET Secret Global Variable.",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_refresh_token",
            display_name="Zoho Refresh Token",
            info="Long-lived refresh token used to auto-generate fresh access tokens. Select the ZOHO_REFRESH_TOKEN Secret Global Variable.",
            required=True,
            load_from_db=True,
        ),
        MessageTextInput(
            name="organization_id",
            display_name="Zoho Books Organization ID",
            info="The organization ID in Zoho Books. Select the ZOHO_ORG_ID Global Variable or paste the ID.",
            required=True,
        ),
        MessageTextInput(
            name="zoho_books_api_url",
            display_name="Zoho Books API URL",
            value="https://www.zohoapis.com/books/v3/",
            info="Base URL for Zoho Books API (e.g. https://www.zohoapis.com/books/v3/).",
            required=True,
        ),
        MessageTextInput(
            name="zoho_accounts_url",
            display_name="Zoho Accounts URL",
            value="https://accounts.zoho.com",
            info="Base URL for Zoho Accounts OAuth endpoint (e.g. https://accounts.zoho.com).",
            required=True,
        ),
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=[
                "list_invoices",
                "get_invoice",
                "list_customers",
                "get_customer",
                "list_customer_payments",
                "get_customer_payment",
            ],
            value="list_invoices",
            info="The Zoho Books AR operation to perform. Use list_* to browse all or get_* to fetch a specific entity by ID.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_id",
            display_name="Entity ID",
            info="ID of a specific invoice, customer, or customer payment (required for get_* operations).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="query_params",
            display_name="Query Parameters",
            info="Optional filter parameters as key=value pairs separated by '&' (e.g. 'status=open&limit=10').",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="ar_tool_output",
            display_name="AR Tool Result",
            method="fetch_zoho_data",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  Build-phase wiring (placeholder — NOT implemented here)
    # ------------------------------------------------------------------ #
    # def _refresh_access_token(self) -> str: ...   # OAuth refresh (mirrors ZohoBooksAPTool)
    # def _make_request(self, endpoint: str) -> dict: ...  # + re-refresh once on 401
    # def _format_results(self, data: dict, operation: str) -> str: ...

    def fetch_zoho_data(self) -> Message:
        operation = self.operation
        entity_id = self.entity_id.strip() if self.entity_id else ""

        if operation and operation.startswith("get_") and not entity_id:
            return Message(
                text=f"Error: An Entity ID is required for the '{operation}' operation."
            )

        # Placeholder — real Zoho Books AR calls (with OAuth refresh-on-401 and
        # §10 retry) are filled in at build phase.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"operation":"{operation}","entity_id":"{entity_id}"}}}}'
            )
        )