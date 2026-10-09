# SPEC: Runtime Pi + Permission System para Agentes Pi

**Fecha:** 2026-08-09  
**Estado:** Borrador  
**Alcance:** Pi-only — sin modificar el comportamiento de agentes Hermes existentes

---

## 1. Resumen

Agregar **Pi** (TypeScript/Node.js, Earendil Inc.) como segundo runtime de agentes en HermesHQ, con un **Permission System granular exclusivo para agentes Pi**. Los agentes Hermes quedan intactos.

### Qué se reutiliza sin cambios
- **Task lifecycle** — `Task`, `Task.messages_json`, `Task.response`, board, streaming
- **EventBroker** — WebSocket fan-out (runtime-agnostic)
- **Scheduler** — `ScheduledTask` solo crea `Task` rows y llama `submit_task`
- **WorkspaceManager** — file I/O genérico
- **PTYManager** — terminal multiplexer (acepta cualquier comando)
- **Agent Builder** — mechanics del LLM tool-calling loop
- **Comms** — routing, broadcast, history
- **Enterprise channels** (Google Chat, Kapso WhatsApp) — ya runtime-agnostic via `/webhooks`
- **Integraciones** — los handlers HTTP de M365/SharePoint/etc. se re-exponen como MCP tools

### Qué es nuevo (Pi-only)
- `PiRuntime` — subprocess Node.js con SDK de Pi en modo RPC
- `PiInstallationManager` — genera `.pi/settings.json` + extensiones TypeScript
- `PermissionPolicy` — tabla + servicio + enforcement exclusivo para Pi
- Pi gateway para canales nativos (Telegram, WhatsApp, Teams, SixAgentic)

---

## 2. Modelo de Datos

### 2.1 Agent — nuevos campos

```python
# El campo run_mode ya existe pero hoy solo significa headless|interactive|hybrid (modo terminal).
# Se agrega runtime_type como discriminador de runtime engine.

runtime_type: Mapped[str] = mapped_column(String(16), default="hermes")  # "hermes" | "pi"

pi_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
# {
#   "tools": ["read", "bash", "edit", "write", "grep", "find", "ls"],
#   "exclude_tools": [],
#   "thinking_level": "medium",
#   "compaction": {"enabled": true, "threshold_tokens": 100000},
#   "max_turns": 50,
#   "project_trust": "always",
#   "system_prompt_override": null
# }

permission_policy_id: Mapped[str | None] = mapped_column(
    ForeignKey("permission_policies.id", ondelete="SET NULL"), nullable=True
)
```

> **Nota:** `runtime_type` es un campo NUEVO, distinto de `run_mode` existente que controla el modo terminal. Los agentes Hermes quedan con `runtime_type = NULL` (default `"hermes"`).

### 2.2 PermissionPolicy — nueva tabla

