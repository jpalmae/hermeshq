# SPEC: Enrolled Runtime — agentes Hermes Desktop con gestión central de HermesHQ

**Fecha:** 2026-09-23
**Estado:** Propuesta
**Depende de:** `SPEC_HERMES_DESKTOP_NATIVE.md` (Opción A implementada — reutiliza guard plugin, service tokens, policy API)
**Objetivo:** Que un agente de HermesHQ ejecute **en la máquina del usuario** vía Hermes Desktop en modo local, con el "cerebro" (config, skills, providers) gestionado por HQ y las políticas de permisos aplicadas centralmente en cada tool call.

---

## Contexto y motivación

La Opción A (bridge) mantiene la ejecución en el servidor: Desktop es solo una ventana. Pero el caso de uso real del usuario es el contrario — "la gracia del Hermes Desktop" — un agente que **tome control del PC del usuario**: navegar carpetas, escribir archivos, usar terminal.

Arquitectura de referencia de Hermes: en modo local, Desktop corre el agente completo en la máquina del usuario (HERMES_HOME local). En modo remoto, el límite de ejecución es el host del gateway — por diseño el cliente remoto no expone su filesystem.

Este spec invierte el bridge: la ejecución va al PC del usuario y HQ pasa a ser el plano de control (config sync + policies + inventario).

## Principios

1. **Ejecución local, control central**: las herramientas del agente corren en el PC del usuario; HQ solo sincroniza configuración y evalúa permisos
2. **Sin fork de Desktop**: el usuario usa Hermes Desktop stock en modo local; el enrolamiento gestiona el HERMES_HOME que Desktop consume
3. **Policies como única frontera**: en la máquina del usuario, lo único que restringe al agente es `hermeshq_guard` evaluando contra HQ
4. **Offline = decisión explícita**: sin conectividad a HQ el guard debe comportarse según una política de failover configurable (fail-open / fail-closed / fail-grace-period)
5. **Multi-tenant**: cada usuario puede enrolar sus dispositivos; un mismo agente puede tener runtime en servidor (bridge A) y en PCs (C) simultáneamente, cada uno como "ubicación de ejecución" distinta

---

## Arquitectura

```
┌─ PC del usuario ──────────────────────────────┐      ┌─ Servidor HQ ─────────────────┐
│                                                │      │                                │
│  Hermes Desktop (modo local, stock)            │      │  HermesHQ backend              │
│    └─ hermes serve local (127.0.0.1)           │      │   ├─ /api/enrollment/* (nuevo) │
│         └─ HERMES_HOME enrolado                │      │   ├─ sync engine (nuevo)       │
│              ├─ config.yaml   ← sincronizado   │◄─────┤   ├─ PermissionEnforcer        │
│              ├─ skills/        ← sincronizado  │      │   └─ /control/permissions/     │
│              ├─ plugins/hermeshq_guard ────────┼─────►│      evaluate (HMAC agent)     │
│              └─ sesiones/memoria (locales)     │      │                                │
│                                                │      │  PostgreSQL (agents, devices,  │
│  hermeshq-enroll (CLI/agente, nuevo)           │      │   enrollment tokens, audit)    │
│   ├─ registro del dispositivo                 │      └────────────────────────────────┘
│   ├─ pull periódico de config/skills          │
│   └─ heartbeat + wake-on-sync                 │      Canales (WhatsApp/Telegram) siguen
└────────────────────────────────────────────────┘      operando en el servidor (Opción A
                                                        / gateway clásico) — no cambian.
```

### Componentes nuevos

1. **`hermeshq-enroll`** — CLI pequeña (pip installable / binario) que corre en el PC del usuario:
   - `enroll https://hq.example.com` → abre browser al login de HQ, obtiene **enrollment token** (JWT corto, scope `enrollment`), registra el dispositivo (nombre, SO, hostname) y obtiene **device credentials** (service token de agente persistente + device_id)
   - `sync` — pull del bundle de configuración del agente asignado (ver abajo), escritura atómica en el HERMES_HOME enrolado
   - `run` (modo servicio/launchd/agent) — sync periódico (TTL configurable, default 5 min) + heartbeat a HQ; opcionalmente levanta `hermes serve` local si no está corriendo
   - Cero acceso a las credenciales del usuario: solo device token propio

2. **Bundle de sync** (respuesta firmada/versionada de HQ):
   - `config.yaml` renderizado con la misma lógica que `_write_config` del installation manager (system prompt, provider, modelo, base URL, API key del provider **cifrada para el dispositivo**)
   - `skills/hermeshq-managed/*` — mismas skills gestionadas
   - `plugins/hermeshq_guard/` + entrada en `plugins.enabled`
   - `SOUL.md`, settings de voz/apariencia heredados del agente
   - **No** se sincronizan: sesiones, memoria conversacional, auth store del gateway (locales del dispositivo)

3. **API de enrolamiento en HQ** (router `/api/enrollment`):
   - `POST /enroll` (user JWT) → crea `EnrolledDevice` + devuelve enrollment token corto
   - `POST /devices/{id}/activate` (enrollment token) → emite device service token (HMAC, rotable)
   - `GET /devices/{id}/bundle?since=<etag>` (device token) → bundle versionado (304 si sin cambios)
   - `POST /devices/{id}/heartbeat` (device token) → status, versión instalada, métricas mínimas
   - `DELETE /devices/{id}` (admin) → revocación inmediata (device token a la denylist hasta expiración)
   - Endpoint de evaluación reutilizado: `/control/permissions/evaluate` ya autentica por HMAC de agente — los devices obtienen el mismo tipo de token

