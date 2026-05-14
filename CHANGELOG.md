# Changelog

## Unreleased

## 2026-05-13

### Added
- **Multi-Provider OIDC Authentication** — Direct Google and Microsoft 365 login without Authentik
  - New `oidc_providers` database table for dynamic provider management
  - Admin UI: Settings → Authentication tab to configure providers (client_id, secret, discovery URL)
  - Google and Microsoft preset buttons for quick setup
  - Provider-specific OIDC state (CSRF protection per provider)
  - Social logout: Google/Microsoft sessions properly terminated on HermesHQ logout
  - Auto-provision users from OIDC claims with configurable allowed domains
  - Backward compatible: existing Authentik/env-based flow still works
- **OIDC Admin API** — CRUD endpoints at `/api/oidc-providers` (admin only)
- **Authentication tab in Settings** — Manage OIDC providers from the UI

### Added (continued)
- **Manual: Authentication section** — New bilingual section covering Google and Microsoft 365 OIDC setup (13 bullets each in Spanish and English)

### Changed
- Login page always shows Google and Microsoft buttons (enterprise appearance)
- OIDC state token now includes provider slug for cross-provider CSRF protection
- `oidc_logout` endpoint accepts `?provider=` parameter for social logout
- `auth/providers` endpoint now includes DB-configured providers alongside env providers

### Security
- Provider-specific state validation prevents cross-provider CSRF attacks
- OIDC admin endpoints require admin role
- Client secrets stored encrypted in database

## 2026-05-11

### Added

- shared avatar service (`services/avatar.py`) eliminating duplicated upload/validation/deletion logic across auth, agents and users routers
- WebSocket reconnection with exponential backoff (1s → 30s), heartbeat (30s ping/10s pong timeout), and first-message token authentication (`{"type":"auth"}`)
- JWT httpOnly cookie authentication alongside Bearer token — login and OIDC callback set `hermeshq_token` cookie; new `POST /auth/logout` endpoint clears it
- OIDC ID token signature verification using JWKS cache (1h TTL) with issuer, audience and expiry validation
- Argon2 password hashing with pbkdf2_sha256 backward-compatible fallback
- Alembic database migration framework replacing ad-hoc SQL schema updates, with automatic legacy fallback
- SQLAlchemy connection pooling (`pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`)
- Code-split settings tabs — `SettingsPage` refactored from 2165 to 321 lines with `React.lazy()` + `Suspense` for 9 independent tab components
- Parameterized `ChannelForm` component eliminating ~60% code duplication between Telegram and WhatsApp configuration panels
- Centralized `AgentFactory` service for agent creation logic shared between direct and template-based creation
- Pydantic schemas (`AgentModeUpdate`, `AgentTemplateOverrides`) for previously untyped `dict` endpoints
- i18n namespaced translations — 16 modules per locale (en/es) replacing single 1266-line monolithic file
- Dynamic `.font-display` CSS class: Doto dots typography for all themes, Space Grotesk for sixmanager themes
- Comprehensive test plan document (`docs/TEST_PLAN.md`) with 213 test cases across 14 categories
- Backend `.dockerignore` to reduce Docker build context size

### Changed

- **Backend Dockerfile**: multi-stage build (builder + runtime), runs as non-root `appuser` (UID 1000)
- **docker-compose.yml**: PostgreSQL port closed to host, explicit internal network, `unless-stopped` restart policy, JSON file logging (10m/3 files), backend resource limits (1G RAM / 1 CPU), `no-new-privileges:true` on all services
- **nginx.conf**: security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy), gzip compression, static asset caching (1 year immutable), proxy timeouts (300s API / 3600s WebSocket)
- **SettingsPage.tsx**: 2165 → 321 lines, 9 lazy-loaded tab components
- **agents.py router**: 1230 → ~100 lines, split into 7 focused sub-routers (CRUD, runtime, avatar, workspace, bulk, template, managed)
- **AgentMessagingPanel.tsx**: ~1100 → 441 lines via shared `ChannelForm` component
- **Frontend HTTP client**: 30s timeout, `withCredentials: true` for cookie support
- **Safe localStorage access** in `sessionStore` and `uiStore` — no more crashes in Safari private browsing
- **hermes-agent** dependency pinned to `v2026.5.7` for reproducible builds
- **CSP `font-src`** updated to allow Google Fonts (Doto typeface loading)
- Health endpoint available at both `/health` (direct) and `/api/health` (nginx proxy)