```python
class PermissionPolicy(Base):
    __tablename__ = "permission_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Tools permitidas/bloqueadas (glob patterns)
    # {"allow": ["read", "bash", "edit"], "deny": ["bash:rm -rf *", "write:**/.env"]}
    tool_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    # Paths (glob patterns sobre el filesystem del workspace)
    # {"allow_paths": ["/workspace/**"], "deny_paths": ["/etc/**", "**/.env", "**/.ssh/**"]}
    path_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    # Comandos shell (sobre tool "bash")
    # {"allow": ["git *", "ls", "npm *"], "deny": ["rm -rf /", "sudo *", "curl * | sh"]}
    command_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    # Red
    # {"allow_domains": ["*.openai.com", "graph.microsoft.com"], "deny_all": false}
    network_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    # Aprobación humana requerida
    # {"require_approval_for": ["bash:sudo *", "write:/system/**"], "auto_approve_threshold": "low|medium|high|none"}
    approval_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

### 2.3 System policies (pre-cargadas en migración)

| Policy | Tools | Paths | Comandos | Approval |
|--------|-------|-------|----------|----------|
| **Pi Developer** | allow: read, bash, edit, write, grep, find, ls | allow: /workspace/**, deny: **/.env, **/node_modules | deny: rm -rf /, sudo * | require: bash:sudo *, bash:rm *; auto: medium |
| **Pi Read-Only** | allow: read, grep, find, ls; deny: * | allow: /workspace/** | deny: * | require: *; auto: none |
| **Pi Full Access** | allow: * | allow: ** | deny: (ninguno) | require: (nada); auto: high |
| **Pi Sandboxed** | allow: read, bash, edit; deny: write:**/.env | allow: /workspace/**, deny: /etc/**, /root/** | deny: sudo *, chmod 777 *, curl * \| sh | require: bash:sudo *; auto: low |

---

## 3. Arquitectura

### 3.1 Runtime Registry en AgentSupervisor

Hoy: `self.runtime = HermesRuntime(...)`  
Cambio: registry por `runtime_type`

```python
class AgentSupervisor:
    def __init__(self, ...):
        self.runtimes: dict[str, RuntimeBase] = {}

    def register_runtime(self, runtime_type: str, runtime: RuntimeBase) -> None:
        self.runtimes[runtime_type] = runtime

    # En _run_task (línea 423):
    runtime = self.runtimes.get(agent.runtime_type or "hermes")
    execution = await runtime.execute(agent, task, stream_callback, ...)
```

### 3.2 Interface `RuntimeBase`

```python
@dataclass
class RuntimeExecutionResult:
    final_response: str
    messages: list[dict]
    tool_calls: list[dict]
    tokens_used: int
    iterations: int
    engine: str
    response_attachments: list[dict]

class RuntimeBase(ABC):
    @abstractmethod
    async def execute(
        self,
        agent: Agent,
        task: Task,
        stream_callback: Callable[[str], Awaitable[None]],
        conversation_history: list[dict],
        session_id: str | None,
    ) -> RuntimeExecutionResult: ...
