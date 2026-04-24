## Authentik Integration Plan

HermesHQ will use Authentik as a self-hosted OIDC provider.

### Architecture

- Authentik authenticates users.
- HermesHQ keeps local authorization:
  - local roles
  - agent assignments
  - internal permissions
- HermesHQ continues to support local admin login.

### Auth Modes

- `local`: only local HermesHQ login
- `hybrid`: local login + Authentik OIDC
- `oidc`: Authentik OIDC primary, local admin login still available behind the login screen

### HermesHQ Environment

```env
AUTH_MODE=hybrid
OIDC_ISSUER_URL=https://auth.example.com/application/o/hermeshq
OIDC_DISCOVERY_URL=
OIDC_CLIENT_ID=<authentik client id>
OIDC_CLIENT_SECRET=<authentik client secret>
OIDC_REDIRECT_URI=https://hq.example.com/api/auth/oidc/callback
OIDC_SCOPE=openid profile email
OIDC_PROVIDER_NAME=Authentik
OIDC_PROVIDER_SLUG=authentik
OIDC_VISIBLE_PROVIDERS=google,microsoft,authentik
OIDC_PROVIDER_LOGIN_URL_GOOGLE=https://auth.example.com/source/oauth/login/google/
OIDC_PROVIDER_LOGIN_URL_MICROSOFT=https://auth.example.com/source/oauth/login/microsoft/
OIDC_PROVIDER_LOGIN_URL_AUTHENTIK=
OIDC_POST_LOGOUT_REDIRECT_URI=https://hq.example.com/
OIDC_AUTO_PROVISION_USERS=false
```

For local development:

```env
AUTH_MODE=hybrid
OIDC_ISSUER_URL=http://localhost:9000/application/o/hermeshq
OIDC_DISCOVERY_URL=http://host.docker.internal:9000/application/o/hermeshq
OIDC_CLIENT_ID=<authentik client id>
OIDC_CLIENT_SECRET=<authentik client secret>
OIDC_REDIRECT_URI=http://localhost:3420/api/auth/oidc/callback
OIDC_PROVIDER_NAME=Authentik
OIDC_PROVIDER_SLUG=authentik
OIDC_VISIBLE_PROVIDERS=google,microsoft,authentik
OIDC_PROVIDER_LOGIN_URL_GOOGLE=http://localhost:9000/source/oauth/login/google/
OIDC_PROVIDER_LOGIN_URL_MICROSOFT=http://localhost:9000/source/oauth/login/microsoft/
OIDC_PROVIDER_LOGIN_URL_AUTHENTIK=
OIDC_POST_LOGOUT_REDIRECT_URI=http://localhost:3420/
OIDC_AUTO_PROVISION_USERS=false
```

### Authentik Setup

#### Local stack

```bash
cp authentik-local/.env.example authentik-local/.env
docker compose --env-file authentik-local/.env -f docker-compose.authentik.yml up -d
```

Initial setup URL:

- `http://localhost:9000/if/flow/initial-setup/`

#### Application setup in Authentik

1. Create an OAuth2/OpenID Connect provider in Authentik.
2. Create an application and bind that provider to it.
3. Use application slug:
   - `hermeshq`
4. Set a strict redirect URI to:
   - `http://localhost:3420/api/auth/oidc/callback`
   - or your production callback URL
5. Copy the Client ID and Client Secret.
6. Set `OIDC_ISSUER_URL` to:
   - `http://localhost:9000/application/o/hermeshq` as the public issuer for local browser testing
   - `https://auth.example.com/application/o/hermeshq` in production
7. For local Docker Desktop testing, also set:
   - `OIDC_DISCOVERY_URL=http://host.docker.internal:9000/application/o/hermeshq`
8. If you want provider-specific buttons in HermesHQ, create Authentik sources with stable slugs:
   - `google`
   - `microsoft`
9. Use source login URLs of the form:
   - `https://auth.example.com/source/oauth/login/<source-slug>/`
   - local example: `http://localhost:9000/source/oauth/login/google/`
10. Set `OIDC_VISIBLE_PROVIDERS` and the matching `OIDC_PROVIDER_LOGIN_URL_*` values in HermesHQ.
11. Optionally keep a generic `Authentik` button by including `authentik` in `OIDC_VISIBLE_PROVIDERS`.

### Login Flow

1. User opens HermesHQ login page.
2. User clicks `Continue with Google`, `Continue with Microsoft`, or `Continue with Authentik`.
3. HermesHQ redirects either:
   - directly to the configured Authentik source URL for Google/Microsoft, or
   - to the generic OIDC authorization endpoint from discovery for the Authentik fallback button.
4. Authentik authenticates the user directly or via the selected source.
5. Authentik redirects back to HermesHQ callback.
6. HermesHQ maps the Authentik identity to a local HermesHQ user by:
   - `oidc_subject`
   - then email
7. HermesHQ issues its own local JWT and continues normally.

### Provider-specific login buttons

- HermesHQ can render provider-specific buttons when all of these are true:
  - `AUTH_MODE` is `hybrid` or `oidc`
  - `OIDC_VISIBLE_PROVIDERS` contains the provider slug
  - the matching `OIDC_PROVIDER_LOGIN_URL_*` is configured
- Recommended provider slugs for Authentik sources:
  - `google`
  - `microsoft`
- The generic Authentik fallback button does not require `OIDC_PROVIDER_LOGIN_URL_AUTHENTIK`; HermesHQ will fall back to OIDC discovery automatically.

### Provisioning Policy

- Recommended default: `OIDC_AUTO_PROVISION_USERS=false`
- With `false`, only users already provisioned in HermesHQ can enter.
- With `true`, HermesHQ auto-creates a local `user` account on first login.

### Logout Behavior

- HermesHQ clears its local JWT.
- If the user logged in via OIDC, HermesHQ redirects to Authentik's `end_session_endpoint` when available.
- `OIDC_POST_LOGOUT_REDIRECT_URI` should point back to the HermesHQ root.

### Notes

- HermesHQ uses OIDC discovery:
  - `/.well-known/openid-configuration`
- The issuer must therefore be the Authentik application issuer, not the raw Authentik root URL.
- In local Docker Desktop testing, `OIDC_ISSUER_URL` stays browser-facing on `localhost`, while `OIDC_DISCOVERY_URL` lets the backend container reach Authentik via `host.docker.internal`.
- In production, use HTTPS for Authentik and HermesHQ.
