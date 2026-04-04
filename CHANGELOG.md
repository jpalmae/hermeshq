# Changelog

## Unreleased

- added a first-run `install.sh` so HermesHQ can be installed with a single `curl | bash` command from GitHub
- parameterized the Docker stack through `.env` for ports, bootstrap admin credentials, PostgreSQL credentials, CORS origins and frontend API base URL
- updated the frontend API/WebSocket base resolution so remote installs no longer depend on `localhost`
- added built-in `scripts/backup-instance.sh` and `scripts/restore-instance.sh` to preserve and rehydrate PostgreSQL, workspaces, `.env` and `cloudflared` token state

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
