# Spec: Creación de agentes con IA, voz (STT/TTS) y descubrimiento de conectores

> **Versión:** 1.1 — revisión técnica incorporada
> **Estado:** Diseño aprobado, pendiente de implementación
> **Autor:** jpalmae
> **Fecha:** 2026-06-19

---

## 0. Contexto

Hoy la creación de agentes en HermesHQ es un formulario manual largo (`AgentsPage.tsx`):
el operador debe conocer de antemano el `runtime_profile`, los `enabled_toolsets`,
los conectores (`integration_configs`) y redactar el `system_prompt` a mano. Esto exige
conocimiento experto del producto y de qué integraciones están habilitadas.

Queremos un **Agent Builder conversacional**: el usuario describe en lenguaje natural
(texto **o voz**) qué agente necesita, y un orquestador con IA:

1. Propone un borrador completo de agente (nombre, propósito, `system_prompt`, perfil,
   toolsets).
2. **Descubre qué conectores hacen falta**, revisa si están habilitados y, si no lo están,
   le indica al usuario exactamente qué pedir a su administrador.
3. Opcionalmente **le habla al usuario** (TTS) y **entiende su voz** (STT).

### Activos que ya existen y se reutilizan

- **Creación centralizada:** `services/agent_factory.py → create_agent_from_config(payload: AgentCreate)`
  es la única fuente de verdad para crear agentes. El builder solo construye el `AgentCreate`.
- **Voz ya disponible:** `requirements` incluye `faster-whisper` (STT), `edge-tts` y `piper-tts` (TTS).
  Existen los integration packages `voice-edge` (online, voces neuronales es/en) y
  `voice-local` (offline con piper), cada uno con `manifest.yaml` y `healthcheck.py`. **No están
  cableados a una UI**: este spec los expone como servicio.
- **Catálogo de conectores declarativo:** `services/managed_capabilities.py →
  list_available_integration_packages(enabled_slugs)` devuelve cada paquete con
  `{slug, name, description, installed, required_fields, fields, plugin_slug, secret_provider, ...}`.
  El flag `installed` = `slug in AppSettings.enabled_integration_packages`. Esto es exactamente
  el insumo para que la IA razone sobre disponibilidad.
- **Secretos:** `models/secret.py` (cifrado), resueltos vía callback `resolve_secret`.

### Decisiones de diseño tomadas

- **Motor de voz: ambos, configurable.** Se expone `voice-edge` y `voice-local`; el motor
  efectivo se elige por `AppSettings` (preferir el paquete habilitado; `voice-edge` por defecto
  si ambos están habilitados).
- **Conector faltante: crear + avisar (no bloquear).** El agente se crea con lo disponible.
  Los conectores requeridos no habilitados se devuelven como una lista accionable
  ("pídele a tu administrador que habilite X y configure los campos Y").
- **Punto de entrada doble:** el botón **"Crear agente con IA"** estará disponible tanto en
  `AgentsPage` como en el **Dashboard** (`DashboardPage`).
- **Modelo del builder:** se agrega `AppSettings.builder_model` (opcional). Si no está set,
  usa el modelo default de la instancia. Al iniciar una sesión, el backend valida que el modelo
  soporta tool-calling (vía flag de catálogo o test `tools=` en el primer call). Si no lo soporta,
  el builder opera en **modo sin tools**: el LLM genera texto estructurado (JSON en el prompt) y
  el backend hace parsing manual de la respuesta. El modo se reporta en `/agent-builder/config`.
- **Infraestructura SSE:** el backend no tiene SSE actualmente (usa WebSocket vía `EventBroker`).
  Se implementa SSE con `fastapi.responses.StreamingResponse` + `media_type="text/event-stream"`
  (sin dependencia adicional). Cada evento es `data: {json}\n\n`. El cliente usa `EventSource`
  nativo o `fetch` con `ReadableStream` (para soportar POST + headers de auth).