### Fixed

- fixed agent start/stop returning 500 due to missing `Agent` model import in `agents_runtime.py` sub-router
- fixed Hermes Versions tab showing empty upstream releases — hook was disconnected from refresh button after SettingsPage split
- fixed `git ls-remote` failure in production — `git` was only in builder stage, not runtime
- fixed Doto dots font not loading — CSP `font-src` was blocking Google Fonts downloads
- fixed agent list returning 404 — double `/agents` prefix in sub-routers after refactor
- fixed `localStorage` writes during drag operations — debounced to 300ms with cleanup
- fixed form race conditions in messaging panel — `isDirty` ref prevents refetch from overwriting user edits
- fixed TypeScript type narrowing — removed `| string` from `auth_source` and `auth_mode` union types
- fixed backend Dockerfile missing `git` in runtime stage for Hermes version upstream queries
- fixed rate limiting nginx directives referencing undefined zones (commented out with setup instructions)
- fixed missing `default export` on settings tab components for React.lazy loading

### Security

- Backend container runs as non-root `appuser` with `no-new-privileges`
- PostgreSQL no longer exposed to host network
- JWT tokens set as httpOnly cookies with `SameSite=Lax`
- OIDC ID tokens now verified with JWKS signature validation
- Argon2 password hashing (backward compatible with pbkdf2_sha256)
- Nginx security headers: CSP, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy, Permissions-Policy
- WebSocket token moved from query parameter to first-message auth (no longer in server logs)

## 2026-05-11 (pre-release)

### Added

- a new selectable `enterprise` theme for both instance default branding and per-user override, alongside the existing `dark`, `light`, and `system` options
- runtime profiles (`standard`, `technical`, `security`) so an agent can carry a declared execution policy across Talk to agent, TUI, schedules, Telegram, and delegated work
- visible runtime capability maps in the UI: `Settings` now shows built-in runtime toolsets and HermesHQ platform plugins, while each agent `Integrations` panel shows the effective built-ins, platform plugins, and enabled integration packages for that specific agent
- a managed integration package system with upload, install, uninstall, catalog metadata, and per-agent enable/disable flows
- bundled productivity integrations always visible in `Settings -> Integrations`: `ms365-mail`, `ms365-calendar`, `sharepoint`, `google-workspace-mail`, `google-calendar`, and `google-drive`
- real integration health checks for Microsoft Graph and Google OAuth-backed managed integrations
- a bundled `snyk-agent-scan` managed integration with health check and manual `scan_skills` action traced into agent activity
- support for uploaded `standard`-compatible integration packages built from legacy Hermes skills, validated with a new `gamma-app` package that exposes Gamma API tools, actions, and a companion skill bundle in HermesHQ format
- an `Integration Factory` workflow for admins and `HQ Operator`: draft integration packages can now be scaffolded, edited file-by-file, validated, and published into `Managed Integrations` without leaving HermesHQ
- a task board / Kanban phase 1 with board columns, drag state persistence, manual board ownership, and a collapsible `Submit task` rail
- agent archival instead of hard delete: archived agents keep `Activity stream`, `Runtime ledger`, and messages for audit, can be listed again with `Show archived`, and open in read-only operational mode
- per-agent Hermes Agent version pinning plus instance default Hermes version selection
- an upstream-aware Hermes Agent version catalog in `Settings`, with real tag discovery from the Hermes repo, automatic catalog entry creation from upstream releases, metadata editing, install/uninstall, default selection, and per-agent override
- version-specific Hermes runtime resolution for tasks, TUI, and gateways using isolated installs under `/app/workspaces/_hermes_versions/<version>`
- instance-level `Backup & Restore` in `Settings`: admins can now create encrypted backup archives, validate them before import, and restore them in `replace` or `merge` mode, including workspaces, branding, provider catalog, users, secrets, agents, channels, schedules, templates, uploaded integration packages, and integration factory state
- real deletion for installed agent skills from the Hermes skill registry; managed skills are also removed from the agent assignment list on delete
- built-in `scripts/backup-instance.sh` and `scripts/restore-instance.sh` to preserve and rehydrate PostgreSQL, workspaces, `.env` and `cloudflared` token state
- `scripts/reset-admin-password.sh` for Docker-based instances, with a backend-safe inline reset path
- a first-run `install.sh` so HermesHQ can be installed with a single `curl | bash` command from GitHub
- GitHub Pages landing content, dark demo assets, refreshed screenshots, and README video/demo support for project presentation
- Telegram chat traceability into agent `Activity stream` by persisting inbound and outbound gateway messages as `channel.telegram.*` events
- native per-agent WhatsApp channels using the Hermes gateway, including bridge asset sync, QR-based pairing, runtime visibility, and shared gateway supervision compatible with multi-platform agent messaging
- two new inference provider presets: `AWS Bedrock` using `auth_type=aws_sdk` and `OpenAI-compatible API` for generic OpenAI-style gateways and self-hosted endpoints
- enterprise MCP access for exposing authorized HermesHQ agents to external AI clients, including scoped credentials, per-agent allowlists, expiry, revocation, audit events, the `/mcp` JSON-RPC endpoint, and a `scripts/hermeshq-mcp-stdio.py` bridge for Claude Code, Codex and other stdio MCP clients
- two bundled voice managed integrations enabled by default at instance level: `voice-edge` for `faster-whisper` + `edge-tts`, and `voice-local` for `faster-whisper` + Piper, both with Spanish and English presets and runtime `stt`/`tts` config generation
- fixed `voice-local` runtime dependencies to install the actual Piper package (`piper-tts`) required by the health check and local TTS path
- backend image now installs WhatsApp bridge dependencies with `npm ci --omit=peer` so Baileys no longer drags in `sharp` during image builds, which keeps local and production deploys from stalling in that layer

