"""Real Foodics transport for the AR ``ar_foodics_processing`` subflow (build-phase).

This is the build-phase swap-in for the broken scaffold in
``ar_tools/foodics_ar.py`` (which used a static ``foodics_api_token`` and a
cross-bundle import that never resolves). Wired via the flow's
``_make_foodics_fetcher()`` seam (``foodics_processing.py``) which returns a
``RealFoodics`` instance when Foodics credentials are configured.

It is **pure Python** (no lfx import) — the subflow component resolves the
credentials itself (it carries ``user_id``; see ``vendor_secrets.py``) and passes
them to the constructor as a plain dict, keeping the transport offline-testable.

Transport contract (consumed by ``_fetch_foodics_with_retry`` /
``foodics_processing.py``):

  * ``_make_foodics_fetcher()`` returns ``RealFoodics(creds)`` (or ``None``).
  * The retry loop sets ``tool.operation`` ∈
    {``list_orders``, ``list_order_items``, ``list_order_payments``} and
    ``tool.entity_id = ""`` (entity_id is unused — the AR flow fetches **all**
    rows per role, not per-order).
  * It then calls ``tool.fetch_foodics_data()`` and ``parse_envelope``'s the
    result → the returned value must be a JSON **§14 envelope string**:
    ``{"status":"ok","code":"AR_OK","trace_id":...,"data":{"rows":[...]}}``.
  * Rows are normalized to the **canonical column names** the flow's
    ``_header_map`` alias-lookups expect (``order_ref``, ``customer_ref``,
    ``item_ref``, ``qty``, ``unit_price``, ``amount``, ``posted_at``,
    ``currency``, ``payment_ref``, ``method`` …) so the files + API paths share
    one downstream consumer.

§10 retry ownership (critical): the transport **raises** on transient failures
(``requests`` Connection/Timeout, or HTTP 408/429/5xx via a custom error carrying
an int ``.code`` — the flow's ``_is_transient`` classifier keys off the exception
type-name or ``code``). It returns an **error-envelope string** (not an
exception) for hard 4xx so the flow records a meaningful ``fetch_failure``.

Foodics OAuth 2.0 (verify against apidocs.foodics.com for the live sandbox):
  * ``POST {token_url}`` ``grant_type=refresh_token`` + client_id/secret/
    refresh_token → ``access_token`` (14-day Bearer) + new ``refresh_token``.
  * Headers: ``Authorization: Bearer {token}`` + ``X-Business: {business_id}``
    + ``Accept: application/json``.
  * Resources (Laravel-style ``{"data":[...], "meta":{...}}`` pagination):
      list_orders         → GET {api_url}/orders
      list_order_items    → GET {api_url}/order-products   (each row carries order_id)
      list_order_payments → GET {api_url}/payments         (each row carries order_id)
    Endpoint names + the token URL/host are **sandbox-configurable** via
    ``FOODICS_API_URL`` / ``FOODICS_TOKEN_URL``; verify the exact sandbox hosts
    against Foodics' docs before live use.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
MAX_PAGES = 100  # pagination safety cap (§4 fail-safe against runaway loops)


class _TransientFoodicsError(Exception):
    """Raised on a transient Foodics failure (408/429/5xx) so the flow's §10
    retry loop retries. Carries an int ``code`` that ``_is_transient`` keys on.
    """

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _s(value: Any) -> str:
    """Stringify a Foodics field, treating ``None``/missing as empty string."""
    if value is None:
        return ""
    return str(value)


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


class RealFoodics:
    """Real Foodics transport (OAuth refresh → Bearer + X-Business, list ops)."""

    # role → (endpoint resource, normalizer method name).
    _OPS: dict[str, tuple[str, str]] = {
        "list_orders": ("orders", "_normalize_orders"),
        "list_order_items": ("order-products", "_normalize_order_items"),
        "list_order_payments": ("payments", "_normalize_order_payments"),
    }

    def __init__(self, creds: dict[str, Any]):
        self.client_id = str(creds.get("client_id") or "")
        self.client_secret = str(creds.get("client_secret") or "")
        self.refresh_token = str(creds.get("refresh_token") or "")
        self.business_id = str(creds.get("business_id") or "")
        self.api_url = str(creds.get("api_url") or "https://api.foodics.com/v2/")
        self.token_url = str(creds.get("token_url") or
                             "https://api.foodics.com/oauth/token")
        self._access_token: Optional[str] = None
        # Set by ``_fetch_foodics_with_retry`` before each call.
        self.operation: str = ""
        self.entity_id: str = ""
        self.trace_id: str = ""

    # ------------------------------------------------------------------ #
    #  OAuth (Foodics 2.0 refresh-token grant)
    # ------------------------------------------------------------------ #

    def _refresh_access_token(self) -> str:
        logger.info("Foodics: refreshing access token from %s", self.token_url)
        response = requests.post(
            self.token_url,
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
                f"Foodics token refresh failed (HTTP {response.status_code}): "
                f"{response.text}"
            )
        token_data = response.json() or {}
        new_token = token_data.get("access_token")
        if not new_token:
            raise RuntimeError(
                f"Foodics token refresh returned no access_token: {token_data}"
            )
        # Foodics issues a new refresh_token on each refresh — store it.
        new_refresh = token_data.get("refresh_token")
        if new_refresh:
            self.refresh_token = new_refresh
        self._access_token = new_token
        return new_token

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Business": self.business_id,
        }

    def _resource_url(self, resource: str) -> str:
        return f"{self.api_url.rstrip('/')}/{resource.lstrip('/')}"

    # ------------------------------------------------------------------ #
    #  HTTP (refresh-on-401; transient raises, hard 4xx → error envelope)
    # ------------------------------------------------------------------ #

    def _get(self, resource: str, params: Optional[dict] = None) -> dict:
        """GET a Foodics resource (with pagination handled by the caller).

        Raises ``_TransientFoodicsError`` on 408/429/5xx and ``requests``
        Connection/Timeout errors (so the §10 loop retries). Raises
        ``RuntimeError`` only on an unrecoverable auth-refresh failure.
        """
        if not self._access_token:
            self._refresh_access_token()  # raises RuntimeError on hard failure

        url = self._resource_url(resource)
        try:
            response = requests.get(url, headers=self._build_headers(),
                                     params=params or {}, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # Transient by name-match in ``_is_transient``.
            logger.warning("Foodics GET %s: connection/timeout: %s", resource, exc)
            raise
        except requests.RequestException as exc:
            # Generic request error (non-Connection/Timeout, e.g. SSL/redirect/
            # chunked-encoding) — treat as transient so the §10 loop retries.
            logger.warning("Foodics GET %s: request error: %s", resource, exc)
            raise _TransientFoodicsError(f"Foodics {resource}: {exc}", 503) from exc

        # 401 → refresh once and retry the GET.
        if response.status_code == 401:
            self._refresh_access_token()  # raises RuntimeError on hard failure
            try:
                response = requests.get(url, headers=self._build_headers(),
                                        params=params or {},
                                        timeout=REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise
            except requests.RequestException as exc:
                raise _TransientFoodicsError(f"Foodics {resource}: {exc}", 503) from exc

        status = response.status_code
        if status in (408, 429) or 500 <= status < 600:
            raise _TransientFoodicsError(
                f"Foodics {resource} HTTP {status}: {response.text[:200]}", status)
        if status >= 400:
            # Hard 4xx — surface as a hard error (caller builds an error envelope).
            raise RuntimeError(f"Foodics {resource} HTTP {status}: {response.text[:200]}")
        try:
            return response.json() or {}
        except ValueError as exc:
            raise RuntimeError(f"Foodics {resource}: invalid JSON: {exc}") from exc

    def _get_all_pages(self, resource: str) -> list[dict]:
        """Fetch all pages of a Laravel-paginated Foodics resource.

        Foodics v2 paginates as ``{"data":[...], "meta":{"current_page":1,
        "last_page":N}}``. Falls back to the raw ``data`` list when no
        ``meta.last_page`` is present. Capped at ``MAX_PAGES`` (§4 fail-safe).
        """
        page = 1
        rows: list[dict] = []
        while page <= MAX_PAGES:
            payload = self._get(resource, params={"page": page})
            data = payload.get("data")
            if isinstance(data, list):
                rows.extend(r for r in data if isinstance(r, dict))
            elif isinstance(data, dict):
                # Some resources nest: {"data": {"data": [...]}} — unwrap once.
                inner = data.get("data")
                if isinstance(inner, list):
                    rows.extend(r for r in inner if isinstance(r, dict))
            meta = _obj(payload.get("meta"))
            last_page = meta.get("last_page")
            try:
                last_page = int(last_page) if last_page is not None else page
            except (TypeError, ValueError):
                last_page = page
            if page >= last_page:
                break
            page += 1
        return rows

    # ------------------------------------------------------------------ #
    #  Row normalization → canonical column names (matches _header_map aliases)
    # ------------------------------------------------------------------ #

    def _normalize_orders(self, rows: list[dict]) -> list[dict]:
        out = []
        for o in rows:
            cust = _obj(o.get("customer"))
            out.append({
                "order_ref": _s(o.get("reference") or o.get("id")),
                "customer_ref": _s(cust.get("id") or o.get("customer_id")
                                   or cust.get("name")),
                "posted_at": _s(o.get("date") or o.get("created_at")),
                "currency": _s(o.get("currency") or o.get("currency_code")),
                "amount": _s(o.get("total") or o.get("grand_total")
                             or o.get("net")),
            })
        return out

    def _normalize_order_items(self, rows: list[dict]) -> list[dict]:
        out = []
        for li in rows:
            prod = _obj(li.get("product"))
            out.append({
                "order_ref": _s(li.get("order_id") or li.get("order")),
                "item_ref": _s(prod.get("id") or li.get("product_id") or li.get("id")),
                "description": _s(prod.get("name") or li.get("name") or li.get("note")),
                "qty": _s(li.get("quantity") or li.get("qty")),
                "unit_price": _s(li.get("price") or li.get("unit_price")
                                 or li.get("rate")),
                "amount": _s(li.get("total") or li.get("amount")
                             or li.get("line_total")),
            })
        return out

    def _normalize_order_payments(self, rows: list[dict]) -> list[dict]:
        out = []
        for p in rows:
            out.append({
                "payment_ref": _s(p.get("id") or p.get("reference")),
                "order_ref": _s(p.get("order_id") or p.get("order")),
                "amount": _s(p.get("amount") or p.get("value") or p.get("total")),
                "method": _s(p.get("method") or p.get("payment_method")
                             or p.get("type")),
                "posted_at": _s(p.get("date") or p.get("created_at")),
            })
        return out

    # ------------------------------------------------------------------ #
    #  Transport entrypoint
    # ------------------------------------------------------------------ #

    def fetch_foodics_data(self) -> str:
        """Fetch the configured operation → JSON §14 envelope string.

        Raises transiently for retryable failures (the §10 loop owns backoff).
        Returns an error-envelope string for hard failures so the flow records a
        meaningful ``fetch_failure``. Returns an ok envelope with ``data.rows``
        on success.
        """
        trace_id = self.trace_id or uuid.uuid4().hex
        op = self.operation or ""
        spec = self._OPS.get(op)
        if spec is None:
            return json.dumps({
                "status": "error", "code": "AR_VALIDATION",
                "trace_id": trace_id,
                "error": {"message": f"unknown Foodics operation: {op!r}"},
            })
        resource, normalizer_name = spec
        normalizer = getattr(self, normalizer_name)
        try:
            rows = self._get_all_pages(resource)
        except _TransientFoodicsError:
            raise  # → §10 retry
        except RuntimeError as exc:
            # Hard failure (bad creds / 4xx / invalid JSON) — error envelope.
            logger.error("Foodics %s: hard failure: %s", op, exc)
            code = "AR_AUTH" if "token refresh" in str(exc).lower() else "AR_UPSTREAM"
            return json.dumps({
                "status": "error", "code": code, "trace_id": trace_id,
                "error": {"message": f"Foodics {op} failed: {exc}"},
            })
        normalized = normalizer(rows)
        return json.dumps({
            "status": "ok", "code": "AR_OK", "trace_id": trace_id,
            "data": {"rows": normalized},
        })