---

## 1. Flujo de extremo a extremo

```
┌──────────────┐   voz/texto   ┌─────────────────────┐
│  Usuario     │ ────────────▶ │  AI Agent Builder    │
│ (Dashboard / │               │  (panel/modal chat)  │
│  AgentsPage) │ ◀──────────── │                      │
└──────────────┘   texto+audio └─────────┬───────────┘
                                          │
            (1) audio → POST /voice/stt   │
            (2) mensaje → POST /agent-builder/message (SSE)
            (3) respuesta texto → POST /voice/tts → <audio>
                                          │
                                          ▼
                          ┌───────────────────────────────┐
                          │  services/agent_builder.py     │
                          │  bucle LLM con tool-calling     │
                          │   • list_capabilities()         │
                          │   • propose_agent_draft()       │
                          │   • finalize_agent()            │
                          └───────────────┬───────────────┘
                                          │  determinístico
                       required_connectors│ (installed sí/no
                                          │  contra AppSettings)
                                          ▼
                          ┌───────────────────────────────┐
                          │ create_agent_from_config()     │
                          │ (agent_factory.py, sin cambios)│
                          └───────────────────────────────┘
```

### Pasos

1. **Entrada de voz (opcional):** el navegador graba audio (`MediaRecorder`), lo envía a
   `POST /voice/stt`, recibe el texto transcrito y lo coloca en el chat.
2. **Conversación:** el texto va a `POST /agent-builder/message`. El orquestador mantiene un
   *draft* de agente y un historial por sesión de builder. Responde por **SSE** (streaming),
   devolviendo en cada turno: `assistant_text`, `draft` parcial y `required_connectors`.
3. **Descubrimiento de conectores:** el LLM usa la tool `list_capabilities` para ver el
   catálogo (con `installed`). El backend recalcula de forma **determinística** la lista de
   conectores requeridos vs. habilitados (no se confía en el LLM para el gating).
4. **Salida de voz (opcional):** si el usuario activó TTS, el `assistant_text` se envía a
   `POST /voice/tts` y se reproduce.
5. **Confirmación:** cuando el draft está completo, el usuario pulsa "Crear agente". El builder
   llama a `finalize_agent`, arma el `AgentCreate` y delega en `create_agent_from_config()`.
6. **Resultado:** se devuelve el agente creado + el bloque `required_connectors` pendientes,
   que la UI muestra como instrucciones para el administrador.

---

## 2. Backend

### 2.1 Servicio de voz — `services/voice.py` (nuevo)

Responsabilidad: STT y TTS reutilizando los paquetes `voice-*` ya instalados.

- `async def transcribe(audio_bytes: bytes, *, language: str | None) -> str`
  - Usa `faster-whisper`. Lee `stt_model` / `stt_language` de la config del paquete de voz
    habilitado (defaults: `base` en ARM64, `small` en x86_64; `es`).
  - Detección de arquitectura: `platform.machine()` → si `aarch64`/`arm64`, usar `base`;
    si `x86_64`, usar `small`. Override vía config del paquete de voz.
- `async def synthesize(text: str, *, voice: str | None) -> bytes`
  - Si el motor activo es `voice-edge` → `edge-tts` (default `es-MX-JorgeNeural`).
  - Si es `voice-local` → `piper`.
- `def resolve_active_voice_engine(enabled_slugs: list[str]) -> "voice-edge" | "voice-local" | None`
  - Reglas: si ambos habilitados → `voice-edge`; si solo uno → ese; si ninguno → `None`
    (la UI oculta los controles de voz).
  - Ejecuta `healthcheck.py` del paquete antes de reportarlo como disponible. Si el healthcheck
    falla, lo trata como no disponible y loguea un warning.

Notas:
- Whisper es CPU-intensivo → límite de tamaño/duración de audio (p. ej. 25 MB / 60 s) y
  ejecución en threadpool (`asyncio.to_thread`) para no bloquear el event loop.