4. **Modelo de datos**:
   - `enrolled_devices`: id, agent_id FK, user_id FK, name, os_info JSON, status (`pending|active|revoked|stale`), last_heartbeat, enroll_token_hash, token_version, public_key (futuro: cifrado del bundle)
   - Un agente puede tener N devices; `agents.desktop_execution` pasa a ser un enum conceptual: `server-bridge` (A) y/o `enrolled` (C) — compatibles entre sí

5. **Guard con failover explícito**:
   - `HERMESHQ_GUARD_FAIL_MODE` sync-eado desde la policy del agente: `fail-open` (default inicial), `fail-closed`, `fail-grace:<segundos>` (permite hasta N seg sin contacto, luego cierra)
   - Cache local firmada de la última evaluación de policies conocida (permite gracedeny en intermitencias cortas)
   - Telemetría: el guard reporta a HQ cada bloqueo (ya lo hace vía serve logs; añadir POST opcional de audit)

6. **UI en HQ**:
   - Tab "Devices" en Agent Detail: lista de dispositivos enrolados, estado, última sincronización, botones: revocar, forzar sync, rotar token
   - Wizard de enrolamiento: `hermeshq-enroll enroll <url> --code XXXX` con código corto mostrado en pantalla
   - Indicador de modo de ejecución por agente: "Servidor (bridge)" / "Dispositivos (N activos)" / ambos

### Flujo de usuario

1. Admin crea el agente en HQ, activa "Enrolled runtime", asigna policies
2. Usuario instala Hermes Desktop + `pipx install hermeshq-enroll` (o binario)
3. `hermeshq-enroll enroll https://hq.example.com` → login en browser → selecciona agente → dispositivo queda `active`
4. Primer `sync` escribe el HERMES_HOME enrolado (default `~/.hermes-hq/<agent-slug>/` para no pisar un `~/.hermes` propio)
5. Usuario abre Desktop y apunta al home enrolado (`hermes desktop --cwd` / `HERMES_HOME=... hermes desktop`, o perfil dedicado) — o Desktop detecta el home enrolado como perfil
6. Chatea: cada tool call pasa por el guard → HQ evalúa → permite/bloquea
7. Admin cambia una policy o el system prompt en HQ → próximo sync (≤5 min) lo aplica en el dispositivo

### Decisiones de seguridad

- **API keys del provider**: viajan en el bundle; se cifra el payload con clave derivada del device token (respaldo: TLS + no-log). Rotación revoca bundles previos
- **Revocación**: admin revoca device → heartbeat/evaluate devuelven 401 → el enroll agent limpia credenciales; el guard entra en fail-mode configurado (default: fail-closed tras gracia de 60s para dispositivos revocados)
- **Auditoría**: cada evaluate deja log (agente, dispositivo, tool, decisión) en ActivityLog — reutiliza el pipeline existente del enforcer
- **Escalada local**: el agente en el PC del usuario corre con los privilegios del usuario (no root/service account) — igual que Desktop stock; documentar

---

## Fases

| Fase | Alcance | Estimación |
|---|---|---|
| C1 | Modelo `enrolled_devices` + migración + API de enrolamiento (enroll/activate/heartbeat/revoke) + tests | 3-4 días |
| C2 | `hermeshq-enroll` CLI (enroll + sync + run) + bundle versionado (config/skills/guard) renderizado con lógica existente | 4-5 días |
| C3 | Guard fail-modes (`fail-open/closed/grace`) + cache firmada + audit de bloqueos | 2-3 días |
| C4 | UI: tab Devices, wizard, estado de ejecución por agente | 2-3 días |
| C5 | Hardening: cifrado del bundle con clave de dispositivo, rotación, rate-limit, E2E en dos máquinas reales | 3 días |

**Total: ~3 semanas**

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Guard offline en la máquina del usuario | fail-modes explícitos + grace period + cache firmada |
| Fuga del bundle (API keys) en el dispositivo | cifrado por dispositivo, rotación, revocación, minimizar keys (solo las del agente) |
| Deriva entre Hermes local y lo que HQ espera (versiones) | el bundle fija `hermes_version`; el enroll agent verifica versión instalada y reporta en heartbeat |
| Desktop no sabe de homes externos | perfiles de Hermes soportan homes dedicados; documentar el flujo (env var / perfil); evaluar plugin Desktop-side más adelante |
| Confusión de usuarios con dos modos (A y C) | UI etiqueta claramente el modo de ejecución por agente y por dispositivo |

## Plan de validación

1. C1-C2 en local: dos agentes (`boss` servidor-bridge y `desktop-test` enrolado) conviviendo en el mismo HQ
2. Guard E2E: policy deny bash → en el PC enrolado el agente reporta bloqueo; quitar policy → permite
3. Revocación: admin revoca → el dispositivo deja de evaluar en <60s y el sync muere
4. Offline: cortar red → comportamiento según fail-mode configurado (verificar los 3 modos)
5. Batería completa + tests nuevos (`test_enrollment.py`, `test_enroll_cli.py`, `test_guard_failmodes.py`)
