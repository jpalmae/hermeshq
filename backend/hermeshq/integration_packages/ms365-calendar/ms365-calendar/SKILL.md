---
name: ms365-calendar
description: Consultar, crear y gestionar eventos del calendario Microsoft 365 del usuario.
---

# Microsoft 365 Calendar Skill

Usa esta skill cuando el usuario necesite ver, crear, editar o eliminar eventos de su calendario Microsoft 365.

## Herramientas disponibles

- `ms365_calendar_list_events` — Lista eventos en un rango de fechas
- `ms365_calendar_get_event` — Obtiene detalle completo de un evento por ID
- `ms365_calendar_create_event` — Crea un nuevo evento en el calendario
- `ms365_calendar_update_event` — Modifica un evento existente
- `ms365_calendar_delete_event` — Elimina un evento del calendario

## Uso correcto

- Las fechas deben estar en formato ISO 8601 (ej: `2026-06-01T10:00:00`)
- Para crear reuniones, incluye `attendees` con lista de emails
- Si piden reunión por Teams (o enlace de Teams), pasa `online_meeting: true` en `ms365_calendar_create_event` (o en `ms365_calendar_update_event` para agregarlo a un evento existente) y entrega el enlace de `teams_join_url` de la respuesta. Si no viene `teams_join_url`, avisa que no se pudo generar; no lo inventes
- Usa `ms365_calendar_list_events` con rango de fechas para ver disponibilidad
- Todo se puede editar en una reunión ya creada con `ms365_calendar_update_event` y el `event_id` devuelto al crearla (guárdalo; no uses el enlace de Teams ni el ID de reunión). Solo envía los campos que cambian:
  - **Horario y día:** `start` y `end`; o `start` solo (conserva la duración actual); o `duration_minutes`. Indica `timezone` (ej. `America/Santiago`) o usa UTC con `Z`.
  - **Asistentes:** `add_attendees` / `add_optional_attendees` suman gente conservando a los actuales; `remove_attendees` quita (reciben cancelación); `attendees` define la lista completa que debe quedar.
  - **Teams:** `online_meeting: true` agrega la reunión de Teams (la respuesta trae `teams_join_url`); `online_meeting: false` la quita.
  - **Otros:** `subject`, `location` y `body` (con texto vacío se borran), `importance`, `show_as`, `reminder_minutes`, `all_day`, `categories`.
- NO borres y recrees la reunión para editarla: cambiaría el enlace de Teams y los asistentes recibirían una cancelación y una invitación nueva
- Al crear (`ms365_calendar_create_event`) también puedes pasar `optional_attendees`, `online_meeting`, `importance`, `show_as`, `reminder_minutes`, `all_day` y `categories`
- Siempre confirma antes de eliminar eventos

## Notas

- Todos los accesos usan permisos delegados del usuario autenticado
- El usuario debe tener su cuenta M365 conectada en Mi cuenta
