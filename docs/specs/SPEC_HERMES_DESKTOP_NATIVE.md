# SPEC: Conexión nativa Hermes Desktop → HermesHQ

**Fecha:** 2026-09-17
**Estado:** Propuesta
**Objetivo:** Permitir que Hermes Desktop (cliente oficial de NousResearch) se conecte a HermesHQ de forma nativa — a través de la autenticación, ciclo de vida y políticas de HermesHQ — sin acceso paralelo ni bypass del sistema de permisos.

---

## Contexto

Hermes Desktop se conecta exclusivamente a un **gateway `hermes serve`** (contrato `tui_gateway`: JSON-RPC sobre HTTP + WebSocket, 218 métodos, spec en `apps/shared/src/gateway-contract.openrpc.json`). Modos soportados: Local, Remote gateway (URL + token/OAuth, acepta prefijos de path vía reverse proxy), SSH y Hermes Cloud.

HermesHQ hoy ejecuta Hermes como subprocess por tarea; no expone `tui_gateway`. Cada agente tiene `HERMES_HOME = <workspace>/.hermes` gestionado por HermesHQ (config.yaml, .env, sesiones, memoria, skills) y venvs versionados compartidos.

Restricciones de la solución nativa:
1. Desktop no se modifica (no fork)
2. Auth única: JWT de HermesHQ (o token emitido por HermesHQ)
3. Permisos: las policies de HermesHQ deben aplicar dentro de la sesión Desktop
4. HermesHQ sigue siendo source of truth de config (sin drift)
5. Todo el tráfico por el dominio único con TLS (`sixagentic.sixmanager.io`)

---

## Opción A — "Desktop Bridge": HermesHQ gestiona `hermes serve` por agente (recomendada)

HermesHQ añade un servicio que arranca/proxya un gateway real por agente, bajo control total del lifecycle.

### Arquitectura

```
Hermes Desktop ── HTTPS ──> sixagentic.sixmanager.io
                             └─ /desktop/{agent_id}/api/*  (FastAPI router)
                                  ├─ auth: JWT HermesHQ + allowlist por agente
                                  ├─ HTTP: httpx reverse proxy → 127.0.0.1:{port}
                                  └─ WS:   puente asyncio → 127.0.0.1:{port}/api/ws
                                            (token de sesión inyectado)
DesktopGatewayService (nuevo, patrón gateway_supervisor)
  ├─ on-demand: primer request → spawn `hermes serve --port 0 --host 127.0.0.1`
  │    con HERMES_HOME=<workspace>/.hermes y venv del agente
  ├─ idle reaper: sin conexiones 30 min → stop del proceso
  ├─ healthcheck + restart con backoff
  └─ registry en memoria: agent_id → (pid, port, token sesión)
```

- Desktop registra la conexión como **Remote gateway** → `https://sixagentic.sixmanager.io/desktop/{agent_id}` (los prefijos de path están soportados por diseño)
- Auth: modo "session token" — HermesHQ emite el token al usuario desde la UI y lo inyecta en cada request proxyeado; el usuario nunca ve credenciales del serve interno
- Contrato completo (218 métodos) gratis: es Hermes real — terminal pane, file browser, voice, artifacts funcionan contra el workspace del agente en el servidor
- Sesiones compartidas: mismas conversaciones que WhatsApp/Telegram (mismo state DB, WAL + lockguards soportan coexistencia multi-proceso)

### Permisos nativos (sin bypass)

Hermes ya expone hooks de plugin (`ctx.register_hook("pre_tool_call", ...)` — patrón usado por `plugins/security-guidance`). HermesHQ ya sincroniza plugins gestionados en cada home (`_sync_managed_plugins`):

- Nuevo plugin gestionado `hermeshq_guard`: en `pre_tool_call` llama a `POST /internal/control/permissions/evaluate` (HMAC con `HERMESHQ_AGENT_TOKEN`, igual que la extensión TS del runtime Pi) y bloquea/tools allow según `_merge_policies`
- Resultado: la sesión Desktop queda sujeta a las mismas policies que las tareas normales, incluido chaining y approval flows

### Config sin drift

- HermesHQ sigue reescribiendo config.yaml/.env en cada sync (comportamiento actual)
- El plugin guard valida versión de state schema vs venv usado por el agente

### Fases

| Fase | Alcance | Estimación |
|---|---|---|
| A1 | `DesktopGatewayService` (spawn/idle-reap/restart) + router proxy HTTP+WS + auth JWT + tests | 4-5 días |
| A2 | Plugin `hermeshq_guard` (pre_tool_call → enforcer) + tests de integración con policies | 2-3 días |
| A3 | UI: toggle por agente "Acceso Desktop", emisión/revocación de token, panel de sesiones activas | 2 días |
| A4 | Endurecimiento: rate-limit por usuario, timeout de token, auditoría de conexiones en ActivityLog, docs | 1-2 días |

