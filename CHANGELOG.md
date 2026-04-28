# Changelog

## Unreleased

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
- real deletion for installed agent skills from the Hermes skill registry; managed skills are also removed from the agent assignment list on delete
- built-in `scripts/backup-instance.sh` and `scripts/restore-instance.sh` to preserve and rehydrate PostgreSQL, workspaces, `.env` and `cloudflared` token state
- `scripts/reset-admin-password.sh` for Docker-based instances, with a backend-safe inline reset path
- a first-run `install.sh` so HermesHQ can be installed with a single `curl | bash` command from GitHub
- GitHub Pages landing content, dark demo assets, refreshed screenshots, and README video/demo support for project presentation
- Telegram chat traceability into agent `Activity stream` by persisting inbound and outbound gateway messages as `channel.telegram.*` events
- native per-agent WhatsApp channels using the Hermes gateway, including bridge asset sync, QR-based pairing, runtime visibility, and shared gateway supervision compatible with multi-platform agent messaging
- two new inference provider presets: `AWS Bedrock` using `auth_type=aws_sdk` and `OpenAI-compatible API` for generic OpenAI-style gateways and self-hosted endpoints
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
- `Settings -> Hermes Versions` now validates manual `release_tag` values against the upstream Hermes repository and warns when a catalog label differs from the detected runtime version after install
- README, manual, and docs now explain Hermes Agent vs HermesHQ more explicitly and present the project as an actively developed system that may still contain rough edges

### Fixed

- increased the Hermes runtime subprocess output limit to reduce false `failed` tasks caused by oversized final result lines
- fixed installer temp directory cleanup so the one-line install flow no longer ends with `tmp_dir: unbound variable`
- fixed the admin password reset flow so it no longer depends on the backend image already containing a new helper file
- documented that a Telegram bot token must be active in only one HermesHQ instance at a time to avoid polling conflicts
- fixed HermesHQ WhatsApp startup so the bundled bridge assets, bridge port, and runtime config no longer depend on missing files inside the upstream `hermes-agent` wheel
- fixed WhatsApp QR pairing in HermesHQ so the UI now renders a real visual QR from the bridge output instead of depending only on ASCII text rendering in the browser

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