### Changed

- `Settings` is now organized into internal tabs (`General`, `Runtime`, `Providers`, `Integrations`, `Factory`, `Hermes Versions`, `Secrets`, `Templates`) to reduce the operational sprawl of one long admin page
- the `enterprise` theme now applies a more structured enterprise control-surface look across the shell, overview, surface system, task board, agent detail, comms, users, settings, manual, nodes, my account, and login screens without replacing the older themes
- the login screen now follows the instance default public theme instead of always presenting a dark entry surface
- `standard` agents now lose direct terminal/process execution in the shared backend runtime and no longer expose the Hermes TUI
- `Settings -> Integrations` now separates built-in runtime capabilities, HermesHQ platform plugins, and installable integration packages more clearly
- agent detail now has a dedicated `Integrations` section with effective capability summaries, managed integration metadata, actions, and connectivity testing
- managed integrations documentation now includes package requirements and the Gamma.app upload flow for converting older skill bundles into HermesHQ-native integration packages
- managed integrations documentation now also includes the full `Integration Factory` authoring flow, including draft lifecycle, validation, publication, and operator-assisted publishing
- the manual and README now document how to configure `AWS Bedrock` versus `OpenAI-compatible API`, including the current credential model and runtime caveats for Bedrock
- managed integrations now support declarative `select` and `boolean` fields so packages like bundled voice presets can be configured cleanly from `Agent -> Integrations`
- `Activity stream` now groups streamed `agent.output` fragments into readable consolidated blocks instead of showing token-like fragments line by line
- `Runtime ledger` and `Activity stream` now include client-side search
- gateway supervision now treats Hermes messaging as one shared gateway process per agent, which avoids WhatsApp/Telegram PID races and keeps multi-platform channels under the same `HERMES_HOME`
- the dependency canvas now varies agent identity shapes by runtime profile instead of rendering every agent the same way
- the installer now auto-installs Docker on Linux hosts when missing, attempts Docker-without-root setup, preserves existing instance env files, shows admin credentials at the end, and performs rollback/cleanup on failed first installs
- the frontend API/WebSocket base resolution no longer depends on `localhost` for remote installs
- the README and in-app manual now distinguish the new in-product `Backup & Restore` flow from the older shell scripts, and document the passphrase-encrypted archive format plus `replace` vs `merge` restore behavior
- `Settings -> Hermes Versions` now validates manual `release_tag` values against the upstream Hermes repository and warns when a catalog label differs from the detected runtime version after install
- README, manual, and docs now explain Hermes Agent vs HermesHQ more explicitly and present the project as an actively developed system that may still contain rough edges

### Fixed

