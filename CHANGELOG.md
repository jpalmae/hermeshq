# Changelog

## 2026-04-03

### Added

- Per-agent Telegram channel management using the native Hermes gateway, including allowlisted Telegram user IDs persisted per agent.
- Per-agent avatars with upload, delete and rendering across the dashboard, agent matrix, dependency canvas and agent detail view.
- Per-user avatars with upload and delete from the `Users` page.
- Per-user theme overrides layered on top of the instance default theme.
- User management with `admin` / `user` roles and assigned-agent scope.

### Changed

- Agent detail `Configuration` is now collapsed by default.
- Dashboard `Primary Readout` now shows the active operator instead of a decorative placeholder.
- The `Users` screen now exposes editable display names and clearer operator icon controls.
