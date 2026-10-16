---
name: ms365-mail
description: Leer, buscar y enviar correos electrónicos usando la cuenta Microsoft 365 del usuario.
---

# Microsoft 365 Mail Skill

Usa esta skill cuando el usuario necesite consultar, buscar o enviar correos de su cuenta Microsoft 365.

## Herramientas disponibles

- `ms365_mail_list` — Lista los correos de una carpeta (inbox, sentitems, drafts, deleteditems o cualquier carpeta/subcarpeta propia por nombre o ruta, ej. `Informaciones`); admite `since`, `unread_only` y `preview_chars`
- `ms365_mail_folders` — Lista todas las carpetas y subcarpetas del buzón con su ruta y cantidad de correos; úsala cuando el usuario nombre una carpeta propia
- `ms365_find_person` — Busca el correo de una persona por nombre en el historial de correos y reuniones del usuario (no en el directorio de la empresa); devuelve candidatos y SIEMPRE se debe pedir confirmación al usuario antes de usarlos
- `ms365_mail_get` — Obtiene un correo por su ID; el cuerpo llega en texto plano y recortado (admite `max_chars`; `format=html` solo si necesitas el HTML original)
- `ms365_mail_send` — Envía un correo en nombre del usuario con varios destinatarios (`to`, `cc`, `bcc`), archivos adjuntos (`attachments`) e imágenes incrustadas (`inline_images`)
- `ms365_mail_search` — Busca correos por palabras clave

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
- Límite: hasta 10 archivos y **18 MB en total** por correo (incluye imágenes incrustadas). Hasta 2,8 MB se envía directo. Por encima, la herramienta intenta enviarlos como adjuntos reales (borrador + subida por partes, requiere el permiso `Mail.ReadWrite`); si la cuenta no lo tiene, **sube los archivos a la carpeta `UltraBot-adjuntos` de OneDrive del usuario y pone los enlaces al final del correo** (la respuesta trae `delivery: enlaces_onedrive`). Avisa al usuario cuando haya salido por enlace, y que destinatarios externos podrían no poder abrirlo. No digas que adjuntar es imposible: solo hay esos límites de tamaño
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
