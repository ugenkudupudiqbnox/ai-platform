# OIDC / Single Sign-On

All authentication is centralized in Keycloak (realm **AIPlatform**). The realm,
clients, roles, groups and seed users are imported automatically on first boot
from the rendered `docker/keycloak/realm.json`.

## Realm objects

### Clients
| Client | Used by | Redirect URI |
|--------|---------|--------------|
| `librechat` | LibreChat (native OIDC) | `https://chat.<domain>/oauth/openid/callback` |
| `langflow` | oauth2-proxy (gates LangFlow) | `https://flow.<domain>/oauth2/callback` |
| `langfuse` | Langfuse (native Keycloak SSO) | `https://trace.<domain>/api/auth/callback/keycloak` |

All clients are confidential with generated secrets (stored in `.env`, mirrored
into each app's configuration).

### Roles & groups
| Group | Realm roles granted |
|-------|---------------------|
| Admins | Admin, Developer, User |
| Developers | Developer, User |
| Users | User |

The `Guest` role exists for limited read-only assignments. Group membership is
emitted in tokens via a `groups` claim (group-membership mapper on each client).

### Seed users
Three users are created (passwords in `.env`):
- `platform-admin` → Admins
- `platform-dev` → Developers
- `platform-user` → Users

## Per-app integration

### LibreChat — native OIDC
Configured via `OPENID_*` env vars pointing at
`https://auth.<domain>/realms/AIPlatform`. Local registration/email login are
disabled; users sign in with the "Sign in with Keycloak" button.

### Langfuse — native Keycloak SSO
Configured via `AUTH_KEYCLOAK_CLIENT_ID/SECRET/ISSUER`. Account linking is
enabled so SSO and the bootstrap admin map to the same user. Password login can
be disabled with `LANGFUSE_AUTH_DISABLE_PASSWORD_LOGIN=true`.

### LangFlow — oauth2-proxy (proxy auth)
Open-source LangFlow has **no native OIDC**. It is therefore fronted by
**oauth2-proxy**, which performs the Keycloak login and only then proxies to
LangFlow. Behind that gate, LangFlow runs with `LANGFLOW_AUTO_LOGIN=true` and a
generated superuser, so every authenticated Keycloak user reaches a logged-in
LangFlow session.

```
Browser → NGINX (flow.<domain>) → oauth2-proxy (Keycloak login) → LangFlow
```

To restrict who may access LangFlow, set allowed groups on oauth2-proxy, e.g.
add to `docker/nginx/oauth2-proxy/oauth2-proxy.cfg`:

```
allowed_groups = ["/Developers", "/Admins"]
```

(and ensure the `groups` scope/claim is requested — it is mapped by default).

## Rotating client secrets

1. Update the secret in `.env` (e.g. `KEYCLOAK_CLIENT_SECRET_LANGFUSE`) and the
   mirrored value (`LANGFUSE_AUTH_KEYCLOAK_CLIENT_SECRET`).
2. Update the matching client in the Keycloak admin console (auth.<domain>).
3. `make up` to recreate the affected app.

## Notes

- Keycloak trusts `X-Forwarded-*` from NGINX (`KC_PROXY_HEADERS=xforwarded`),
  so issuer URLs and redirects use the public HTTPS hostnames.
- The bootstrap (master realm) admin is separate from the `AIPlatform` realm
  users; use it only for Keycloak administration.
