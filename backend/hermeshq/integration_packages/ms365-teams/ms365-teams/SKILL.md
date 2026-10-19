---
name: ms365-teams
description: Leer y enviar mensajes en chats y equipos de Microsoft Teams del usuario.
---

# Microsoft 365 Teams Skill

Usa esta skill cuando el usuario necesite consultar o interactuar con sus chats y equipos de Microsoft Teams.

## Herramientas disponibles

- `teams_list_teams` — Lista los equipos de Teams a los que pertenece el usuario
- `teams_list_chats` — Lista los chats recientes del usuario
- `teams_get_chat_messages` — Obtiene mensajes de un chat específico
- `teams_send_chat_message` — Envía un mensaje a un chat
- `teams_send_to_person` — Envía un mensaje a una PERSONA por su correo (encuentra o crea el chat 1:1)

## Uso correcto

- Usa `teams_list_chats` para descubrir IDs de chats disponibles
- Usa `teams_get_chat_messages` con el `chat_id` para leer mensajes
- Para enviar, necesitas el `chat_id` y el `message`
- `teams_list_teams` muestra los equipos, no los chats individuales

## Mandar un mensaje a una persona

1. Si tienes solo el nombre, `ms365_find_person` y confirma con el usuario cuál es.
2. Llama `teams_send_to_person(email, message)`: la primera vez devuelve una **vista previa** sin enviar.
3. Muéstrale al usuario a quién y qué texto, y pregunta si lo envías. Con su «sí», repite la llamada con `aprobado_por_el_usuario=true`. Termina ahí tu respuesta y espera su siguiente mensaje: la herramienta rechaza la aprobación si el usuario no respondió entre la vista previa y el envío.
4. Nunca envíes sin esa aprobación ni inventes el texto. Informa el resultado tal cual (si el chat era nuevo o ya existía).

## Notas

- Usa permisos delegados `Chat.Read` / `Chat.ReadWrite` del usuario autenticado
- El usuario debe tener su cuenta M365 conectada en Mi cuenta
