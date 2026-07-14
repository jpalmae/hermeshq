# Specs de Mejoras — 2026-07-13

Revisión completa del repo `jpalmae/hermeshq` (branch `unstable`). Reemplaza y cierra
`mejoras_propuestas_20260701.md`: los items 4 (locks gateway), 5 (path hardcoded), 7
(graceful shutdown) y 8 (CI) de ese documento **ya están resueltos** y se verificaron en
código. Lo pendiente de ese doc se re-especifica aquí junto con hallazgos nuevos.

Prioridades: **P0** = seguridad / corregir ya · **P1** = esta iteración · **P2** = siguiente iteración.

---

## P0 — Seguridad

### SPEC-01 · Eliminar defaults inseguros de secretos en docker-compose

**Prioridad:** P0 · **Esfuerzo:** S (½ día)

**Problema.** `docker-compose.yml` define fallbacks inseguros que se activan silenciosamente
si el operador no exporta las variables:

- Línea 42: `JWT_SECRET: ${JWT_SECRET:-change-me}`
- Línea 59: `ADMIN_PASSWORD: ${ADMIN_PASSWORD:-admin123}`
- Líneas 8/41: `POSTGRES_PASSWORD:-hermeshq`

Es especialmente grave porque `backend/hermeshq/config.py:70-95` tiene lógica para
auto-generar un `JWT_SECRET` fuerte cuando está vacío — pero el default `change-me` del
compose **nunca llega vacío**, así que la auto-generación se anula y la instancia queda
firmando JWTs (y derivando la clave Fernet del SecretVault, ver comentario en
`config.py:74`) con un secreto público y adivinable.

**Solución propuesta.**
1. En `docker-compose.yml`: cambiar a sintaxis obligatoria `${JWT_SECRET:?Debe definir JWT_SECRET}`
   y `${ADMIN_PASSWORD:?Debe definir ADMIN_PASSWORD}`, o quitar el default para que llegue
   vacío y opere la auto-generación de `config.py`.
2. En `config.py`: además de auto-generar cuando está vacío, **rechazar el arranque** si
   `jwt_secret` está en una denylist (`change-me`, `secret`, `changeme`, longitud < 16).
3. Documentar en README/install.sh la generación: `openssl rand -base64 32`.
4. Revisar `install.sh` para que genere secretos aleatorios en la instalación.

**Criterios de aceptación.**
- `docker compose up` sin `JWT_SECRET` definido: o falla con mensaje claro, o arranca con
  secreto auto-generado fuerte persistido en `.env` — nunca con `change-me`.
- Test en `backend/tests/` que verifica el rechazo de secretos débiles.
- `ADMIN_PASSWORD` sin default débil.

---

### SPEC-02 · Hardening del endpoint de voz (rate limit STT + semáforo TTS)

**Prioridad:** P0 · **Esfuerzo:** M (1 día) · *(pendiente de julio, items 1 y 6)*

**Problema.** Verificado en código actual:
- `backend/hermeshq/services/voice.py` solo tiene `_STT_SEMAPHORE` (líneas 23-34).
  No existe semáforo para TTS: N requests simultáneos lanzan N procesos edge-tts/piper.
- No hay rate limiting por usuario en el router de voz (`routers/voice.py`) — STT con
  Whisper es CPU-intensivo; un usuario autenticado puede saturar el server. El patrón ya
  existe en el repo: `routers/auth.py:82` (`_check_login_rate`) hace rate limit por IP.

**Solución propuesta.**
1. Añadir `_TTS_SEMAPHORE` en `voice.py` espejo del de STT, con límite configurable
   (`HERMESHQ_TTS_MAX_CONCURRENT`, default mayor que STT porque TTS es más liviano).
2. Rate limit por usuario (no solo IP) en los endpoints STT/TTS reutilizando el patrón de
   `_check_login_rate`: ventana deslizante, p. ej. 20 STT/min y 60 TTS/min, HTTP 429 con
   `Retry-After`.
3. Validar tamaño máximo del audio subido antes de decodificar (p. ej. 25 MB / 5 min).

**Criterios de aceptación.**
- Tests: exceder el límite devuelve 429; concurrencia TTS acotada al valor del semáforo.
- Límites configurables por env var y documentados.

---

### SPEC-03 · Sanitización de HTML en el plugin PDF

**Prioridad:** P0 · **Esfuerzo:** S (½ día) · *(pendiente de julio, item 10)*