- **Semáforo STT:** `asyncio.Semaphore(1)` global para transcripciones concurrentes. En VPS
  pequeños (≤2 CPU), un solo Whisper a la vez; el resto espera en cola con timeout de 30s
  (si expira, devuelve 503).
- El modelo Whisper se carga *lazy* y se cachea en memoria (singleton por proceso).
- Consumo de memoria aproximado: `base` ~250MB, `small` ~500MB. En instancias con <2GB RAM,
  el healthcheck de voice-local debe reportar `stt: false` si la memoria disponible es insuficiente.

### 2.2 Router de voz — `routers/voice.py` (nuevo)

- `POST /voice/stt` — `multipart/form-data` (`file`), devuelve `{ "text": str, "language": str }`.
  - Formato esperado: `audio/webm` (MediaRecorder default), `audio/wav`, o `audio/mpeg`.
    Validar `Content-Type` del upload; rechazar con 415 si no es audio.
  - Rate limit: 10 requests/minuto por usuario (configurable vía `AppSettings.voice_rate_limit`).
  - Límite de tamaño: 25 MB. Rechazo temprano con 413.
- `POST /voice/tts` — `{ "text": str, "voice"?: str }`, devuelve `audio/mpeg` (o `audio/wav`).
  - Rate limit: mismo que STT.
  - Límite de texto: 2000 caracteres. Rechazo con 400 si excede.
- `GET /voice/config` — devuelve `{ "engine": str|null, "stt": bool, "tts": bool, "voices": [...] }`
  para que la UI sepa si mostrar micrófono/altavoz. Ejecuta healthcheck antes de reportar.
- Autenticación: usuario autenticado (mismo `Depends` que el resto de routers).
- Registrar en `main.py`: `app.include_router(voice.router, prefix=settings.api_prefix)`.

### 2.3 Orquestador — `services/agent_builder.py` (nuevo)

Bucle de tool-calling contra el LLM. Reutiliza la resolución de provider/api_key existente
(`_resolve_runtime_defaults` / `AppSettings.default_*` y `resolve_secret`).

**Selección de modelo:**

Al iniciar una sesión, el builder resuelve el modelo en este orden:
1. `AppSettings.builder_model` (si está configurado)
2. `AppSettings.default_model` (modelo default de la instancia)
3. Hardcoded fallback: el primer modelo del provider default

El backend valida si el modelo soporta tool-calling. Si no (o si no se puede determinar),
opera en **modo sin tools**: el system prompt incluye instrucciones para que el LLM devuelva
JSON estructurado con los campos del draft, y el backend hace parsing + validación. El modo
se reporta en la respuesta de la sesión (`tool_mode: "native" | "fallback"`).

**Tools expuestas al LLM (modo nativo):**

| Tool | Entrada | Salida | Efecto |
|------|---------|--------|--------|
| `list_capabilities` | — | catálogo de `list_available_integration_packages()` con `installed`, `required_fields`, `description` | solo lectura |
| `list_runtime_profiles` | — | perfiles válidos (`standard/technical/security`) y sus defaults | solo lectura |
| `propose_agent_draft` | campos del agente | draft normalizado | actualiza el draft en sesión |
| `finalize_agent` | confirmación | `AgentCreate` validado | dispara la creación |

**Salida estructurada de cada turno** (`AgentBuilderTurn`):

```jsonc
{
  "assistant_text": "He preparado un agente que lee SharePoint y envía correo…",
  "draft": {
    "friendly_name": "Asistente Documental",
    "system_prompt": "...",
    "runtime_profile": "standard",
    "enabled_toolsets": [],
    "integration_configs": { "sharepoint": {}, "ms365-mail": {} }
  },
  "required_connectors": [
    {
      "slug": "ms365-mail",
      "name": "Microsoft 365 Mail",
      "installed": false,
      "required_fields": ["tenant_id", "client_id"],
      "admin_instructions": "Pide a tu administrador que habilite 'Microsoft 365 Mail' en Ajustes → Integraciones y configure tenant_id y client_id."
    }
  ],
  "ready_to_create": false
}
```