- fixed instance backup export so JSON columns mapped to names like `metadata_json` are serialized through the ORM attribute instead of leaking SQLAlchemy internals such as `MetaData`
- fixed the `Backup & Restore` UI so optional-history checkboxes align correctly and successful backup creation leaves a visible fallback `Download again` link when the browser does not start the ZIP download automatically
- fixed `install.sh` updates on hosts where an existing HermesHQ stack was started with `sudo docker` while the current shell also has access to a separate non-sudo Docker context; the installer now reuses the Docker context that already owns the running stack and suppresses noisy future-timestamp warnings from GitHub archives
- increased the Hermes runtime subprocess output limit to reduce false `failed` tasks caused by oversized final result lines
- fixed installer temp directory cleanup so the one-line install flow no longer ends with `tmp_dir: unbound variable`
- fixed the admin password reset flow so it no longer depends on the backend image already containing a new helper file
- documented that a Telegram bot token must be active in only one HermesHQ instance at a time to avoid polling conflicts
- fixed HermesHQ WhatsApp startup so the bundled bridge assets, bridge port, and runtime config no longer depend on missing files inside the upstream `hermes-agent` wheel
- fixed WhatsApp QR pairing in HermesHQ so the UI now renders a real visual QR from the bridge output instead of depending only on ASCII text rendering in the browser
- fixed the backend image build path for the WhatsApp bridge by resolving Baileys from the published npm package and normalizing remaining git dependencies to `git+https`, so remote Docker builds no longer stall on GitHub SSH

## 2026-04-03

### Added

- Per-agent Telegram channel management using the native Hermes gateway, including allowlisted Telegram user IDs persisted per agent.
- Instance-wide Hermes TUI skin management for admins. A single uploaded YAML skin can now be applied as the default look for every agent TUI.
- Provider registry and preset-driven runtime configuration for admins, covering Kimi Coding, Z.AI Coding Plan, OpenRouter API, OpenAI API, Gemini API and Anthropic API.
- Real HermesHQ inter-agent tools exposed to agents themselves: `hq_list_agents`, `hq_direct_message` and `hq_delegate_task`.
- Per-agent avatars with upload, delete and rendering across the dashboard, agent matrix, dependency canvas and agent detail view.
- Per-user avatars with upload and delete from the `Users` page.
- Per-user theme overrides layered on top of the instance default theme.
- Per-user language overrides (`en` / `es`) layered on top of the instance default language.
- User management with `admin` / `user` roles and assigned-agent scope.
- In-app user manual with screenshots, exposed from the operator section in the sidebar.
- Self-service `My Account` page for display name, avatar, theme preference and personal password changes.

### Changed

- Agent detail `Configuration` is now collapsed by default.
- The agent detail view now translates its main UI chrome into Spanish when the user selects `es`, including configuration, conversation, terminal shell chrome, Telegram settings, skills and workspace panels.
- Dashboard `Primary Readout` now shows the active operator instead of a decorative placeholder.
- The `Users` screen now exposes editable display names and clearer operator icon controls.
- Hermes runtime prompt fragments now use the configured instance app name, falling back to `HermesHQ` only when no branding name is set.
- `Comms` now enforces hierarchy-aware delegation: independent agents delegate freely, subordinates can escalate upward or delegate downward within their branch, and cross-branch lateral delegation is blocked.
- `Comms` now previews those hierarchy rules in the UI, including disabled destinations and a visual routing scope for the selected source agent.
- Delegated child tasks now generate a real callback path to the parent agent: HermesHQ persists a `delegate_result` message, creates a follow-up callback task in the delegator and surfaces the result in the parent runtime ledger.
- Delegations started from Telegram now preserve the origin chat context so the delegator can auto-reply to that same Telegram conversation when the delegated child finishes.
- `Message edges` in `Comms` now shows human-readable agent names plus delegate/direct/broadcast counts instead of raw IDs.
- `Settings` now controls the instance default language, while `My Account` and the sidebar operator section expose a personal language override for every user.
- `Settings` now also controls a shared Hermes TUI skin, which HermesHQ propagates into each agent `HERMES_HOME` and activates through `display.skin`.
- `Settings` now includes a provider registry where admins can edit provider names, base URLs, enabled state and default models. Agent creation and instance runtime defaults can start from those presets instead of typing provider/model/base URL/secret ref by hand.
- The `Kimi Coding` preset was corrected to use `https://api.kimi.com/coding/v1`.
# Unreleased

- frontend builds are now protected against accidentally baking a local `VITE_API_BASE_URL` such as `http://localhost:8000/api` into production bundles
- the frontend Dockerfile no longer defaults `VITE_API_BASE_URL` to localhost; the safe default path is the browser-side `/api` proxy
