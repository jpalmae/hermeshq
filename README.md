# HermesHQ

Initial end-to-end implementation for a multi-agent operations panel based on the specification in [HermesHQ_AGENTS.md](./HermesHQ_AGENTS.md).

## Included in this cut

- FastAPI backend with:
  - JWT auth
  - per-user theme preference override
  - admin/user RBAC with assigned-agent scope
  - local node bootstrap
  - agent CRUD
  - per-agent avatar upload
  - task submission/cancellation
  - strict local agent runtime via `hermes-agent`
  - activity feed
  - websocket event stream
  - inter-agent comms
  - secrets vault
  - templates
  - scheduled tasks
  - workspace explorer APIs
  - local PTY websocket
- React/Vite frontend with:
  - Nothing-inspired dark mode foundation
  - login screen
  - dashboard overview
  - agents list/detail
  - tasks board
  - nodes
  - comms
  - settings
  - users and assignments
  - workspace editor
  - PTY terminal pane
  - per-agent Telegram channel management
  - per-user operator avatar

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
- Telegram bindings are now managed per agent. HermesHQ writes the agent's `.hermes/config.yaml` and `.hermes/.env`, then supervises `hermes gateway run` for that agent. Configure a bot token as a secret and reference it from the agent detail page.
- Agent avatars are stored per agent under the persistent workspaces volume and are rendered across the agent detail page, dashboard and dependency canvas.
- User avatars are stored separately from branding and can be managed from the `Users` page. The active operator avatar is reflected in the shell and dashboard.
- Instance theme is still controlled by admins in `Settings`, but each user can now override the theme from the left shell without needing admin access.
- Local node metrics are real. Remote node provisioning and remote node metrics are still not implemented and return `501`.
- Redis-backed pub/sub, remote node daemon/provisioning and xterm.js-grade PTY rendering are still incomplete versus the full spec.