```

`HermesRuntime` ya cumple este contract. `PiRuntime` lo implementa nuevo.

### 3.3 PiRuntime

```python
class PiRuntime(RuntimeBase):
    """
    Ejecuta agentes Pi como subprocess Node.js en modo RPC.
    
    Flujo:
    1. spawn `node pi-runner.mjs --mode rpc` con env del agente
    2. RPC handshake: send {method: "init", config: pi_config}
    3. Para cada task: send {method: "prompt", text: prompt}
    4. Recibir eventos JSON-RPC: text_delta, tool_call, tool_result, done
    5. Stream text_delta → stream_callback
    6. On done → construir RuntimeExecutionResult
    7. On task cancel → send {method: "abort"}
    """
    
    def __init__(self, session_factory, event_broker, workspace_manager):
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.workspace_manager = workspace_manager
        self.active_processes: dict[str, subprocess.Popen] = {}  # agent_id -> process
        self.rpc_clients: dict[str, PiRpcClient] = {}
    
    async def execute(self, agent, task, stream_callback, conversation_history, session_id):
        workspace = self.workspace_manager.build_workspace_path(agent.id)
        pi_home = workspace / ".pi"
        
        # 1. Ensure Pi installation is synced
        installation = PiInstallationManager(...)
        await installation.sync_agent_installation(agent)
        
        # 2. Spawn Node.js process
        env = installation.build_process_env(agent)
        process = subprocess.Popen(
            ["node", str(PI_RUNNER_SCRIPT), "--mode", "rpc"],
            cwd=str(workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.active_processes[agent.id] = process
        
        # 3. RPC communication
        client = PiRpcClient(process.stdin, process.stdout)
        self.rpc_clients[agent.id] = client
        
        await client.init({
            "tools": agent.pi_config.get("tools", ["read", "bash", "edit"]),
            "thinking_level": agent.pi_config.get("thinking_level", "medium"),
            "system_prompt": installation.compose_system_prompt(agent),
            "extensions": ["hermeshq-security", "hermeshq-integrations"],
        })
        
        # 4. Send prompt
        prompt = task.prompt
        async for event in client.prompt(prompt):
            if event["type"] == "text_delta":
                await stream_callback(event["delta"])
            elif event["type"] == "tool_call":
                await self.event_broker.publish({
                    "type": "tool.progress", "agent_id": agent.id, 
                    "task_id": task.id, "tool": event["tool"], "input": event["input"]
                })
            elif event["type"] == "done":
                return RuntimeExecutionResult(
                    final_response=event["response"],
                    messages=event["messages"],
                    tool_calls=event["tool_calls"],
                    tokens_used=event.get("tokens", 0),
                    iterations=event.get("turns", 1),
                    engine="pi",
                    response_attachments=event.get("attachments", []),
                )
```

#### `pi-runner.mjs`

Bridge Node.js que HermesHQ spawnea. Usa el SDK de Pi:

```javascript
import { createAgentSession, SessionManager, ModelRuntime } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "fs";

// RPC over stdin/stdout
const rl = readline.createInterface({ input: process.stdin });

rl.on("line", async (line) => {
  const msg = JSON.parse(line);
  
  if (msg.method === "init") {
    // Create session with config from HermesHQ
    const modelRuntime = await ModelRuntime.create();
    const { session } = await createAgentSession({
      model: resolveModel(msg.config, modelRuntime),
      tools: msg.config.tools,
      thinkingLevel: msg.config.thinking_level || "medium",
      sessionManager: SessionManager.inMemory(),
      cwd: process.cwd(),
    });
    
    // Subscribe to events → emit to stdout
    session.subscribe((event) => {
      if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
        process.stdout.write(JSON.stringify({type: "text_delta", delta: event.assistantMessageEvent.delta}) + "\n");
      }
      if (event.type === "tool_execution_start") {
        process.stdout.write(JSON.stringify({type: "tool_call", tool: event.toolName, input: event.args}) + "\n");
      }
      if (event.type === "agent_settled") {
        process.stdout.write(JSON.stringify({
          type: "done",
          response: extractResponse(session.messages),
          messages: session.messages,
          tool_calls: extractToolCalls(session.messages),
        }) + "\n");
      }
    });
    
    currentSession = session;
  }
  
  if (msg.method === "prompt") {
    await currentSession.prompt(msg.text);
  }
  
  if (msg.method === "abort") {
    await currentSession.abort();
  }
});
```

### 3.4 PiInstallationManager

Análogo a `HermesInstallationManager` pero para Pi:

```python
class PiInstallationManager:
    
    async def sync_agent_installation(self, agent: Agent) -> None:
        workspace = workspace_manager.build_workspace_path(agent.id)
        pi_home = workspace / ".pi"
        
        # 1. Create .pi/ directory structure
        pi_home.mkdir(parents=True, exist_ok=True)
        (pi_home / "extensions").mkdir(exist_ok=True)
        (pi_home / "skills").mkdir(exist_ok=True)
        (pi_home / "prompts").mkdir(exist_ok=True)
        
        # 2. Write settings.json
        self._write_settings(agent, pi_home)
        
        # 3. Generate permission extension
        self._write_security_extension(agent, pi_home)
        
        # 4. Generate integration extension (MCP bridge)
        self._write_integration_extension(agent, pi_home)
        
        # 5. Ensure npm packages installed
        self._ensure_npm_packages(pi_home)
    
    def _write_settings(self, agent, pi_home):
        config = agent.pi_config or {}
        settings = {
            "compaction": config.get("compaction", {"enabled": True, "threshold_tokens": 100000}),
            "defaultProjectTrust": config.get("project_trust", "always"),
            "retry": {"enabled": True, "maxRetries": 3},
        }
        (pi_home / "settings.json").write_text(json.dumps(settings, indent=2))
    
    def _write_security_extension(self, agent, pi_home):
        """Generate .pi/extensions/hermeshq-security.ts from PermissionPolicy."""
        policy = agent.permission_policy  # loaded from DB
        template = SECURITY_EXTENSION_TEMPLATE
        rendered = render_template(template, {
            "ALLOWED_TOOLS": json.dumps(policy.tool_rules.get("allow", ["*"])),
            "DENIED_COMMANDS": json.dumps(policy.command_rules.get("deny", [])),
            "PROTECTED_PATHS": json.dumps(policy.path_rules.get("deny_paths", [])),
            "REQUIRE_APPROVAL": json.dumps(policy.approval_rules.get("require_approval_for", [])),
        })
        (pi_home / "extensions" / "hermeshq-security.ts").write_text(rendered)
    
    def _write_integration_extension(self, agent, pi_home):
        """Generate .pi/extensions/hermeshq-integrations.ts (MCP bridge)."""
        # This extension exposes HermesHQ integrations as Pi custom tools
        # by calling the internal control API
        integrations = agent.integration_configs or {}
        rendered = render_template(INTEGRATION_EXTENSION_TEMPLATE, {
            "HERMESHQ_API_URL": "${HERMESHQ_INTERNAL_API_URL}",
            "AGENT_TOKEN": "${HERMESHQ_AGENT_TOKEN}",
            "INTEGRATIONS": json.dumps(integrations),
        })
        (pi_home / "extensions" / "hermeshq-integrations.ts").write_text(rendered)
    
    def build_process_env(self, agent: Agent) -> dict[str, str]:
        env = build_safe_env()
        env["HERMESHQ_AGENT_ID"] = agent.id
        env["HERMESHQ_AGENT_TOKEN"] = create_agent_service_token(agent.id)
        env["HERMESHQ_INTERNAL_API_URL"] = get_settings().internal_api_base_url
        
        # Provider credentials (same resolution as Hermes)
        api_key = resolve_agent_api_key(agent)
        env["ANTHROPIC_API_KEY"] = api_key or ""
        if agent.base_url:
            env["OPENAI_BASE_URL"] = agent.base_url
        
        return env
```

### 3.5 Permission Extension Template

```typescript
// .pi/extensions/hermeshq-security.ts
// GENERATED BY HERMESHQ — DO NOT EDIT
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const ALLOWED_TOOLS = /* {{ALLOWED_TOOLS}} */ ["*"];
  const DENIED_COMMANDS = /* {{DENIED_COMMANDS}} */ [];
  const PROTECTED_PATHS = /* {{PROTECTED_PATHS}} */ [];
  const REQUIRE_APPROVAL = /* {{REQUIRE_APPROVAL}} */ [];

  pi.on("tool_call", async (event, ctx) => {
    // Tool allowlist
    if (!ALLOWED_TOOLS.includes("*") && !ALLOWED_TOOLS.includes(event.toolName)) {
      return { block: true, reason: `Tool '${event.toolName}' not in allowlist` };
    }

    // Command patterns (bash tool)
    if (event.toolName === "bash") {
      const cmd = (event.input as { command?: string }).command ?? "";
      for (const pattern of DENIED_COMMANDS) {
        if (globMatch(cmd, pattern)) {
          return { block: true, reason: `Command denied by policy: ${pattern}`, terminate: false };
        }
      }
      for (const pattern of REQUIRE_APPROVAL) {
        if (globMatch(cmd, pattern)) {
          const ok = await ctx.ui.confirm("Approval required", `Allow: ${cmd}?`);
          if (!ok) return { block: true, reason: "Approval denied by user" };
        }
      }
    }

    // Path protection (read/write/edit)
    if (["read", "write", "edit"].includes(event.toolName)) {
      const path = (event.input as { path?: string }).path ?? "";
      for (const pattern of PROTECTED_PATHS) {
        if (globMatch(path, pattern)) {
          return { block: true, reason: `Path protected by policy: ${pattern}` };
        }
      }
    }
  });
}

