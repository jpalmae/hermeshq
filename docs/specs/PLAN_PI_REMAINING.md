# Plan de Trabajo — Pi Runtime: Items Restantes

**Fecha:** 2026-08-21  
**Base:** unstable @ 3b2593b (todo lo core deployado a prod)  
**Estado:** Pi + Permission System funcional end-to-end en prod

---

## Contexto

El sistema Pi ya funciona: agentes Pi corren tasks, permission policies bloquean tools/paths/commands, y el frontend permite crear/editar/asignar policies. Los items restantes son de completitud, UX, y hardening.

---

## Fase 1: UX — Tab "Permissions" dedicada en Agent Detail

**Problema:** Hoy la política se selecciona dentro de Config tab — no hay vista dedicada para ver qué está permitido/bloqueado ni para probar comandos.

**Entregable:** Tab "Permissions" en `V2AgentDetailPage` que solo aparece cuando `runtime_type === "pi"`. Contiene:

- Policy asignada (nombre + descripción) con link al editor en Settings
- Resumen visual de lo permitido/bloqueado (tools, paths, commands, network)
- **Test panel**: input tool + command → muestra allowed/denied/requires_approval
- Botón "Remove policy" (setea `permission_policy_id = null`)

**Archivos:**
- `frontend/src/v2/pages/V2AgentDetailPage.tsx` — agregar tab `"permissions"` al array TABS
- `frontend/src/v2/pages/V2AgentPermissionsTab.tsx` — nuevo componente
- `frontend/src/lib/i18n/locales/{en,es}/v2.ts` — claves nuevas

**Estimación:** 0.5 día

---

## Fase 2: Pi Native Gateway

**Problema:** Los canales nativos (Telegram, WhatsApp, SixAgentic) los maneja el gateway Hermes que despacha al Pi runtime. Esto funciona pero tiene overhead y limitaciones (el gateway Hermes consume recursos del agente).

**Entregable:** Un gateway Pi nativo en Node.js que:
- Escucha canales directamente (sin pasar por Hermes)
- Recibe mensajes → crea `Task` → despacha al PiRuntime → responde al canal
- Usa el mismo sistema de auth que los agents Hermes (HMAC + agent token)
- Respeta las permission policies del agente antes de ejecutar el mensaje

**Diseño:**
- Nuevo script `backend/hermeshq/scripts/pi_gateway.mjs` — standalone Node.js gateway
- Escucha en puerto asignado (o usa webhook si el canal lo soporta)
- Llama a `POST /internal/control/submit-task` para crear tasks
- PiInstallationManager genera `.pi/gateway/` con la config del canal

**Archivos:**
- `backend/hermeshq/scripts/pi_gateway.mjs` — nuevo
- `backend/hermeshq/services/pi_gateway_manager.py` — manager del proceso
- `backend/hermeshq/services/gateway_process_manager.py` — dispatch por `runtime_type`
- `backend/hermeshq/services/pi_installation.py` — genera gateway config

**Alternativa si es muy complejo:** Documentar que el gateway Hermes actúa como bridge permanente y aceptarlo como diseño.

**Estimación:** 2-3 días (o 0 si se acepta el bridge Hermes como diseño)

---

## Fase 3: Skills en Pi

**Problema:** Los skills (SKILL.md) se cargan via toolsets Hermes (`skills_list`, `skill_view`). Pi no tiene este sistema nativo.

**Entregable:** Puentear los skills existentes a Pi vía la integration extension — cada skill se expone como una tool que lee el SKILL.md y devuelve su contenido como contexto al modelo.

**Diseño:**
- En `PiInstallationManager._write_integration_extension()`: agregar tools `skill_list` y `skill_view`
- Estas tools leen los SKILL.md desde el workspace del agente (`work/skills/`)
- Pi ya tiene tools `read`, `ls` — el agente puede explorar los skills manualmente
- Alternativa: exponer via MCP si Pi lo soporta nativamente

**Archivos:**
- `backend/hermeshq/services/pi_installation.py` — `_write_skills_extension()`
- `backend/hermeshq/scripts/pi_runner.mjs` — skill tool registration

**Estimación:** 1 día

---

## Fase 4: Hardening + Cleanup

**4a. Cleanup de models.json**
- Cada agente Pi escribe `~/.pi/agent/{id}/models.json` — acumulación infinita
- Agregar cleanup al archivar/eliminar agente Pi
- O usar un solo models.json compartido (pero con merge, no overwrite)

**4b. install.sh — pi_config cleanup**
- Agentes Hermes tienen `pi_config: null` — OK pero podría ser más limpio
- En `create_agent_from_config`: si `runtime_type != "pi"`, setear `pi_config = None` explícitamente

**4c. Permission policy — merge path**
- Hoy solo hay override completo. Agregar soporte para múltiples policies encadenadas (ej: base policy + custom override)
- Campo `permission_policy_ids: list[str]` en vez de `permission_policy_id: str` — permite combinar "Standard Safe" + "Custom Domain Whitelist"

**Estimación:** 0.5 día (4a + 4b), 1 día (4c — más complejo)

---

## Resumen de estimaciones

| Fase | Qué | Estimación | Prioridad |
|------|-----|-----------|-----------|
| 1 | Tab Permissions en Agent Detail | 0.5 día | Media — mejora UX significativa |
| 2 | Pi Native Gateway | 2-3 días | Baja — bridge Hermes funciona hoy |
| 3 | Skills en Pi | 1 día | Media — habilita uso de skills |
| 4a | Cleanup models.json | 0.5 día | Baja |
| 4b | install.sh cleanup | 0.5 día | Baja |
| 4c | Policy chaining | 1 día | Baja |

**Total: ~6 días de trabajo**

---

## Recomendación de orden

1. **Fase 1** primero — impacto visual inmediato, bajo riesgo
2. **Fase 4a + 4b** juntas — cleanup rápido
3. **Fase 3** — habilita skills que los usuarios piden
4. **Fase 2** — evaluar si el bridge Hermes es suficiente o si se necesita gateway nativo
5. **Fase 4c** — policy chaining solo si hay demanda real
