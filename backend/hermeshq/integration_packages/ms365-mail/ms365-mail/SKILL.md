---
name: ms365-mail
description: Leer, buscar y enviar correos electrónicos usando la cuenta Microsoft 365 del usuario.
---

# Microsoft 365 Mail Skill

Usa esta skill cuando el usuario necesite consultar, buscar o enviar correos de su cuenta Microsoft 365.

## Herramientas disponibles

- `ms365_mail_list` — Lista los correos de una carpeta (inbox, sentitems, drafts, deleteditems o cualquier carpeta/subcarpeta propia por nombre o ruta, ej. `Informaciones`); admite `since`, `unread_only` y `preview_chars`
- `ms365_mail_folders` — Lista todas las carpetas y subcarpetas del buzón con su ruta y cantidad de correos; úsala cuando el usuario nombre una carpeta propia
- `ms365_find_person` — Busca el correo de una persona por nombre en el historial de correos y reuniones del usuario y en el directorio de toda la empresa (con cargo y área; si responde `directorio_no_disponible`, el usuario debe reconectar su cuenta M365 para dar el permiso User.ReadBasic.All); devuelve candidatos y SIEMPRE se debe pedir confirmación al usuario antes de usarlos
- `ms365_inbox_estado` — Mantiene el estado del informe horario de correos y entrega los datos del informe (críticos, nuevos, backlog) en UNA llamada; no escribas scripts ni edites work/cron_inbox_estado.json
- `ms365_mail_get_many` — Obtiene hasta 10 correos por sus IDs en UNA llamada (en paralelo); úsala siempre que necesites leer más de uno
- `ms365_mail_get` — Obtiene un correo por su ID; el cuerpo llega en texto plano y recortado (admite `max_chars`; `format=html` solo si necesitas el HTML original)
- `ms365_mail_send` — Envía un correo en nombre del usuario con varios destinatarios (`to`, `cc`, `bcc`), archivos adjuntos (`attachments`) e imágenes incrustadas (`inline_images`)
- `ms365_mail_forward` — REENVÍA un correo recibido conservando su cuerpo, adjuntos e imágenes incrustadas. Úsala para «reenvía el correo de X a Y»; no intentes sacar los adjuntos ni armar otro correo. Primero devuelve una vista previa (qué viaja y a quién): muéstrala, termina tu respuesta y espera el «sí» del usuario; la aprobación solo vale si el usuario respondió entre la vista previa y el envío
- `ms365_mail_reply` — Responde un correo recibido (al remitente o a todos con `reply_all`) dentro del mismo hilo, con la misma vista previa y aprobación
- `ms365_mail_attachments` — Lista los adjuntos e imágenes incrustadas de un correo recibido y, con `download=true`, los guarda en el workspace (hasta 10 archivos de 10 MB)
- `ms365_mail_search` — Busca correos en todo el buzón o en una carpeta: palabras clave más filtros `from`, `subject`, `since`, `until`, `has_attachments` y `folder`; devuelve los más recientes primero

## Uso correcto

- Usa `ms365_mail_list` primero para obtener IDs de correos
- Usa `ms365_mail_get` para leer el cuerpo de un correo específico; si `body_truncated` es true, vuelve a llamarlo con un `max_chars` mayor solo si realmente necesitas el resto
- Para enviar, asegúrate de tener `to`, `subject` y `body`
- Para buscar, usa `ms365_mail_search` con términos relevantes

## Notas

- Todos los accesos usan permisos delegados del usuario autenticado
- El usuario debe tener su cuenta M365 conectada en Mi cuenta

## Varios destinatarios y adjuntos

- `to`, `cc` y `bcc` aceptan **todos los destinatarios que necesites en un solo correo**: pásalos como lista (un elemento por dirección). También se aceptan varias direcciones en un texto separadas por `;` o `,`. Nunca envíes un correo por destinatario para evitar un error: si falla, lee el mensaje de error, que indica qué dirección es inválida.
- Para adjuntar archivos (PDF, Excel, Word, PowerPoint, imágenes, etc.) usa `attachments`: `[{"path": "outputs/informe.pdf"}]` (opcional `name` para cambiar el nombre con que llega). El archivo debe estar en tu directorio de trabajo.
- Límite: hasta 10 archivos y **25 MB en total** por correo (incluye imágenes incrustadas). Hasta 2,8 MB se envía directo. Por encima, la herramienta intenta enviarlos como adjuntos reales (borrador + subida por partes, requiere el permiso `Mail.ReadWrite`); si la cuenta no lo tiene, **sube los archivos a la carpeta `UltraBot-adjuntos` de OneDrive del usuario y pone los enlaces al final del correo** (la respuesta trae `delivery: enlaces_onedrive`). Avisa al usuario cuando haya salido por enlace, y que destinatarios externos podrían no poder abrirlo. No digas que adjuntar es imposible: solo hay esos límites de tamaño
- Los PDF, Word, Excel y PowerPoint que generas con `hq_*_generate` quedan en tu directorio de trabajo y se pueden adjuntar directamente.

## Imágenes embebidas y firmas (inline_images)

Nunca pegues imágenes en base64 dentro del HTML: se truncan y rompen el correo. En su lugar:

1. Deja las imágenes (PNG, JPEG o GIF) en tu directorio de trabajo.
2. En el HTML referencia cada una como `<img src="cid:logo">`.
3. Pásalas en `inline_images`: `[{"path": "work/firma/logo.png", "content_id": "logo"}]`.

Límites: hasta 10 imágenes, 1,5 MB cada una y 2,5 MB en total. Solo se aceptan imágenes del directorio de
trabajo. Si algún `content_id` no aparece como `cid:` en el HTML, la respuesta trae un `warning`.

## Informes por ventana de tiempo (rápido)

Para "los correos de la última hora" o "desde las 08:00" usa `ms365_mail_list` con `since` (fecha ISO 8601 en UTC,
por ejemplo `2026-10-06T20:00:00Z`) y, si solo importan los pendientes, `unread_only: true`. Así el filtro lo hace
Microsoft y no traes 50 correos para descartar la mayoría: la tarea responde antes y gasta menos.

## Buscar correos rápido (una sola búsqueda bien hecha)
Cada llamada tarda segundos y cada vuelta del agente cuesta más: **no pruebes variantes de palabras una por una**. Haz UNA búsqueda combinando filtros: `ms365_mail_search(query="cuentas", from="Servantis", since="2026-10-07", count=20)`. Las palabras de `query` deben aparecer todas: usa 1 o 2 palabras clave, no frases largas. Si el usuario da un remitente, una fecha o una carpeta, ponlos como `from`, `since`/`until` o `folder`. Si no aparece, amplía UNA vez (quita una palabra o usa `ms365_mail_list` con `since`) y si sigue sin aparecer, dilo y pide un dato más; no encadenes más de 3 búsquedas.

**Leer varios correos:** nunca llames `ms365_mail_get` uno por uno; junta los IDs y usa `ms365_mail_get_many(message_ids=[...], max_chars=1500)`: una vuelta en vez de diez.
