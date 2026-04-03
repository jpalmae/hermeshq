# HermesHQ

Initial end-to-end implementation for a multi-agent operations panel based on the specification in [HermesHQ_AGENTS.md](./HermesHQ_AGENTS.md).

## Included in this cut

- FastAPI backend with:
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
  - websocket event stream
  - inter-agent comms with hierarchy-aware delegation rules
  - secrets vault
  - provider registry with editable presets
  - templates
  - scheduled tasks
  - workspace explorer APIs
  - local PTY websocket
- React/Vite frontend with:
  - Nothing-inspired dark mode foundation
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
  - per-agent Telegram channel management
  - per-user operator avatar
  - instance-wide Hermes TUI skin upload for admins

## Frontend fonts

The UI loads these Google Fonts globally:

- `Doto`
- `Space Grotesk`
- `Space Mono`

## Run backend

```bash
cd backend
.venv/bin/python -m uvicorn hermeshq.main:app --reload
```

Recommended backend setup with `uv`:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python git+https://github.com/NousResearch/hermes-agent.git
uvicorn hermeshq.main:app --reload
```

API default URL: `http://localhost:8000`

Default login:

- username: `admin`
- password: `admin123`

## Run frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend default URL: `http://localhost:5173`

## Run with Docker

```bash
docker compose up --build -d
```

URLs:

- frontend: `http://localhost:3420`
- backend: `http://localhost:8000`

## Notes

- The Docker stack uses PostgreSQL 16 and the backend connects through `asyncpg`.
- Task execution is strict: if `hermes-agent` is missing, the agent has no valid credentials, or the provider rejects the request, the task is marked `failed`.
- The bundled Docker runtime uses `/bin/sh` for PTY sessions so the embedded terminal works reliably inside the container image.
- Admins can upload a single Hermes skin YAML in `Settings`. HermesHQ distributes it to every agent installation under `.hermes/skins/`, writes `display.skin` into each agent `config.yaml`, and new TUI sessions pick up that shared look automatically.
- Telegram bindings are now managed per agent. HermesHQ writes the agent's `.hermes/config.yaml` and `.hermes/.env`, then supervises `hermes gateway run` for that agent. Configure a bot token as a secret and reference it from the agent detail page.
- Admins now manage a provider registry in `Settings`. Presets exist for Kimi Coding, Z.AI Coding Plan, OpenRouter API, OpenAI API, Gemini API and Anthropic API. Their base URLs and default models remain editable so the catalog can adapt if a provider changes its endpoint conventions.
- New agents no longer depend only on free-text provider fields. The create flow can start from a provider preset, auto-filling runtime provider, model, base URL and secret selection while still allowing manual overrides when needed.
- `Comms` now enforces real hierarchy rules for `Delegate`: independent agents can delegate freely, subordinate agents can escalate upward or delegate downward inside their own branch, and cross-branch lateral delegation is blocked.
- The `Comms` screen reflects those rules before sending by disabling invalid targets and visualizing upward, downward and blocked routes for the selected source agent.
- Delegations now create a real callback path to the delegating agent when the child task finishes. HermesHQ persists a `delegate_result` message in `Comms`, creates a follow-up task for the parent agent, and surfaces the result in the delegator runtime ledger.
- If a delegation originates from Telegram, HermesHQ now preserves the source chat context and can auto-reply into that same Telegram conversation once the delegated child task completes.
- Agent avatars are stored per agent under the persistent workspaces volume and are rendered across the agent detail page, dashboard and dependency canvas.
- User avatars are stored separately from branding and can be managed from the `Users` page. The active operator avatar is reflected in the shell and dashboard.
- Instance theme is still controlled by admins in `Settings`, but each user can now override the theme from the left shell without needing admin access.
- Instance language is controlled by admins in `Settings`, but each user can now override the UI language between English and Spanish from the left shell or `My Account`.
- The left shell exposes `My Account` for any user, including profile edits, avatar management and password changes without admin intervention.
- UI localization intentionally affects the application chrome only. It does not rewrite backend error payloads, Hermes TUI output or model-generated content already stored in tasks/logs.
- The in-app `Manual` is available from the operator section in the sidebar and includes annotated screenshots of the main operational surfaces.
- Local node metrics are real. Remote node provisioning and remote node metrics are still not implemented and return `501`.
- Redis-backed pub/sub, remote node daemon/provisioning and xterm.js-grade PTY rendering are still incomplete versus the full spec.
