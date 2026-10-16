---
name: catalogo-de-capacidades
description: "Mapa en lenguaje simple de lo que sabes hacer y con qué herramienta o skill. Consúltalo ANTES de decir que algo no se puede, antes de armar un método propio y siempre que alguien pregunte '¿puedes…?'. Última verificación: 2026-10-08."
---

# Catálogo de capacidades

Úsalo así: busca lo que te piden en la columna "Si piden…", usa lo que dice la columna "Haz esto" y **no inventes otro método**. Si no aparece aquí, revisa tus herramientas y tus skills antes de contestar "no puedo".

## Correo
| Si piden… | Haz esto |
|---|---|
| Ver o resumir correos | `ms365_mail_list` con `since` (ISO UTC), `unread_only` y `preview_chars`. No abras correos uno por uno: la vista previa alcanza para clasificar |
| Revisar una carpeta propia (ej. "Informaciones") | `ms365_mail_folders` para ver las carpetas y luego `ms365_mail_list` con el nombre o la ruta en `folder`. Nunca la reemplaces por la bandeja de entrada sin avisar |
| Leer un correo concreto | `ms365_mail_get` (texto recortado; sube `max_chars` solo si hace falta) |
| Buscar correos por palabra | `ms365_mail_search` |
| El correo de una persona dado su nombre | `ms365_find_person` (busca en su historial de correos y reuniones, no en el directorio de la empresa). **Muestra el correo encontrado y pide confirmación** |
| Enviar un correo | `ms365_mail_send`: varios destinatarios en `to`, `cc` y `bcc` (listas, todos en un solo correo) |
| Adjuntar archivos | `attachments` en `ms365_mail_send`: hasta 10 archivos y 18 MB. Hasta 2,8 MB sale directo; por encima sale como adjunto real si la cuenta tiene `Mail.ReadWrite`, y si no, como enlace de OneDrive (avísale al usuario) |
| Imágenes dentro del cuerpo (firma) | `inline_images` (1,5 MB cada una) con `cid:` en el HTML. Nunca pegues base64 |

## Calendario y reuniones
| Si piden… | Haz esto |
|---|---|
| Ver agenda | `ms365_calendar_list_events` con rango; el detalle (asistentes) solo con `ms365_calendar_get_event` de las reuniones que aún no terminan, de a una |
| Crear una reunión | `ms365_calendar_create_event` (`attendees`, `optional_attendees`, `location`; salas: agrega el correo de la sala en `attendees`). Para Teams: `online_meeting: true`, y entrega el `teams_join_url` |
| Cambiar una reunión ya creada | `ms365_calendar_update_event` con el `event_id`: horario y día (`start`, `end` o `duration_minutes`), asistentes (`add_attendees`, `remove_attendees`), Teams (`online_meeting` true o false), título, lugar, descripción, importancia y recordatorio. **Nunca borres y recrees** para editar |
| Cancelar una reunión | `ms365_calendar_delete_event`, tras confirmar |

## Archivos y documentos
| Si piden… | Haz esto |
|---|---|
| Buscar un archivo en SharePoint u OneDrive | `sharepoint_search` y usa el `item_id` y `drive_id` del resultado. **Nunca adivines rutas, sitios ni ids** |
| Leer o descargar un archivo | `sharepoint_read_file` o `sharepoint_download_file` (con `item_id` y `drive_id`) |
| El enlace de un archivo que generaste | `sharepoint_share_file` (lo sube a OneDrive y devuelve el enlace; por defecto solo para la organización) |
| Generar un PDF, Word, Excel o PowerPoint | `hq_pdf_generate`, `hq_docx_generate`, `hq_xlsx_generate`, `hq_pptx_generate` |
| Presentación corporativa con la plantilla | La skill de presentaciones que tengas instalada. No armes `python-pptx` a mano |
| Analizar una imagen | `vision_analyze`, una imagen por llamada |
| Crear una imagen | La skill `generador-imagenes` |

## Tareas programadas y coordinación
| Si piden… | Haz esto |
|---|---|
| Programar algo recurrente | `hq_schedule_request` (queda pendiente: el usuario la activa escribiendo `aprobar CODIGO`). Ver `hq_schedule_list`; borrar con `hq_schedule_delete`. No uses el cron nativo, está desactivado |
| Pedirle algo a otro agente | `hq_list_agents`, `hq_delegate_task` o `hq_direct_message` |

## Cómo funciona el sistema (para planificar)
- Un mensaje de chat tiene **9 minutos como máximo**; una tarea programada, 15. Divide el trabajo largo en mensajes y entrega avance.
- Si el usuario escribe solo "para" o "cancela", el sistema cancela la tarea en curso: no hace falta que tú hagas nada.
- Llamadas a herramientas **de a una**, nunca en paralelo. Máximo 6 herramientas por mensaje.
- `clarify` y `session_search` no están disponibles: si necesitas aclarar, pregunta en el texto, con todas las preguntas juntas.

## Lo que NO se puede hoy (verificado 2026-10-08)
- Buscar a cualquier persona en el directorio completo de la empresa (falta un permiso; solo se encuentra a quien ya tiene correos o reuniones con el usuario).
- Adjuntos reales de más de 2,8 MB sin el permiso `Mail.ReadWrite` (sale como enlace).
- Reuniones recurrentes (semanal o mensual): crea los eventos individuales y avisa.
- Reglas de Outlook, o leer los chats de Teams: no hay herramienta.
Si algo no está aquí ni en tus herramientas, dilo con claridad, ofrece la alternativa más cercana y no prometas lo que no puedes verificar.
