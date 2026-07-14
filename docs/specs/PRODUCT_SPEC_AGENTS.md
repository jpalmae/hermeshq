# HermesHQ — Panel de Control Multi-Agente para Hermes Agent

## Documento de Especificaciones Técnicas v1.0

**Proyecto:** HermesHQ  
**Propósito:** Panel web de administración para crear, orquestar y monitorear múltiples instancias de Hermes Agent corriendo en uno o más servidores  
**Stack:** Python 3.11+ / FastAPI / PostgreSQL / Redis / React (Vite) / Docker  
**Licencia:** MIT  

---

## 1. Visión del Producto

HermesHQ es un panel de control web que permite a un operador humano administrar una flota de agentes Hermes desde una interfaz centralizada. A diferencia del CLI de Hermes (que controla un solo agente local) o del Gateway (que conecta un agente a plataformas de mensajería), HermesHQ opera como una capa de orquestación superior que:

- Despliega y destruye instancias de `AIAgent` bajo demanda (locales o en nodos remotos vía SSH/Docker)
- Asigna a cada agente su propio workspace aislado, modelo LLM, toolsets, skills y system prompt
- Permite comunicación inter-agente usando el mecanismo nativo `delegate_task` y `send_message` de Hermes
- Expone un dashboard en tiempo real con logs de actividad, uso de tokens, estado de tareas y salud de cada agente
- Persiste toda la configuración, sesiones e historial en PostgreSQL para sobrevivir reinicios

Este **no** es un mockup ni un MVP. Es un sistema productivo diseñado para operar 24/7 con múltiples agentes ejecutando tareas concurrentes.

---

## 2. Arquitectura General

```
┌─────────────────────────────────────────────────────────────┐
│                        BROWSER (React)                       │
│  Dashboard │ Agent Manager │ Task Board │ Comms │ Settings   │
└──────────┬──────────────────────────────────────┬────────────┘
           │  REST + WebSocket (JWT auth)          │
┌──────────▼──────────────────────────────────────▼────────────┐
│                    HermesHQ API Server (FastAPI)              │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐ │
│  │ Agent       │  │ Task         │  │ Comms               │ │
│  │ Lifecycle   │  │ Dispatcher   │  │ Router              │ │
│  │ Manager     │  │              │  │ (inter-agent msgs)  │ │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬──────────┘ │
│         │                │                      │            │
│  ┌──────▼────────────────▼──────────────────────▼──────────┐ │
│  │              Agent Process Supervisor                    │ │
│  │  (spawns/monitors AIAgent instances per-thread)         │ │
│  └──────┬───────────────────────────────────┬──────────────┘ │
│         │                                   │                │
│  ┌──────▼──────┐                    ┌───────▼─────────┐     │
│  │ Local Node  │                    │ Remote Nodes    │     │
│  │ (subprocess │                    │ (SSH + Docker   │     │
│  │  AIAgent)   │                    │  AIAgent)       │     │
│  └─────────────┘                    └─────────────────┘     │
│                                                              │
│  ┌────────────┐  ┌─────────┐  ┌──────────────────────────┐  │
│  │ PostgreSQL │  │ Redis   │  │ Filesystem               │  │
│  │ (state,    │  │ (pubsub,│  │ /var/hermeshq/workspaces │  │
│  │  history)  │  │  queues) │  │ /agent-{uuid}/...       │  │
│  └────────────┘  └─────────┘  └──────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Componentes principales

**API Server (FastAPI):** Punto de entrada para el frontend. Expone endpoints REST para CRUD de agentes, nodos, tareas y configuración. Mantiene conexiones WebSocket para streaming de logs y estado en tiempo real.

**Agent Process Supervisor:** Módulo Python que importa `AIAgent` de hermes-agent como librería (según la guía oficial `guides/python-library`). Cada agente corre en su propio thread con su propio `AIAgent` instance, workspace y configuración. El supervisor monitorea health, reinicia agentes caídos y gestiona el ciclo de vida completo.

**Comms Router:** Implementa la comunicación inter-agente. Usa Redis pub/sub para enrutar mensajes entre agentes locales y entre nodos. Cuando el agente A necesita delegar trabajo al agente B, el router intercepta el `delegate_task` y lo redirige al agente correcto según su `agent_id`.

**Remote Node Manager:** Para agentes en servidores remotos, usa SSH para instalar hermes-agent, desplegar configuración y ejecutar instancias de `AIAgent` dentro de contenedores Docker. Se comunica con el API server vía un daemon ligero (`hermeshq-node-agent`) que corre en cada nodo remoto.

---

## 3. Modelo de Datos (PostgreSQL)

```sql
-- ============================================================
-- NODOS: Servidores físicos o VMs donde corren agentes
-- ============================================================
CREATE TABLE nodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL UNIQUE,
    hostname        VARCHAR(255) NOT NULL,
    ssh_user        VARCHAR(64),
    ssh_port        INTEGER DEFAULT 22,
    ssh_key_path    TEXT,
    node_type       VARCHAR(20) NOT NULL DEFAULT 'local',  -- 'local' | 'remote_ssh' | 'remote_docker'
    hermes_path     TEXT DEFAULT '~/.hermes',
    max_agents      INTEGER DEFAULT 10,
    status          VARCHAR(20) DEFAULT 'offline',         -- 'online' | 'offline' | 'error'
    last_heartbeat  TIMESTAMPTZ,
    system_info     JSONB DEFAULT '{}',                    -- CPU, RAM, GPU info
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- AGENTES: Cada instancia de AIAgent con su configuración
-- ============================================================
CREATE TABLE agents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id             UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    name                VARCHAR(128) NOT NULL,
    slug                VARCHAR(128) NOT NULL UNIQUE,      -- identificador corto para inter-agent comms
    description         TEXT,
    status              VARCHAR(20) DEFAULT 'stopped',     -- 'running' | 'stopped' | 'error' | 'starting' | 'paused'
    run_mode            VARCHAR(20) DEFAULT 'hybrid',      -- 'headless' | 'interactive' | 'hybrid'
    
    -- Configuración del AIAgent
    model               VARCHAR(255) NOT NULL DEFAULT 'anthropic/claude-sonnet-4',
    provider            VARCHAR(64) DEFAULT 'openrouter',  -- 'openrouter' | 'anthropic' | 'openai' | 'custom'
    api_key_ref         VARCHAR(128),                      -- referencia a secreto en vault (nunca el key directo)
    base_url            VARCHAR(512),                      -- endpoint custom si aplica
    
    -- Toolsets y capabilities
    enabled_toolsets    TEXT[] DEFAULT '{}',                -- array de toolsets habilitados
    disabled_toolsets   TEXT[] DEFAULT '{}',                -- array de toolsets deshabilitados
    skills              TEXT[] DEFAULT '{}',                -- skills adicionales a instalar
    mcp_servers         JSONB DEFAULT '[]',                -- MCP servers config [{url, name}]
    
    -- Prompt y personalidad
    system_prompt       TEXT,                               -- ephemeral_system_prompt para AIAgent
    soul_md             TEXT,                               -- contenido SOUL.md custom
    personality         VARCHAR(64),                        -- nombre de personalidad Hermes
    context_files       JSONB DEFAULT '[]',                 -- archivos AGENTS.md adicionales [{path, content}]
    
    -- Workspace
    workspace_path      TEXT NOT NULL,                      -- /var/hermeshq/workspaces/agent-{uuid}
    working_directory   TEXT,                               -- cwd dentro del workspace
    
    -- Límites operacionales
    max_iterations      INTEGER DEFAULT 90,
    max_tokens_per_task INTEGER DEFAULT 100000,
    auto_approve_cmds   BOOLEAN DEFAULT FALSE,             -- auto-aprobar comandos peligrosos
    command_allowlist   TEXT[] DEFAULT '{}',
    
    -- Comunicación inter-agente
    can_receive_tasks   BOOLEAN DEFAULT TRUE,               -- puede recibir delegate_task de otros agentes
    can_send_tasks      BOOLEAN DEFAULT TRUE,               -- puede enviar delegate_task a otros agentes
    supervisor_agent_id UUID REFERENCES agents(id),         -- agente supervisor (para jerarquías)
    team_tags           TEXT[] DEFAULT '{}',                 -- tags de equipo para routing (ej: 'devops', 'research')
    
    -- Metadata
    total_tasks         INTEGER DEFAULT 0,
    total_tokens_used   BIGINT DEFAULT 0,
    last_activity       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_agents_node ON agents(node_id);
CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_agents_team_tags ON agents USING GIN(team_tags);

-- ============================================================
-- TAREAS: Cada invocación de chat/run_conversation
-- ============================================================
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    parent_task_id  UUID REFERENCES tasks(id),             -- si fue delegada por otra tarea
    source_agent_id UUID REFERENCES agents(id),            -- agente que delegó esta tarea
    
    title           VARCHAR(512),
    prompt          TEXT NOT NULL,
    system_override TEXT,                                   -- system_message override para esta tarea
    
    status          VARCHAR(20) DEFAULT 'queued',           -- 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
    priority        INTEGER DEFAULT 5,                      -- 1 (max) a 10 (min)
    
    -- Resultado
    response        TEXT,
    error_message   TEXT,
    messages_json   JSONB,                                  -- historial completo de mensajes
    tool_calls      JSONB DEFAULT '[]',                     -- resumen de tool calls ejecutados
    tokens_used     INTEGER DEFAULT 0,
    iterations      INTEGER DEFAULT 0,
    
    -- Timing
    queued_at       TIMESTAMPTZ DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    
    -- Metadata
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX idx_tasks_agent ON tasks(agent_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);

-- ============================================================
-- MENSAJES INTER-AGENTE: Comunicación entre agentes
-- ============================================================
CREATE TABLE agent_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent_id   UUID NOT NULL REFERENCES agents(id),
    to_agent_id     UUID NOT NULL REFERENCES agents(id),
    task_id         UUID REFERENCES tasks(id),
    
    message_type    VARCHAR(20) NOT NULL,                   -- 'delegate' | 'response' | 'broadcast' | 'direct'
    content         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',
    
    status          VARCHAR(20) DEFAULT 'pending',          -- 'pending' | 'delivered' | 'read' | 'failed'
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ
);

CREATE INDEX idx_agent_msgs_to ON agent_messages(to_agent_id, status);
CREATE INDEX idx_agent_msgs_from ON agent_messages(from_agent_id);

