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

const manualSections: ManualSection[] = [
  {
    id: "overview",
    eyebrow: "Orientacion",
    title: "Que es HermesHQ y como se organiza",
    summary:
      "HermesHQ es un panel operativo para crear, supervisar y conversar con agentes Hermes desde una sola interfaz. La app separa tareas globales de instancia y tareas específicas de cada agente.",
    bullets: [
      "El menú izquierdo es la navegación principal. Desde ahí accedes a Dashboard, Agents, Tasks, Schedules y Comms; si eres admin también verás Users, Nodes y Settings.",
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
      "Si la instancia tiene una skin global de TUI configurada por un admin, esa apariencia se aplica a todas las sesiones nuevas del terminal Hermes.",
      "Talk to this agent envía mensajes como tareas operativas y conserva un historial estilo conversación.",
      "Runtime ledger resume ejecuciones, resultados y errores; cuando este agente delega trabajo, el resultado del subordinado vuelve a aparecer aquí como una task callback automática.",
      "Configuration permite editar parámetros como system prompt o avatar cuando haga falta.",
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
      "Si una delegación nace desde Telegram, HermesHQ conserva el contexto del chat de origen. Cuando el agente subordinado termina, el resultado puede volver automáticamente a ese mismo chat de Telegram.",
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
      "Si usas Kimi Coding, el preset correcto queda apuntando a `https://api.kimi.com/coding/v1`.",
      "Los usuarios sin privilegios no pueden modificar estos parámetros globales, pero sí elegir su tema e idioma personales.",
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
];

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
  return (
    <div className="space-y-8">
      <section className="grid gap-6 xl:grid-cols-[0.42fr_1fr]">
        <aside className="panel-frame p-6 xl:sticky xl:top-8 xl:self-start">
          <p className="panel-label">Manual de usuario</p>
          <h1 className="mt-4 max-w-[12ch] text-4xl leading-none text-[var(--text-display)] md:text-5xl">
            Operar HermesHQ de punta a punta
          </h1>
          <p className="mt-4 max-w-[34ch] text-sm leading-6 text-[var(--text-secondary)]">
            Esta guía explica el flujo completo de uso de la plataforma, desde la navegación general hasta la administración de
            agentes, usuarios, tareas recurrentes y canales externos.
          </p>
          <div className="mt-8 space-y-2">
            {manualSections.map((section, index) => (
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
                    Admin
                  </span>
                ) : null}
              </a>
            ))}
          </div>
        </aside>

        <div className="space-y-6">
          <section className="panel-frame p-6 md:p-8">
            <p className="panel-label">Inicio rapido</p>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              <div className="border-b border-[var(--border)] pb-4 md:border-b-0 md:border-r md:pr-4">
                <p className="panel-label">1. Entrar</p>
                <p className="mt-2 text-sm leading-6 text-[var(--text-primary)]">
                  Inicia sesión con tu cuenta. Si eres usuario estándar, verás solo los agentes que te asignaron.
                </p>
              </div>
              <div className="border-b border-[var(--border)] pb-4 md:border-b-0 md:border-r md:px-4">
                <p className="panel-label">2. Ubicar el flujo</p>
                <p className="mt-2 text-sm leading-6 text-[var(--text-primary)]">
                  Usa Dashboard para visión general, Agents para operar agentes y Tasks o Schedules para ejecuciones manuales o recurrentes.
                </p>
              </div>
              <div className="md:pl-4">
                <p className="panel-label">3. Ejecutar</p>
                <p className="mt-2 text-sm leading-6 text-[var(--text-primary)]">
                  Conversa con el agente, usa la TUI, programa tareas o vincula canales externos como Telegram según el caso.
                </p>
              </div>
            </div>
          </section>

          {manualSections.map((section) => (
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
