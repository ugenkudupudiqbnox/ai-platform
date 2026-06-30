"""Zoho Books accounts-payable tool.

Packaged as part of the `ap_tools` Extension Bundle so it is a server-trusted
component (recognized on the public playground path) rather than inline custom
code. Credential inputs use ``load_from_db=True`` so they resolve from LangFlow
Secret Global Variables selected in the UI — the secret values never live in the
flow JSON.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output, SecretStrInput
from lfx.schema import Message

import requests


class ZohoBooksAPTool(Component):
    # Plain class name as the canonical `name` so the component is addressable
    # both as the bundle address (ext:ap_tools:ZohoBooksAPTool@extra) AND by the
    # bare class name. The latter is what the existing flow nodes use for
    # `data.type`, so the public-playground validator recognizes them without
    # requiring the flow's nodes to be re-added from the palette.
    name = "ZohoBooksAPTool"
    display_name = "Zoho Books AP Tool"
    description = (
        "Fetches accounts payable data from Zoho Books — including bills, vendors, "
        "and payments. Automatically refreshes the OAuth access token using the "
        "provided client ID, client secret, and refresh token. Call this tool when "
        "the user asks about outstanding bills, vendor information, payment status, "
        "or any accounts payable query that requires live Zoho Books data."
    )
    icon = "BookOpen"

    inputs = [
        SecretStrInput(
            name="zoho_client_id",
            display_name="Zoho Client ID",
            info="Client ID of your Zoho OAuth app (from Zoho API Console). Select the ZOHO_CLIENT_ID Secret Global Variable.",
            required=True,
            load_from_db=True,
        ),
        SecretStrInput(
            name="zoho_client_secret",
            display_name="Zoho Client Secret",
            info="Client secret of your Zoho OAuth app (from Zoho API Console). Select the ZOHO_CLIENT_SECRET Secret Global Variable.",
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
            info="The organization ID in Zoho Books (found in Zoho Books Settings). Select the ZOHO_ORG_ID Global Variable or paste the ID.",
            required=True,
        ),
        MessageTextInput(
            name="zoho_books_api_url",
            display_name="Zoho Books API URL",
            value="https://www.zohoapis.com/books/v3/",
            info="Base URL for Zoho Books API (e.g., https://www.zohoapis.com/books/v3/).",
            required=True,
        ),
        MessageTextInput(
            name="zoho_accounts_url",
            display_name="Zoho Accounts URL",
            value="https://accounts.zoho.com",
            info="Base URL for Zoho Accounts OAuth endpoint (e.g., https://accounts.zoho.com).",
            required=True,
        ),
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=[
                "list_bills",
                "get_bill",
                "list_vendors",
                "get_vendor",
                "list_payments",
                "get_payment",
            ],
            value="list_bills",
            info="The Zoho Books operation to perform. Use list_* to browse all or get_* to fetch a specific entity by ID.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_id",
            display_name="Entity ID",
            info="ID of a specific bill, vendor, or payment (required for get_bill, get_vendor, get_payment).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="query_params",
            display_name="Query Parameters",
            info="Optional filter parameters as key=value pairs separated by '&' (e.g., 'status=open&limit=10').",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="ap_tool_output",
            display_name="AP Tool Result",
            method="fetch_zoho_data",
        ),
    ]

    # ------------------------------------------------------------------ #
    #  OAuth token management
    # ------------------------------------------------------------------ #

    def _refresh_access_token(self) -> str:
        """Use refresh token + client credentials to obtain a fresh access token."""
        token_url = f"{self.zoho_accounts_url.rstrip('/')}/oauth/v2/token"

        self.log(
            f"Refreshing access token from: {token_url}",
            name="Zoho OAuth Refresh",
        )

        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self.zoho_client_id,
                "client_secret": self.zoho_client_secret,
                "refresh_token": self.zoho_refresh_token,
            },
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Token refresh failed (HTTP {response.status_code}): {response.text}"
            )

        token_data = response.json()
        new_token = token_data.get("access_token")

        if not new_token:
            raise RuntimeError(
                f"Token refresh returned no access_token. Response: {token_data}"
            )

        self.log("Successfully refreshed access token.", name="Zoho OAuth Refresh")
        return new_token

    def _get_access_token(self) -> str:
        """Return a valid access token by refreshing via OAuth."""
        return self._refresh_access_token()

    # ------------------------------------------------------------------ #
    #  Request helpers
    # ------------------------------------------------------------------ #

    def _build_headers(self, access_token: str) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        }

    def _build_url(self, endpoint: str) -> str:
        base = self.zoho_books_api_url.rstrip("/")
        return f"{base}/{endpoint}"

    def _make_request(self, endpoint: str) -> dict:
        access_token = self._get_access_token()
        headers = self._build_headers(access_token)
        params = {"organization_id": self.organization_id}
        if self.query_params:
            for pair in self.query_params.split("&"):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    params[key.strip()] = val.strip()
        url = self._build_url(endpoint)
        self.log(f"Requesting Zoho Books: {url} with params={params}", name="Zoho Books API")
        response = requests.get(url, headers=headers, params=params, timeout=30)

        # If the token expired mid-request, refresh once and retry
        if response.status_code == 401:
            self.log("Access token expired during request. Attempting re-refresh...", name="Zoho Books API")
            access_token = self._get_access_token()
            headers = self._build_headers(access_token)
            response = requests.get(url, headers=headers, params=params, timeout=30)

        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------ #
    #  Result formatting
    # ------------------------------------------------------------------ #

    def _format_results(self, data: dict, operation: str) -> str:
        lines = [f"Zoho Books — Operation: {operation}"]

        if operation == "list_bills":
            bills = data.get("bills", [])
            lines.append(f"Total bills found: {len(bills)}")
            for b in bills:
                lines.append(
                    f"  • Bill ID: {b.get('bill_id')} | Vendor: {b.get('vendor_name')} | "
                    f"Amount: {b.get('total')} | Status: {b.get('status')} | Due: {b.get('due_date')}"
                )

        elif operation == "get_bill":
            bill = data
            lines.append(
                f"Bill ID: {bill.get('bill_id')} | Vendor: {bill.get('vendor_name')} | "
                f"Amount: {bill.get('total')} | Balance: {bill.get('balance')} | "
                f"Status: {bill.get('status')} | Due: {bill.get('due_date')}"
            )

        elif operation == "list_vendors":
            vendors = data.get("contacts", [])
            lines.append(f"Total vendors found: {len(vendors)}")
            for v in vendors:
                lines.append(
                    f"  • Vendor ID: {v.get('contact_id')} | Name: {v.get('contact_name')} | "
                    f"Payable: {v.get('outstanding_payable')} | Status: {v.get('status')}"
                )

        elif operation == "get_vendor":
            vendor = data
            lines.append(
                f"Vendor ID: {vendor.get('contact_id')} | Name: {vendor.get('contact_name')} | "
                f"Email: {vendor.get('email')} | Phone: {vendor.get('phone')} | "
                f"Payable: {vendor.get('outstanding_payable')} | Status: {vendor.get('status')}"
            )

        elif operation == "list_payments":
            payments = data.get("payments", [])
            lines.append(f"Total payments found: {len(payments)}")
            for p in payments:
                lines.append(
                    f"  • Payment ID: {p.get('payment_id')} | Amount: {p.get('amount')} | "
                    f"Date: {p.get('date')} | Mode: {p.get('payment_mode')}"
                )

        elif operation == "get_payment":
            payment = data
            lines.append(
                f"Payment ID: {payment.get('payment_id')} | Amount: {payment.get('amount')} | "
                f"Date: {payment.get('date')} | Mode: {payment.get('payment_mode')} | "
                f"Description: {payment.get('description')}"
            )

        else:
            lines.append(str(data))

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Main output method
    # ------------------------------------------------------------------ #

    def fetch_zoho_data(self) -> Message:
        operation = self.operation
        entity_id = self.entity_id.strip() if self.entity_id else ""

        endpoint_map = {
            "list_bills": "bills",
            "get_bill": f"bills/{entity_id}" if entity_id else "bills",
            "list_vendors": "contacts",
            "get_vendor": f"contacts/{entity_id}" if entity_id else "contacts",
            "list_payments": "payments",
            "get_payment": f"payments/{entity_id}" if entity_id else "payments",
        }

        if operation.startswith("get_") and not entity_id:
            return Message(
                text=f"Error: An Entity ID is required for the '{operation}' operation. "
                f"Please provide the bill/vendor/payment ID in the 'entity_id' field."
            )

        endpoint = endpoint_map.get(operation, "bills")

        try:
            data = self._make_request(endpoint)
            result_text = self._format_results(data, operation)
            return Message(text=result_text)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "N/A"
            body = e.response.text if e.response else str(e)
            return Message(text=f"Zoho Books API Error (HTTP {status}): {body}")
        except requests.exceptions.ConnectionError as e:
            return Message(text=f"Connection error to Zoho Books API: {e}")
        except requests.exceptions.Timeout:
            return Message(text="Request to Zoho Books API timed out after 30 seconds.")
        except Exception as e:
            return Message(text=f"Unexpected error fetching data from Zoho Books: {e}")