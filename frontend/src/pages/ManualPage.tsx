import { useI18n } from "../lib/i18n";

type ManualSection = {
  id: string;
  eyebrow: string;
  title: string;
  summary: string;
  audience?: string;
  image?: {
    src: string;
    alt: string;
    caption: string;
  };
  bullets: string[];
};

type ManualContent = {
  sidebarLabel: string;
  heroTitle: string;
  heroSummary: string;
  quickstartLabel: string;
  quickstartSteps: Array<{ label: string; body: string }>;
  adminBadge: string;
  sections: ManualSection[];
};

const manualContent: Record<"en" | "es", ManualContent> = {
  es: {
    sidebarLabel: "Manual de usuario",
    heroTitle: "Operar HermesHQ de punta a punta",
    heroSummary:
      "Esta guía explica el flujo completo de uso de la plataforma, desde la navegación general hasta la administración de agentes, usuarios, tareas recurrentes y canales externos.",
    quickstartLabel: "Inicio rapido",
    quickstartSteps: [
      {
        label: "1. Entrar",
        body: "Inicia sesión con tu cuenta. Si eres usuario estándar, verás solo los agentes que te asignaron.",
      },
      {
        label: "2. Ubicar el flujo",
        body: "Usa Dashboard para visión general, Agents para operar agentes y Tasks o Schedules para ejecuciones manuales o recurrentes.",
      },
      {
        label: "3. Ejecutar",
        body: "Conversa con el agente, usa la TUI, programa tareas o vincula canales externos como Telegram según el caso.",
      },
    ],
    adminBadge: "Admin",
    sections: [
      {
        id: "overview",
        eyebrow: "Orientacion",
        title: "Que es HermesHQ y como se organiza",
        summary:
          "HermesHQ es un panel operativo para crear, supervisar y conversar con agentes Hermes desde una sola interfaz. La app separa tareas globales de instancia y tareas específicas de cada agente.",
        bullets: [
          "El menú izquierdo es la navegación principal. Desde ahí accedes a Dashboard, Agents, Tasks, Schedules y Comms; si eres admin también verás Users, Nodes y Settings.",
          "HermesHQ no reemplaza a Hermes Agent: lo usa como motor real de ejecución. La diferencia es que aquí cada agente vive además dentro de una capa de control con identidad, workspace propio, canales, trazabilidad y gobierno multiusuario.",
          "El tema visual se resuelve con dos niveles: tema por defecto de la instancia y preferencia personal del usuario.",
          "El idioma sigue la misma lógica: la instancia define un idioma por defecto y cada usuario puede sobrescribirlo entre inglés y español desde My Account o desde la sección Operator del sidebar.",
          "Las secciones visibles dependen de tu rol. Un usuario normal solo ve lo que le fue asignado por un administrador.",
          "Los cambios relevantes en agentes, tareas y canales se reflejan en tiempo real mediante el runtime stream y los paneles de detalle.",
        ],
      },
      {
        id: "install",
        eyebrow: "Despliegue",
        title: "Instalación rápida con una sola línea",
        summary:
          "HermesHQ puede instalarse en un servidor limpio con Docker mediante un instalador remoto estilo curl pipe bash.",
        bullets: [
          "El instalador descarga la rama main desde GitHub, instala la app en `~/hermeshq`, preserva un `.env` existente y genera uno nuevo cuando se trata de una primera instalación.",
          "El comando base es `curl -fsSL https://raw.githubusercontent.com/jpalmae/hermeshq/main/install.sh | bash`.",
          "Si el servidor tiene varias interfaces o una IP fija conocida, conviene invocarlo con `HERMESHQ_HOST=<ip-o-dns>` para que el frontend quede apuntando al backend correcto.",
          "La stack Docker queda parametrizada por `.env`, incluyendo puertos, credenciales bootstrap, PostgreSQL, CORS y base URL del frontend.",
          "Después de instalar, puedes entrar al frontend y continuar la configuración desde Settings, Users, Providers y los detalles de cada agente.",
        ],
      },
      {
        id: "dashboard",
        eyebrow: "Pantalla inicial",
        title: "Dashboard operativo",
        summary:
          "El dashboard resume el estado vivo de la flota, la actividad reciente y el mapa de dependencias entre agentes.",
        image: {
          src: "/manual/dashboard.png",
          alt: "Dashboard de HermesHQ",
          caption: "Vista general con Primary Readout, Live Feed, mapa de agentes y actividad reciente.",
        },
        bullets: [
          "Primary Readout muestra el número de agentes activos, métricas rápidas y el operador autenticado.",
          "Live Feed presenta una muestra corta del stream en tiempo real para no saturar la lectura.",
          "Dependency Canvas permite revisar visualmente relaciones entre agentes y navegar hacia Agent Studio.",
          "Current Fleet y Recent Activity sirven para abrir agentes rápidamente y confirmar qué está ocurriendo en la instancia.",
        ],
      },
      {
        id: "agents",
        eyebrow: "Inventario",
        title: "Crear y administrar agentes",
        summary:
          "La sección Agents concentra el inventario completo, el formulario de alta y las acciones operativas básicas sobre cada agente.",
        image: {
          src: "/manual/agents.png",
          alt: "Listado de agentes",
          caption: "Agent matrix con acciones de runtime y acceso al detalle de cada agente.",
        },
        bullets: [
          "Al crear un agente, puedes partir desde un preset de provider mantenido por la instancia. Eso rellena runtime provider, modelo, base URL y secret ref sugerido antes de cualquier ajuste manual.",
          "También puedes elegir un runtime profile. `standard` apunta a agentes administrativos o de integración SaaS, `technical` a operación técnica general y `security` a trabajo de ciberseguridad más profundo.",
          "Friendly name es el nombre visible y recordable. Si name o slug se dejan vacíos, se derivan automáticamente a partir de ese nombre.",
          "Cada agente tiene workspace, instalación Hermes propia, skills y canales asociados dentro de su directorio aislado.",
          "Desde el listado puedes iniciar, detener, reiniciar y eliminar agentes. El detalle completo se abre haciendo clic sobre la tarjeta.",
        ],
      },
      {
        id: "agent-detail",
        eyebrow: "Operacion profunda",
        title: "Detalle del agente",
        summary:
          "Dentro de cada agente conviven la TUI, la conversación directa, el historial de runtime, skills, logs, workspace y configuración avanzada.",
        image: {
          src: "/manual/agent-detail.png",
          alt: "Detalle del agente",
          caption: "Vista de detalle con Terminal siempre visible y paneles colapsables para el resto de herramientas.",
        },
        bullets: [
          "Terminal muestra la TUI real de Hermes. Puedes mantenerla embebida, flotarla o expandirla a pantalla casi completa.",
          "La disponibilidad de Terminal depende del runtime profile del agente. Un perfil `standard` no expone TUI ni acceso a terminal/procesos; `technical` y `security` sí.",
          "Si la instancia tiene una skin global de TUI configurada por un admin, esa apariencia se aplica a todas las sesiones nuevas del terminal Hermes.",
          "Talk to this agent envía mensajes como tareas operativas y conserva un historial estilo conversación.",
          "Runtime ledger resume ejecuciones, resultados y errores; cuando este agente delega trabajo, el resultado del subordinado vuelve a aparecer aquí como una task callback automática.",
          "Configuration permite editar parámetros como system prompt o avatar cuando haga falta.",
          "Integrations vive en una sección propia. Desde ahí puedes habilitar o deshabilitar integraciones gestionadas por agente, completar sus campos requeridos y probar conectividad sin tocar código.",
          "La misma sección ahora muestra `Capacidades efectivas`, separando claramente qué viene del perfil base del runtime, qué plugins pone HermesHQ por defecto y qué paquetes de integración están habilitados en ese agente.",
          "Hermes skill registry ahora permite eliminar skills instaladas directamente desde el agente. Si la skill es gestionada por HermesHQ, también se desasigna para que no reaparezca en la siguiente sincronización.",
          "Hermes skill registry, Logs y Workspace están pensados para investigación y soporte técnico. Se mantienen colapsados por defecto para no ensuciar la vista.",
        ],
      },
      {
        id: "tasks",
        eyebrow: "Ejecucion",
        title: "Enviar tareas manualmente",
        summary:
          "La página Tasks sirve para disparar trabajos puntuales y revisar su resultado en formato operativo.",
        image: {
          src: "/manual/tasks.png",
          alt: "Pantalla de tareas",
          caption: "Dispatch manual de tareas con selección de agente y seguimiento posterior.",
        },
        bullets: [
          "Submit Task permite elegir el agente por friendly name, escribir la instrucción y lanzar la ejecución.",
          "La lista histórica ayuda a ver el estado real de cada task: queued, running, completed o failed.",
          "Si un agente está detenido, HermesHQ puede iniciar el runtime según el flujo habilitado antes de ejecutar la tarea.",
          "Usa esta pantalla cuando quieras una ejecución puntual sin entrar al detalle completo del agente.",
        ],
      },
      {
        id: "schedules",
        eyebrow: "Automatizacion",
        title: "Programar tareas recurrentes",
        summary:
          "Schedules centraliza las tareas periódicas para que un agente ejecute acciones en ventanas horarias definidas.",
        image: {
          src: "/manual/schedules.png",
          alt: "Pantalla de schedules",
          caption: "Programación de tareas por agente con frecuencia recurrente y control de estado.",
        },
        bullets: [
          "Usa esta vista cuando necesites una instrucción automática, por ejemplo revisar una fuente, resumir novedades o generar un informe cada cierto intervalo.",
          "Las programaciones se filtran por agente y pueden abrirse también desde el detalle de cualquier agente.",
          "Si el runtime del agente no está corriendo, HermesHQ puede iniciar el agente para despachar la ejecución programada.",
          "Comms y Schedules son conceptos distintos: Comms delega una interacción puntual entre agentes; Schedules define recurrencia real.",
        ],
      },
      {
        id: "comms",
        eyebrow: "Coordinacion",
        title: "Delegacion entre agentes",
        summary:
          "Comms router está diseñado para intercambios one-off entre agentes dentro de la misma instancia.",
        bullets: [
          "Puedes seleccionar origen, destino y mensaje para generar una delegación puntual y trazable.",
          "El sistema registra los eventos y las tareas resultantes, permitiendo seguir la conversación operativa entre agentes.",
          "Las reglas jerárquicas se aplican solo a Delegate: agentes independientes delegan libremente, subordinados pueden escalar hacia supervisores o delegar hacia abajo dentro de su propia rama, y las delegaciones laterales entre ramas quedan bloqueadas.",
          "La propia pantalla de Comms muestra esas rutas válidas e inválidas antes de enviar, incluyendo destinos deshabilitados y una vista visual del alcance del agente origen.",
          "Cuando la tarea hija termina, HermesHQ genera un `delegate_result`: queda visible en Comms history y además crea una task de retorno en el runtime ledger del agente delegador.",
          "Si el agente destino está detenido, HermesHQ puede levantarlo antes de enviar la delegación. Para automatizar una delegación periódica debes usar Schedules, no Comms.",
        ],
      },
      {
        id: "telegram",
        eyebrow: "Canales",
        title: "Telegram persistente por agente",
        summary:
          "HermesHQ ya puede vincular un agente a Telegram usando el gateway nativo de Hermes y mantener esa unión en forma permanente.",
        bullets: [
          "La configuración vive por agente, no a nivel global, de modo que cada uno puede tener su propio bot, allowlist y modo operativo.",
          "Los IDs permitidos determinan quién puede interactuar con el agente desde Telegram. Cualquier usuario fuera de esa lista queda excluido.",
          "El token del bot debe guardarse como secreto y luego asociarse al agente desde su configuración de mensajería.",
          "El estado del canal se supervisa desde HermesHQ, pero el procesamiento real ocurre en la instalación Hermes del agente.",
          "Cada mensaje nuevo que entra o sale por Telegram queda trazado en el `Activity stream` del agente como evento `channel.telegram.inbound` o `channel.telegram.outbound`, lo que permite auditoría sin mezclarlo con el runtime ledger.",
          "Si una delegación nace desde Telegram, HermesHQ conserva el contexto del chat de origen. Cuando el agente subordinado termina, el resultado puede volver automáticamente a ese mismo chat de Telegram.",
          "No conectes el mismo bot a dos instancias activas de HermesHQ al mismo tiempo. Telegram solo permite un polling activo por token y, si hay conflicto, se rompe tanto la entrega de mensajes como la trazabilidad.",
        ],
      },
      {
        id: "users",
        eyebrow: "Administracion",
        title: "Usuarios, roles y asignaciones",
        summary:
          "Los administradores gestionan usuarios, roles y asignaciones de agentes desde la pantalla Users.",
        audience: "Solo administradores",
        image: {
          src: "/manual/users.png",
          alt: "Pantalla de usuarios",
          caption: "Gestión de cuentas, roles, avatares, contraseñas y asignaciones de agentes.",
        },
        bullets: [
          "Existen dos roles: admin y user. Los admins controlan la instancia; los users solo operan los agentes asignados.",
          "La política de contraseña exige mínimo ocho caracteres, al menos una mayúscula, un número y un carácter especial.",
          "Desde aquí puedes crear usuarios, cambiar display name, subir icono del operador, resetear contraseña y eliminar cuentas.",
          "Las asignaciones determinan qué agentes puede ver y manipular cada usuario estándar en todo el sistema.",
        ],
      },
      {
        id: "account",
        eyebrow: "Perfil personal",
        title: "My Account para cualquier usuario",
        summary:
          "Además del registro administrativo de usuarios, cada operador dispone de una página personal para gestionar su propia identidad y seguridad.",
        bullets: [
          "My Account está disponible desde la sección Operator del sidebar, sin necesidad de privilegios de administrador.",
          "Manual también vive dentro de la sección Operator, de modo que la ayuda quede cerca del perfil del usuario y no mezclada con el menú operativo principal.",
          "Desde ahí puedes cambiar display name, icono/avatar, preferencia de tema personal y preferencia personal de idioma.",
          "También puedes cambiar tu propia contraseña validando primero la contraseña actual.",
          "La política de contraseña sigue siendo la misma en todo el sistema: mínimo ocho caracteres, una mayúscula, un número y un carácter especial.",
        ],
      },
      {
        id: "settings",
        eyebrow: "Instancia",
        title: "Settings globales, branding y defaults",
        summary:
          "La página Settings reúne la configuración global de la instancia: branding, defaults de runtime, tema por defecto y otros parámetros sensibles.",
        audience: "Solo administradores",
        image: {
          src: "/manual/settings.png",
          alt: "Pantalla de settings",
          caption: "Branding global, tema por defecto, defaults de runtime y activos visuales de la instancia.",
        },
        bullets: [
          "Branding permite definir nombre de la app, short name, logo y favicon persistentes.",
          "Default Theme establece el modo base de la instancia; cada usuario puede luego aplicar su override personal desde el shell.",
          "Default Language define el idioma base de la interfaz. Cada usuario puede mantener ese valor o elegir su propio override entre inglés y español.",
          "TUI Skin permite subir un archivo YAML de skin de Hermes y usarlo como apariencia global para todas las TUI de agentes.",
          "Runtime defaults controla provider, modelo, base URL y secreto por defecto para los nuevos agentes, ahora apoyándose en presets del catálogo de providers.",
          "Provider registry permite mantener los providers soportados por la instancia, editando nombre, URL base, modelo por defecto y estado habilitado sin necesidad de tocar código.",
          "Managed integrations también vive en Settings. Desde ahí puedes subir paquetes `.tar.gz`, instalarlos o desinstalarlos globalmente y revisar qué tools, campos y perfiles soporta cada integración antes de habilitarla por agente.",
          "Settings también muestra `Capacidades base del runtime`, con los toolsets built-in por perfil y los plugins de plataforma HermesHQ incluidos por defecto, para distinguirlos de las integraciones que sí se instalan como paquete.",
          "Si usas Kimi Coding, el preset correcto queda apuntando a `https://api.kimi.com/coding/v1`.",
          "Los usuarios sin privilegios no pueden modificar estos parámetros globales, pero sí elegir su tema e idioma personales.",
        ],
      },
      {
        id: "integration-setup",
        eyebrow: "Integraciones",
        title: "Cómo configurar integraciones gestionadas",
        summary:
          "Las integraciones gestionadas se instalan primero a nivel de instancia desde Settings y luego se habilitan por agente desde Agent Detail. El flujo actual ya cubre instalación, desinstalación, campos declarativos y prueba de conexión.",
        bullets: [
          "Flujo general: en `Settings -> Integrations` instala la integración; luego en `Agent -> Integrations` completa los campos, selecciona los secretos requeridos y usa `Test connection` antes de habilitarla para producción.",
          "Las integraciones bundled quedan siempre visibles en Settings aunque estén desinstaladas. Desinstalar una integración la deshabilita para la instancia, pero no la borra del catálogo porque forma parte del código base.",
          "Microsoft 365 Mail y Microsoft 365 Calendar usan Microsoft Graph con `client_credentials`. Debes crear una app registration en Entra ID, obtener `tenant_id` y `client_id`, crear un client secret, guardarlo en `Settings -> Secrets` y completar también el `mailbox` del buzón que quieres validar.",
          "Para SharePoint se usa el mismo patrón de Microsoft Graph. Debes completar `tenant_id`, `client_id`, `client_secret_ref` y `site_url`. El `site_url` debe ser una URL real del sitio de SharePoint y el test valida acceso a ese sitio vía Graph.",
          "Microsoft requiere permisos de aplicación y admin consent. Para mail normalmente necesitas `Mail.Read`; para calendar `Calendars.Read`; para SharePoint `Sites.Read.All` y, según el caso, `Files.Read.All`.",
          "Google Workspace Mail, Google Calendar y Google Drive usan OAuth con refresh token. Debes completar `client_id`, guardar el client secret y el refresh token como secretos, y referenciarlos desde `client_secret_ref` y `refresh_token_ref`.",
          "Google Calendar además acepta `calendar_id`; si no lo completas, el valor típico es `primary`. Google Drive acepta `drive_id` opcional para shared drives, pero el health check base funciona también sin ese campo.",
          "Snyk Agent Scan requiere un secreto con `SNYK_TOKEN`. En esta fase se usa como integración de auditoría manual: `Test connection` prepara el scanner y la acción `Run skill scan` revisa las skills instaladas del agente dejando trazabilidad en `Activity stream`.",
          "Hoy estas integraciones ya soportan configuración y prueba de credenciales, pero todavía no todas exponen tools operativas de negocio. El valor actual es dejar el catálogo listo, tipado y testeable antes de agregar herramientas de uso diario.",
        ],
      },
      {
        id: "tips",
        eyebrow: "Buenas practicas",
        title: "Consejos de uso y soporte",
        summary:
          "Estas recomendaciones ayudan a operar HermesHQ con menos fricción y a diagnosticar problemas más rápido.",
        bullets: [
          "Usa Talk to this agent para conversar con el modelo; usa Terminal cuando necesites la TUI completa o una sesión interactiva de Hermes.",
          "Si después de suspender el laptop la app se ve vacía, vuelve a iniciar sesión. HermesHQ ya corta la sesión expirada en vez de mostrar datos en falso.",
          "Si el PTY muestra caracteres extraños con dos usuarios conectados al mismo agente, recuerda que la TUI compartida no está pensada para multi-control intensivo.",
          "Mantén friendly names claros y describe bien el rol del agente para que la delegación, los schedules y los canales externos sean más comprensibles.",
          "Para respaldo de instancia, el repositorio incluye scripts de backup y restore que guardan PostgreSQL, workspaces, `.env` y el token de `cloudflared` sin depender de procedimientos manuales externos.",
        ],
      },
    ],
  },
  en: {
    sidebarLabel: "User manual",
    heroTitle: "Operate HermesHQ end to end",
    heroSummary:
      "This guide explains the full platform workflow, from general navigation to agent administration, users, recurring tasks, and external channels.",
    quickstartLabel: "Quick start",
    quickstartSteps: [
      {
        label: "1. Sign in",
        body: "Log in with your account. Standard users only see the agents assigned to them.",
      },
      {
        label: "2. Find the workflow",
        body: "Use Dashboard for a global view, Agents to operate agents, and Tasks or Schedules for manual or recurring execution.",
      },
      {
        label: "3. Execute",
        body: "Talk to the agent, use the TUI, schedule tasks, or connect external channels such as Telegram depending on the job.",
      },
    ],
    adminBadge: "Admin",
    sections: [
      {
        id: "overview",
        eyebrow: "Orientation",
        title: "What HermesHQ is and how it is organized",
        summary:
          "HermesHQ is an operations panel for creating, supervising, and talking to Hermes agents from a single interface. The app separates instance-wide work from agent-specific work.",
        bullets: [
          "The left sidebar is the main navigation. From there you access Dashboard, Agents, Tasks, Schedules, and Comms; admins also see Users, Nodes, and Settings.",
          "HermesHQ does not replace Hermes Agent: it uses it as the real execution engine. The difference is that here each agent also lives inside a control layer with identity, its own workspace, channels, traceability, and multi-user governance.",
          "The visual theme is resolved in two layers: instance default theme and personal user preference.",
          "Language follows the same rule: the instance defines a default language and each user can override it between English and Spanish from My Account or the Operator section in the sidebar.",
          "Visible sections depend on your role. A standard user only sees what an administrator assigned.",
          "Relevant changes in agents, tasks, and channels are reflected in real time through the runtime stream and detail panels.",
        ],
      },
      {
        id: "install",
        eyebrow: "Deployment",
        title: "One-line quick installation",
        summary:
          "HermesHQ can be installed on a clean Docker-ready server through a remote installer using a curl-pipe-bash workflow.",
        bullets: [
          "The installer downloads the main branch from GitHub, installs the app into `~/hermeshq`, preserves an existing `.env`, and generates a new one for first-time installs.",
          "The base command is `curl -fsSL https://raw.githubusercontent.com/jpalmae/hermeshq/main/install.sh | bash`.",
          "If the server has multiple interfaces or a known static IP, run it with `HERMESHQ_HOST=<ip-or-dns>` so the frontend points to the correct backend.",
          "The Docker stack is parameterized through `.env`, including ports, bootstrap credentials, PostgreSQL, CORS, and frontend base URL.",
          "After installation, open the frontend and continue setup from Settings, Users, Providers, and each agent detail page.",
        ],
      },
      {
        id: "dashboard",
        eyebrow: "Home screen",
        title: "Operational dashboard",
        summary:
          "The dashboard summarizes live fleet state, recent activity, and dependency relationships between agents.",
        image: {
          src: "/manual/dashboard.png",
          alt: "HermesHQ dashboard",
          caption: "Overview with Primary Readout, Live Feed, agent map, and recent activity.",
        },
        bullets: [
          "Primary Readout shows the number of active agents, quick metrics, and the authenticated operator.",
          "Live Feed presents a short sample of the real-time stream to avoid visual overload.",
          "Dependency Canvas gives a visual view of agent relationships and provides navigation into Agent Studio.",
          "Current Fleet and Recent Activity help you open agents quickly and confirm what is happening in the instance.",
        ],
      },
      {
        id: "agents",
        eyebrow: "Inventory",
        title: "Create and manage agents",
        summary:
          "The Agents section concentrates the full inventory, the creation form, and the main operational actions for each agent.",
        image: {
          src: "/manual/agents.png",
          alt: "Agents list",
          caption: "Agent matrix with runtime actions and access to each agent detail page.",
        },
        bullets: [
          "When creating an agent, you can start from a provider preset maintained by the instance. That pre-fills runtime provider, model, base URL, and suggested secret ref before manual changes.",
          "You can also choose a runtime profile. `standard` targets administrative or SaaS-style agents, `technical` targets general technical operations, and `security` targets deeper cybersecurity work.",
          "Friendly name is the visible, easy-to-remember name. If name or slug are left blank, they are derived automatically from it.",
          "Each agent has its own workspace, Hermes installation, skills, and channels inside an isolated directory.",
          "From the list you can start, stop, restart, and delete agents. Open the full detail view by clicking the card.",
        ],
      },
      {
        id: "agent-detail",
        eyebrow: "Deep operation",
        title: "Agent detail view",
        summary:
          "Inside each agent you have the TUI, direct conversation, runtime history, skills, logs, workspace, and advanced configuration.",
        image: {
          src: "/manual/agent-detail.png",
          alt: "Agent detail",
          caption: "Detail view with Terminal always visible and collapsible panels for the rest of the tools.",
        },
        bullets: [
          "Terminal shows the real Hermes TUI. You can keep it embedded, float it, or expand it to almost full screen.",
          "Terminal availability depends on the agent runtime profile. A `standard` profile does not expose TUI or terminal/process access; `technical` and `security` do.",
          "If the instance has a global TUI skin configured by an admin, that appearance is applied to all new Hermes terminal sessions.",
          "Talk to this agent sends messages as operational tasks and keeps a conversation-like history.",
          "Runtime ledger summarizes executions, results, and errors; when this agent delegates work, the subordinate result comes back here as an automatic callback task.",
          "Configuration lets you edit settings such as system prompt or avatar when needed.",
          "Integrations has its own section. From there you can enable or disable managed integrations per agent, complete required fields, and test connectivity without touching code.",
          "That same section now exposes `Effective capabilities`, clearly separating what comes from the base runtime profile, which plugins HermesHQ injects by default, and which integration packages are enabled on that specific agent.",
          "Hermes skill registry now lets you delete installed skills directly from the agent. If the skill is HermesHQ-managed, it is also unassigned so it does not come back on the next sync.",
          "Hermes skill registry, Logs, and Workspace are intended for investigation and technical support. They stay collapsed by default to keep the view clean.",
        ],
      },
      {
        id: "tasks",
        eyebrow: "Execution",
        title: "Send tasks manually",
        summary:
          "The Tasks page is for launching one-off jobs and reviewing the result in an operational format.",
        image: {
          src: "/manual/tasks.png",
          alt: "Tasks screen",
          caption: "Manual task dispatch with agent selection and follow-up.",
        },
        bullets: [
          "Submit Task lets you choose the agent by friendly name, write the instruction, and launch execution.",
          "The historical list helps you read the real state of each task: queued, running, completed, or failed.",
          "If an agent is stopped, HermesHQ can start the runtime first depending on the enabled flow.",
          "Use this page when you need a one-off execution without opening the full agent detail view.",
        ],
      },
      {
        id: "schedules",
        eyebrow: "Automation",
        title: "Schedule recurring tasks",
        summary:
          "Schedules centralizes recurring tasks so an agent can execute actions in defined time windows.",
        image: {
          src: "/manual/schedules.png",
          alt: "Schedules screen",
          caption: "Per-agent scheduling with recurring frequency and status control.",
        },
        bullets: [
          "Use this view when you need automatic instructions such as checking a source, summarizing updates, or generating a report every interval.",
          "Schedules can be filtered by agent and also opened from any agent detail page.",
          "If the agent runtime is not running, HermesHQ can start it to dispatch the scheduled execution.",
          "Comms and Schedules are different concepts: Comms is for one-off interaction between agents; Schedules is real recurrence.",
        ],
      },
      {
        id: "comms",
        eyebrow: "Coordination",
        title: "Agent-to-agent delegation",
        summary:
          "Comms router is designed for one-off exchanges between agents inside the same instance.",
        bullets: [
          "You can choose source, target, and message to generate a single traceable delegation.",
          "The system records the events and resulting tasks, making the operational conversation between agents visible.",
          "Hierarchy rules apply only to Delegate: independent agents delegate freely, subordinates can escalate to supervisors or delegate downward inside their own branch, and lateral cross-branch delegations are blocked.",
          "The Comms screen shows those valid and invalid paths before sending, including disabled targets and a visual scope for the selected source agent.",
          "When the child task finishes, HermesHQ generates a `delegate_result`: it is visible in Comms history and also creates a return task in the delegating agent runtime ledger.",
          "If the destination agent is stopped, HermesHQ can start it before dispatching the delegation. For periodic delegation, use Schedules instead of Comms.",
        ],
      },
      {
        id: "telegram",
        eyebrow: "Channels",
        title: "Persistent Telegram per agent",
        summary:
          "HermesHQ can bind an agent to Telegram using the native Hermes gateway and keep that binding persistent.",
        bullets: [
          "Configuration lives per agent, not globally, so each agent can have its own bot, allowlist, and operating mode.",
          "Allowed IDs determine who can interact with the agent from Telegram. Anyone outside that list is excluded.",
          "The bot token must be stored as a secret and then attached to the agent from its messaging configuration.",
          "Channel state is supervised by HermesHQ, but the real processing happens inside the agent Hermes installation.",
          "Each new Telegram message in or out is traced in the agent `Activity stream` as a `channel.telegram.inbound` or `channel.telegram.outbound` event, which gives auditability without mixing it into the runtime ledger.",
          "If a delegation starts from Telegram, HermesHQ preserves the origin chat context. When the subordinate agent finishes, the result can return automatically to that same Telegram chat.",
          "Do not connect the same bot to two active HermesHQ instances at the same time. Telegram only allows one active polling consumer per token, and conflicts break both delivery and traceability.",
        ],
      },
      {
        id: "users",
        eyebrow: "Administration",
        title: "Users, roles, and assignments",
        summary:
          "Administrators manage users, roles, and agent assignments from the Users screen.",
        audience: "Admins only",
        image: {
          src: "/manual/users.png",
          alt: "Users screen",
          caption: "Management of accounts, roles, avatars, passwords, and agent assignments.",
        },
        bullets: [
          "There are two roles: admin and user. Admins control the instance; users only operate assigned agents.",
          "The password policy requires at least eight characters, one uppercase letter, one number, and one special character.",
          "From here you can create users, change display name, upload operator icon, reset password, and delete accounts.",
          "Assignments determine which agents a standard user can view and operate across the system.",
        ],
      },
      {
        id: "account",
        eyebrow: "Personal profile",
        title: "My Account for any user",
        summary:
          "In addition to administrative user management, every operator has a personal page for identity and security settings.",
        bullets: [
          "My Account is available from the Operator section in the sidebar, without requiring admin privileges.",
          "Manual also lives in the Operator section, so help stays close to the user profile instead of being mixed with the main operational menu.",
          "From there you can change display name, icon/avatar, personal theme preference, and personal language preference.",
          "You can also change your own password after validating the current password.",
          "The password policy is the same across the system: minimum eight characters, one uppercase letter, one number, and one special character.",
        ],
      },
      {
        id: "settings",
        eyebrow: "Instance",
        title: "Global settings, branding, and defaults",
        summary:
          "The Settings page groups instance-wide configuration: branding, runtime defaults, default theme, and other sensitive parameters.",
        audience: "Admins only",
        image: {
          src: "/manual/settings.png",
          alt: "Settings screen",
          caption: "Global branding, default theme, runtime defaults, and instance visual assets.",
        },
        bullets: [
          "Branding lets you define persistent app name, short name, logo, and favicon.",
          "Default Theme sets the base look for the instance; each user can then apply a personal override from the shell.",
          "Default Language defines the base UI language. Each user can keep it or choose a personal override between English and Spanish.",
          "TUI Skin lets you upload a Hermes YAML skin and use it as the shared appearance for all agent TUIs.",
          "Runtime defaults control provider, model, base URL, and default secret for new agents, now supported by provider catalog presets.",
          "Provider registry lets you maintain supported instance providers, editing name, base URL, default model, and enabled state without changing code.",
          "Managed integrations also lives in Settings. From there you can upload `.tar.gz` packages, install or uninstall them globally, and review which tools, fields, and runtime profiles each integration supports before enabling it on an agent.",
          "Settings also shows `Default runtime capabilities`, listing the built-in toolsets per profile and the HermesHQ platform plugins that ship by default, so they stay clearly separate from package-installed integrations.",
          "If you use Kimi Coding, the correct preset points to `https://api.kimi.com/coding/v1`.",
          "Non-privileged users cannot change these global parameters, but they can still choose their own theme and language.",
        ],
      },
      {
        id: "integration-setup",
        eyebrow: "Integrations",
        title: "How to configure managed integrations",
        summary:
          "Managed integrations are installed first at instance level from Settings and then enabled per agent from Agent Detail. The current flow already covers install, uninstall, declarative fields, and connection testing.",
        bullets: [
          "General flow: install the integration from `Settings -> Integrations`; then go to `Agent -> Integrations`, complete the fields, select the required secrets, and run `Test connection` before using it in production.",
          "Bundled integrations stay visible in Settings even when uninstalled. Uninstalling disables the integration for the instance, but it does not remove it from the catalog because it is part of the base code.",
          "Microsoft 365 Mail and Microsoft 365 Calendar use Microsoft Graph with `client_credentials`. Create an app registration in Entra ID, obtain `tenant_id` and `client_id`, create a client secret, store it in `Settings -> Secrets`, and also provide the `mailbox` you want to validate.",
          "SharePoint uses the same Microsoft Graph pattern. You must provide `tenant_id`, `client_id`, `client_secret_ref`, and `site_url`. The `site_url` must be a real SharePoint site URL and the health check validates that site through Graph.",
          "Microsoft integrations require application permissions and admin consent. For mail you typically need `Mail.Read`; for calendar `Calendars.Read`; for SharePoint `Sites.Read.All` and, depending on the use case, `Files.Read.All`.",
          "Google Workspace Mail, Google Calendar, and Google Drive use OAuth with a refresh token. Provide `client_id`, store the client secret and refresh token as secrets, and reference them through `client_secret_ref` and `refresh_token_ref`.",
          "Google Calendar also accepts `calendar_id`; if you leave it blank, the usual value is `primary`. Google Drive accepts an optional `drive_id` for shared drives, but the base health check also works without it.",
          "Snyk Agent Scan requires a secret containing `SNYK_TOKEN`. In this phase it is used as a manual audit integration: `Test connection` bootstraps the scanner and `Run skill scan` reviews installed agent skills while leaving traceability in the `Activity stream`.",
          "At the moment these integrations already support configuration and credential testing, but not all of them expose day-to-day business tools yet. The current value is having a typed, testable catalog ready before adding operational tools.",
        ],
      },
      {
        id: "tips",
        eyebrow: "Best practices",
        title: "Usage and support tips",
        summary:
          "These recommendations help operate HermesHQ with less friction and diagnose problems faster.",
        bullets: [
          "Use Talk to this agent for model conversation; use Terminal when you need the full TUI or an interactive Hermes session.",
          "If the app looks empty after suspending the laptop, log in again. HermesHQ now expires the session cleanly instead of showing false empty state.",
          "If the PTY shows strange characters with two users connected to the same agent, remember that the shared TUI is not designed for heavy multi-control.",
          "Keep friendly names clear and describe the agent role well so delegation, schedules, and external channels stay understandable.",
          "For instance backup, the repository includes backup and restore scripts covering PostgreSQL, workspaces, `.env`, and the `cloudflared` token without relying on external manual steps.",
        ],
      },
    ],
  },
};