**Problema.** `backend/hermeshq/plugin_templates/hermeshq_pdf/__init__.py:100` pasa HTML
directo a `weasyprint.HTML` sin sanitizar. WeasyPrint resuelve recursos externos
(`<img src>`, `<link>`, `url()`): un agente que renderice HTML de origen no confiable
habilita SSRF (leer endpoints internos / metadata del cloud) y lectura de archivos locales
vía `file://`.

**Solución propuesta.**
1. Sanitizar el HTML de entrada con `bleach` o `nh3` (allowlist de tags/atributos de
   formato; sin `link`, `iframe`, `object`; `src` solo `data:` o https de dominios permitidos).
2. Pasar un `url_fetcher` custom a WeasyPrint que bloquee `file://` y redes privadas
   (127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16) — defensa en profundidad.
3. Test con payloads: `<img src="file:///etc/passwd">`, `url(http://169.254.169.254/…)`.

**Criterios de aceptación.**
- Los payloads del punto 3 no producen fetch ni inclusión de contenido; test automatizado.

---

## P1 — Higiene del repositorio

### SPEC-04 · Purgar binarios del historial git (466 MB → ~50 MB)

**Prioridad:** P1 · **Esfuerzo:** M (1 día, requiere coordinación) · **⚠️ Destructivo: requiere force-push**

**Problema.** `sixagentic.apk` (20 MB) y `sixagentic.ipa` (19.7 MB) están trackeados y el
`.apk` tiene **14+ versiones en el historial** (~19 MB cada una, verificado con
`git rev-list --objects`). El `.git` pesa 466 MB: clones y CI lentos.

**Solución propuesta.**
1. Mover los binarios actuales a **GitHub Releases** (o artefactos de CI) y enlazarlos
   desde el README.
2. `git rm sixagentic.apk sixagentic.ipa` + añadir `*.apk` / `*.ipa` a `.gitignore`.
3. Reescribir historial: `git filter-repo --path sixagentic.apk --path sixagentic.ipa --invert-paths`.
4. Coordinar: avisar a todo clon existente (re-clonar tras el force-push), re-apuntar PRs abiertos.
5. El hook `check-added-large-files` (ya en `.pre-commit-config.yaml`, max 500 KB) previene
   reincidencias — verificar que todos los devs tengan pre-commit instalado.

**Criterios de aceptación.**
- `du -sh .git` < 60 MB tras re-clonar; los binarios descargables desde Releases.

---

### SPEC-05 · Limpieza de la raíz del repo

**Prioridad:** P1 · **Esfuerzo:** S (½ día)

**Problema.** La raíz acumula documentos de trabajo y archivos temporales:
`SPEC_ISSUE_7.md`, `SPEC_ISSUE_8.md`, `SPEC_RESPONSE_ATTACHMENTS.md`,
`SPEC_AI_AGENT_BUILDER.md`, `DESIGN_CONCURRENCY_SIZING.md`, `hermes-versioning.md`,
`snyk.md`, `mejoras_propuestas_20260701.md`, `debates/` (vacío), 4 screenshots
`tmp-*.png` (~1 MB, ya ignorados pero presentes en disco).

**Solución propuesta.**
1. Crear `docs/specs/` y mover ahí todos los `SPEC_*.md` + `DESIGN_*.md`.
2. Mover `hermes-versioning.md` y `snyk.md` a `docs/`.
3. Cerrar `mejoras_propuestas_20260701.md`: convertir los items aún pendientes en issues
   de GitHub (referenciando estas specs) y eliminar el archivo.
4. Eliminar `debates/` y los `tmp-*.png` locales.

**Criterios de aceptación.**
- Raíz solo con: README, CHANGELOG, LICENSE, VERSION, install.sh, docker-compose.yml,
  configs y los directorios de código.

---

### SPEC-06 · Dependabot + actualización de hooks pre-commit

**Prioridad:** P1 · **Esfuerzo:** S (½ día)

**Problema.** No existe `.github/dependabot.yml` (verificado). Los hooks de
`.pre-commit-config.yaml` están desactualizados: ruff `v0.5.0` (actual ~0.9+), mypy
`v1.10.0`, eslint `v9.5.0`. Sin actualización automática, las versiones divergen de las
del CI y se pierden fixes de seguridad.

**Solución propuesta.**
1. Crear `.github/dependabot.yml` con 4 ecosistemas: `pip` (`/backend`), `npm`
   (`/frontend`), `npm` (`/backend/hermeshq/assets/whatsapp-bridge`), `github-actions` (`/`).
   Agrupar updates menores/patch en un solo PR semanal para no inundar.
