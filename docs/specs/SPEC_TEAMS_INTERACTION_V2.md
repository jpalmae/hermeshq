# SPEC: Teams Interaction V2 — mejora funcional de la interacción usuarios ↔ agentes por Microsoft Teams

**Fecha:** 2026-09-24
**Estado:** Pendiente de aprobación
**Alcance:** funcional (experiencia, gobierno y operación). Sin cambios de protocolo del adapter upstream salvo lo indicado.

---

## Estado actual (línea base)

| Dimensión | Hoy |
|---|---|
| Bot | 1 bot de Azure por agente (client_id/secret/tenant como secret del canal) |
| Entrada de mensajes | DM: responde todo · grupos/canales: solo @mention (o todo con RSC + require_mention) |
| Identidad | `TEAMS_ALLOWED_USERS` = lista global de AAD object IDs por agente; existe campo `users.teams_id` + resolver, pero el flujo de emparejamiento es manual |
| Trazabilidad | Cada mensaje → Task en HQ (mismo pipeline que Telegram) |
| Interacción rica | Adaptive Cards de aprobación (Allow Once/Session/Always/Deny), attachments de entrada (docs/imagen), typing indicator, resúmenes de reuniones |
| Sesiones | 1 sesión continua por conversación; sin gestión visible para el usuario |
| Gobierno | Sin policies del PermissionEnforcer en tareas originadas por gateway hermes (solo Pi/Desktop) · sin cuotas por usuario · sin horarios |
| Voz | No (audio recibido se ignora o falla según formato) |
| Proactivo | Solo `TEAMS_HOME_CHANNEL` para cron; sin notificaciones de eventos HQ |

## Objetivos

1. Que un usuario de Teams **interactúe con su agente como canal de primera clase**: identidad propia, sesiones, voz y respuestas proactivas.
2. Que la interacción por Teams quede bajo el **mismo gobierno** que el resto de superficies (policies, cuotas, auditoría por usuario).
3. Que operar el canal sea **sin fricciones** para el admin (diagnóstico, emparejamiento, onboarding).

---

## P1 — Identidad y onboarding de usuarios (fundación)

**P1.1 Emparejamiento por código (pairing flow)**
- Usuario desconocido escribe al bot → el agente responde con un **código corto de 6 dígitos** y queda "pendiente de emparejar" en HQ.
- Admin ve la solicitud en el canal → tab de emparejamiento → la aprueba (vincula `users.teams_id` ↔ AAD id, asigna agente) o la rechaza.
- Reemplaza el flujo actual de copiar object IDs a mano en la UI.
- *Esfuerzo: 3-4 días* (usa `unauthorized_dm_behavior=pair` que ya existe como concepto en Kapso).

**P1.2 Atribución real de tareas**
- Los tasks originados en Teams llevan `created_by_user_id` del usuario emparejado (hoy quedan anónimos).
- Impacto inmediato en: auditoría por persona, filtros "mis tareas", cuotas P3.
- *Esfuerzo: 1-2 días* (el resolver ya existe; falta propagarlo al crear la task).

**P1.3 Resolución por UPN/email en la UI**
- El admin configura allowlists escribiendo `usuario@empresa.com` en vez de object IDs (resolución vía Graph con las credenciales del bot).
- *Esfuerzo: 1-2 días*.

## P2 — Experiencia conversacional

**P2.1 Sesiones visibles y controlables por el usuario**
- Comandos nativos en el chat: `/new` (nueva sesión), `/status` (task en curso), `/stop` (cancelar).
- Título de sesión visible (el generador de títulos ya existe en hermes).
- Sesión persistente por hilo de Teams (thread) además de por conversación.
- *Esfuerzo: 3-4 días*.

**P2.2 Notas de voz (bidireccional)**
- Entrada: mensaje de voz → STT (Whisper ya integrado en `voice.py`) → prompt del agente.
- Salida: opción de respuesta hablada por usuario (`/voz on`) usando el TTS configurado del agente.
- *Esfuerzo: 2-3 días*.

**P2.3 Notificaciones proactivas de HQ**
- Eventos que el usuario puede activar: task completada (de cualquier superficie: web, desktop, teams), resultado de schedules, aprobaciones pendientes que le corresponden.
- Implementación: card interactiva en el chat DM del usuario ("Ver detalle" → deep-link a la task en la web).
- *Esfuerzo: 3 días* (engancha con el EventBroker existente).

