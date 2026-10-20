---
name: sharepoint
description: Navegar, buscar y acceder a archivos en SharePoint y OneDrive del usuario.
---

# SharePoint & OneDrive Skill

Usa esta skill cuando el usuario necesite explorar, buscar o acceder a documentos en SharePoint o OneDrive.

## Herramientas disponibles

- `sharepoint_list_drives` — Lista las unidades disponibles (OneDrive + bibliotecas SharePoint)
- `sharepoint_list_files` — con `depth` (2-3) devuelve el árbol en una sola llamada; para «mostrar todas las carpetas a las que tengo acceso» usa UNA llamada con `solo_carpetas=true` y `depth=4` (trae solo carpetas con su número de archivos). Nunca llames carpeta por carpeta.
- `sharepoint_list_files` — Lista archivos en OneDrive o en un sitio SharePoint específico
- `sharepoint_get_file` — Obtiene información (metadatos) de un archivo por su ruta; NO devuelve el contenido
- `sharepoint_download_file` — Descarga un archivo al workspace y devuelve `local_path` (úsala para imágenes: luego `vision_analyze` con esa ruta)
- `sharepoint_upload_file` — Guarda un archivo del workspace en una carpeta del OneDrive del propio usuario (p. ej. `Escritorio/Usuario/Proyectos/IA`; también acepta su ruta de Windows `C:\Users\…\OneDrive - Contoso\…`). Crea las carpetas que falten y nunca sobrescribe. Úsala cuando pidan "guárdalo en mi carpeta X"; si OneDrive está sincronizado, el archivo aparece en su equipo. Si falla con 403, dilo y entrega el archivo adjunto
- `sharepoint_share_file` — Sube un archivo del workspace (PDF, PPTX, imagen…; máx. 50 MB) al OneDrive del usuario y devuelve un **enlace** para abrirlo o compartirlo. Úsala cuando pidan "el link" de un archivo que generaste o digan que no lo pueden visualizar en el chat. El enlace es solo para la organización salvo que pidan compartir con externos (`scope: anonymous`)
- `sharepoint_read_file` — Lee el CONTENIDO de un archivo (PDF, Word, Excel, PowerPoint, texto, csv, html). Úsala cuando pregunten qué dice o contiene un archivo
- `sharepoint_search` — Busca documentos usando Microsoft Search

## Uso correcto

- Usa `sharepoint_list_drives` para descubrir qué unidades tiene el usuario
- Para acceder a un sitio SharePoint específico, provee la `site_url` (ej: `https://empresa.sharepoint.com/sites/Marketing`)
- Sin `site_url`, opera sobre el OneDrive personal del usuario
- Usa `sharepoint_search` para encontrar documentos por nombre o contenido
- Para leer un archivo: localízalo con `sharepoint_search` (o `sharepoint_list_files`) y llama a `sharepoint_read_file` UNA sola vez pasando el `item_id` y el `drive_id` del resultado; es más fiable que armar la ruta o el `site_url` a mano. No uses `sharepoint_get_file` para leer contenido
- Los archivos OneNote (.one) no se pueden leer con estas herramientas: no intentes extraerlos con Python. Entrega al usuario el enlace `url` del resultado para abrirlo en el navegador
- **Nunca inventes rutas, nombres de sitio ni ids.** Si no conoces la ubicación exacta, busca con `sharepoint_search` y usa el `item_id` y `drive_id` del resultado con `sharepoint_read_file` o `sharepoint_download_file`. Para ver sitios y bibliotecas usa `sharepoint_list_drives`.
- Para imágenes de SharePoint: `sharepoint_download_file` (o `sharepoint_read_file`, que las descarga) y después `vision_analyze` con el `local_path`, UNA imagen por llamada y en secuencia. Nunca pases la URL web de SharePoint a `vision_analyze` ni la descargues con Python: requiere inicio de sesión
- Si el texto sale truncado, avisa al usuario y ofrece leer con un `max_chars` mayor
- Límite: 10 MB por archivo. Los PDF escaneados (solo imagen) no tienen texto extraíble

## Notas

- Usa permisos `Files.Read.All` del usuario autenticado
- El usuario debe tener su cuenta M365 conectada en Mi cuenta
