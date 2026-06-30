# AP Tools — LangFlow Extension Bundle

Accounts-payable agent tools for the "AP Tool - Ugen" / "Cosmic AP" flow
(`e812b2dc-68c4-49c0-a714-92c8d558128a`):

- **Zoho Books AP Tool** (`ZohoBooksAPTool`) — bills, vendors, payments. Auto-refreshes
  the OAuth access token from the stored client_id / client_secret / refresh_token on
  every call and re-refreshes once on a 401.
- **FOODICS AP Tool** (`FoodicsAPTool`) — suppliers and purchase orders. Static
  `Bearer` token auth.

## Why a bundle (not inline custom components)

The flow is exposed via LangFlow's **public shareable playground**
(`https://flow.<domain>/playground/<flow_id>`). LangFlow blocks unauthenticated
public builds when a flow contains *custom* component code
(`Public flows cannot be built without authentication when they contain custom
components`). Components shipped via an installed Extension Bundle are registered in
the server's trusted component registry, so the public build path substitutes the
server's trusted code for those component types and **builds without the insecure
`LANGFLOW_ALLOW_PUBLIC_CUSTOM_COMPONENTS=true` toggle**. The Zoho OAuth-refresh logic
is preserved (it's real Python in the bundle — not expressible as a static built-in
HTTP Request component).

## Credentials

Each component's `SecretStrInput` credential fields use `load_from_db=True`, so in the
LangFlow UI you select a **Secret Global Variable** from a dropdown — only the variable
*name* is stored in the flow JSON, never the secret value. Create these Secret Global
Variables and select them on the component fields:

- `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID`
- `FOODICS_API_TOKEN`

> Rotate the Zoho OAuth credentials (client_id, client_secret, refresh_token) before
> populating the Global Variables — they were pasted in plaintext in an earlier session.

## Layout

```
ap_tools/                     # inline-bundle dir MUST be snake_case (bundle-name pattern)
  extension.json              # v0 Extension manifest (bundle = ap_tools; id = ap-tools)
  pyproject.toml              # pip-installable metadata + langflow.extension entry-point
  components/ap_tools/
    zoho_books_ap.py          # ZohoBooksAPTool
    foodics_ap.py             # FoodicsAPTool
```

> The inline-bundle directory name must be lowercase snake_case (it is validated
> against the bundle-name pattern). Hyphens are rejected with
> `inline-bundle-name-invalid`. The extension `id` in `extension.json` may stay
> hyphenated (`ap-tools`); only the directory name is constrained.

## Deployment (this repo)

Bind-mount `docker/langflow-extensions` into the `langflow` container at `/app/extensions`
and set `LANGFLOW_COMPONENTS_PATH=/app/extensions` (see `docker-compose.yml`). LangFlow
discovers each subfolder with an `extension.json` as an inline bundle at the `@extra`
slot. Built-in components are unaffected (they load from the prebuilt component index).

## Validate offline

```bash
docker exec langflow python -m lfx extension validate /app/extensions/ap_tools
```