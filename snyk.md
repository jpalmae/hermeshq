# Snyk Agent Scan integration plan

## Goal

Integrate `snyk/agent-scan` into HermesHQ as an optional managed integration focused on security scanning of the agent ecosystem, not as a mandatory runtime dependency.

## Why it fits

- HermesHQ already manages skills, integration packages, workspaces and agent runtimes.
- `agent-scan` is designed for agent supply-chain style checks, especially skills and MCP-related components.
- The right fit is an installable security integration with explicit operator control.

## Positioning

- Optional managed integration package
- Off by default
- Admin-controlled
- Scoped to security/technical operational profiles
- Results visible in HermesHQ, not only as raw CLI output

## Non-goals

- Do not run on every agent turn
- Do not make it part of the one-line installer
- Do not hardwire Snyk-specific UI into the platform where a generic integration action model is better
- Do not overclaim support for arbitrary workspace scanning if the upstream tool is primarily centered on skills and MCP components

## Proposed phases

### Phase 1

Deliver a useful local-first integration package with:

- managed integration package `snyk-agent-scan`
- secret-based configuration (`SNYK_TOKEN`)
- health test
- manual action to scan installed agent skills
- basic result summary
- ActivityLog entry for traceability

Notes:

- Keep the scope honest: phase 1 scans installed skills for an agent.
- Use a generic `integration actions` backend/UI path so Snyk does not require a special-case screen.

### Phase 2

- Persist security findings in a dedicated model/table
- richer findings UI with filters and drill-down
- scheduled scans
- package-level scan support for uploaded integration packages

### Phase 3

- policy controls
- blocking/approval workflows
- org-wide security reporting
- possible separation into a dedicated external package repo

## Phase 1 architecture

### Package model

Package lives as a managed integration package with:

- `manifest.yaml`
- `healthcheck.py`
- `actions.py`
- optional companion skill

Phase 1 does not require agent runtime tools yet. The value is admin-triggered scanning and visibility.

### Integration metadata

- slug: `snyk-agent-scan`
- supported profiles: `technical`, `security`
- required field: `api_key_ref`
- secret provider: `snyk`
- test action: `health`
- actions:
  - `scan_skills`

### Execution model

- HermesHQ runs the scanner from backend-controlled code
- HermesHQ bootstraps a dedicated tool runtime under a managed cache/venv
- `SNYK_TOKEN` is injected only for the scan process
- scans target the agent skill directory under `HERMES_HOME/skills`

### Result handling

Phase 1 returns:

- success/failure
- summary counts
- raw JSON payload from `agent-scan --json`

And writes an `ActivityLog` event for the agent such as:

- `security.snyk.scan_skills`

## UX shape

### Settings -> Integrations

- install/uninstall `Snyk Agent Scan`
- see required secret provider and supported profiles

### Agent -> Integrations

- configure `SNYK_TOKEN` secret ref
- `Test connection`
- `Run skill scan`
- see the latest result inline after a run

## Operational constraints

- Snyk token required
- network egress required from backend
- scanning is explicit and manual in phase 1
- Snyk receives scan-related data according to their tool behavior and terms

## Future repo strategy

If the integration becomes production-critical, move it to a separate repo and install it through HermesHQ as a package artifact rather than bundling it permanently in core.
