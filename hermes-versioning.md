# Hermes Agent Versioning Plan

## Goal
Allow HermesHQ to pin a specific Hermes Agent version per agent while keeping a default instance version for new agents and safe staged rollouts.

## Why
Hermes Agent releases move fast. Production agents should not all jump to a new version immediately. HermesHQ needs:
- a stable default
- per-agent overrides
- safe canary rollout
- simple rollback

## Scope
Phase 1 keeps everything inside the current shared backend architecture.
No separate execution plane yet.

## Data model

### Agent
Add:
- `hermes_version: str | None`

Semantics:
- `null` means inherit instance default
- non-null means pinned version for this agent

### AppSettings
Add:
- `default_hermes_version: str | None`
- optionally `installed_hermes_versions: JSON`

If we want a cleaner model later:
- new table `hermes_versions`

### Optional future table
`hermes_versions`
- `version`
- `release_tag`
- `source_ref`
- `install_status`
- `installed_path`
- `installed_at`
- `notes`
- `is_default`

## Filesystem layout
Install Hermes once per version, not once per agent.

Suggested paths:
- `/app/workspaces/_hermes_versions/0.8.0/`
- `/app/workspaces/_hermes_versions/0.9.0/`

Inside each version root:
- virtualenv or isolated install
- `hermes` binary
- version metadata file

Agents keep their own:
- workspace
- `HERMES_HOME`

But runtime resolves the Hermes binary from the version root.

## Effective resolution
For every agent, resolve:

1. `agent.hermes_version` if set
2. else `app_settings.default_hermes_version`
3. else fallback to currently bundled default

This effective version must be used consistently by:
- Talk to agent
- TUI
- Telegram
- schedules
- delegations

## Backend changes

### Models
- `backend/hermeshq/models/agent.py`
- `backend/hermeshq/models/app_settings.py`

### Migrations
- `backend/hermeshq/database.py`

### Version manager service
Add new service:
- `backend/hermeshq/services/hermes_version_manager.py`

Responsibilities:
- install version if missing
- resolve installed path
- verify `hermes --version`
- list installed versions
- remove version if unused
- return effective version for an agent

### Installation manager
- `backend/hermeshq/services/hermes_installation.py`

Responsibilities:
- use version manager to resolve Hermes binary
- write version metadata into agent installation if useful
- resync when version changes

### Runtime
- `backend/hermeshq/services/hermes_runtime.py`

Responsibilities:
- invoke task runner using the selected Hermes version environment/binary

### TUI / PTY
- `backend/hermeshq/main.py`
- `backend/hermeshq/services/pty_manager.py`

Responsibilities:
- start PTY using the correct Hermes binary for that agent

### Gateways
- `backend/hermeshq/services/gateway_supervisor.py`

Responsibilities:
- run `hermes gateway run` from the correct version

## UI changes

### Settings
New section:
- `Hermes versions`

Capabilities:
- list installed versions
- install a version
- set default version
- see which one is latest
- optionally display detected `hermes --version`

### Agent detail
In `Configuration`:
- `Hermes version`
- choices:
  - `Inherit default`
  - installed versions

Changing version should:
- destroy PTY
- resync installation
- restart gateways if enabled

## Operational behavior

### New agents
- inherit default Hermes version

### Existing agents
- migration leaves them `null`
- therefore they inherit default unless explicitly pinned

### Production rollout
Recommended:
1. keep default on stable version
2. pin a test agent to new version
3. validate TUI, tasks, Telegram, integrations
4. promote new version to default only after confidence

### Rollback
- set agent back to previous version
- resync installation
- rebuild session/gateways

## Logging and audit
Add activity events:
- `agent.hermes_version.changed`
- `hermes.version.installed`
- `hermes.version.default_changed`

Recommended event details:
- previous version
- next version
- actor
- resolved version path

## Safety checks
Block selecting a version if:
- not installed
- install failed
- binary version mismatch

Warn if:
- integration package declares incompatible Hermes range
- runtime profile or plugin compatibility is unknown

## Future compatibility metadata
For integration packages later:
- `minimum_hermes_version`
- `tested_hermes_versions`

For skills later:
- optional compatibility note only, unless we formalize skill metadata

## Implementation phases

### Phase 1
- version manager service
- default version in settings
- per-agent override
- runtime/TUI/gateway resolution

### Phase 2
- dedicated `hermes_versions` table
- install/remove from UI
- version health checks
- better audit events

### Phase 3
- compatibility checks for integrations/packages
- rollout helpers
- compare behavior between versions

## First concrete milestone
Support these two versions:
- `0.8.0`
- `0.9.0`

Target behavior:
- `default = 0.8.0`
- one or two canary agents pinned to `0.9.0`

## Notes
As of April 13, 2026, Hermes Agent release:
- `v2026.4.13`
- release name: `Hermes Agent v0.9.0`

Source:
- https://github.com/NousResearch/hermes-agent/releases/tag/v2026.4.13