**Gating determinístico:** tras cada propuesta del LLM, el servicio cruza
`draft.integration_configs.keys()` contra `AppSettings.enabled_integration_packages`.
Para cada slug no habilitado se construye `admin_instructions` a partir del manifest
(`name`, `required_fields`, `fields`). Esta lista **nunca** la decide el LLM.

### 2.4 Router del builder — `routers/agent_builder.py` (nuevo)

- `POST /agent-builder/sessions` → crea una sesión de builder, devuelve `session_id` + `tool_mode`.
- `POST /agent-builder/sessions/{id}/message` → recibe `{ "text": str }`, responde **SSE**
  con eventos `delta` (texto en streaming) y un evento final `turn` (`AgentBuilderTurn`).
  - Implementación SSE: `StreamingResponse(generator(), media_type="text/event-stream")`.
    Cada chunk es `data: {"type":"delta","text":"..."}\n\n`. El evento final es
    `data: {"type":"turn","turn":{...AgentBuilderTurn}}\n\n`.
- `POST /agent-builder/sessions/{id}/finalize` → crea el agente vía
  `create_agent_from_config()` y devuelve `{ agent, required_connectors }`.
  - **Manejo de errores de creación:** si `create_agent_from_config()` falla (slug duplicado,
    workspace inválido, provider sin API key, etc.), se devuelve HTTP 422 con
    `{ "error": str, "field": str|null, "recoverable": true }`. El frontend muestra el error en
    el chat y permite al usuario corregir (ej: cambiar el nombre) y reintentar.
- Estado de sesión: en memoria (dict `session_id → {draft, history, created_at}`) con TTL de 30 min.
  - **Resiliencia:** si el backend se reinicia, las sesiones se pierden. El frontend guarda el
    último draft en `localStorage` como respaldo. Al detectar que la sesión expiró (404 en message),
    ofrece al usuario "restaurar último borrador" desde localStorage.
  - **Limpieza:** un task async periódico purga sesiones con `created_at > 30 min` cada 5 minutos.
- **RBAC:** respeta los mismos permisos que la creación manual de agentes (quien no puede
  crear agentes no puede finalizar). El descubrimiento de conectores es de solo lectura.

### 2.5 Esquemas — `schemas/agent_builder.py` (nuevo)

`AgentBuilderMessage`, `AgentBuilderTurn`, `RequiredConnector`, `AgentBuilderFinalizeResult`.
El `draft` se mapea 1:1 a `AgentCreate` en `finalize`.

---

## 3. Frontend

### 3.1 Capa de API (nuevo)

- `api/voice.ts` — `useVoiceConfig()`, `transcribe(blob)`, `synthesize(text, voice)`.
- `api/agentBuilder.ts` — `useCreateBuilderSession()`, `sendBuilderMessage()` (SSE),
  `useFinalizeAgent()`. `useFinalizeAgent` invalida `["agents"]` y `["dashboard"]`
  (igual que `useCreateAgent`).

### 3.2 Componente — `components/AiAgentBuilder.tsx` (nuevo)

Panel/modal de chat con:
- Lista de mensajes (usuario/asistente) con streaming.
- **Botón de micrófono** (`MediaRecorder` → `/voice/stt`); se oculta si `voice.config.stt === false`.
  - Formato de grabación: `MediaRecorder` con `mimeType: "audio/webm"` (default del navegador).
- **Toggle de altavoz** (respuesta → `/voice/tts` → `<audio autoplay>`); oculto si `tts === false`.
  - **Autoplay policy:** el primer play requiere un gesture del usuario (el click en el toggle
    cuenta como gesture). En iOS Safari, usar `audio.load()` + `audio.play()` dentro del handler
    del click del toggle, no en un useEffect.
