"""Real Zoho Books transport for the AR ``ar_issue_invoice`` subflow (build-phase).

This is the build-phase swap-in for ``StubZohoUpload`` (``zoho_upload_flow.py``)
wired via ``set_transport(RealZoho(creds))`` from ``ZohoUploadFlowComponent.run``.
It performs **real** HTTP against Zoho Books (OAuth refresh-on-401 + POST
``/invoices`` + DELETE ``/invoices/{id}``), mirroring the working pattern in
``ap_tools/components/ap_tools/zoho_books_ap.py`` (``_refresh_access_token`` /
``_build_headers`` / ``organization_id``-as-query-param / 401→re-refresh→retry).

It is **pure Python** (no lfx import) so it is offline-testable and stays out of
the lfx build path; the subflow component resolves the credentials itself (it
carries ``user_id``; see ``vendor_secrets.py``) and passes them to the
constructor as a plain dict.

Transport contract (matches ``StubZohoUpload`` / consumed by the flow's §10 retry
loop ``_retry_loop`` / ``_retry_loop_delete`` in ``zoho_upload_flow.py``):

  create_invoice(invoice, idempotency_key) -> dict with keys
      ok, http_status, code, zoho_id, zoho_ref, duplicate, transient
  delete_invoice(zoho_id) -> dict with keys
      ok, http_status, code, transient

The flow retries results where ``http_status`` is 408/429/5xx (transient), stops
on success codes (``AR_OK`` / ``AR_DUPLICATE``), and does not retry hard 4xx.

Zoho Books v3 invoice-POST specifics (verify against the live sandbox):
  * Success: HTTP 200/201 with JSON body ``{"code": 0, "invoice": {...}}``.
  * Duplicate ``invoice_number``: a non-zero body ``code`` + a message containing
    "already exists" → idempotent replay → ``AR_DUPLICATE`` (success code).
  * ``organization_id`` is a **query param** (not body/header) — mirrors the AP tool.
  * Idempotency: Zoho has no standard idempotency-key header for invoice
    creation; de-dup is by unique ``invoice_number`` + duplicate detection. The
    flow's ``idempotency_key`` is carried in the result dict for audit only.

The output methods **do not raise** for ordinary API errors — they return a
transient/hard result dict so the flow's §10 loop owns retry/backoff (§10).
Only an unrecoverable OAuth-refresh failure returns a hard ``AR_AUTH``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Result codes — mirror ``zoho_upload_flow.py`` (kept local to avoid a circular
# import; the subflow module imports RealZoho, so this module must not import it).
CODE_OK: str = "AR_OK"
CODE_DUPLICATE: str = "AR_DUPLICATE"
CODE_UPSTREAM: str = "AR_UPSTREAM"
CODE_AUTH: str = "AR_AUTH"
CODE_VALIDATION: str = "AR_VALIDATION"
CODE_FORBIDDEN: str = "AR_FORBIDDEN"
CODE_NOT_FOUND: str = "AR_NOT_FOUND"
CODE_UNEXPECTED: str = "AR_UNEXPECTED"

REQUEST_TIMEOUT = 30  # seconds (mirrors ap_tools)


def _to_number(value: Any) -> float:
    """Coerce a 2dp money string (or number) to a float for Zoho's API."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _is_transient_status(status: int) -> bool:
    """§10 transient = 408/429/5xx (retry); 4xx otherwise is hard (no retry)."""
    return status in (408, 429) or 500 <= status < 600


