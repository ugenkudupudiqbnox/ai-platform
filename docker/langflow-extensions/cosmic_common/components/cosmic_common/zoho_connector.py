"""Zoho connector component (constitution §10, §16).

Generic, reusable CRUD base for the Zoho Finance Suite API. Credentials come ONLY
from Secret Global Variables via ``SecretStrInput(..., load_from_db=True)``
(§16) — never hard-coded, never in flow JSON. At build phase it implements §10
retry (3 attempts, exponential backoff, idempotency-keyed) + §16 SSRF guards +
OAuth refresh-on-401 over ``requests`` (in-image). The AR-specific ops compose
this base (see ``ar_tools.ZohoBooksARTool``). Scaffold only; never raises.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output, SecretStrInput
from lfx.schema import Message


class ZohoConnectorComponent(Component):
    name = "ZohoConnectorComponent"
    display_name = "Zoho Connector"
    description = (
        "Generic CRUD client for the Zoho Finance Suite API. Call this to read "
        "or write Zoho entities; AR-specific operations compose it. Credentials "
        "are resolved from Secret Global Variables only (§16)."
    )
    icon = "Plug"

    inputs = [
        SecretStrInput(
            name="zoho_client_id",
            display_name="Zoho Client ID",
            info="OAuth client id stored as a Secret Global Variable (§16).",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_client_secret",
            display_name="Zoho Client Secret",
            info="OAuth client secret stored as a Secret Global Variable (§16).",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_refresh_token",
            display_name="Zoho Refresh Token",
            info="OAuth refresh token stored as a Secret Global Variable (§16).",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_access_token",
            display_name="Zoho Access Token (optional)",
            info="Cached access token if present; refreshed on 401 otherwise. Secret Global Variable (§16).",
            load_from_db=True,
        ),
        MessageTextInput(
            name="organization_id",
            display_name="Organization ID",
            info="Zoho organization id for the request.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="api_url",
            display_name="API URL",
            info="Base API URL (e.g. https://www.zohoapis.com).",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="accounts_url",
            display_name="Accounts URL",
            info="OAuth accounts URL for token refresh (e.g. https://accounts.zoho.com).",
            required=True,
            tool_mode=True,
        ),
        DropdownInput(
            name="resource",
            display_name="Resource",
            options=["invoices", "creditnotes", "customerpayments", "customers", "contacts", "chartofaccounts"],
            value="invoices",
            info="Zoho resource path segment.",
            tool_mode=True,
        ),
        DropdownInput(
            name="method",
            display_name="Method",
            options=["GET", "POST", "PUT"],
            value="GET",
            info="HTTP method. POST/PUT mutate state and require §19 approval upstream.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_id",
            display_name="Entity ID",
            info="Optional entity id for single-resource GET/PUT.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="query_params",
            display_name="Query Params (JSON)",
            info='Optional JSON object of query parameters, e.g. {"contact_id":"..."}.',
            tool_mode=True,
        ),
        MultilineInput(
            name="body",
            display_name="Body (JSON)",
            info="Optional JSON body for POST/PUT.",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="zoho_output",
            display_name="Zoho Result",
            method="call",
        ),
    ]

    def call(self) -> Message:
        method = self.method or "GET"
        resource = self.resource or "invoices"
        # Placeholder — §10 retry + §16 SSRF + OAuth refresh-on-401 is build
        # phase over `requests` (in-image). Emits ZohoUploadResult for writes or
        # a fetch envelope for reads. Never raises.
        return Message(
            text=(
                '{"status":"ok","code":"AR_NOT_IMPLEMENTED",'
                f'"data":{{"method":"{method}","resource":"{resource}","http_status":0}}}}'
            )
        )