function globMatch(text: string, pattern: string): boolean {
  const re = new RegExp(
    "^" + pattern
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*\*/g, ".*")
      .replace(/\*/g, "[^/]*")
      .replace(/\?/g, ".") + "$"
  );
  return re.test(text);
}
```

### 3.6 Integration Extension Template

```typescript
// .pi/extensions/hermeshq-integrations.ts
// GENERATED BY HERMESHQ — Exposes platform integrations as Pi tools
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const API_URL = process.env.HERMESHQ_INTERNAL_API_URL!;
const AGENT_TOKEN = process.env.HERMESHQ_AGENT_TOKEN!;
const AGENT_ID = process.env.HERMESHQ_AGENT_ID!;
const INTEGRATIONS = /* {{INTEGRATIONS}} */ {};

async function callControl(path: string, method: string, body?: any) {
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-HermesHQ-Agent-ID": AGENT_ID,
      "X-HermesHQ-Agent-Token": AGENT_TOKEN,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res.json();
}

export default function (pi: ExtensionAPI) {
  // Register a tool per integration action
  // Each integration exposes its actions via /control/integrations/{slug}/actions
  
  for (const [slug, config] of Object.entries(INTEGRATIONS)) {
    // Register tools dynamically based on integration manifest
    pi.registerTool({
      name: `integration_${slug}`,
      label: slug,
      description: `Call ${slug} integration`,
      parameters: Type.Object({
        action: Type.String({ description: "Action name" }),
        args: Type.Record(Type.String(), Type.Any(), { description: "Action arguments" }),
      }),
      async execute(_id, params) {
        const result = await callControl(
          `/control/integrations/${slug}/actions/${params.action}`,
          "POST",
          params.args
        );
        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }], details: {} };
      },
    });
  }

  // Also register HQ control tools (same as hermeshq_control plugin for Hermes)
  pi.registerTool({
    name: "hq_list_agents",
    description: "List agents visible to this HermesHQ instance",
    parameters: Type.Object({}),
    async execute() {
      const data = await callControl("/control/agents", "GET");
      return { content: [{ type: "text", text: JSON.stringify(data) }], details: {} };
    },
  });

  // ... other control tools (create_agent, send_message, etc.)
  // These mirror the hermeshq_control plugin tools
}
```

---

## 4. Gateway para Pi Agents

Los canales nativos (Telegram, WhatsApp, Teams, SixAgentic) hoy spawnean el binario de Hermes en modo gateway. Para Pi agents hay dos opciones:

### Opción A (recomendada): Gateway unificado HQ-side

El gateway se queda en Hermes (es Python, ya funciona). Cuando un mensaje llega para un agente Pi:

1. El gateway Hermes recibe el mensaje (como hoy)
2. Lo enruta a HQ via `POST /internal/control/submit-task`
3. HQ crea un `Task` y lo despacha al `PiRuntime`
4. Cuando Pi responde, HQ envía la respuesta de vuelta al gateway via callback HTTP

**Ventajas:**
- Cero cambios en `gateway_process_manager.py`
- Un solo proceso gateway por agente (no importa el runtime_type)
- Reutiliza toda la infraestructura de channels existente

**Desventaja:**
- Cada agente Pi necesita un "mini-gateway" Hermes (ligero, sin LLM, solo message routing)

**Implementación:** El gateway existente ya llama a `HERMESHQ_INTERNAL_API_URL` cuando recibe un mensaje. Solo hay que modificar el `hermeshq_comms` plugin para que haga `POST /internal/control/submit-task` en vez de ejecutar el loop de Hermes directamente. El supervisor ya sabe despachar por `runtime_type`.

### Opción B: Pi-native gateway

Pi soporta channels nativamente (telegram, etc.). Se podría spawn un proceso Pi en modo gateway. Mayor trabajo, menor beneficio.

**Recomendación: Opción A.** Menos código, menos superficie de fallo.

---

## 5. Integraciones para Pi Agents

Las integraciones (M365 Mail, Calendar, Teams, SharePoint) hoy son plugins Hermes (Python). Para Pi se exponen via la **Integration Extension** (sección 3.6) que llama a los mismos endpoints HTTP del internal control API.

Flujo:
1. Agente Pi tiene `integration_configs = {"ms365-mail": {"mailbox": "user@corp.com"}}`
2. `PiInstallationManager._write_integration_extension` genera `.pi/extensions/hermeshq-integrations.ts` con una tool por integración habilitada
3. Cuando Pi llama `integration_ms365-mail({action: "send_mail", args: {...}})`, la extensión hace `POST /control/integrations/ms365-mail/actions/send_mail`
4. HQ ejecuta el handler del integration package existente (sin cambios)
5. Response vuelve al agente Pi

**Beneficio:** Los integration packages (Python) no se modifican. Solo se re-exponen via HTTP.

---

## 6. API

### 6.1 Permission Policies CRUD

```
GET    /api/permission-policies               → list
POST   /api/permission-policies               → create (admin)
GET    /api/permission-policies/{id}          → detail
PUT    /api/permission-policies/{id}          → update (admin)
DELETE /api/permission-policies/{id}          → delete (admin, not system)
```

### 6.2 Agent — Pi fields

```
# En AgentCreate / AgentUpdate (nuevos campos opcionales):
runtime_type: "pi"               # default omite el campo (hermes)
pi_config: { tools, thinking_level, ... }
permission_policy_id: "uuid"