class RealZoho:
    """Real Zoho Books transport (OAuth refresh-on-401 + invoice POST/DELETE).

    Construct with a ``creds`` dict of resolved Secret Global Variables:

        {
          "client_id": ..., "client_secret": ..., "refresh_token": ...,
          "organization_id": ...,
          "books_api_url": "https://www.zohoapis.com/books/v3/",
          "accounts_url":   "https://accounts.zoho.com",
        }

    The subflow only constructs this when the required creds are present
    (``read_secret`` returned non-``None``); otherwise it stays on
    ``StubZohoUpload`` so offline/no-creds runs are unaffected.
    """

    def __init__(self, creds: dict[str, Any]):
        self.client_id = str(creds.get("client_id") or "")
        self.client_secret = str(creds.get("client_secret") or "")
        self.refresh_token = str(creds.get("refresh_token") or "")
        self.organization_id = str(creds.get("organization_id") or "")
        self.books_api_url = str(creds.get("books_api_url") or
                                 "https://www.zohoapis.com/books/v3/")
        self.accounts_url = str(creds.get("accounts_url") or
                               "https://accounts.zoho.com")
        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  OAuth token management (mirrors ap_tools/zoho_books_ap.py:117-155)
    # ------------------------------------------------------------------ #

    def _refresh_access_token(self) -> str:
        token_url = f"{self.accounts_url.rstrip('/')}/oauth/v2/token"
        logger.info("Zoho: refreshing access token from %s", token_url)
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zoho token refresh failed (HTTP {response.status_code}): "
                f"{response.text}"
            )
        token_data = response.json()
        new_token = token_data.get("access_token")
        if not new_token:
            raise RuntimeError(
                f"Zoho token refresh returned no access_token: {token_data}"
            )
        self._access_token = new_token
        return new_token

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {self._access_token}",
            "Content-Type": "application/json",
        }

    def _api_url(self, endpoint: str) -> str:
        return f"{self.books_api_url.rstrip('/')}/{endpoint.lstrip('/')}"

    def _org_params(self) -> dict:
        return {"organization_id": self.organization_id}

    # ------------------------------------------------------------------ #
    #  Invoice <-> Zoho body mapping
    # ------------------------------------------------------------------ #

    @staticmethod
    def _invoice_to_zoho_body(invoice: dict) -> dict:
        """Map an ``InvoiceData`` dict to a Zoho Books ``POST /invoices`` body.

        ``InvoiceData`` (invoice-data.schema.json) required fields: invoice_id,
        invoice_number, customer_ref, tenant, issue_date, due_date, line_items,
        subtotal, total, currency, status, balance_due, contract_version.
        Each ``line_item``: line_id, item_ref, description, qty, unit_price,
        amount (the last three are 2dp strings). Zoho expects quantity/rate as
        numbers and the customer as ``customer_id``.
        """
        invoice = invoice or {}
        line_items = []
        for li in invoice.get("line_items") or []:
            if not isinstance(li, dict):
                continue
            item: dict[str, Any] = {
                "name": str(li.get("description") or ""),
                "description": str(li.get("description") or ""),
                "quantity": _to_number(li.get("qty")),
                "rate": _to_number(li.get("unit_price")),
            }
            item_ref = str(li.get("item_ref") or "").strip()
            if item_ref:
                # item_ref is a Zoho item id/SKU — send as item_id when present.
                item["item_id"] = item_ref
            line_items.append(item)
        body: dict[str, Any] = {
            "customer_id": str(invoice.get("customer_ref") or ""),
            "invoice_number": str(invoice.get("invoice_number") or ""),
            "date": str(invoice.get("issue_date") or ""),
            "due_date": str(invoice.get("due_date") or ""),
            "currency_code": str(invoice.get("currency") or ""),
            "line_items": line_items,
        }
        notes = str(invoice.get("notes") or "").strip()
        if notes:
            body["notes"] = notes
        # po_number intentionally omitted pending Zoho v3 field-name verification.
        return body

    # ------------------------------------------------------------------ #
    #  Transport contract
    # ------------------------------------------------------------------ #

    def create_invoice(self, invoice: dict, idempotency_key: str) -> dict:
        """POST the invoice to Zoho Books → result dict (StubZohoUpload shape)."""
        if not self._access_token:
            try:
                self._refresh_access_token()
            except Exception as exc:  # noqa: BLE001 — surface as hard AR_AUTH
                logger.error("Zoho create_invoice: auth refresh failed: %s", exc)
                return {"ok": False, "http_status": 401, "code": CODE_AUTH,
                        "zoho_id": "", "zoho_ref": "", "duplicate": False,
                        "transient": False, "error": str(exc)}

        url = self._api_url("invoices")
        body = self._invoice_to_zoho_body(invoice)
        params = self._org_params()
        try:
            response = requests.post(url, json=body, headers=self._build_headers(),
                                      params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            # Network error → transient (the §10 loop retries).
            logger.warning("Zoho create_invoice: network error: %s", exc)
            return {"ok": False, "http_status": 0, "code": CODE_UPSTREAM,
                    "zoho_id": "", "zoho_ref": "", "duplicate": False,
                    "transient": True, "error": str(exc)}

        # 401 → refresh once and retry the POST (mirrors ap_tools).
        if response.status_code == 401:
            try:
                self._refresh_access_token()
            except Exception as exc:  # noqa: BLE001
                logger.error("Zoho create_invoice: re-refresh failed: %s", exc)
                return {"ok": False, "http_status": 401, "code": CODE_AUTH,
                        "zoho_id": "", "zoho_ref": "", "duplicate": False,
                        "transient": False, "error": str(exc)}
            try:
                response = requests.post(url, json=body, headers=self._build_headers(),
                                         params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.warning("Zoho create_invoice: network error after refresh: %s", exc)
                return {"ok": False, "http_status": 0, "code": CODE_UPSTREAM,
                        "zoho_id": "", "zoho_ref": "", "duplicate": False,
                        "transient": True, "error": str(exc)}

        return self._map_create_response(response, invoice, idempotency_key)

    def _map_create_response(self, response, invoice: dict,
                             idempotency_key: str) -> dict:
        status = response.status_code
        invoice_number = str((invoice or {}).get("invoice_number") or "")
        # Zoho returns a JSON body with its own `code` (0 = success) + `message`.
        body: Any = {}
        try:
            body = response.json() or {}
        except ValueError:
            body = {}
        zoho_code = body.get("code")
        message = str(body.get("message") or "")

        # Success.
        if status in (200, 201) and zoho_code == 0:
            inv = body.get("invoice") or {}
            zoho_id = str(inv.get("invoice_id") or "")
            zoho_ref = str(inv.get("invoice_number") or invoice_number)
            return {"ok": True, "http_status": status, "code": CODE_OK,
                    "zoho_id": zoho_id, "zoho_ref": zoho_ref,
                    "duplicate": False, "transient": False,
                    "idempotency_key": idempotency_key}

        # Duplicate invoice_number → idempotent replay (Zoho: non-zero code +
        # "already exists" message). Treated as a success code by the flow.
        if "already exists" in message.lower() or zoho_code in (1007, 36004,
                                                                36422):
            return {"ok": True, "http_status": status, "code": CODE_DUPLICATE,
                    "zoho_id": "", "zoho_ref": invoice_number,
                    "duplicate": True, "transient": False,
                    "idempotency_key": idempotency_key}

        # Transient (429/5xx) → the §10 loop retries.
        if _is_transient_status(status):
            return {"ok": False, "http_status": status, "code": CODE_UPSTREAM,
                    "zoho_id": "", "zoho_ref": "", "duplicate": False,
                    "transient": True, "error": message,
                    "idempotency_key": idempotency_key}

        # Hard 4xx — map to the closest contract code.
        if status == 400:
            code = CODE_VALIDATION
        elif status == 403:
            code = CODE_FORBIDDEN
        elif status == 404:
            code = CODE_NOT_FOUND
        else:
            code = CODE_UNEXPECTED
        return {"ok": False, "http_status": status, "code": code,
                "zoho_id": "", "zoho_ref": "", "duplicate": False,
                "transient": False, "error": message,
                "idempotency_key": idempotency_key}

    def delete_invoice(self, zoho_id: str) -> dict:
        """DELETE the invoice from Zoho Books → result dict (StubZohoUpload shape)."""
        if not self._access_token:
            try:
                self._refresh_access_token()
            except Exception as exc:  # noqa: BLE001
                logger.error("Zoho delete_invoice: auth refresh failed: %s", exc)
                return {"ok": False, "http_status": 401, "code": CODE_AUTH,
                        "transient": False, "error": str(exc)}

        url = self._api_url(f"invoices/{zoho_id}")
        params = self._org_params()
        try:
            response = requests.delete(url, headers=self._build_headers(),
                                        params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("Zoho delete_invoice: network error: %s", exc)
            return {"ok": False, "http_status": 0, "code": CODE_UPSTREAM,
                    "transient": True, "error": str(exc)}

        # 401 → refresh once and retry the DELETE.
        if response.status_code == 401:
            try:
                self._refresh_access_token()
            except Exception as exc:  # noqa: BLE001
                logger.error("Zoho delete_invoice: re-refresh failed: %s", exc)
                return {"ok": False, "http_status": 401, "code": CODE_AUTH,
                        "transient": False, "error": str(exc)}
            try:
                response = requests.delete(url, headers=self._build_headers(),
                                            params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.warning("Zoho delete_invoice: network error after refresh: %s", exc)
                return {"ok": False, "http_status": 0, "code": CODE_UPSTREAM,
                        "transient": True, "error": str(exc)}

        status = response.status_code
        # 204 / 200 = deleted; 404 = already gone (idempotent success).
        if status in (200, 204) or status == 404:
            return {"ok": True, "http_status": status, "code": CODE_OK,
                    "transient": False}
        if _is_transient_status(status):
            return {"ok": False, "http_status": status, "code": CODE_UPSTREAM,
                    "transient": True}
        code = CODE_FORBIDDEN if status == 403 else CODE_UNEXPECTED
        try:
            message = str((response.json() or {}).get("message") or "")
        except ValueError:
            message = ""
        return {"ok": False, "http_status": status, "code": code,
                "transient": False, "error": message}