2. `pre-commit autoupdate` + corregir lo que rompa el ruff nuevo.
3. Añadir job de CI que corra `pre-commit run --all-files` (ver SPEC-08).

**Criterios de aceptación.**
- Dependabot abre PRs; `pre-commit run --all-files` pasa limpio en CI.

---

## P1 — Calidad y testing

### SPEC-07 · Infraestructura de tests de frontend (Vitest)

**Prioridad:** P1 · **Esfuerzo:** L (2-3 días)

**Problema.** **0 tests de frontend** (verificado: ningún `*.test.*` en `frontend/src`)
contra ~447 tests de backend. `package.json` ni siquiera tiene script `test` ni `lint`.
Los componentes más críticos y grandes no tienen red de seguridad.

**Solución propuesta.**
1. Instalar `vitest`, `@testing-library/react`, `@testing-library/user-event`, `jsdom`,
   `msw` (mock de API).
2. Scripts en `package.json`: `"test": "vitest run"`, `"test:watch": "vitest"`,
   `"lint": "eslint src"` (el `eslint.config.js` ya existe).
3. Primeros objetivos (mayor riesgo/valor):
   - `src/api/client.ts` — interceptors de auth y manejo de errores.
   - `src/components/ChannelForm.tsx` (685 líneas) — validación de formulario.
   - `src/components/AiAgentBuilder.tsx` (412 líneas) — flujo del builder con msw.
   - Stores de zustand y hooks extraídos en SPEC-09.
4. Job `frontend-test` en `ci.yml`.

**Criterios de aceptación.**
- `npm test` corre en CI y falla el pipeline si algo rompe; ≥ 25 tests iniciales cubriendo
  los 4 objetivos.

---

### SPEC-08 · Lint y cobertura en CI

**Prioridad:** P1 · **Esfuerzo:** S (½-1 día)

**Problema.** El CI actual (`.github/workflows/ci.yml`) corre pytest, `tsc --noEmit` y
builds Docker, pero **no corre ruff, mypy ni eslint** — solo viven en pre-commit, que es
opcional en cada máquina. Tampoco hay medición de cobertura.

**Solución propuesta.**
1. Job `lint` (rápido, sin Postgres): `ruff check backend/ && ruff format --check backend/`,
   `mypy` (misma config que pre-commit), `eslint frontend/src`.
2. Añadir `--cov=hermeshq --cov-report=xml --cov-fail-under=60` al job de backend
   (medir baseline real primero y fijar el umbral justo debajo; subirlo gradualmente).
3. Hacer `lint` requisito de los jobs de build (`needs: [lint]`).

**Criterios de aceptación.**
- PR con error de lint o cobertura bajo el umbral → CI rojo. Badge de CI en README.

---

### SPEC-09 · Refactor `AgentDetailPage.tsx` (1.794 líneas)

**Prioridad:** P1 · **Esfuerzo:** L (2-3 días)