**P2.4 Acciones rápidas en cards**
- Cards con botones para flujos comunes: aprobar/rechazar (ya existe para comandos), "reintentar task", "resumir día", quick-replies configurables por agente.
- *Esfuerzo: 2-3 días*.

## P3 — Gobierno y seguridad

**P3.1 Policies del PermissionEnforcer en tareas de gateway (Hermes runtime)**
- Hoy las policies (deny bash, paths, red) aplican solo a Pi y Desktop enrolado. Con el plugin `hermeshq_guard` ya construido, extender la inyección a agentes con canal Teams activo → toda herramienta ejecutada por un mensaje de Teams pasa por el enforcer.
- **Esta es la pieza que unifica el gobierno en todas las superficies.**
- *Esfuerzo: 2-3 días* (el mecanismo existe; es activarlo para gateways y probar).

**P3.2 Cuotas y límites por usuario**
- Límite de tareas/día y de tokens/día por usuario Teams (configurable por agente y por usuario).
- Al alcanzar el límite: aviso cordial, no bloqueo silencioso.
- *Esfuerzo: 2 días* (contador sobre las tasks atribuidas de P1.2).

**P3.3 Horarios y modos**
- Horario de atención por agente (ej. Lun-Vie 8-18): fuera de horario responde con mensaje configurado y encola para el siguiente horario (o descarta, a elección).
- Modo vacaciones/pausa por agente y por usuario.
- *Esfuerzo: 2 días*.

**P3.4 Higiene de contenido**
- Filtro de attachments por tipo y tamaño (por agente).
- Redacción de secretos en respuestas (integración con patrones existentes de redacción de auditoría).
- *Esfuerzo: 1-2 días*.

## P4 — Operación y escalabilidad

**P4.1 Wizard de alta de bot asistido**
- La UI guía el alta: valida credenciales, verifica webhook público alcanzable, envía mensaje de prueba end-to-end, diagnostica errores comunes (RSC faltante, tenant equivocado).
- *Esfuerzo: 3 días*.

**P4.2 Salud del canal en la UI**
- Panel por canal: última entrega, latencia, tasa de error, mensajes/día, usuarios activos — con alertas.
- *Esfuerzo: 2-3 días* (sobre ActivityLog + runtime ledger).

**P4.3 Bot router multi-agente (opcional, a decidir)**
- Un solo bot de la organización que enruta a N agentes ("habla con @Operador") en lugar de un bot Azure por agente.
- Pros: menos alta en Azure, discovery central. Contras:UX menos personal, complejidad de enrutamiento.
- *Esfuerzo: 5-6 días*. **Recomiendo evaluarlo DESPUÉS de P1-P3** — hoy el costo por bot es bajo y el aislamiento por agente es más simple.

---

## Resumen y orden propuesto

| Fase | Valor funcional | Esfuerzo |
|---|---|---|
| P1 Identidad (pairing, atribución, UPN) | Fundacional — sin esto no hay gobierno por usuario | ~1 semana |
| P2 Conversación (sesiones, voz, proactivo, cards) | El salto de experiencia para el usuario final | ~2 semanas |
| P3 Gobierno (policies, cuotas, horarios, higiene) | Paridad de control con web/desktop | ~1.5 semanas |
| P4 Operación (wizard, salud, router) | Menos fricción admin; router opcional | ~2 semanas (P4.3 opcional) |

**Total P1-P4 completo: ~6.5 semanas** · **P1+P3 solamente (gobierno primero): ~2.5 semanas**

## Métricas de éxito
- % de mensajes de Teams atribuidos a un usuario HQ (target: >95% post-P1)
- Tiempo de alta de un usuario nuevo en el canal: de ~10 min manual a <2 min (pairing)
- Tareas de Teams pasando por el enforcer: 100% post-P3.1
- Tickets/incidencias de configuración del canal: -70% post-P4.1

## Fuera de alcance (explícito)
- Cambios en el protocolo Bot Framework o en el adapter upstream de hermes-agent (todo se hace desde HermesHQ)
- Teams Meetings/Graph webhooks (ya cubierto por pipeline existente)
- Sustituir Adaptive Cards por UI custom embebida (pestañas de Teams) — posible fase futura
