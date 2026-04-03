# Changelog

## 2026-04-03

### Added

- Per-agent Telegram channel management using the native Hermes gateway, including allowlisted Telegram user IDs persisted per agent.
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
- `Message edges` in `Comms` now shows human-readable agent names plus delegate/direct/broadcast counts instead of raw IDs.
- `Settings` now controls the instance default language, while `My Account` and the sidebar operator section expose a personal language override for every user.
