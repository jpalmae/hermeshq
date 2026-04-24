## Authentik Pending

### Current state

- Authentik local stack is running with:
  - [docker-compose.authentik.yml](/Users/jpalmae/dev/hermeshq/docker-compose.authentik.yml)
  - [authentik-local/.env.example](/Users/jpalmae/dev/hermeshq/authentik-local/.env.example)
- HermesHQ is configured locally in `hybrid` mode against Authentik through OIDC.
- OIDC login and logout URLs from HermesHQ are already resolving to:
  - `http://localhost:9000/application/o/authorize/...`
  - `http://localhost:9000/application/o/hermeshq/end-session/...`
- HermesHQ local `.env` currently includes:
  - `AUTH_MODE=hybrid`
  - `OIDC_ISSUER_URL=http://localhost:9000/application/o/hermeshq`
  - `OIDC_DISCOVERY_URL=http://host.docker.internal:9000/application/o/hermeshq`
  - `OIDC_CLIENT_ID=hermeshq-local`
  - `OIDC_CLIENT_SECRET=hermeshq-local-secret-2026`
  - `OIDC_REDIRECT_URI=http://localhost:3420/api/auth/oidc/callback`
  - `OIDC_PROVIDER_NAME=Authentik`
  - `OIDC_PROVIDER_SLUG=authentik`
  - `OIDC_VISIBLE_PROVIDERS=authentik`
  - `OIDC_PROVIDER_LOGIN_URL_GOOGLE=`
  - `OIDC_PROVIDER_LOGIN_URL_MICROSOFT=`
  - `OIDC_PROVIDER_LOGIN_URL_AUTHENTIK=`
  - `OIDC_POST_LOGOUT_REDIRECT_URI=http://localhost:3420/`
  - `OIDC_AUTO_PROVISION_USERS=false`

### Authentik objects already created

- OAuth2 provider:
  - name: `HermesHQ OIDC`
  - id: `1`
  - client_id: `hermeshq-local`
- Application:
  - slug: `hermeshq`
  - name: `HermesHQ`
- Provider setup URLs confirmed:
  - issuer: `http://localhost:9000/application/o/hermeshq/`
  - discovery: `http://localhost:9000/application/o/hermeshq/.well-known/openid-configuration`

### Local accounts already prepared

- Authentik admin:
  - username: `akadmin`
  - password: `Opencom123`
  - email: `akadmin@local.authentik`
- Authentik user for login test:
  - username: `jpalma@gmail.com`
  - password: `Opencom123`
  - email: `jpalma@gmail.com`
- HermesHQ local admin mapped by email:
  - email: `jpalma@gmail.com`
  - role: `admin`

### What still needs to be done

1. Run the real browser login flow end-to-end from HermesHQ with the Authentik user.
2. Verify the HermesHQ session lands on the local admin account mapped by email.
3. Confirm OIDC logout clears both:
   - HermesHQ local token
   - Authentik session
4. Decide whether to keep:
   - `OIDC_AUTO_PROVISION_USERS=false`
   - or enable controlled auto-provisioning later

### Social login pending in Authentik

These were not configured yet:

- Google source
- Microsoft source
- Microsoft Entra / enterprise source

They require provider-side OAuth credentials before continuing.

Once those sources exist, HermesHQ can expose direct buttons by setting:

- `OIDC_VISIBLE_PROVIDERS=google,microsoft,authentik`
- `OIDC_PROVIDER_LOGIN_URL_GOOGLE=http://localhost:9000/source/oauth/login/google/`
- `OIDC_PROVIDER_LOGIN_URL_MICROSOFT=http://localhost:9000/source/oauth/login/microsoft/`

The source login URL pattern is based on the Authentik source slug:

- `http://localhost:9000/source/oauth/login/<source-slug>/`

### Next implementation step

Once Google or Microsoft credentials are available:

1. create the corresponding source in Authentik with slug `google` and/or `microsoft`
2. confirm the source login URLs work directly:
   - `http://localhost:9000/source/oauth/login/google/`
   - `http://localhost:9000/source/oauth/login/microsoft/`
3. set the matching `OIDC_PROVIDER_LOGIN_URL_*` values in HermesHQ
4. set `OIDC_VISIBLE_PROVIDERS=google,microsoft,authentik`
5. test HermesHQ login again through those direct provider buttons
6. keep HermesHQ as OIDC client only, with local authorization unchanged