**Problema.** `frontend/src/pages/AgentDetailPage.tsx` concentra 1.794 líneas: detalle,
chat/ledger, terminal, canales, configuración. Es la página más tocada (varios fixes de
los PRs #108-#114 pasaron por aquí) y la más propensa a regresiones. `AgentsPage.tsx`
(960) y `ChannelForm.tsx` (685) tienen el mismo problema en menor grado.

**Solución propuesta.**
1. Extraer por sección a `src/pages/agent-detail/`: `OverviewTab.tsx`, `ChatLedger.tsx`,
   `TerminalTab.tsx`, `ChannelsTab.tsx`, `SettingsTab.tsx` (espejo del patrón que ya
   existe en `src/components/settings/`).
2. Extraer data-fetching a hooks (`useAgentDetail`, `useAgentChannels`) sobre react-query.
3. Sin cambios de comportamiento: refactor puro, verificado manualmente + tests de SPEC-07
   sobre los hooks extraídos.
4. Regla eslint `max-lines` (p. ej. 500) como warning para frenar el crecimiento.

**Criterios de aceptación.**
- Ningún archivo del refactor > 500 líneas; `tsc` y build pasan; funcionalidad idéntica.

---

### SPEC-10 · Dividir `routers/auth.py` (1.339 líneas, 20 endpoints)

**Prioridad:** P1 · **Esfuerzo:** M (1-2 días)

**Problema.** `backend/hermeshq/routers/auth.py` mezcla 4 dominios: login local + rate
limit, MFA completo (tokens, códigos, envío), OIDC (discovery, provision), y utilidades
de avatar/cookies/serialización. 20 endpoints en un archivo dificultan el review — crítico
en el archivo más sensible del backend.

**Solución propuesta.**
1. Paquete `routers/auth/`: `local.py` (login/logout/refresh + rate limit), `mfa.py`,
   `oidc.py`, `helpers.py` (cookies, `_serialize_user`, avatar), `__init__.py` que agrega
   los sub-routers preservando rutas y `tags`.
2. Mover `_check_login_rate`/`_record_login_attempt` a un módulo reutilizable
   (`core/rate_limit.py`) — lo consume también SPEC-02.
3. Los tests existentes (`test_auth_rate_limit.py`, `test_auth_refresh.py`) deben pasar
   sin modificación (mismas rutas públicas).

**Criterios de aceptación.**
- Rutas y OpenAPI idénticos (diff de `/openapi.json` antes/después vacío); tests verdes.

---

### SPEC-11 · Dividir `services/hermes_installation.py` (1.406 líneas, 60 funciones)

**Prioridad:** P2 · **Esfuerzo:** M (1-2 días)

**Problema.** 60 funciones en un solo servicio: descarga/instalación del runtime Hermes,
sanitización de entorno, verificación de versiones, wiring de workspaces.

**Solución propuesta.** Paquete `services/hermes_installation/` separado por fase
(`download.py`, `install.py`, `environment.py`, `verify.py`) con fachada que preserve la
API pública actual. Aprovechar para tipar retornos y reducir los `except Exception`
locales (ver SPEC-13).

**Criterios de aceptación.** Imports externos sin cambios; tests de instalación verdes.

---

## P2 — Robustez y operación

### SPEC-12 · Persistencia y truncado del Agent Builder

**Prioridad:** P2 · **Esfuerzo:** M (1-2 días) · *(pendiente de julio, items 2 y 3)*

**Problema.** Verificado en `backend/hermeshq/services/agent_builder.py`:
- `_sessions: dict[str, BuilderSession]` (línea 97) — en memoria. Un restart del backend
  (deploy, crash, supervisor) mata todas las sesiones de builder a mitad de flujo. Ya hay
  expiración (`is_expired`, `purge_expired_sessions`), pero no persistencia.
- `self.history: list[dict[str, str]]` (línea 86) crece sin límite: conversaciones largas
  inflan cada request al LLM (costo + riesgo de exceder contexto).

**Solución propuesta.**
1. Truncado: mantener system prompt + últimos N turnos (p. ej. 30) o presupuesto de
   caracteres; es el fix barato y de mayor impacto.
2. Persistencia: serializar `BuilderSession` a la base (tabla `builder_sessions` con TTL)
   o a Redis si se introduce; rehidratar en `get_builder_session` si no está en memoria.
3. Si el backend escala a >1 worker, la persistencia deja de ser opcional — documentarlo.

**Criterios de aceptación.**
- Test: sesión sobrevive a un restart simulado; history nunca supera el límite configurado.

---

### SPEC-13 · Política de manejo de excepciones (90× `except Exception`)

**Prioridad:** P2 · **Esfuerzo:** M (continuo)

**Problema.** 90 ocurrencias de `except Exception` en `backend/hermeshq` (0 bare
`except:`, bien). Muchas probablemente tragan errores que deberían loggearse con stack
trace o propagarse — dificulta diagnosticar los bugs tipo los de los PRs #108-#114.

**Solución propuesta.**
1. Auditoría rápida clasificando cada una: (a) legítima (boundary de proceso/gateway),
   (b) debe capturar excepción específica, (c) debe propagarse.
2. Regla mínima: todo `except Exception` legítimo debe loggear con `logger.exception(...)`
   (stack trace) y nunca `pass` silencioso.
3. Habilitar reglas ruff `BLE` (blind-except) y arreglar por módulos, empezando por
   `services/` de gateways y supervisor.

**Criterios de aceptación.**
- `ruff check --select BLE` limpio o con `noqa` justificado por comentario; cero
  `except Exception: pass` sin log.

---

### SPEC-14 · Observabilidad: métricas Prometheus

**Prioridad:** P2 · **Esfuerzo:** M (1-2 días) · *(pendiente de julio, item 12)*

**Problema.** Existe `/health` (`main.py:393`) y health de dashboard/MCP/nodos, pero no
hay `/metrics`. Para un control plane que supervisa procesos de agentes y gateways, no hay
forma de graficar restarts, crash loops, latencia de requests ni profundidad de colas.

**Solución propuesta.**
1. `prometheus-fastapi-instrumentator` para métricas HTTP estándar en `/metrics`
   (protegido por auth admin o red interna).
2. Métricas custom: `hermeshq_gateway_restarts_total{agent,channel}`,
   `hermeshq_agent_processes_running`, `hermeshq_stt_requests_total`,
   `hermeshq_builder_sessions_active` — instrumentar en `gateway_process_manager.py` y
   `agent_supervisor.py`, que ya cuentan estos eventos.
3. Dashboard Grafana de ejemplo en `docs/` (opcional).

**Criterios de aceptación.**
- `/metrics` expone las series; test de humo que verifica su presencia.

---

### SPEC-15 · Code-splitting del frontend

**Prioridad:** P2 · **Esfuerzo:** S (½ día)

**Problema.** No hay `React.lazy`/`Suspense` (verificado en `App.tsx`/`main.tsx`): las 10+
páginas (incluidas `ManualPage` de 1.097 líneas y `AgentTerminal` con xterm, que es pesado)
van en un solo bundle inicial.

**Solución propuesta.**
1. `React.lazy` + `Suspense` por ruta en el router; fallback con el spinner existente.
2. Import dinámico de `@xterm/xterm` solo al montar el terminal.
3. Medir con `vite build` antes/después (reportar tamaños en el PR).

**Criterios de aceptación.**
- Bundle inicial reducido ≥ 30%; navegación sin regresiones visibles.

---

### SPEC-16 · Detección de silencio / validación en STT

**Prioridad:** P2 · **Esfuerzo:** S-M (1 día) · *(pendiente de julio, item 11)*

**Problema.** `voice.py` (289 líneas) decodifica y transcribe todo lo que llega, sin
detectar audio vacío/silencio. Whisper sobre silencio gasta CPU y suele alucinar texto.

**Solución propuesta.**
1. Tras el resample a 16 kHz mono, calcular energía RMS; si está bajo umbral durante todo
   el clip, responder `{"text": "", "reason": "silence"}` sin invocar Whisper.
2. Umbral configurable (`HERMESHQ_STT_SILENCE_RMS`); opcional `webrtcvad` si el RMS simple
   da falsos positivos.

**Criterios de aceptación.**
- Test con WAV de silencio → respuesta vacía sin llamar al modelo (mock del transcriptor).

---

### SPEC-17 · `AGENTS.md` en la raíz

**Prioridad:** P2 · **Esfuerzo:** S (2 h) · *(pendiente de julio, item 9)*

**Problema.** Sin documento de comandos para devs/agentes. `HermesHQ_AGENTS.md` (56 KB)
es otra cosa (specs de producto), y su nombre confunde.

**Solución propuesta.**
1. Crear `AGENTS.md` conciso: setup, tests backend (`cd backend && python -m pytest tests/`),
   typecheck (`cd frontend && npx tsc --noEmit`), lint (`pre-commit run --all-files`),
   arranque local (docker-compose y bare-metal), mapa de directorios (routers/services/
   plugin_templates), convenciones (ramas `main`/`unstable`, versionado CalVer).
2. Renombrar `HermesHQ_AGENTS.md` → `docs/specs/PRODUCT_SPEC_AGENTS.md` (junto con SPEC-05).

**Criterios de aceptación.**
- Un dev nuevo puede correr tests y lint solo con el `AGENTS.md`.

---

## Orden de ejecución sugerido

| Semana | Specs | Tema |
|--------|-------|------|
| 1 | SPEC-01, SPEC-03, SPEC-02 | Seguridad (P0) |
| 1-2 | SPEC-05, SPEC-06, SPEC-17, SPEC-08 | Higiene rápida + CI (baratas, desbloquean el resto) |
| 2 | SPEC-04 | Purga del historial (coordinar force-push) |
| 2-3 | SPEC-07 | Tests de frontend |
| 3-4 | SPEC-09, SPEC-10 | Refactors grandes con la red de tests ya puesta |
| 5+ | SPEC-11 a SPEC-16 | Robustez y operación |

**Dependencias:** SPEC-07 antes de SPEC-09 (tests protegen el refactor) · SPEC-10 crea
`core/rate_limit.py` que reutiliza SPEC-02 (o viceversa; coordinar) · SPEC-04 mejor tras
mergear PRs abiertos para no invalidarlos.