function ManualImage({
  src,
  alt,
  caption,
}: {
  src: string;
  alt: string;
  caption: string;
}) {
  return (
    <figure className="panel-frame overflow-hidden">
      <img src={src} alt={alt} className="block w-full object-cover object-top" />
      <figcaption className="border-t border-[var(--border)] px-5 py-4 text-sm text-[var(--text-secondary)]">
        {caption}
      </figcaption>
    </figure>
  );
}

export function ManualPage() {
  const { locale } = useI18n();
  const content = manualContent[locale] ?? manualContent.en;

  return (
    <div className="space-y-8">
      <section className="grid gap-6 xl:grid-cols-[0.42fr_1fr]">
        <aside className="panel-frame p-6 xl:sticky xl:top-8 xl:self-start">
          <p className="panel-label">{content.sidebarLabel}</p>
          <h1 className="mt-4 max-w-[12ch] text-4xl leading-none text-[var(--text-display)] md:text-5xl">
            {content.heroTitle}
          </h1>
          <p className="mt-4 max-w-[34ch] text-sm leading-6 text-[var(--text-secondary)]">{content.heroSummary}</p>
          <div className="mt-8 space-y-2">
            {content.sections.map((section, index) => (
              <a
                key={section.id}
                href={`#${section.id}`}
                className="flex items-start justify-between gap-3 border-b border-[var(--border)] py-3 text-sm text-[var(--text-primary)]"
              >
                <span className="min-w-0">
                  <span className="panel-label">{String(index + 1).padStart(2, "0")}</span>
                  <span className="mt-1 block">{section.title}</span>
                </span>
                {section.audience ? (
                  <span className="shrink-0 rounded-full border border-[var(--border-visible)] px-3 py-1 text-[10px] uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                    {content.adminBadge}
                  </span>
                ) : null}
              </a>
            ))}
          </div>
        </aside>

        <div className="space-y-6">
          <section className="panel-frame p-6 md:p-8">
            <p className="panel-label">{content.quickstartLabel}</p>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              {content.quickstartSteps.map((step, index) => (
                <div
                  key={step.label}
                  className={
                    index < 2
                      ? "border-b border-[var(--border)] pb-4 md:border-b-0 md:border-r md:pr-4"
                      : "md:pl-4"
                  }
                >
                  <p className="panel-label">{step.label}</p>
                  <p className="mt-2 text-sm leading-6 text-[var(--text-primary)]">{step.body}</p>
                </div>
              ))}
            </div>
          </section>

          {content.sections.map((section) => (
            <section id={section.id} key={section.id} className="panel-frame p-6 md:p-8">
              <div className="flex flex-wrap items-start justify-between gap-4 border-b border-[var(--border)] pb-5">
                <div className="max-w-[52rem]">
                  <p className="panel-label">{section.eyebrow}</p>
                  <h2 className="mt-3 text-3xl leading-tight text-[var(--text-display)] md:text-4xl">{section.title}</h2>
                  <p className="mt-4 max-w-[60ch] text-sm leading-7 text-[var(--text-secondary)]">{section.summary}</p>
                </div>
                {section.audience ? (
                  <div className="rounded-full border border-[var(--border-visible)] px-4 py-2 text-xs uppercase tracking-[0.12em] text-[var(--text-secondary)]">
                    {section.audience}
                  </div>
                ) : null}
              </div>

              <div className={`mt-6 grid gap-6 ${section.image ? "xl:grid-cols-[0.9fr_1.1fr]" : ""}`}>
                <div className="space-y-3">
                  {section.bullets.map((bullet) => (
                    <div key={bullet} className="border-b border-[var(--border)] pb-3 last:border-b-0 last:pb-0">
                      <p className="text-sm leading-7 text-[var(--text-primary)]">{bullet}</p>
                    </div>
                  ))}
                </div>
                {section.image ? <ManualImage {...section.image} /> : null}
              </div>
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}
