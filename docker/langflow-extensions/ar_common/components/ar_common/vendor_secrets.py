"""Vendor secret resolution for AR subflow transports (constitution §16, build-phase).

The real Zoho Books + Foodics transports (``zoho_transport.RealZoho``,
``foodics_transport.RealFoodics``) live in this ``ar_common`` bundle and are
constructed by the subflow components' Python seams
(``ZohoUploadFlowComponent.run`` → ``set_transport`` and
``FoodicsProcessingFlowComponent`` → ``_make_foodics_fetcher``) — they are **not**
built by lfx as canvas nodes. Consequently the ``SecretStrInput(load_from_db=True)``
build-time resolution that populates ``self.<secret>`` on canvas-node components
(used by ``ap_tools``/``ar_tools``) does **not** fire for these transports.

This helper lets the subflow component itself — which **is** built by lfx per run
and therefore carries ``user_id`` (the encrypted Secret Global Variable DB lookup
is keyed on ``user_id`` + name; ``langflow/services/variable/service.py``) — read
a LangFlow Secret Global Variable by name at runtime and thread the plaintext into
the transport constructor. The transport stays pure-Python (no lfx dependency) and
offline-testable (constructed with a plain creds dict).

Resolution order (first non-empty value wins):

1. **LangFlow Secret Global Variable** (encrypted DB, by ``user_id`` + name)
   via ``component.variables(name, name)`` — lfx's sync ``run_until_complete``
   wrapper (``lfx/utils/async_helpers.py``), safe from both a sync worker thread
   with no running loop and from inside a running event loop. CREDENTIAL-type
   variables come back as ``pydantic.SecretStr`` and are unwrapped via
   ``get_secret_value()``.
2. **``os.getenv(name)``** — local/offline dev, and a fallback for run paths where
   ``user_id`` is not propagated (e.g. some RunFlow-as-tool contexts) or the DB is
   a noop session.
3. ``default``.

Returns ``None`` when nothing is found, so callers keep their stub/fail-safe
behaviour (``StubZohoUpload`` / the Foodics files path) and offline self-tests
stay green — no credential is *required* for the bundle to import or for the flows
to run offline (``ar_common`` remains ``requiresCredentials: false``).
"""

from __future__ import annotations

import os
from typing import Any, Optional


def _unwrap(value: Any) -> Optional[str]:
    """Coerce a resolved variable to a plain ``str | None``.

    LangFlow returns ``pydantic.SecretStr`` for CREDENTIAL-type Secret Global
    Variables and ``str`` for GENERIC ones; either may also be ``None``.
    """
    if value is None:
        return None
    # CREDENTIAL-type Secret Global Variables arrive as pydantic.SecretStr.
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        s = get_secret_value()
        return s or None
    s = str(value)
    return s or None


def read_secret(component: Any, name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a named secret for the given (lfx-built) component.

    ``component`` is the subflow component instance (e.g. ``self`` inside
    ``run()``); it must expose lfx's ``variables(name, field)`` sync wrapper, which
    is defined on the ``Component`` base class. Pass ``None`` to skip the DB
    lookup (env-only resolution).
    """
    # 1. LangFlow Secret Global Variable (encrypted DB, per user_id).
    if component is not None:
        try:
            value = component.variables(name, name)
        except Exception:
            # user_id not set, variable not found, noop DB, loop errors — fall through.
            value = None
        unwrapped = _unwrap(value)
        if unwrapped:
            return unwrapped

    # 2. Environment fallback (offline / no-user_id / noop DB).
    env = os.getenv(name)
    if env:
        return env

    # 3. default.
    return default


def read_creds(component: Any, names: "list[str]") -> "dict[str, Optional[str]]":
    """Resolve a batch of named secrets into a ``{name: value | None}`` dict.

    A ``None`` value for a required secret tells the caller the vendor is not
    configured and it should keep its stub/fail-safe behaviour.
    """
    return {name: read_secret(component, name) for name in names}