# En AgentRead (nuevos campos):
runtime_type: "hermes" | "pi"
pi_config: { ... } | null
permission_policy: { id, name } | null
```

### 6.3 HQ Operator — Pi control tools

Nuevas tools en `hermeshq_control` plugin:

```python
hq_control_list_policies        # Listar permission policies
hq_control_create_policy        # Crear policy
hq_control_assign_policy        # Asignar policy a agente Pi
hq_control_test_permission      # Dry-run: ¿tool/comando permitido?
```

`hq_control_create_agent` ya existe — se le agrega soporte para `runtime_type: "pi"`.

### 6.4 Test Permission

```
POST /api/agents/{id}/test-permission
Body: { "tool": "bash", "input": { "command": "rm -rf /tmp" } }
Response: { "allowed": false, "reason": "Command denied by policy: rm -rf *", "policy": "Pi Developer" }
```

---

## 7. Frontend (V2)

### 7.1 Agent Create — runtime selector

En el formulario de crear agente, un toggle: **Hermes** | **Pi**

Si Pi:
- Tools selector (checkboxes: read, bash, edit, write, grep, find, ls)
- Thinking level (dropdown)
- Permission policy (dropdown)
- Provider/Model (igual que Hermes)

### 7.2 Agent Detail — cuando `runtime_type === "pi"`

- **Config tab**: pi_config editable (tools, thinking level, compaction, project trust)
- **Permission tab**: policy asignada + editor de reglas + test panel
- **Terminal tab**: funciona igual (PTYManager es agnóstico)
- **Tasks/Channels/Comms**: funcionan igual

### 7.3 Settings → Permission Policies

- Lista de políticas (tabla)
- Editor (tool rules, path rules, command rules, network rules, approval rules)
- Solo aplica a agentes Pi

---

## 8. Migración

```python
def upgrade():
    # 1. permission_policies table
    op.create_table("permission_policies", ...)

    # 2. Agent columns
    op.add_column("agents", sa.Column("runtime_type", sa.String(16), server_default="hermes"))
    op.add_column("agents", sa.Column("pi_config", sa.JSON, nullable=True))
    op.add_column("agents", sa.Column("permission_policy_id", sa.String(36),
                  sa.ForeignKey("permission_policies.id", ondelete="SET NULL"), nullable=True))

    # 3. Seed system policies
    for policy in SYSTEM_POLICIES: ...
