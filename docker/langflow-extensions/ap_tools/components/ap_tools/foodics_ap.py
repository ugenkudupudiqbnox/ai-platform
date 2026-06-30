"""FOODICS accounts-payable tool.

Packaged as part of the `ap_tools` Extension Bundle so it is a server-trusted
component (recognized on the public playground path) rather than inline custom
code. The API token input uses ``load_from_db=True`` so it resolves from a
LangFlow Secret Global Variable selected in the UI — the secret value never
lives in the flow JSON.
"""

from lfx.custom import Component
from lfx.io import DropdownInput, MessageTextInput, Output, SecretStrInput
from lfx.schema import Message

import requests


class FoodicsAPTool(Component):
    # Plain class name as the canonical `name` so the component is addressable
    # both as the bundle address (ext:ap_tools:FoodicsAPTool@extra) AND by the
    # bare class name. The latter is what the existing flow nodes use for
    # `data.type`, so the public-playground validator recognizes them without
    # requiring the flow's nodes to be re-added from the palette.
    name = "FoodicsAPTool"
    display_name = "FOODICS AP Tool"
    description = (
        "Fetches accounts payable data from FOODICS — specifically suppliers and "
        "purchase orders. Call this tool when the user asks about supplier information, "
        "purchase order status, outstanding procurement balances, or any accounts payable "
        "query that requires live FOODICS data."
    )
    icon = "Truck"

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
            info="Base URL for the FOODICS API (e.g., https://api.foodics.com/v2/).",
            required=True,
        ),
        DropdownInput(
            name="operation",
            display_name="Operation",
            options=[
                "list_suppliers",
                "get_supplier",
                "list_purchase_orders",
                "get_purchase_order",
            ],
            value="list_suppliers",
            info="The FOODICS operation to perform. Use list_* to browse all or get_* to fetch a specific entity by ID.",
            tool_mode=True,
        ),
        MessageTextInput(
            name="entity_id",
            display_name="Entity ID",
            info="ID of a specific supplier or purchase order (required for get_supplier and get_purchase_order).",
            tool_mode=True,
        ),
        MessageTextInput(
            name="query_params",
            display_name="Query Parameters",
            info="Optional filter parameters as key=value pairs separated by '&' (e.g., 'per_page=10&page=1').",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            name="ap_tool_output",
            display_name="AP Tool Result",
            method="fetch_foodics_data",
        ),
    ]

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.foodics_api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_url(self, endpoint: str) -> str:
        base = self.foodics_api_url.rstrip("/")
        return f"{base}/{endpoint}"

    def _make_request(self, endpoint: str) -> dict:
        headers = self._build_headers()
        params = {}
        if self.query_params:
            for pair in self.query_params.split("&"):
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    params[key.strip()] = val.strip()
        url = self._build_url(endpoint)
        self.log(f"Requesting FOODICS API: {url} with params={params}", name="FOODICS API")
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _format_results(self, data: dict, operation: str) -> str:
        lines = [f"FOODICS — Operation: {operation}"]

        if operation == "list_suppliers":
            suppliers = data.get("data", data.get("suppliers", []))
            if isinstance(suppliers, dict):
                suppliers = suppliers.get("data", [])
            lines.append(f"Total suppliers found: {len(suppliers)}")
            for s in suppliers:
                lines.append(
                    f"  • Supplier ID: {s.get('id') or s.get('supplier_id')} | "
                    f"Name: {s.get('name') or s.get('supplier_name')} | "
                    f"Code: {s.get('code')} | Balance: {s.get('balance')} | "
                    f"Status: {s.get('status') or s.get('is_active')}"
                )

        elif operation == "get_supplier":
            supplier = data.get("data", data)
            lines.append(
                f"Supplier ID: {supplier.get('id') or supplier.get('supplier_id')} | "
                f"Name: {supplier.get('name') or supplier.get('supplier_name')} | "
                f"Code: {supplier.get('code')} | Email: {supplier.get('email')} | "
                f"Phone: {supplier.get('phone')} | Balance: {supplier.get('balance')} | "
                f"Status: {supplier.get('status') or supplier.get('is_active')}"
            )

        elif operation == "list_purchase_orders":
            orders = data.get("data", data.get("purchase_orders", []))
            if isinstance(orders, dict):
                orders = orders.get("data", [])
            lines.append(f"Total purchase orders found: {len(orders)}")
            for o in orders:
                lines.append(
                    f"  • PO ID: {o.get('id') or o.get('purchase_order_id')} | "
                    f"Supplier: {o.get('supplier_name')} | "
                    f"Total: {o.get('total') or o.get('total_amount')} | "
                    f"Status: {o.get('status')} | Date: {o.get('date') or o.get('order_date')}"
                )

        elif operation == "get_purchase_order":
            order = data.get("data", data)
            lines.append(
                f"PO ID: {order.get('id') or order.get('purchase_order_id')} | "
                f"Supplier: {order.get('supplier_name')} | "
                f"Total: {order.get('total') or order.get('total_amount')} | "
                f"Status: {order.get('status')} | Date: {order.get('date') or order.get('order_date')}"
            )

        else:
            lines.append(str(data))

        return "\n".join(lines)

    def fetch_foodics_data(self) -> Message:
        operation = self.operation
        entity_id = self.entity_id.strip() if self.entity_id else ""

        endpoint_map = {
            "list_suppliers": "suppliers",
            "get_supplier": f"suppliers/{entity_id}" if entity_id else "suppliers",
            "list_purchase_orders": "purchase-orders",
            "get_purchase_order": f"purchase-orders/{entity_id}" if entity_id else "purchase-orders",
        }

        if operation.startswith("get_") and not entity_id:
            return Message(
                text=f"Error: An Entity ID is required for the '{operation}' operation. "
                f"Please provide the supplier or purchase order ID in the 'entity_id' field."
            )

        endpoint = endpoint_map.get(operation, "suppliers")

        try:
            data = self._make_request(endpoint)
            result_text = self._format_results(data, operation)
            return Message(text=result_text)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "N/A"
            body = e.response.text if e.response else str(e)
            return Message(text=f"FOODICS API Error (HTTP {status}): {body}")
        except requests.exceptions.ConnectionError as e:
            return Message(text=f"Connection error to FOODICS API: {e}")
        except requests.exceptions.Timeout:
            return Message(text="Request to FOODICS API timed out after 30 seconds.")
        except Exception as e:
            return Message(text=f"Unexpected error fetching data from FOODICS: {e}")