**Total: ~2 semanas**

### Riesgos

- Puente WS (asyncio raw frames) — el punto más delicado; mitigar con tests E2E contra un serve real
- RAM por proceso serve (~150-250 MB por agente activo) — mitigado por idle reaper
- Contract drift entre versiones de Hermes y Desktop — fijar `HERMES_VERSION` como ya hace HermesHQ

---

## Opción B — "Gateway Facade": HermesHQ implementa el contrato tui_gateway

HermesHQ habla el protocolo directamente; no hay subprocess Hermes para servir Desktop.

### Arquitectura

```
Hermes Desktop ──> /gateway/api/status  → {"auth_required": true, "auth_providers": ["token"]}
                ──> /gateway/api/ws     → JSON-RPC bridge
                     ├─ agents.list          → Agentes de HermesHQ (1 perfil por agente:
                     │                         UNA conexión Desktop muestra TODOS los agentes)
                     ├─ session.list/create  → mapear a sesiones/tareas HermesHQ
                     ├─ prompt.submit        → crear task vía AgentSupervisor
                     ├─ stream de eventos    → EventBroker → traducción a eventos gateway
                     │                         (deltas, tool activity, completion)
                     ├─ approval.pending/respond → PermissionEnforcer (mapeo 1:1)
                     ├─ config.get/model.options → registro de providers HermesHQ
                     └─ file.attach/artifacts  → attachments API existente
```

### Ventajas sobre A

- Funciona también para agentes **Pi** (no solo runtime Hermes)
- Todo pasa por el pipeline de tareas: quotas, auditoría, policies, aislamiento, scheduler
- Una única conexión Desktop = roster completo de agentes (el modelo "profiles" de Desktop encaja perfecto)
- Cero procesos Hermes extra en el servidor

### Desventajas

- Reimplementar superficie de protocolo: subset realista ~30-45 métodos (chat, sesiones, roster, approvals, config básica), pero Desktop hace "Test" (HTTP+WS+discovery auth) y usa métodos no triviales (terminal pane, file browser, artifacts, voice) que habría que acotar/degradar con `gateway.capabilities`
- Streaming token-level: las tareas HermesHQ emiten chunks vía EventBroker — hay que traducir formato con fidelidad o el transcript de Desktop se ve degradado
- Mantenimiento: el contrato evoluciona con cada release de Hermes/Desktop — se adopta una dependencia de compatibilidad permanente
- Esfuerzo: 3-6 semanas para cobertura útil

### Fases (resumen)

1. Discovery + auth token (3d) · 2. WS JSON-RPC kernel + agents.list/session.* (5d) · 3. prompt.submit + streaming bridge (5d) · 4. approvals + capabilities + degradación elegante (4d) · 5. UI + hardening + E2E (4d)

---

## Comparativa

| Criterio | A: Bridge | B: Facade |
|---|---|---|
| Esfuerzo | ~2 semanas | 4-6 semanas |
| Cobertura funcional Desktop | 100% (Hermes real) | Subset (~70%) |
| Agentes Pi | no (runtime Hermes) | sí |
| Permisos | via plugin hook (igual que Pi) | nativo en el pipeline |
| Una conexión = todos los agentes | no (1 URL por agente; registry Desktop lo mitiga) | sí |
| Riesgo contract drift | bajo (serve real) | alto |
| Carga servidor | +1 proceso por agente activo | ninguno extra |
| Streaming/UI fidelity | idéntica al CLI | dependiente del bridge |

## Recomendación

**Opción A primero** — entrega valor completo en ~2 semanas con riesgo acotado y sin perseguir el contrato. El proxy por-agente encaja con el modelo multi-gateway de Desktop (fleet rail), y el plugin `hermeshq_guard` extiende el patrón de permisos ya probado en el runtime Pi.

**Opción B como evolución** si después se quiere soportar agentes Pi en Desktop y unificar roster — el trabajo del enforcer y del EventBroker es reutilizable.

---

## Plan de validación

1. A1 en vmpi: serve real por agente + proxy + Desktop conectando (token) — validar Test/Reachable, chat, streaming, terminal pane
2. A2: task con policy deny (bash) desde Desktop → debe bloquear el tool call con mensaje del enforcer
3. A3: token revocado → conexión cae en el próximo request
4. Batería existente + tests nuevos: `test_desktop_gateway.py`, `test_hermeshq_guard_plugin.py`
