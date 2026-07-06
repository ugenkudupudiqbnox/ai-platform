# AR Tools — LangFlow Extension Bundle

Accounts-receivable source-system tools for the **Cosmic AR Agent**. See
[`docs/cosmic-ar-architecture.md`](../../../docs/cosmic-ar-architecture.md) for
the design and [`docs/cosmic-ar-constitution.md`](../../../docs/cosmic-ar-constitution.md)
for the binding standards.

- **Zoho Books AR Tool** (`ZohoBooksARTool`) — invoices, customer contacts, and
  customer payments. Will auto-refresh the OAuth access token from the stored
  client_id / client_secret / refresh_token and re-refresh once on a 401 (mirrors
  the existing `ZohoBooksAPTool` pattern; deferred to build phase).
- **FOODICS AR Tool** (`FoodicsARTool`) — POS receipts and sales. Static `Bearer`
  token auth.

> **Scaffold only.** Both components are valid, importable `lfx` Component
> skeletons whose output methods return placeholder `Message` responses. No HTTP
> calls or OAuth-refresh logic is implemented yet — that is the build phase.

## Why a bundle (not inline custom components)

Same rationale as the sibling `ap_tools` bundle: the agent flow is exposed via
LangFlow's public shareable playground, which blocks unauthenticated public
builds when a flow contains *custom* component code. Components shipped via an
installed Extension Bundle register in the server's trusted component registry,
so the public build path substitutes the server's trusted code and **builds
without the insecure `LANGFLOW_ALLOW_PUBLIC_CUSTOM_COMPONENTS=true` toggle**.

## Credentials

Each component's `SecretStrInput` credential fields use `load_from_db=True`, so
in the LangFlow UI you select a **Secret Global Variable** from a dropdown — only
the variable *name* is stored in the flow JSON, never the secret value. Create
these Secret Global Variables and select them on the component fields:

- `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID`
- `FOODICS_API_TOKEN`

> Rotate the Zoho OAuth credentials before populating the Global Variables.

## Layout

```
ar_tools/                     # inline-bundle dir MUST be snake_case (bundle-name pattern)
  extension.json              # v1 Extension manifest (bundle = ar_tools; id = ar-tools)
  pyproject.toml              # pip metadata + langflow.extension entry-point
  components/ar_tools/
    zoho_books_ar.py          # ZohoBooksARTool
    foodics_ar.py             # FoodicsARTool
```

## Deployment (this repo)

Bind-mount `docker/langflow-extensions` into the `langflow` container at
`/app/extensions` and set `LANGFLOW_COMPONENTS_PATH=/app/extensions` (see
`docker-compose.yml`). LangFlow discovers each subfolder with an `extension.json`
as an inline bundle at the `@extra` slot.

## Validate offline

```bash
docker exec langflow python -m lfx extension validate /app/extensions/ar_tools
```