```

---

## 9. Dockerfile

```dockerfile
# Add Node.js 22 for Pi runtime
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
    npm install -g @earendil-works/pi-coding-agent@latest && \
    rm -rf /var/lib/apt/lists/*
```

Startup check en `main.py`:
```python
pi_available = shutil.which("pi") is not None
if pi_available:
    app.state.pi_runtime = PiRuntime(AsyncSessionLocal, event_broker, workspace_manager)
    app.state.supervisor.register_runtime("pi", app.state.pi_runtime)
    logger.info("Pi runtime available")
else:
    logger.warning("Pi runtime not installed — agents with runtime_type='pi' will fail")
```

---

## 10. Plan de Implementación

### Fase 1: Permission System — 2 días
- Tabla `PermissionPolicy` + migración + seed
- API CRUD
- `PermissionEnforcer` service (evalúa reglas)
- Tests de enforcer

### Fase 2: PiRuntime Core — 3 días
- `RuntimeBase` interface
- Refactorizar `HermesRuntime` para implementar `RuntimeBase` (sin cambiar comportamiento)
- `PiRuntime` + `PiRpcClient` + `pi-runner.mjs`
- Runtime registry en `AgentSupervisor`
- `PiInstallationManager` (settings.json + extensions)
- Tests de task execution

### Fase 3: Permission Extension + Integration Extension — 2 días
- Template generator para `hermeshq-security.ts`
- Template generator para `hermeshq-integrations.ts`
- Env vars (`HERMESHQ_AGENT_TOKEN`, etc.)
- Test E2E: crear agente Pi, asignar policy, enviar task, verificar enforcement

### Fase 4: Frontend — 2 días
- Agent Create: runtime selector + Pi config
- Agent Detail: Pi tab + Permission tab
- Settings: Permission Policies CRUD
- Test permission panel

### Fase 5: HQ Operator + Builder — 1 día
- Nuevas control tools (list/create/assign policies, create Pi agents)
- Builder: `list_runtimes` tool + `run_mode` en draft
- `finalize_agent_from_draft` soporta `runtime_type: "pi"`

### Fase 6: Dockerfile + Hardening — 1 día
- Node.js + Pi en Dockerfile
- Startup check
- Logs/metrics unificadas
- Documentación

**Total: ~11 días**

---

## 11. Lo que NO cambia

| Componente | Estado |
|-----------|--------|
| Agentes Hermes existentes | **Intactos** — runtime_type default "hermes", sin policy |
| HermesInstallationManager | **Intacto** — solo se llama para agentes hermes |
| Toolsets / STANDARD_ENABLED_TOOLSETS | **Intacto** — sistema de Hermes, no aplica a Pi |
| `hermes_runtime.py` | **Refactor menor** — extraer interface `RuntimeBase`, sin cambios de comportamiento |
| EventBroker | **Intacto** |
| Scheduler | **Intacto** |
| Comms | **Intacto** |
| Gateway (process_manager) | **Intacto** (Opción A — gateway unificado) |
| Integration packages (Python) | **Intactos** — se re-exponen via HTTP para Pi |
| PTYManager | **Intacto** |
| Mobile app | **Intacto** |
| WorkspaceManager | **Intacto** (file I/O genérico) |

---

## 12. Criterios de Aceptación

- [ ] Admin puede crear/editar permission policies desde V2
- [ ] Agente Pi arranca, recibe tasks y responde
- [ ] HQ Operator puede crear agente Pi via tool
- [ ] HQ Operator puede asignar policy via tool
- [ ] Policy "Pi Developer" aplicada a agente Pi bloquea `rm -rf /`
- [ ] Policy "Pi Read-Only" aplicada bloquea tool `write` y `edit`
- [ ] `test-permission` endpoint predice correctamente
- [ ] Integraciones M365 funcionan para agente Pi via integration extension
- [ ] Terminal funciona para agente Pi
- [ ] Scheduler crea tasks que se despachan a PiRuntime
- [ ] Agentes Hermes existentes funcionan sin cambios
- [ ] Fresh install (install.sh) funciona con Pi opcional