- **Panel lateral "Borrador del agente"**: muestra `draft` en vivo (nombre, perfil, prompt).
- **Respaldo en localStorage:** guarda `draft` + `session_id` en `localStorage` bajo key
  `hermeshq:builder_draft`. Si la sesión expira (404), ofrece "Restaurar último borrador".
- **Tarjetas "Conectores requeridos"**: por cada `required_connector`,
  ✅ habilitado / ⚠️ *pendiente* con `admin_instructions` y un botón "Copiar instrucción".
- **Botón "Crear agente"** habilitado cuando `ready_to_create === true`; llama a `finalize`.
  - Si `finalize` devuelve 422, muestra el error en el chat y permite corregir + reintentar.

### 3.3 Puntos de entrada (botón "Crear agente con IA")

1. **`AgentsPage.tsx`**: botón primario junto al de creación manual existente, abre el modal
   `AiAgentBuilder`.
2. **`DashboardPage.tsx`**: añadir el botón en la zona de acciones rápidas (junto a los
   `Link to="/agents"` ya presentes, ~líneas 334/351). Opciones:
   - Abrir el mismo modal `AiAgentBuilder` *in situ*, **o**
   - Navegar a `/agents?builder=1` y que `AgentsPage` abra el modal al leer el query param.
   Recomendado: **modal in situ** para no perder el contexto del dashboard.

### 3.4 i18n

Agregar claves nuevas en `lib/i18n/locales/{es,en}/agents.ts` (y/o un namespace
`agentBuilder.ts`): título, placeholder, estados de voz, textos de conectores requeridos,
instrucciones para administrador.

---

## 4. Seguridad y rendimiento

- **Gating en backend, no en el LLM.** `required_connectors` se calcula contra el estado real
  de `AppSettings`. El LLM solo sugiere; el backend decide qué está habilitado.
- **RBAC.** `finalize` exige el mismo permiso que crear agentes. STT/TTS y descubrimiento son
  de lectura, disponibles para cualquier usuario autenticado.
- **Rate limiting.** STT y TTS limitados a N requests/minuto por usuario (default 10,
  configurable vía `AppSettings.voice_rate_limit`). Evita abuso de recursos CPU.
- **Límites de audio.** Tamaño/duración máximos en `/voice/stt` (25 MB, 60s); rechazo con 413.
  Validación de Content-Type (solo `audio/*`); rechazo con 415.
- **Semáforo STT.** Máximo 1 transcripción concurrente en instancias ≤2 CPU. Timeout 30s con 503.
- **Costo de modelos.** Whisper cargado *lazy* y cacheado; si la concurrencia crece, evaluar
  cola dedicada para STT/TTS (fuera de alcance v1).
- **Datos sensibles.** El builder no recibe valores de secretos; solo conoce qué `required_fields`
  faltan, nunca sus valores.
- **Auditoría.** Registrar en el audit log la creación de agentes vía builder con
  `action="agent.created_via_builder"` (distinto de `agent.created` para distinguir el origen).
- **Sesiones efímeras.** TTL de 30 min, purga automática cada 5 min. No persisten datos
  sensibles del usuario más allá de la conversación activa.

---

## 5. Plan de implementación por fases

**Fase 1 — Voz (backend)**
- `services/voice.py` (STT/TTS + selección de motor + healthcheck + semáforo) y
  `routers/voice.py` (`/voice/stt`, `/voice/tts`, `/voice/config` con rate limiting).
  Registrar en `main.py`. Tests de selección de motor, límites, y rate limit.

**Fase 2 — Orquestador (backend)**
- `services/agent_builder.py` (resolución de modelo, validación de tool-calling, tools + gating
  determinístico, modo fallback sin tools), `schemas/agent_builder.py`,
  `routers/agent_builder.py` (SSE + finalize → `create_agent_from_config` + manejo de errores 422).
  Registrar en `main.py`.
