# AGENTS.md

Guía para desarrolladores y agentes que trabajan en este repo.

## Setup

```bash
git clone https://github.com/jpalmae/hermeshq.git
cd hermeshq

# Backend
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .  # instala hermes-agent desde el repo de NousResearch

# Frontend
cd ../frontend && npm install
```

## Tests

```bash
# Backend (requiere PostgreSQL en localhost:5432 o usar Docker)
cd backend && python -m pytest tests/ -q

# Frontend (cuando existan tests de Vitest)
cd frontend && npm test
```

## Typecheck

```bash
cd frontend && npx tsc --noEmit
```

## Lint

```bash
# Pre-commit (ruff + mypy + eslint + hooks)
pre-commit run --all-files

# Solo ruff
cd backend && ruff check . && ruff format --check .

# Solo eslint
cd frontend && npx eslint src
```

## Arranque local

```bash
# Docker (recomendado)
cp .env.example .env  # editar secrets
docker compose up -d

# Bare-metal
cd backend && uvicorn hermeshq.main:app --reload
cd frontend && npm run dev
```

## Estructura del repo

```
backend/hermeshq/
  main.py              App entrypoint, lifespan, rutas
  config.py            Settings (Pydantic Settings)
  database.py          Engine, session factory, init_database()
  core/
    events.py          EventBroker (WebSocket streaming)
    security.py        Auth helpers
  routers/             FastAPI routers (agents, tasks, auth, comms, etc.)
  services/            Lógica de negocio
    agent_supervisor.py  Task execution + lifecycle
    hermes_runtime.py    Hermes Agent runtime (subprocess + fallback)
    hermes_installation.py  Venv management, config.yaml, gateway env
    gateway_supervisor.py  Gateway lifecycle (Telegram, WhatsApp, etc.)
    agent_builder.py     Agent Builder (conversational LLM)
    voice.py             STT (Whisper) + TTS (edge-tts/piper)
    scheduler.py         Cron-based scheduled tasks
  models/              SQLAlchemy models
  schemas/             Pydantic schemas
  plugin_templates/    Plugins inyectados a agents (pdf, audio, comms)
  scripts/             hermes_task_runner.py (subprocess del runtime)
frontend/src/
  pages/               Route components (AgentDetailPage, TasksPage, etc.)
  components/          Reusable components
  api/                 API client + react-query hooks
  stores/              Zustand stores (session, realtime, ui)
```

## Convenciones

- **Branches:** `main` = producción, `unstable` = desarrollo. Todo feature work va a `unstable`.
- **Versionado:** CalVer `YYYY.M.D.N` en `VERSION`. Auto-bump en merge a main.
- **Commits:** Conventional commits (`feat:`, `fix:`, `security:`, `chore:`).
- **PRs:** Squash merge. Target `unstable` para features, `main` solo para sync.
- **Tests:** Backend en `backend/tests/`. Naming: `test_*` en clases `Test*`.
- **Sin comentarios en código** salvo que se pidan explícitamente.

## Variables de entorno clave

| Var | Descripción |
|-----|-------------|
| `JWT_SECRET` | Secreto para firmar JWTs. Obligatorio en producción. |
| `ADMIN_PASSWORD` | Password del usuario admin inicial. |
| `DEBUG` | `true` para bypass de checks de seguridad (dev only). |
| `DATABASE_URL` | URL de PostgreSQL async (`postgresql+asyncpg://...`). |
| `HERMES_VERSION` | Versión de hermes-agent a instalar (tag de git). |

## Servidor de pruebas

- **Host:** `100.72.224.83` (Tailscale)
- **User:** `smith2-jpe`
- **App:** `https://sixagentic.sixmanager.io`
- **Deploy:** `git pull origin main && docker compose up -d --build backend`
