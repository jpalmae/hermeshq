# Plan de Pruebas en Profundidad — HermesHQ

## 1. Autenticación

### 1.1 Login local
- [ ] Login con admin/Admin123! → JWT + cookie httpOnly
- [ ] Login con credenciales incorrectas → 401
- [ ] Logout → cookie limpia, sesión terminada

### 1.2 Sesión
- [ ] Token expirado → redirect a login
- [ ] Cookie httpOnly presente tras login
- [ ] Navegación entre páginas mantiene sesión

### 1.3 Perfil
- [ ] GET /api/auth/me → datos del usuario
- [ ] PUT /api/auth/me/preferences → guardar tema/idioma
- [ ] PUT /api/auth/me/profile → actualizar nombre
- [ ] PUT /api/auth/me/password → cambiar contraseña
- [ ] Avatar: subir, ver, eliminar

## 2. Dashboard (/)
- [ ] Carga overview con métricas
- [ ] Mapa de agentes visible
- [ ] Gráfico de tokens/actividad
- [ ] Lista de tareas recientes
- [ ] Fuente Doto visible en títulos

## 3. Agentes

### 3.1 Lista de agentes (/agents)
- [ ] Lista de agentes visible
- [ ] Crear agente nuevo → aparece en lista
- [ ] Eliminar agente → desaparece de lista
- [ ] Filtros y búsqueda funcionan

### 3.2 Detalle de agente (/agents/:id)
- [ ] Datos generales visibles
- [ ] Editar nombre, descripción, modelo, provider
- [ ] Start → status cambia a "running"
- [ ] Stop → status cambia a "stopped"
- [ ] Restart → status vuelve a "running"

### 3.3 Avatar de agente
- [ ] Subir imagen → se muestra
- [ ] Eliminar imagen → vuelve a default
- [ ] Rechazar archivo > 2MB
- [ ] Rechazar formato no permitido

### 3.4 Workspace
- [ ] Listar archivos del workspace
- [ ] Leer contenido de archivo
- [ ] Escribir/guardar archivo

### 3.5 Versiones de Hermes
- [ ] Dropdown muestra "Inherit" por defecto
- [ ] Solo muestra versiones instaladas (no "bundled")
- [ ] Cambiar versión → se guarda

### 3.6 Mensajería (Telegram/WhatsApp)
- [ ] Panel Telegram muestra configuración
- [ ] Panel WhatsApp muestra configuración
- [ ] Guardar cambios en form
- [ ] Start/stop gateway

### 3.7 Operaciones bulk
- [ ] Enviar mensaje a múltiples agentes
- [ ] Crear tarea para múltiples agentes

### 3.8 Templates
- [ ] Crear agente desde template
- [ ] Bootstrap system operator

## 4. Tareas (/tasks)
- [ ] Lista de tareas visible
- [ ] Crear tarea nueva → aparece en lista
- [ ] Ver detalle de tarea
- [ ] Cancelar tarea
- [ ] Mover tarea en board (drag & drop)
- [ ] Queue state visible

## 5. Schedules (/schedules)
- [ ] Lista de scheduled tasks visible
- [ ] Crear scheduled task
- [ ] Editar scheduled task
- [ ] Eliminar scheduled task

## 6. Cuenta (/account)
- [ ] Datos del usuario visible
- [ ] Editar perfil
- [ ] Cambiar contraseña
- [ ] Subir/cambiar/eliminar avatar personal

## 7. Usuarios (/users) — Admin
- [ ] Lista de usuarios visible
- [ ] Crear usuario nuevo
- [ ] Editar usuario existente
- [ ] Eliminar usuario
- [ ] Avatar: subir, ver, eliminar por admin

## 8. Nodos (/nodes)
- [ ] Lista de nodos visible
- [ ] Crear nodo
- [ ] Editar nodo
- [ ] Test conexión de nodo
- [ ] Métricas de nodo
- [ ] Provision de nodo

## 9. Comms (/comms)
- [ ] Enviar mensaje directo
- [ ] Broadcast a todos los agentes
- [ ] Historial de mensajes visible
- [ ] Topología de comunicación

## 10. Settings (/settings)

### 10.1 General
- [ ] Cambiar app name → se refleja en sidebar
- [ ] Cambiar tema (dark/light/enterprise/sixmanager)
- [ ] Tema sixmanager → fuente Space Grotesk (sin dots)
- [ ] Otros temas → fuente Doto (dots)
- [ ] Subir logo → se muestra
- [ ] Eliminar logo → vuelve a default
- [ ] Subir favicon → se muestra
- [ ] Subir TUI skin

### 10.2 Backup/Restore
- [ ] Crear backup con passphrase
- [ ] Validar backup
- [ ] Restaurar backup (merge y replace)

### 10.3 Runtime
- [ ] Cambiar runtime profile default
- [ ] Cambiar Hermes version default

### 10.4 Providers
- [ ] Lista de providers visible
- [ ] Editar provider → guardar cambios

### 10.5 Integrations
- [ ] Lista de paquetes instalados
- [ ] Instalar nuevo paquete
- [ ] Desinstalar paquete

### 10.6 Factory
- [ ] Listar drafts
- [ ] Crear draft nuevo
- [ ] Editar metadata de draft
- [ ] Validar draft
- [ ] Publicar draft

### 10.7 External Access (MCP)
- [ ] Crear token MCP
- [ ] Ver tokens activos
- [ ] Disable/enable token
- [ ] Revocar token

### 10.8 Hermes Versions
- [ ] Lista de versiones en catálogo
- [ ] Refresh upstream tags → muestra versiones nuevas
- [ ] Agregar versión desde upstream al catálogo
- [ ] Instalar versión
- [ ] Desinstalar versión
- [ ] Eliminar del catálogo

### 10.9 Secrets
- [ ] Lista de secrets visible
- [ ] Crear secret nuevo
- [ ] Editar secret
- [ ] Eliminar secret

### 10.10 Templates
- [ ] Lista de templates visible
- [ ] Crear template
- [ ] Editar template
- [ ] Eliminar template

## 11. WebSocket
- [ ] Conexión WebSocket se establece al cargar app
- [ ] Reconexión automática tras desconexión
- [ ] Heartbeat funcional (no se cae por inactividad)
- [ ] Eventos en tiempo real (tareas, status de agentes)

## 12. Terminal
- [ ] Abrir terminal de agente
- [ ] Ejecutar comando → respuesta visible
- [ ] Cerrar terminal

## 13. Infraestructura / DevOps

### 13.1 Contenedores
- [ ] Backend corre como usuario appuser (no root)
- [ ] PostgreSQL no expone puerto al host
- [ ] no-new-privileges activo en backend

### 13.2 Headers de seguridad
- [ ] X-Frame-Options: DENY
- [ ] X-Content-Type-Options: nosniff
- [ ] Content-Security-Policy presente
- [ ] Referrer-Policy: strict-origin-when-cross-origin
- [ ] Permissions-Policy presente

### 13.3 Nginx
- [ ] Proxy API → backend funciona
- [ ] Proxy WebSocket → backend funciona
- [ ] Static assets cache headers correctos
- [ ] Gzip habilitado
- [ ] Rate limiting en login (5 req/min)

## 14. i18n
- [ ] Cambiar idioma a español → textos cambian
- [ ] Cambiar idioma a inglés → textos cambian
- [ ] No hay keys sin traducir visibles (tipo "agent.title")