-- ============================================================
-- SECRETOS: Vault simple para API keys (encriptados en reposo)
-- ============================================================
CREATE TABLE secrets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(128) NOT NULL UNIQUE,               -- 'openrouter_main', 'anthropic_team', etc.
    provider    VARCHAR(64),                                 -- 'openrouter' | 'anthropic' | 'openai' | etc.
    value_enc   BYTEA NOT NULL,                             -- valor encriptado con Fernet
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- LOGS DE ACTIVIDAD: Audit trail completo
-- ============================================================
CREATE TABLE activity_logs (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    UUID REFERENCES agents(id) ON DELETE SET NULL,
    task_id     UUID REFERENCES tasks(id) ON DELETE SET NULL,
    node_id     UUID REFERENCES nodes(id) ON DELETE SET NULL,
    event_type  VARCHAR(64) NOT NULL,                       -- 'agent.started', 'task.completed', 'tool.executed', etc.
    severity    VARCHAR(10) DEFAULT 'info',                 -- 'debug' | 'info' | 'warn' | 'error'
    message     TEXT,
    details     JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_activity_agent ON activity_logs(agent_id, created_at DESC);
CREATE INDEX idx_activity_event ON activity_logs(event_type);
CREATE INDEX idx_activity_time ON activity_logs(created_at DESC);

-- ============================================================
-- CRON JOBS: Tareas programadas asignadas a agentes
-- ============================================================
CREATE TABLE scheduled_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name            VARCHAR(128) NOT NULL,
    cron_expression VARCHAR(128) NOT NULL,                  -- formato cron estándar
    prompt          TEXT NOT NULL,
    enabled         BOOLEAN DEFAULT TRUE,
    last_run        TIMESTAMPTZ,
    next_run        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TEMPLATES DE AGENTES: Configuraciones reutilizables
-- ============================================================
CREATE TABLE agent_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(128) NOT NULL UNIQUE,
    description     TEXT,
    config          JSONB NOT NULL,                         -- snapshot de columnas de agents (model, toolsets, prompt, etc.)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 4. Módulos del Backend (FastAPI)

### 4.1 Estructura de directorios

```
hermeshq/
├── main.py                         # FastAPI app, lifespan, CORS, mounts
├── config.py                       # Settings via pydantic-settings (.env)
├── database.py                     # SQLAlchemy async engine + session factory
├── models/                         # SQLAlchemy ORM models
│   ├── node.py
│   ├── agent.py
│   ├── task.py
│   ├── message.py
│   ├── secret.py
│   ├── activity.py
│   ├── scheduled_task.py
│   └── template.py
├── schemas/                        # Pydantic request/response schemas
│   ├── node.py
│   ├── agent.py
│   ├── task.py
│   ├── message.py
│   └── common.py
├── routers/                        # FastAPI routers (endpoints)
│   ├── nodes.py                    # CRUD nodos + health check
│   ├── agents.py                   # CRUD agentes + start/stop/restart
│   ├── tasks.py                    # Submit task, cancel, list, get result
│   ├── comms.py                    # Mensajes inter-agente, broadcast
│   ├── templates.py                # CRUD templates de agentes
│   ├── secrets.py                  # CRUD secretos (solo ref, nunca valor)
│   ├── dashboard.py                # Métricas agregadas para dashboard
│   ├── logs.py                     # Query activity logs
│   ├── ws.py                       # WebSocket endpoint para streaming
│   └── auth.py                     # Login, JWT tokens, user management
├── services/                       # Lógica de negocio
│   ├── agent_supervisor.py         # Spawn/monitor/kill AIAgent instances
│   ├── task_dispatcher.py          # Queue + dequeue + dispatch tareas
│   ├── comms_router.py             # Inter-agent message routing via Redis
│   ├── node_manager.py             # SSH connectivity, Docker deploy, health
│   ├── workspace_manager.py        # Create/destroy workspace dirs
│   ├── secret_vault.py             # Encrypt/decrypt API keys (Fernet)
│   ├── metrics_collector.py        # Recopilar métricas de agentes
│   └── scheduler.py                # Cron-like scheduler para scheduled_tasks
├── core/                           # Utilidades transversales
│   ├── security.py                 # JWT, password hashing, auth deps
│   ├── events.py                   # Event bus interno (in-process pub/sub)
│   ├── exceptions.py               # Custom exceptions
│   └── logging.py                  # Structured logging config
├── workers/                        # Background workers
│   ├── agent_worker.py             # Thread pool para AIAgent instances
│   └── heartbeat_worker.py         # Polling de health de nodos remotos
├── migrations/                     # Alembic migrations
│   └── versions/
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 4.2 Servicio: Agent Supervisor (`services/agent_supervisor.py`)

Este es el módulo central. Gestiona el ciclo de vida de cada `AIAgent`.

```python
"""
AgentSupervisor: Spawns and monitors AIAgent instances.

Cada agente corre en un thread dedicado dentro de un ThreadPoolExecutor.
Se crea una nueva instancia de AIAgent por agente (thread-safe según docs de Hermes).

Responsabilidades:
- start_agent(agent_id): Crea AIAgent con la configuración de DB, lo ejecuta en thread
- stop_agent(agent_id): Interrupt + cleanup del AIAgent
- restart_agent(agent_id): stop + start
- submit_task(agent_id, prompt, ...): Encola tarea para el agente
- get_agent_status(agent_id): Estado del thread + métricas
- health_check(): Verifica todos los agentes activos

Integración con Hermes:
- Usa `from run_agent import AIAgent` (hermes como librería Python)
- Configura AIAgent con quiet_mode=True siempre
- Usa callbacks para streaming de output al WebSocket del frontend
- Cada agente tiene su propio workspace dir como cwd

Ejemplo de spawn:
    agent_config = db.get_agent(agent_id)
    ai_agent = AIAgent(
        model=agent_config.model,
        quiet_mode=True,
        enabled_toolsets=agent_config.enabled_toolsets or None,
        disabled_toolsets=agent_config.disabled_toolsets or None,
        ephemeral_system_prompt=agent_config.system_prompt,
        max_iterations=agent_config.max_iterations,
        skip_context_files=False,  # usa context_files del workspace
        skip_memory=False,
        api_key=vault.decrypt(agent_config.api_key_ref),
        base_url=agent_config.base_url,
        platform="hermeshq",
    )
    os.chdir(agent_config.workspace_path)
    result = ai_agent.run_conversation(
        user_message=task.prompt,
        task_id=str(task.id),
        system_message=task.system_override,
    )
"""
```

### 4.3 Servicio: Comms Router (`services/comms_router.py`)

```python
"""
CommsRouter: Enrutamiento de mensajes entre agentes.

Usa Redis pub/sub para comunicación en tiempo real.
Cada agente suscribe a un channel: "hermeshq:agent:{agent_id}"
El router también publica en "hermeshq:broadcast:{team_tag}" para broadcasts.

Flujo de delegate_task inter-agente:
1. Agente A ejecuta una tarea y su AIAgent produce un tool_call "delegate_task"
2. El AgentSupervisor intercepta este tool_call antes del dispatch nativo de Hermes
3. El supervisor consulta al CommsRouter para resolver el agente destino:
   - Si delegate_task especifica un agent_slug -> routing directo
   - Si especifica team_tags -> el router elige el agente menos cargado del team
   - Si no especifica destino -> el router usa el supervisor_agent_id o rechaza
4. El router crea un registro en agent_messages y una task en tasks
5. El router publica en Redis channel del agente destino
6. El agente destino recoge la tarea de su cola y la ejecuta
7. El resultado se publica de vuelta al agente origen vía Redis
8. El supervisor inyecta el resultado como tool_result en la conversación del agente A

Channels Redis:
  hermeshq:agent:{agent_id}         -> mensajes directos
  hermeshq:broadcast:{team_tag}     -> broadcast a equipos
  hermeshq:tasks:{agent_id}         -> cola de tareas pendientes
  hermeshq:results:{task_id}        -> resultado de tarea delegada
"""
```

### 4.4 Servicio: Node Manager (`services/node_manager.py`)

```python
"""
NodeManager: Gestión de nodos locales y remotos.

Para nodos remotos:
1. Conexión SSH con paramiko (key-based auth)
2. Verificar/instalar hermes-agent en el nodo remoto
3. Desplegar hermeshq-node-agent (daemon ligero FastAPI)
4. El node-agent expone:
   - POST /agent/start -> spawn AIAgent local
   - POST /agent/stop  -> kill AIAgent
   - POST /agent/task  -> submit task a agente local
   - GET  /agent/status -> estado de agentes locales
   - GET  /health       -> heartbeat con métricas del sistema
   - WS   /stream       -> streaming de logs y output

Para nodos con Docker:
1. Conexión SSH al host Docker
2. Build/pull imagen hermeshq-agent (basada en hermes-agent)
3. Cada agente corre como container con:
   - Volume mount para workspace
   - Variables de entorno para API keys
   - Network bridge para comunicación inter-container
   - Resource limits (CPU, RAM)

Health monitoring:
- Heartbeat cada 30s vía GET /health
- Si falla 3 heartbeats consecutivos -> marcar nodo como 'error'
- Alertar al frontend vía WebSocket
"""
```

### 4.5 Servicio: Workspace Manager (`services/workspace_manager.py`)

```python
"""
WorkspaceManager: Crea y gestiona directorios de trabajo aislados para cada agente.

Estructura de workspace:
    /var/hermeshq/workspaces/
    └── agent-{uuid}/
        ├── AGENTS.md           -> Context file principal (generado desde agent.context_files)
        ├── SOUL.md             -> Personalidad del agente (desde agent.soul_md)
        ├── .hermes/            -> Config Hermes local del agente
        │   ├── config.yaml
        │   ├── skills/         -> Skills custom del agente
        │   ├── memory/         -> Persistent memory del agente
        │   └── sessions.db     -> SQLite sessions (gestionado por Hermes)
        ├── work/               -> Directorio de trabajo del agente
        └── shared/             -> Symlink a /var/hermeshq/shared/ (datos compartidos entre agentes)

Operaciones:
- create_workspace(agent_id, config) -> Crea estructura, escribe AGENTS.md y SOUL.md
- destroy_workspace(agent_id) -> Elimina directorio completo (con confirmación)
- sync_config(agent_id) -> Re-escribe AGENTS.md/SOUL.md desde DB
- get_workspace_size(agent_id) -> Tamaño en disco del workspace
- list_workspace_files(agent_id, path) -> ls del workspace
- read_workspace_file(agent_id, path) -> Leer archivo del workspace
- write_workspace_file(agent_id, path, content) -> Escribir archivo al workspace
"""
```

### 4.6 Servicio: PTY Terminal Manager (`services/pty_manager.py`)

```python
"""
PTYManager: Gestión de pseudo-terminales para exponer la TUI de Hermes en el browser.

En vez de correr AIAgent solo como librería silenciosa (quiet_mode=True),
cada agente puede opcionalmente spawnearse dentro de un PTY real ejecutando
el CLI completo de Hermes (`hermes` command). Esto captura toda la salida
formateada: spinners Rich, multiline editing, colores ANSI, tool output,
slash commands — exactamente como se vería en una terminal local.

Arquitectura:
    Browser (xterm.js)  <-->  WebSocket  <-->  PTYManager  <-->  PTY (hermes CLI)

Implementación:
- Usa `pty.openpty()` para crear un par master/slave por agente
- El proceso hijo ejecuta `hermes` dentro del slave PTY con el env del agente
- El master fd se lee de forma asíncrona y se envía por WebSocket al browser
- El input del usuario desde xterm.js viaja por WebSocket → master fd → proceso
- Soporta resize (SIGWINCH) cuando el usuario redimensiona la terminal en el browser

Modos de operación por agente:
- 'headless': quiet_mode=True, sin PTY, solo callbacks (default, más eficiente)
- 'interactive': PTY completo con TUI de Hermes visible en el browser
- 'hybrid': headless + PTY attach on-demand (el agente corre headless, pero el
  operador puede "attacharse" a ver la TUI en cualquier momento, similar a tmux attach)

El modo 'hybrid' es el recomendado para producción:
1. El agente corre normalmente en headless (bajo consumo de recursos)
2. Cuando el operador abre la terminal del agente en el panel, se crea un PTY
   que ejecuta `hermes` con --session-id del agente activo
3. El operador puede ver el output en tiempo real e incluso interactuar
4. Cuando cierra la terminal, el PTY se destruye pero el agente sigue corriendo

Para nodos remotos:
- El hermeshq-node-agent en el nodo remoto expone WS /pty/{agent_id}
- El API server del panel proxea: Browser <-> API WS <-> Node WS <-> PTY
- Latencia aceptable para texto (no video), típicamente <50ms adicional

Dependencias:
- pty (stdlib Python)
- fcntl (para non-blocking reads)
- asyncio con loop.add_reader() para bridgear fd sync → async
- signal.SIGWINCH para resize

Clase principal:
    class PTYSession:
        agent_id: UUID
        master_fd: int
        slave_fd: int
        pid: int               # PID del proceso hermes
        ws_connections: set     # WebSockets conectados a esta sesión
        cols: int = 120
        rows: int = 40

    class PTYManager:
        sessions: dict[UUID, PTYSession]

        async def create_session(agent_id, cols, rows) -> PTYSession
        async def destroy_session(agent_id)
        async def attach(agent_id, websocket) -> detach cuando WS cierra
        async def write_input(agent_id, data: bytes)
        async def resize(agent_id, cols, rows)
        def list_sessions() -> list[PTYSession]
"""
```

---

## 5. API Endpoints

### 5.1 Autenticación

```
POST   /api/auth/login              -> JWT token (username + password)
POST   /api/auth/refresh            -> Refresh JWT
GET    /api/auth/me                 -> Current user info
```

### 5.2 Nodos

```
GET    /api/nodes                   -> Lista todos los nodos
POST   /api/nodes                   -> Registrar nuevo nodo
GET    /api/nodes/{id}              -> Detalle de nodo + agentes
PUT    /api/nodes/{id}              -> Actualizar config del nodo
DELETE /api/nodes/{id}              -> Eliminar nodo (si no tiene agentes activos)
POST   /api/nodes/{id}/test         -> Test conectividad SSH
POST   /api/nodes/{id}/provision    -> Instalar hermes-agent en nodo remoto
GET    /api/nodes/{id}/metrics      -> CPU, RAM, GPU, disk del nodo
```

### 5.3 Agentes

```
GET    /api/agents                  -> Lista todos los agentes (filtrable por status, node, team_tag)
POST   /api/agents                  -> Crear nuevo agente
GET    /api/agents/{id}             -> Detalle del agente + stats
PUT    /api/agents/{id}             -> Actualizar configuración
DELETE /api/agents/{id}             -> Eliminar agente + workspace
POST   /api/agents/{id}/start      -> Iniciar agente
POST   /api/agents/{id}/stop       -> Detener agente
POST   /api/agents/{id}/restart    -> Reiniciar agente
POST   /api/agents/{id}/mode       -> Cambiar modo: headless | interactive | hybrid
GET    /api/agents/{id}/logs       -> Logs del agente (paginados)
GET    /api/agents/{id}/sessions   -> Sesiones Hermes del agente
GET    /api/agents/{id}/workspace  -> Explorar archivos del workspace
GET    /api/agents/{id}/workspace/{path} -> Leer archivo específico
PUT    /api/agents/{id}/workspace/{path} -> Escribir archivo al workspace
POST   /api/agents/from-template/{template_id} -> Crear agente desde template
```

### 5.4 Tareas

```
POST   /api/tasks                   -> Submit nueva tarea a un agente
GET    /api/tasks                   -> Lista tareas (filtrable por agent, status, date)
GET    /api/tasks/{id}              -> Detalle de tarea + messages_json + tool_calls
POST   /api/tasks/{id}/cancel       -> Cancelar tarea en ejecución
GET    /api/tasks/{id}/stream       -> SSE stream del output de la tarea
GET    /api/tasks/queue              -> Estado de la cola global
```

### 5.5 Comunicación Inter-Agente

```
POST   /api/comms/send              -> Enviar mensaje directo entre agentes
POST   /api/comms/broadcast         -> Broadcast a team_tag
GET    /api/comms/history           -> Historial de comunicaciones (filtrable)
GET    /api/comms/topology          -> Mapa de relaciones entre agentes
```

### 5.6 Templates

```
GET    /api/templates               -> Lista templates
POST   /api/templates               -> Crear template (desde config o desde agente existente)
GET    /api/templates/{id}          -> Detalle
PUT    /api/templates/{id}          -> Actualizar
DELETE /api/templates/{id}          -> Eliminar
```

### 5.7 Dashboard y Métricas

```
GET    /api/dashboard/overview      -> Stats globales (agentes activos, tareas hoy, tokens, etc.)
GET    /api/dashboard/agents        -> Estado resumido de cada agente
GET    /api/dashboard/tokens        -> Uso de tokens por agente/modelo/período
GET    /api/dashboard/tasks/stats   -> Tareas completadas/fallidas/pendientes por período
```

### 5.8 WebSocket

```
WS     /ws/stream                   -> Stream unificado de eventos:
                                       - agent.status_changed
                                       - task.started / task.progress / task.completed / task.failed
                                       - agent.tool_call (cada tool call del agente)
                                       - agent.output (streaming del texto del agente)
                                       - comms.message (mensajes inter-agente)
                                       - node.health_changed
                                       - system.alert
```

### 5.9 Terminal PTY (WebSocket)

```
WS     /ws/pty/{agent_id}           -> Terminal interactiva del agente

Protocolo bidireccional binario/JSON:
  Browser → Server:
    { "type": "input",  "data": "<keystroke bytes base64>" }
    { "type": "resize", "cols": 120, "rows": 40 }
    { "type": "detach" }

  Server → Browser:
    { "type": "output", "data": "<terminal output bytes base64>" }
    { "type": "connected", "cols": 120, "rows": 40, "mode": "hybrid" }
    { "type": "disconnected", "reason": "agent_stopped" }

Comportamiento:
- Al conectar, si el agente está en modo 'interactive', conecta al PTY existente
- Si está en modo 'hybrid', crea PTY on-demand y attacha a la sesión activa
- Si está en modo 'headless', retorna error 400 indicando cambiar modo primero
- Múltiples browsers pueden conectarse al mismo PTY (read-only para los adicionales,
  o configurable para permitir input desde múltiples fuentes)
- Al desconectar el último browser, en modo 'hybrid' el PTY se destruye;
  en modo 'interactive' el PTY persiste

Para agentes en nodos remotos:
  El API server proxea el WebSocket hacia el hermeshq-node-agent:
    Browser <-WS-> API Server <-WS-> Node Agent <-PTY-> hermes CLI
  El endpoint en el node-agent es: WS /pty/{agent_id}
```

---

## 6. Frontend (React + Vite + TailwindCSS)

### 6.1 Estructura

```
frontend/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/                        # Axios client + React Query hooks
│   │   ├── client.ts               # Axios instance con JWT interceptor
│   │   ├── agents.ts               # useAgents, useAgent, useCreateAgent, etc.
│   │   ├── tasks.ts                # useTasks, useSubmitTask, etc.
│   │   ├── nodes.ts
│   │   ├── comms.ts
│   │   └── dashboard.ts
│   ├── hooks/
│   │   ├── useWebSocket.ts         # Conexión WS + auto-reconnect + event dispatch
│   │   ├── useTerminal.ts          # Hook para xterm.js: connect, resize, input, attach/detach
│   │   └── useAuth.ts
│   ├── stores/                     # Zustand stores
│   │   ├── agentStore.ts           # Estado real-time de agentes (actualizado via WS)
│   │   ├── taskStore.ts            # Tareas activas y sus streams
│   │   ├── ptyStore.ts             # Estado de sesiones PTY abiertas por agente
│   │   └── uiStore.ts              # Sidebar, modals, theme
│   ├── pages/
│   │   ├── Dashboard.tsx           # Overview con cards de métricas + actividad reciente
│   │   ├── Agents.tsx              # Lista de agentes con filtros + acciones rápidas
│   │   ├── AgentDetail.tsx         # Config + logs + sesiones + workspace explorer
│   │   ├── AgentCreate.tsx         # Wizard de creación de agente (o desde template)
│   │   ├── Tasks.tsx               # Task board (kanban o lista)
│   │   ├── TaskDetail.tsx          # Output de tarea + tool calls + timeline
│   │   ├── Comms.tsx               # Vista de comunicaciones inter-agente (chat-like)
│   │   ├── Nodes.tsx               # Gestión de nodos + health
│   │   ├── Templates.tsx           # CRUD de templates
│   │   ├── Settings.tsx            # Config global, secretos, usuarios
│   │   └── Login.tsx
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Sidebar.tsx
│   │   │   ├── Header.tsx
│   │   │   └── Layout.tsx
│   │   ├── agents/
│   │   │   ├── AgentCard.tsx       # Card con status indicator, modelo, acciones
│   │   │   ├── AgentForm.tsx       # Formulario completo de config
│   │   │   ├── AgentStatusBadge.tsx
│   │   │   ├── AgentLogs.tsx       # Log viewer con auto-scroll y filtros
│   │   │   ├── AgentTerminal.tsx   # xterm.js wrapper — TUI embebida del agente
│   │   │   ├── TerminalToolbar.tsx # Controles: attach/detach, mode switch, resize, screenshot
│   │   │   └── WorkspaceExplorer.tsx # File tree + editor
│   │   ├── tasks/
│   │   │   ├── TaskSubmitForm.tsx  # Prompt + agent selector + opciones
│   │   │   ├── TaskCard.tsx
│   │   │   ├── TaskOutput.tsx      # Streaming output con markdown render
│   │   │   └── ToolCallTimeline.tsx # Timeline visual de tool calls
│   │   ├── comms/
│   │   │   ├── MessageThread.tsx
│   │   │   ├── AgentTopology.tsx   # Grafo visual de relaciones (react-flow)
│   │   │   └── BroadcastForm.tsx
│   │   ├── dashboard/
│   │   │   ├── StatsCards.tsx      # Agentes activos, tareas hoy, tokens
│   │   │   ├── TokenChart.tsx      # Gráfico de uso de tokens (recharts)
│   │   │   ├── ActivityFeed.tsx    # Feed de actividad reciente
│   │   │   └── AgentGrid.tsx       # Grid de agentes con status real-time
│   │   └── shared/
│   │       ├── JsonEditor.tsx      # Editor JSON para configs avanzadas
│   │       ├── TerminalView.tsx    # Terminal-like output viewer
│   │       └── ConfirmDialog.tsx
│   └── utils/
│       ├── formatters.ts
│       └── constants.ts
├── tailwind.config.js
├── vite.config.ts
├── package.json
└── tsconfig.json
```

### 6.2 Vistas Principales

**Dashboard:** Grid de 4 stat cards (agentes activos, tareas hoy, tokens consumidos, tasa de éxito). Debajo, dos columnas: a la izquierda un grid de agentes con indicador de status LED (verde/amarillo/rojo), nombre, modelo y última actividad; a la derecha un activity feed tipo timeline con los últimos 50 eventos.

**Agent Manager:** Tabla/grid de agentes con columnas: nombre, nodo, modelo, status, tareas completadas, tokens, última actividad. Cada fila tiene botones de start/stop/restart/delete. Botón "Nuevo Agente" abre wizard. Filtros por nodo, status, team_tag.

**Agent Detail:** Tabs: Terminal (TUI embebida del agente vía xterm.js — ver sección 6.3), Config (formulario editable con todos los campos del agente), Logs (terminal view con los últimos 1000 logs filtrable por severity), Tasks (lista de tareas del agente), Workspace (file explorer con viewer/editor inline), Sessions (sesiones Hermes con historial). El tab Terminal es el default cuando el agente está running.

**Task Board:** Vista kanban con columnas Queued / Running / Completed / Failed. Cada card muestra título, agente asignado, duración, tokens. Click abre detalle con streaming output, tool call timeline y mensajes completos. Formulario "Nueva Tarea" con selector de agente, prompt textarea, y opciones avanzadas (system override, priority).

**Comms:** Vista dividida. Izquierda: lista de conversaciones inter-agente. Derecha: thread de mensajes estilo chat con sender, timestamp, contenido. Tab "Topología" muestra grafo de relaciones usando react-flow (nodos = agentes, edges = delegaciones/mensajes).

### 6.3 Terminal Embebida (AgentTerminal)

El componente `AgentTerminal.tsx` renderiza la TUI completa de Hermes dentro del browser usando xterm.js. Esto permite al operador ver exactamente lo que vería si estuviera en una terminal SSH conectada al agente: spinners, colores, tool output con syntax highlighting, slash commands, multiline editing — todo.

**Stack del componente:**

```
AgentTerminal.tsx
├── @xterm/xterm              # Core terminal emulator
├── @xterm/addon-fit          # Auto-resize al container
├── @xterm/addon-web-links    # Links clickeables en output
├── @xterm/addon-search       # Ctrl+F para buscar en output
├── @xterm/addon-unicode11    # Soporte unicode completo (emojis de Hermes)
└── @xterm/addon-webgl        # Render GPU-accelerated (performance)
```

**Dependencias npm:**
```json
{
  "@xterm/xterm": "^5.5",
  "@xterm/addon-fit": "^0.10",
  "@xterm/addon-web-links": "^0.11",
  "@xterm/addon-search": "^0.15",
  "@xterm/addon-unicode11": "^0.8",
  "@xterm/addon-webgl": "^0.18"
}
```

**Comportamiento del componente:**

```typescript
/**
 * AgentTerminal: xterm.js wrapper que conecta al PTY del agente vía WebSocket.
 *
 * Props:
 *   agentId: string          - UUID del agente
 *   mode: 'readonly' | 'interactive'  - si el usuario puede enviar input
 *   autoConnect: boolean     - conectar automáticamente al montar
 *
 * Lifecycle:
 *   1. Mount: Inicializa xterm.Terminal con theme oscuro y font monospace
 *   2. Fit: addon-fit calcula cols/rows del container y envía resize al server
 *   3. Connect: Abre WebSocket a /ws/pty/{agentId} con JWT en query param
 *   4. Stream: Recibe output del PTY y lo escribe en xterm via terminal.write()
 *   5. Input: Captura keystrokes del usuario y los envía al WS como input events
 *   6. Resize: ResizeObserver detecta cambios de tamaño → fit → resize event al WS
 *   7. Unmount: Cierra WebSocket (en modo hybrid, esto destruye el PTY)
 *
 * Reconnección:
 *   Si el WebSocket se cae, intenta reconectar cada 3s con backoff exponencial
 *   hasta 30s max. Muestra overlay "Reconectando..." sobre la terminal.
 *
 * Temas:
 *   El terminal usa un theme que matchea el design system del panel.
 *   Hermes emite secuencias ANSI estándar que xterm renderiza nativamente.
 *   No hay necesidad de parsear o transformar el output.
 *
 * Performance:
 *   - addon-webgl para rendering GPU (caída a canvas 2D si no soportado)
 *   - Buffer de escritura: acumula output por 16ms antes de flush (reduce reflows)
 *   - Scrollback buffer: 10000 líneas (configurable)
 *
 * Ejemplo de uso en AgentDetail.tsx:
 *   <AgentTerminal
 *     agentId={agent.id}
 *     mode={canInteract ? 'interactive' : 'readonly'}
 *     autoConnect={agent.status === 'running'}
 *   />
 */
```

**TerminalToolbar:** Barra superior sobre la terminal con controles:
- **Status LED:** Verde (conectado), amarillo (reconectando), rojo (desconectado)
- **Mode switch:** Toggle entre headless/hybrid/interactive (llama POST /api/agents/{id}/mode)
- **Attach/Detach:** Conectar/desconectar del PTY sin afectar al agente
- **Read-only toggle:** Deshabilitar input del usuario (útil para monitoreo)
- **Fullscreen:** Expandir terminal a pantalla completa
- **Screenshot:** Capturar el estado actual de la terminal como imagen
- **Search:** Toggle barra de búsqueda (addon-search)

**Vista multi-terminal en Dashboard:**

Para monitoreo de múltiples agentes simultáneamente, el Dashboard puede mostrar una grid de mini-terminales (componente `AgentGrid.tsx` actualizado). Cada celda muestra una terminal reducida (60x15 chars) en modo readonly del agente. Click expande a vista completa en Agent Detail. Esto da una vista tipo "centro de control" donde se ve toda la actividad de la flota en un vistazo.

```
┌──────────────────────────────────────────────────────────────────┐
│  Dashboard  >  Agent Fleet Monitor                               │
├──────────┬──────────┬──────────┬──────────┬──────────────────────┤
│ Agent A  │ Agent B  │ Agent C  │ Agent D  │  Stats              │
│ ●running │ ●running │ ●idle   │ ●running │                      │
│┌────────┐│┌────────┐│┌────────┐│┌────────┐│  Agents: 4/6 active │
││$ hermes ││$ hermes ││         ││$ hermes ││  Tasks today: 47    │
││> Working││> Searchn││ Waiting ││> Deploy ││  Tokens: 1.2M       │
││  on... ││  for... ││  for   ││  to... ││  Success: 94%       │
││         ││         ││  task   ││         ││                      │
│└────────┘│└────────┘│└────────┘│└────────┘│                      │
├──────────┴──────────┴──────────┴──────────┴──────────────────────┤
│  Activity Feed                                                    │
│  14:32 Agent A completed task "Generate report" (2.3k tokens)    │
│  14:31 Agent D delegated "fetch metrics" to Agent B              │
│  14:30 Agent B tool_call: web_search("Q1 revenue data")         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 7. Comunicación Inter-Agente: Detalle Técnico

### 7.1 Mecanismo usando primitivas nativas de Hermes

Hermes Agent tiene dos herramientas nativas relevantes:

**`delegate_task`:** Herramienta que en el Hermes original spawna un subagente local. En HermesHQ, interceptamos esta herramienta en el `handle_function_call` para redirigirla a otro agente gestionado por el panel.

**`send_message`:** Herramienta para enviar mensajes a plataformas. La extendemos para permitir envío a otros agentes del panel.

### 7.2 Flujo de delegación

```
Agente "Investigador" recibe tarea: "Investiga el mercado de IA en Chile y genera un reporte"
    │
    ├── AIAgent decide usar delegate_task(
    │       task="Buscar datos de startups de IA en Chile",
    │       target="web-scraper"   # <-- slug de otro agente en HermesHQ
    │   )
    │
    ├── AgentSupervisor intercepta el tool_call
    │   ├── Resuelve "web-scraper" -> agent_id del agente Web Scraper
    │   ├── Crea task en DB con parent_task_id y source_agent_id
    │   ├── Publica en Redis channel del Web Scraper
    │   └── Espera resultado en Redis channel hermeshq:results:{task_id}
    │
    ├── Agente "Web Scraper" recoge la tarea
    │   ├── Ejecuta AIAgent.run_conversation(prompt=task_prompt)
    │   ├── Usa web_search, browser, terminal para buscar datos
    │   ├── Retorna resultado
    │   └── Publica resultado en hermeshq:results:{task_id}
    │
    ├── AgentSupervisor recibe resultado
    │   ├── Inyecta como tool_result en la conversación del Investigador
    │   └── El Investigador continúa su tarea con los datos
    │
    └── Investigador genera el reporte final
```

### 7.3 Registro de herramientas custom para inter-agent

```python
"""
Al crear un AIAgent para HermesHQ, inyectamos herramientas adicionales
en el system prompt que permiten al agente descubrir y comunicarse con
otros agentes del panel.

Herramientas inyectadas:
- list_team_agents(team_tag?) -> Lista agentes disponibles con sus capabilities
- delegate_to_agent(agent_slug, task, context?) -> Delega tarea a agente específico
- broadcast_team(team_tag, message) -> Envía mensaje a todos los agentes de un equipo
- query_agent(agent_slug, question) -> Pregunta simple a otro agente (sync, timeout 60s)

Estas herramientas se registran en el registry de Hermes usando
registry.register() antes de construir el AIAgent, aprovechando
el sistema de tool registration at import time.
"""
```

---

## 8. Despliegue

### 8.1 Docker Compose (producción)

```yaml
# docker-compose.yml
version: "3.9"

services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8420:8420"
    environment:
      - DATABASE_URL=postgresql+asyncpg://hermeshq:${DB_PASSWORD}@db:5432/hermeshq
      - REDIS_URL=redis://redis:6379/0
      - SECRET_KEY=${SECRET_KEY}
      - FERNET_KEY=${FERNET_KEY}
      - HERMESHQ_WORKSPACES=/var/hermeshq/workspaces
    volumes:
      - workspaces:/var/hermeshq/workspaces
      - ssh_keys:/root/.ssh:ro
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: "4"
          memory: 8G

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3420:80"
    depends_on:
      - api
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: hermeshq
      POSTGRES_USER: hermeshq
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hermeshq"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

volumes:
  workspaces:
  pgdata:
  redisdata:
  ssh_keys:
```

### 8.2 Dockerfile del API Server

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client curl build-essential nodejs npm pandoc \
    && rm -rf /var/lib/apt/lists/*

# Instalar hermes-agent como librería
RUN pip install --no-cache-dir \
    git+https://github.com/NousResearch/hermes-agent.git

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hermeshq/ hermeshq/
COPY alembic.ini .
COPY migrations/ migrations/

RUN mkdir -p /var/hermeshq/workspaces

EXPOSE 8420

CMD ["uvicorn", "hermeshq.main:app", "--host", "0.0.0.0", "--port", "8420", "--workers", "1"]
```

Nota: Se usa `--workers 1` porque el AgentSupervisor mantiene estado in-process (threads de AIAgent). Para escalar horizontalmente, se necesitaría externalizar el supervisor a un proceso dedicado con Redis como bus.

### 8.3 Requirements principales

```
fastapi>=0.115
uvicorn[standard]>=0.30
sqlalchemy[asyncio]>=2.0
asyncpg>=0.30
alembic>=1.14
redis>=5.0
pydantic>=2.0
pydantic-settings>=2.0
python-jose[cryptography]>=3.3
passlib[bcrypt]>=1.7
cryptography>=43.0
paramiko>=3.4
python-multipart>=0.0.9
hermes-agent  # git+https://github.com/NousResearch/hermes-agent.git
```

---

## 9. Seguridad

**Autenticación:** JWT con access token (15min) + refresh token (7d). Passwords hasheados con bcrypt. Primer usuario se crea en setup inicial.

**Secretos:** Todas las API keys se almacenan encriptadas con Fernet (symmetric encryption). La FERNET_KEY se pasa como variable de entorno y nunca se persiste en DB. El frontend nunca recibe valores de API keys, solo referencias (`api_key_ref`).

**Aislamiento de workspaces:** Cada agente opera en su propio directorio. Los agentes no pueden acceder al filesystem fuera de su workspace excepto vía el directorio `shared/`.

**Aprobación de comandos:** El sistema de `DANGEROUS_PATTERNS` de Hermes se mantiene activo. Los comandos peligrosos se pueden auto-aprobar por agente (`auto_approve_cmds`) o usar la `command_allowlist` del agente. Las aprobaciones se logean en `activity_logs`.

**Nodos remotos:** Conexión SSH exclusivamente con key-based auth. El `hermeshq-node-agent` expone solo un puerto interno (no público) y valida requests con un shared secret rotado diariamente.

---

## 10. Configuración Inicial y Bootstrap

```bash
# 1. Clonar el repo
git clone https://github.com/sixmanager/hermeshq.git
cd hermeshq

# 2. Copiar y editar configuración
cp .env.example .env
# Editar: DB_PASSWORD, SECRET_KEY, FERNET_KEY, y al menos una API key de LLM

# 3. Levantar servicios
docker compose up -d

# 4. Ejecutar migraciones
docker compose exec api alembic upgrade head

# 5. Crear usuario admin
docker compose exec api python -m hermeshq.scripts.create_admin

# 6. Acceder al panel
# Frontend: http://localhost:3420
# API docs: http://localhost:8420/docs

# 7. En el panel:
#    a. Agregar un nodo local (se auto-detecta)
#    b. Crear un secreto con API key de OpenRouter/Anthropic
#    c. Crear primer agente seleccionando modelo, toolsets y prompt
#    d. Iniciar el agente y enviar primera tarea
```

---

## 11. Prioridades de Implementación

### Fase 1 — Core funcional (semanas 1-3)
1. Modelo de datos + migraciones Alembic
2. CRUD completo de nodos, agentes, secretos, templates
3. AgentSupervisor: spawn/stop AIAgent local
4. PTYManager: crear/destruir PTY sessions, attach/detach
5. WorkspaceManager: crear/destruir workspaces
6. TaskDispatcher: submit/execute/complete tareas
7. WebSocket para streaming de output + PTY proxy
8. Frontend: Login, Dashboard básico, Agent CRUD, Task submit + output view
9. Frontend: AgentTerminal con xterm.js + TerminalToolbar + resize
10. Auth JWT

### Fase 2 — Inter-agent y monitoring (semanas 4-5)
1. CommsRouter: Redis pub/sub + delegate interception
2. Herramientas custom inter-agente (list_team_agents, delegate_to_agent, etc.)
3. Frontend: Comms view, topología, broadcast
4. Frontend: Dashboard multi-terminal grid (fleet monitor)
5. Métricas y charts de tokens/tareas
6. Activity logs viewer
7. Scheduled tasks (cron)

### Fase 3 — Nodos remotos y hardening (semanas 6-7)
1. NodeManager: SSH + provisioning
2. hermeshq-node-agent daemon con PTY proxy remoto
3. Docker-based agent deployment en nodos remotos
4. PTY proxy chain: Browser ↔ API ↔ Node Agent ↔ PTY
5. Health monitoring + alertas
6. Workspace explorer con editor inline
7. Templates de agente con wizard de creación
8. Rate limiting, error recovery, auto-restart

### Fase 4 — Polish y features avanzados (semana 8+)
1. Tool call timeline visual
2. Kanban board para tareas
3. Export de trayectorias (training data)
4. MCP server integration en la UI
5. Skills browser/installer desde la UI
6. Bulk operations (start/stop múltiples agentes)
7. Notificaciones (email/Telegram/webhook)

---

## 12. Convenciones de Código

**Python (backend):**
- Type hints obligatorios en todas las funciones públicas
- Docstrings Google style en módulos y clases
- async/await para todo I/O (DB, Redis, HTTP)
- Tests con pytest + pytest-asyncio
- Linting: ruff
- Formato: ruff format (compatible black)

**TypeScript (frontend):**
- Strict mode habilitado
- React Query para data fetching (no useEffect para fetch)
- Zustand para estado global
- Componentes funcionales con hooks exclusivamente
- TailwindCSS para estilos (no CSS modules)
- Linting: eslint + prettier

**Git:**
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`
- Branch: `main` (producción), `develop` (integración), `feat/xxx` (features)
- PR obligatorio para merge a develop/main

**Docker:**
- Multi-stage builds para frontend
- Health checks en todos los servicios
- No correr como root dentro de containers (donde sea posible)

---

## 13. Variables de Entorno

```bash
# === Base de datos ===
DATABASE_URL=postgresql+asyncpg://hermeshq:password@db:5432/hermeshq

# === Redis ===
REDIS_URL=redis://redis:6379/0

# === Seguridad ===
SECRET_KEY=<random-64-char-string>           # Para JWT
FERNET_KEY=<fernet-key-base64>               # Para encriptar secretos en DB

# === Paths ===
HERMESHQ_WORKSPACES=/var/hermeshq/workspaces # Raíz de workspaces de agentes

# === API Keys por defecto (opcionales, se pueden cargar via UI) ===
OPENROUTER_API_KEY=                          # Si se quiere un default global
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# === Servidor ===
HERMESHQ_HOST=0.0.0.0
HERMESHQ_PORT=8420
HERMESHQ_LOG_LEVEL=info

# === Frontend ===
VITE_API_URL=http://localhost:8420           # URL del API server
VITE_WS_URL=ws://localhost:8420/ws/stream   # URL del WebSocket
```

---

*Documento generado para ser consumido por un agente de codificación. Cada sección contiene suficiente detalle para implementación directa sin ambigüedades. El agente debe seguir las fases de prioridad indicadas en la sección 11.*
