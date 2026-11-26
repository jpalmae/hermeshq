---
name: reuniones
description: "Gestiona reuniones del calendario M365 de Ana: crear (con Teams, salas, asistentes), modificar, eliminar y ver las de hoy. Reemplaza a crear-reuniones, modificar-eliminar-reuniones y reuniones-hoy. Usar cuando digan 'Agenda una reunión', 'Crea una reunión', 'Programa una cita', 'Borra la reunión de mañana', 'Cancela la reunión con X', 'Cambia la reunión de las 10 a las 11', 'Mueve la reunión a otra hora', '¿Qué reuniones tengo hoy?', '¿Qué tengo en la agenda hoy?'."
version: 1.0.0
author: usuario (creada con hq_skill_crear)
---

# Reuniones

## Cuándo usarla
- Agenda una reunión
- Crea una reunión
- Programa una cita
- Borra la reunión de mañana
- Cancela la reunión con X
- Cambia la reunión de las 10 a las 11
- Mueve la reunión a otra hora
- ¿Qué reuniones tengo hoy?
- ¿Qué tengo en la agenda hoy?

## Flujo
1. Identificar la acción: crear / modificar-eliminar / ver de hoy. Si es ambigua, preguntar en una línea. (herramienta: `skills_list`)
2. Crear: reunir info — asunto, fecha-hora en Chile, duración (SIEMPRE preguntar si no la dan), asistentes, ubicación (sala o remota), descripción. Si falta algo, preguntar en un solo mensaje. (herramienta: `ms365_calendar_list_events`)
3. Crear: detectar cruces con ms365_calendar_list_events (rango día completo UTC, count 50). Si hay cruce, avisar antes de confirmar. (herramienta: `ms365_calendar_list_events`)
4. Crear: resolver asistentes por nombre con ms365_find_person. Mostrar candidatos y pedir confirmación de cada uno. (herramienta: `ms365_find_person`)
5. Crear: mostrar resumen completo (asunto, fecha-hora Chile, duración, asistentes, sala, Teams) y pedir confirmación. Al confirmar, crear con ms365_calendar_create_event (online_meeting: true por defecto) y verificar con ms365_calendar_get_event. (herramienta: `ms365_calendar_create_event`)
6. Mod/Elim: pedir al menos 1 dato de búsqueda (nombre de la reunión, asistentes, o día y horario). Ventana: el día indicado o la semana laboral actual. (herramienta: `ms365_calendar_list_events`)
7. Mod/Elim: buscar con ms365_calendar_list_events (start/end obligatorios) + ms365_calendar_get_event por cada candidato (la lista no trae asistentes). Mostrar resumen completo y pedir confirmación. Si es eliminar, advertir que es irreversible. (herramienta: `ms365_calendar_get_event`)
8. Mod/Elim: ejecutar ms365_calendar_delete_event o ms365_calendar_update_event (solo campos que cambian; con add_attendees/remove_attendees para no perder asistentes). Verificar con ms365_calendar_get_event. (herramienta: `ms365_calendar_delete_event`)
9. Hoy: calcular rango UTC del día (verano: día-1 21:00Z → día+1 01:00Z; invierno: día-1 20:00Z → día+1 00:00Z). ms365_calendar_list_events con start/end, count 50. (herramienta: `ms365_calendar_list_events`)
10. Hoy: ms365_calendar_get_event UNA A UNA (nunca en paralelo) solo de las reuniones que no terminan. Presentar tabla en hora Chile: hora, asunto, quién organizó, asistentes, estado (✅/⏳/⚡/🔁/❌). (herramienta: `ms365_calendar_get_event`)

## Reglas
- Usa solo las herramientas indicadas; no escribas scripts ni HTML a mano.
- No inventes cifras ni datos: todo sale de la fuente.
- NUNCA ms365_calendar_list_events sin start y end (Error 400).
- Confirmación obligatoria antes de crear, modificar o eliminar. Eliminar es irreversible: advertirlo.
- Horas SIEMPRE en Chile (verano UTC-3, invierno UTC-4). Restar horas, no sumar.
- online_meeting: true por defecto. Entregar teams_join_url al usuario.
- Sala se reserva por email en attendees, NUNCA en location.
- No inventar asistentes, salas, horarios ni event_ids.
- get_event de no terminadas: una a una, nunca en paralelo.
- Sin recurrencia: crear eventos individuales del período.

## Entrega
Resumen en hora Chile de la reunión creada/modificada/eliminada, o tabla con las reuniones del día.

## Ejemplo
> Agenda una reunión con Rosa el jueves a las 10 para revisar el proyecto BI
