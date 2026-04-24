![HermesHQ dark hero](./frontend/public/manual/readme-hero-dark.png)

# HermesHQ

HermesHQ is a Docker-first control plane for running and operating multiple [Hermes Agent](https://github.com/NousResearch/hermes-agent) instances from one web application.

> [!WARNING]
> HermesHQ is under active development. Expect ongoing changes, rough edges, and occasional bugs while the product continues to evolve.

It keeps Hermes as the real execution engine, then adds the operational layer around it:

- managed agents with separate workspaces and `HERMES_HOME`
- web UI, RBAC, users, and assigned-agent scope
- task dispatch, schedules, runtime ledger, and activity stream
- hierarchy-aware inter-agent delegation
- per-agent Telegram and WhatsApp channels
- provider presets, secrets vault, runtime profiles, and managed integrations

Project landing page: [jpalmae.github.io/hermeshq](https://jpalmae.github.io/hermeshq/)  
Controlled demo page: [jpalmae.github.io/hermeshq/demo.html](https://jpalmae.github.io/hermeshq/demo.html)

<a href="https://jpalmae.github.io/hermeshq/demo.html">
  <img src="./frontend/public/manual/hermeshq-demo-preview.gif" alt="HermesHQ demo preview" width="100%" />
</a>

Click the preview above to open the demo page with player controls, or open the raw video directly: [HermesHQ demo video](./frontend/public/manual/hermeshq-demo.mp4)

## Hermes Agent vs HermesHQ

HermesHQ does not replace `hermes-agent`; it orchestrates it.

If you install and run `hermes-agent` directly on your machine, you get the Hermes runtime by itself:

- local CLI/TUI usage
- one local `HERMES_HOME`
- direct prompt, tool, and plugin handling
- local sessions and config managed by the operator

HermesHQ uses that same Hermes runtime underneath, but wraps it in a control plane:

- managed agents with separate workspaces and `HERMES_HOME`
- web UI, RBAC, users, and assigned-agent scope
- task dispatch, schedules, and runtime ledger
- inter-agent comms and hierarchy-aware delegation
- per-agent Telegram and WhatsApp channels
- provider presets, secrets vault, and managed integrations
- runtime profiles and capability visibility

In short:

- `hermes-agent` alone = execution engine used directly
- HermesHQ = control plane plus managed multi-agent runtime built on top of Hermes

## License

HermesHQ follows the same license as [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent): MIT.

## What It Looks Like

Default UI, without customer-specific branding:

### Dashboard

![HermesHQ dashboard](frontend/public/manual/dashboard.png)

### Agent Detail

![HermesHQ agent detail](frontend/public/manual/agent-detail.png)

### Comms

![HermesHQ comms](frontend/public/manual/comms.png)

### Integrations

![HermesHQ integrations](frontend/public/manual/integrations.png)

### Settings

![HermesHQ settings](frontend/public/manual/settings.png)

## Why HermesHQ

Hermes works well as a local runtime. HermesHQ adds the things teams usually need once they move from one agent to an actual operational setup:

- multiple agents with separate identity, prompts, skills, integrations, and channels
- central task dispatch and scheduled execution
- runtime traceability through ledger and activity events
- hierarchy-aware delegation and callbacks
- per-user access scope and admin controls
- Docker-based deployment, backup, and restore

## Core Capabilities

### Runtime and Operations

- strict local agent runtime via `hermes-agent`
- runtime profiles for `standard`, `technical`, and `security`
- task submission, cancellation, and scheduled runs
- TUI/PTY support for allowed profiles
- runtime ledger and activity stream
- real WebSocket event stream

### Multi-Agent Control Plane

- agent CRUD and local node bootstrap
- inter-agent comms with hierarchy-aware delegation rules
- delegate result callbacks back to the parent agent
- per-agent Telegram and WhatsApp channels with activity traceability
- assigned-agent scope for non-admin users

### Configuration and Governance

- JWT auth
- admin/user RBAC
- self-service profile and password changes
- per-user theme override
- per-user language override with instance default
- secrets vault
- editable provider registry and presets
- managed integration package catalog with install/uninstall and per-agent tests

### UX

- dashboard, agents, tasks, comms, users, settings, schedules
- English and Spanish UI
- in-app manual
- runtime capability visibility in `Settings` and per-agent `Integrations`
- global Hermes TUI skin upload for admins

## One-Line Install

HermesHQ installs and runs with Docker by default.

The installer:

- installs Docker automatically on supported Linux hosts if it is missing
- enables the Docker service and adds the current user to the `docker` group
- downloads the current `main` branch tarball
- installs into `~/hermeshq`
- preserves an existing `.env` if present
- preserves `.cloudflared.env` if present
- generates a new `.env` with bootstrap credentials on first install
- prints the final admin credentials at the end of the run
- rolls back a failed fresh install instead of leaving containers and images behind
- runs `docker compose up --build -d`

### Prerequisites

- `curl`
- `tar`
- `python3`

Docker is installed automatically on supported Linux hosts. On non-Linux systems, install Docker first.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/jpalmae/hermeshq/main/install.sh | bash
```

If the server has multiple interfaces, pass the public host or DNS name explicitly:

```bash
HERMESHQ_HOST=your-server-ip-or-dns curl -fsSL https://raw.githubusercontent.com/jpalmae/hermeshq/main/install.sh | bash
```

### Useful Installer Overrides

- `INSTALL_DIR=/srv/hermeshq`
- `BRANCH=main`
- `HERMESHQ_HOST=your-server-ip-or-dns`
- `ADMIN_USERNAME=admin`
- `ADMIN_PASSWORD=YourPassword123!`
- `FRONTEND_PORT=3420`
- `BACKEND_PORT=8000`
- `SKIP_START=1`

### What You Get

- frontend: `http://<host>:3420`
- backend: `http://<host>:8000`
- Docker-managed PostgreSQL and persistent workspaces
- final admin credentials printed at the end of install
- automatic rollback cleanup on failed first-time installs

## Run With Docker Manually

```bash
docker compose up --build -d
```

Default URLs:

- frontend: `http://localhost:3420`
- backend: `http://localhost:8000`

## Local Development

### Backend

```bash
cd backend
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python git+https://github.com/NousResearch/hermes-agent.git
.venv/bin/python -m uvicorn hermeshq.main:app --reload
```

API default URL: `http://localhost:8000`

Default login:

- username: `admin`
- password: `admin123`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend dev URL: `http://localhost:5173`

## Managed Integrations

HermesHQ supports managed integration packages that are separate from the core runtime.

Admins can:

- upload `.tar.gz` integration packages from `Settings -> Integrations`
- install or uninstall them globally
- enable and configure them per agent
- run per-agent connection tests

HermesHQ also includes an `Integration Factory` for administrators and `HQ Operator`.

- create a draft package scaffold from `Settings -> Factory`
- edit `manifest.yaml`, plugin files, health checks, and companion skill content in-browser
- validate the draft before publication
- publish the draft directly into `Managed Integrations`, where it becomes installable and usable by `standard` agents once enabled per agent

This turns `HQ Operator` into a practical builder/publisher for reusable capabilities instead of leaving every new skill or plugin as a one-off technical artifact.

### Integration Factory Flow

Typical flow:

1. open `Settings -> Factory`
2. create a draft with a unique `slug`, human-readable `name`, description, and template
3. review and edit the generated files:
   - `manifest.yaml`
   - `plugin/__init__.py`
   - `plugin/plugin.yaml`
   - optional `healthcheck.py`
   - optional `actions.py`
   - optional `skill/SKILL.md`
4. use `Validate` to check:
   - package structure
   - manifest/profile declarations
   - plugin metadata
   - Python syntax for every `*.py` file
5. use `Publish` when validation is clean
6. go to `Settings -> Integrations` and confirm the package is now in the managed catalog
7. enable that integration per agent from `Agent -> Integrations`
8. complete required fields and secrets, then run `Test connection`

Important behavior:

- publishing keeps the draft record, so you can continue iterating and republish later
- published drafts become uploaded managed integrations
- standard users still do not author code; they consume approved integrations after an admin enables them
- `HQ Operator` exposes matching administrative tools for the same lifecycle: list, create, edit files, validate, publish, and delete drafts

HermesHQ also supports uploaded `standard`-compatible integrations built as real plugins instead of shell wrappers. A practical example is `gamma-app`, which can be uploaded as an integration package and then enabled on administrative agents to create presentations, documents, webpages, and social content through Gamma's public API.

This separation is intentional:

- skills describe behavior
- integration packages install real plugins/tools
- runtime profiles decide which capabilities are allowed

Typical package requirements:

- `manifest.yaml` with slug, version, supported profiles, fields, and actions
- `plugin/` with `__init__.py` and `plugin.yaml`
- optional `healthcheck.py` and `actions.py`
- optional `skill/` companion content for agent guidance
- secrets referenced through HermesHQ `Settings -> Secrets`, not hardcoded into the package

## Runtime Profiles

Agents now declare a `runtime profile`:

- `standard`
- `technical`
- `security`

In the current phase:

- `standard` blocks TUI access
- `standard` blocks terminal/process usage in task execution
- built-in capabilities are visible in `Settings -> Runtime built-ins`
- each agent shows effective capabilities in `Agent -> Integrations`

This is already real enforcement for `standard` versus technical/security agents, even though the deeper execution-plane split is still a later phase.

## Hermes Agent Versions

HermesHQ can manage multiple Hermes Agent builds side by side and assign them per agent or as the instance default.

`Settings -> Hermes Versions` now has two paths:

- `Upstream releases`: HermesHQ queries the real upstream Hermes repository and lists actual tags plus the package version detected from each release
- `Manual catalog entry`: still available for advanced cases, but the backend now validates the `release_tag` against upstream before saving

Recommended flow:

1. open `Settings -> Hermes Versions`
2. use `Refresh upstream tags`
3. review the real upstream list
4. click `Add to catalog` on the release you want
5. use `Install` to materialize it under `/app/workspaces/_hermes_versions/<version>`
6. optionally set it as the instance default or pin it on specific agents

Important behavior:

- HermesHQ now treats the upstream Hermes repo as the source of truth for release tags
- `release_tag` is validated before save and before install, so broken tags fail early instead of failing only during `git checkout`
- when HermesHQ can detect a package version from upstream metadata, it proposes that as the catalog version automatically
- if the catalog label differs from the runtime version detected after install, HermesHQ surfaces a warning so rollout decisions are made against the real runtime version

## Backup and Restore

HermesHQ includes instance-level backup and restore scripts:

- [`scripts/backup-instance.sh`](scripts/backup-instance.sh)
- [`scripts/restore-instance.sh`](scripts/restore-instance.sh)

The backup captures:

- PostgreSQL as a custom-format dump
- the persistent Docker volume mounted at `/app/workspaces`
- `.env` if present
- `.cloudflared.env` if present

Create a backup:

```bash
./scripts/backup-instance.sh
```

Restore from a bundle:

```bash
./scripts/restore-instance.sh ./backups/hermeshq-backup-YYYYMMDDTHHMMSSZ.tar.gz
```

If `.cloudflared.env` is present and contains `TUNNEL_TOKEN`, the restore script also restarts `cloudflared`.

## Operational Notes

- The Docker stack uses PostgreSQL 16 and the backend connects through `asyncpg`.
- `docker-compose.yml` reads ports, admin bootstrap credentials, PostgreSQL credentials, CORS origins, and frontend API base from `.env`.
- The frontend falls back to the current browser hostname for API and WebSocket calls if `VITE_API_BASE_URL` is not explicitly set.
- Task execution is strict: if `hermes-agent` is missing, the agent has no valid credentials, or the provider rejects the request, the task is marked `failed`.
- Telegram bot tokens should be attached to only one active HermesHQ instance at a time. Running the same bot in two environments causes Telegram polling conflicts.
- UI localization affects the application chrome only. It does not rewrite backend error payloads, Hermes TUI output, or model-generated content already stored in tasks/logs.

## Included In This Cut

### Backend

- JWT auth
- per-user theme preference override
- per-user language preference override
- instance-wide default language
- self-service profile and password change for the current user
- admin/user RBAC with assigned-agent scope
- local node bootstrap
- agent CRUD
- per-agent avatar upload
- task submission/cancellation
- strict local agent runtime via `hermes-agent`
- activity feed
- WebSocket event stream
- inter-agent comms with hierarchy-aware delegation rules
- secrets vault
- provider registry with editable presets
- runtime profiles for standard, technical, and security agents
- managed integration package catalog with install/uninstall and per-agent tests
- templates
- scheduled tasks
- workspace explorer APIs
- local PTY WebSocket
- installed skill deletion per agent

### Frontend

- English/Spanish UI localization
- login screen
- dashboard overview
- agents list/detail
- tasks board
- nodes
- comms
- settings
- in-app user manual with screenshots
- self-service `My Account`
- users and assignments
- workspace editor
- PTY terminal pane
- per-agent integrations section with declarative config forms
- per-agent Telegram and WhatsApp channel management
- Telegram and WhatsApp message traceability in agent activity logs
- per-user operator avatar
- instance-wide Hermes TUI skin upload for admins

## UI Fonts

The UI loads these Google Fonts globally:

- `Doto`
- `Space Grotesk`
- `Space Mono`