- Tests: matching instalado vs faltante, armado de `AgentCreate`, gating no delegado al LLM,
  manejo de errores de creación, modo fallback.

**Fase 3 — Frontend**
- `api/voice.ts`, `api/agentBuilder.ts` (SSE vía fetch + ReadableStream),
  `components/AiAgentBuilder.tsx` (con localStorage backup, autoplay gesture, manejo de 422).
- Botón "Crear agente con IA" en `AgentsPage` y en `DashboardPage` (modal in situ).
- i18n es/en.

**Fase 4 — Pruebas y pulido**
- E2E: crear un agente por voz que requiera un conector deshabilitado y verificar el aviso al admin.
- E2E: probar modo fallback sin tools (usar un modelo sin tool-calling).
- E2E: error de slug duplicado → corregir → reintento exitoso.
- Verificación de salud: `GET /voice/config` reporta correctamente según healthcheck y memoria.
- Verificación manual con `/verify` o `/run` del flujo completo.

---

## 6. Fuera de alcance (v1)

- Flujo de aprobación/solicitud automática al administrador (solo se muestran instrucciones).
- Soporte de voz en la app móvil (este spec cubre la web; la móvil puede seguir después).
- Edición conversacional de agentes existentes (solo creación en v1).
- Clonación de voz / voces personalizadas más allá de las que exponen `voice-edge`/`voice-local`.

---

## 7. Archivos afectados (resumen)

**Backend (nuevos):** `services/voice.py`, `routers/voice.py`, `services/agent_builder.py`,
`schemas/agent_builder.py`, `routers/agent_builder.py`.
**Backend (editados):** `main.py` (registrar 2 routers), `models/app_settings.py` (agregar
`builder_model` y `voice_rate_limit`), `services/managed_capabilities.py` (healthcheck en
resolve_active_voice_engine).
**Frontend (nuevos):** `api/voice.ts`, `api/agentBuilder.ts`, `components/AiAgentBuilder.tsx`,
`lib/i18n/locales/{es,en}/agentBuilder.ts`.
**Frontend (editados):** `pages/AgentsPage.tsx`, `pages/DashboardPage.tsx`,
`lib/i18n/locales/index.ts`.
**Sin cambios (reutilizado):** `services/agent_factory.py`, `services/managed_capabilities.py`
(catálogo), manifests de `voice-edge`/`voice-local`.

---

## 8. Cambios respecto a v1.0 (changelog de la spec)

| Área | Cambio | Razón |
|------|--------|-------|
| Modelo LLM | `AppSettings.builder_model` + validación de tool-calling + modo fallback | No todos los modelos soportan `tools=` |
| Whisper ARM | Default `base` en ARM64, `small` en x86_64 | ARM64 con ≤1GB RAM OOM con `small` |
| Semáforo STT | `asyncio.Semaphore(1)` + timeout 30s | STT es CPU-intensivo, evitar OOM en VPS pequeños |
| Healthcheck voz | `resolve_active_voice_engine` ejecuta healthcheck | Reportar disponibilidad real, no solo si está instalado |
| Rate limiting | 10 req/min STT+TTS por usuario | Prevenir abuso de CPU |
| SSE | Implementación con `StreamingResponse` sin dependencias extra | No había SSE en el backend |
| Formato audio | Especificar `audio/webm` + validar Content-Type (415) | MediaRecorder produce WebM/Opus |
| Sesiones | TTL 30 min + purga cada 5 min + localStorage backup | Sobrevivir reinicios del backend |
| Errores finalize | HTTP 422 con `recoverable: true` + reintento conversacional | Slug duplicado, workspace inválido, etc. |
| Autoplay TTS | Gesture explícito antes del primer play | Política de autoplay de browsers |
| Audit log | `action="agent.created_via_builder"` | Distinguir origen del agente |
