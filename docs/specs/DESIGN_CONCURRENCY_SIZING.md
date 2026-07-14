# Design: Concurrency Semaphore + Smart Sizing

**Fecha:** 2026-05-21  
**Estado:** Aprobado — listo para desarrollo por fases

---

## 1. Semaphore Configurable

### Problema actual

El `AgentSupervisor` tiene un semaphore hardcodeado en `8`:

```python
self._concurrency_semaphore = asyncio.Semaphore(8)  # max concurrent task processes
```

Esto es insuficiente para deployments grandes y excesivo para deployments pequeños.

### Solución

#### 1.1 Nuevo campo en `config.py`

```python
class Settings(BaseSettings):
    # ... existing fields ...
    
    # Max concurrent hermes_task_runner subprocesses.
    # Each process uses 50MB RAM. Default: 8 (safe for 1GB container).
    # For production sizing: available_RAM_MB / 60 (50MB per process + 20% headroom)
    concurrency_semaphore: int = 8
```

#### 1.2 AgentSupervisor lo lee dinámicamente

```python
# agent_supervisor.py — constructor
from hermeshq.config import get_settings

class AgentSupervisor:
    def __init__(self, ...):
        settings = get_settings()
        self._concurrency_semaphore = asyncio.Semaphore(settings.concurrency_semaphore)
```

#### 1.3 Variable de entorno

```bash
# .env
CONCURRENCY_SEMAPHORE=8    # default, overridden by installer
```

#### 1.4 docker-compose.yml

```yaml
backend:
  environment:
    CONCURRENCY_SEMAPHORE: ${CONCURRENCY_SEMAPHORE:-8}
```

---

## 2. Smart Installer — Resource-Aware Setup

### Flujo del instalador

```
install.sh
  │
  ├─ ¿Fresh install?
  │   └─ YES → prompt: "¿Cuántos agentes planeas desplegar?"
  │            │
  │            ├─ Input: N agentes
  │            │
  │            ├─ Calcular recursos necesarios
  │            │   ├── RAM necesaria
  │            │   ├── CPU necesaria
  │            │   ├── Disco necesario
  │            │   └── Semaphore value
  │            │
  │            ├─ Detectar recursos disponibles
  │            │   ├── RAM disponible (total - used)
  │            │   ├── CPU cores
  │            │   └── Disco disponible
  │            │
  │            ├─ ¿Recursos suficientes?
  │            │   ├── YES → Configurar docker-compose + .env → Continuar
  │            │   └── NO  → Mostrar tabla comparativa → Preguntar:
  │            │             ├── "Reducir a X agentes (máximo posible)"
  │            │             ├── "Continuar de todas formas (no recomendado)"
  │            │             └── "Cancelar instalación"
  │            │
  │            └─ Escribir config
  │                ├── CONCURRENCY_SEMAPHORE en .env
  │                ├── deploy.resources.limits en docker-compose.yml
  │                └── PostgreSQL tuning en docker-compose.yml
  │
  └─ NO (update) → Preservar .env existente
                    └─ Solo actualizar si CONCURRENCY_SEMAPHORE no existe
```

### 2.1 Tabla de sizing

| Agentes totales | Concurrentes (50%) | Semaphore | RAM backend | RAM postgres | RAM total Docker | CPU cores | Disco |
|-----------------|---------------------|-----------|-------------|--------------|------------------|-----------|-------|
| 10              | 5                   | 5         | 1 GB        | 512 MB       | **2 GB**         | 2         | 30 GB |
| 25              | 12                  | 12        | 2 GB        | 1 GB         | **4 GB**         | 4         | 50 GB |
| 50              | 25                  | 25        | 4 GB        | 2 GB         | **8 GB**         | 4         | 80 GB |
| 100             | 50                  | 50        | 8 GB        | 4 GB         | **16 GB**        | 8         | 150 GB |
| 200             | 100                 | 100       | 16 GB       | 8 GB         | **32 GB**        | 16        | 300 GB |

**Fórmulas:**

```bash
SEMAPHORE         = min(TOTAL_AGENTS / 2, available_RAM_for_backend / 60)
RAM_BACKEND       = SEMAPHORE * 50 + 500   # 50MB per worker + 500MB base
RAM_POSTGRES      = SEMAPHORE * 10 + 200   # ~10MB per conn + 200MB base
RAM_TOTAL_DOCKER  = RAM_BACKEND + RAM_POSTGRES + 256  # 256MB for frontend+overhead
CPU_CORES         = ceil(SEMAPHORE / 6) + 1  # ~6 workers per core + 1 for system
DISK_GB           = TOTAL_AGENTS * 1.5 + 5   # 1.5GB per agent + 5GB base
```

### 2.2 Detección de recursos del sistema

```bash
detect_system_resources() {
  # RAM disponible (MB)
  if [ "$(uname -s)" = "Linux" ]; then
    total_ram_mb=$(free -m | awk '/^Mem:/ {print $2}')
    available_ram_mb=$(free -m | awk '/^Mem:/ {print $7}')
  elif [ "$(uname -s)" = "Darwin" ]; then
    total_ram_mb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
    # macOS doesn't have "available" easily — use 70% of total as safe estimate
    available_ram_mb=$(( total_ram_mb * 70 / 100 ))
  fi

  # CPU cores
  cpu_cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)

  # Disco disponible en el path de instalación (GB)
  install_dir="${INSTALL_DIR:-$HOME/hermeshq}"
  mkdir -p "$install_dir" 2>/dev/null
  available_disk_gb=$(df -BG "$install_dir" | awk 'NR==2 {gsub(/G/,"",$4); print $4}')
}
```

### 2.3 Cálculo y validación

```bash
calculate_sizing() {
  local agents=$1
  
  concurrent=$(( agents / 2 ))
  semaphore=$concurrent
  
  ram_backend=$(( semaphore * 50 + 500 ))     # MB
  ram_postgres=$(( semaphore * 10 + 200 ))     # MB
  ram_total=$(( ram_backend + ram_postgres + 256 ))
  
  cpu_needed=$(( (semaphore / 6) + 1 ))
  disk_needed=$(( agents * 1500 / 1000 + 5 ))  # GB (agents * 1.5GB + 5GB)
  
  # Clamp semaphore to available resources
  local max_by_ram=$(( (available_ram_mb - 756) / 60 ))  # reserve 756MB for system
  if [ "$max_by_ram" -lt "$semaphore" ]; then
    semaphore=$max_by_ram
    concurrent=$semaphore
    ram_backend=$(( semaphore * 50 + 500 ))
    ram_postgres=$(( semaphore * 10 + 200 ))
    ram_total=$(( ram_backend + ram_postgres + 256 ))
  fi
}

validate_resources() {
  local issues=()
  
  if [ "$ram_total" -gt "$available_ram_mb" ]; then
    issues+=("RAM: necesitas ${ram_total}MB, disponible ${available_ram_mb}MB")
  fi
  
  if [ "$cpu_needed" -gt "$cpu_cores" ]; then
    issues+=("CPU: necesitas ${cpu_needed} cores, disponible ${cpu_cores}")
  fi
  
  if [ "$disk_needed" -gt "$available_disk_gb" ]; then
    issues+=("Disco: necesitas ${disk_needed}GB, disponible ${available_disk_gb}GB")
  fi
  
  if [ ${#issues[@]} -gt 0 ]; then
    return 1  # insufficient
  fi
  return 0  # sufficient
}
```

### 2.4 Prompt interactivo

```
$ curl -fsSL https://raw.githubusercontent.com/jpalmae/hermeshq/main/install.sh | bash

  ╔══════════════════════════════════════════╗
  ║       HermesHQ Installer v2026.5.19      ║
  ╚══════════════════════════════════════════╝

  Docker ✅  |  Docker Compose ✅  |  System ✅

  ── Resource Planning ──────────────────────

  ¿Cuántos agentes planeas desplegar? (1-200): 100

  System detected:
    RAM:  32 GB total / 24 GB available
    CPU:  8 cores
    Disk: 500 GB available

  Calculated for 100 agents (50 concurrent):

    ┌──────────────┬─────────────┬──────────────┐
    │ Resource     │ Needed      │ Available    │
    ├──────────────┼─────────────┼──────────────┤
    │ RAM          │ 16 GB       │ 24 GB     ✅ │
    │ CPU          │ 8 cores     │ 8 cores   ✅ │
    │ Disk         │ 150 GB      │ 500 GB    ✅ │
    │ Semaphore    │ 50          │              │
    └──────────────┴─────────────┴──────────────┘

  ✅ Resources sufficient — proceeding with installation.
```

**Ejemplo con recursos insuficientes:**

```
  ¿Cuántos agentes planeas desplegar? (1-200): 100

  System detected:
    RAM:  8 GB total / 5 GB available
    CPU:  2 cores
    Disk: 100 GB available

  Calculated for 100 agents (50 concurrent):

    ┌──────────────┬─────────────┬──────────────┐
    │ Resource     │ Needed      │ Available    │
    ├──────────────┼─────────────┼──────────────┤
    │ RAM          │ 16 GB       │ 5 GB      ❌ │
    │ CPU          │ 8 cores     │ 2 cores   ❌ │
    │ Disk         │ 150 GB      │ 100 GB    ❌ │
    └──────────────┴─────────────┴──────────────┘

  ❌ Insufficient resources for 100 agents.

  Maximum supported with this system: 12 agents (6 concurrent)

    ┌──────────────┬─────────────┬──────────────┐
    │ Resource     │ Needed      │ Available    │
    ├──────────────┼─────────────┼──────────────┤
    │ RAM          │ 4 GB        │ 5 GB      ✅ │
    │ CPU          │ 2 cores     │ 2 cores   ✅ │
    │ Disk         │ 23 GB       │ 100 GB    ✅ │
    │ Semaphore    │ 6           │              │
    └──────────────┴─────────────┴──────────────┘

  Options:
    1) Install for 12 agents (recommended)
    2) Install for 100 agents anyway (NOT recommended — will likely OOM)
    3) Cancel installation

  Choose [1/2/3]: 
```

### 2.5 Escritura de configuración

Cuando el usuario confirma, el instalador:

#### `.env`

```bash
# Agrega al .env generado:
CONCURRENCY_SEMAPHORE=50
```

#### `docker-compose.yml`

El instalador genera un override o modifica los resource limits:

```bash
# Genera docker-compose.override.yml con los resource limits calculados
cat > "$INSTALL_DIR/docker-compose.override.yml" <<EOF
services:
  postgres:
    command: >
      postgres
        -c shared_buffers=${PG_SHARED_BUFFERS}
        -c max_connections=${PG_MAX_CONNECTIONS}
        -c work_mem=64MB
        -c effective_cache_size=${PG_EFFECTIVE_CACHE}
    deploy:
      resources:
        limits:
          memory: ${RAM_POSTGRES}M
          cpus: '${CPU_POSTGRES}'
  backend:
    deploy:
      resources:
        limits:
          memory: ${RAM_BACKEND}M
          cpus: '${CPU_BACKEND}'
  frontend:
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: '0.5'
EOF
```

### 2.6 PostgreSQL tuning automático

```bash
calculate_postgres_tuning() {
  local pg_ram_mb=$1
  
  # shared_buffers: 25% de RAM dedicada a postgres
  pg_shared_buffers=$(( pg_ram_mb / 4 ))
  
  # effective_cache_size: 75% de RAM dedicada a postgres
  pg_effective_cache=$(( pg_ram_mb * 3 / 4 ))
  
  # max_connections: semaphore * 2 (task queries + event queries)
  pg_max_connections=$(( semaphore * 2 ))
  
  # Format as PostgreSQL units
  if [ "$pg_shared_buffers" -ge 1024 ]; then
    PG_SHARED_BUFFERS="${pg_shared_buffers}MB"  # e.g. "1024MB"  
  else
    PG_SHARED_BUFFERS="${pg_shared_buffers}MB"
  fi
  PG_EFFECTIVE_CACHE="${pg_effective_cache}MB"
  PG_MAX_CONNECTIONS=$pg_max_connections
}
```

### 2.7 Updates existentes

Para instalaciones existentes (`FRESH_INSTALL=0`), el instalador:

1. Preserva el `.env` existente (no sobrescribe)
2. Si `CONCURRENCY_SEMAPHORE` no existe en `.env`, lo agrega con el valor default `8`
3. Si `docker-compose.override.yml` no existe, no lo crea (el usuario puede generar uno después con un comando `hermeshq-resize`)
4. Muestra un aviso:

```
  ℹ️  Existing installation detected.
  Current CONCURRENCY_SEMAPHORE: 8
  
  To resize your deployment, run:
    hermeshq-resize --agents 50
```

---

## 3. Comando `hermeshq-resize`

Script standalone para redimensionar una instalación existente:

```bash
$ hermeshq-resize --agents 100

  Current: 8 agents, semaphore 8
  Target:  100 agents, semaphore 50

  System resources:
    RAM: 32 GB available → 16 GB needed ✅
    CPU: 8 cores → 8 needed ✅
    Disk: 500 GB → 150 GB needed ✅

  This will:
    1. Update CONCURRENCY_SEMAPHORE=50 in .env
    2. Regenerate docker-compose.override.yml
    3. Restart backend container

  Proceed? [y/N]: 
```

También soporta detectar y sugerir:

```bash
$ hermeshq-resize --detect

  Current deployment:
    Agents registered: 24
    Semaphore: 8
    Container limits: 1GB RAM, 1 CPU

  Detected system resources:
    RAM: 16 GB total / 12 GB available
    CPU: 4 cores
    Disk: 200 GB available

  Recommended resize:
    → Semaphore: 12 (for 24 agents at 50% concurrency)
    → Backend: 2 GB RAM, 3 CPUs
    → PostgreSQL: 512 MB RAM, shared_buffers=128MB

  Run `hermeshq-resize --agents 24` to apply.
```

Ubicación: `scripts/hermeshq-resize.sh`

---

## 4. Settings UI — Resource Configuration

Nueva tab en Settings para que el administrador gestione recursos desde la web.

### 4.1 Nueva tab: "Resources"

```
┌─ Settings ──────────────────────────────────────────────────┐
│ [General] [Branding] [Hermes Versions] [Secrets] [Resources]│
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ── Concurrency ────────────────────────────────────────    │
│                                                             │
│  Max concurrent tasks (semaphore)                           │
│  ┌──────────────────────────────────┐  ┌──────────┐        │
│  │ 50                               │  │ [Apply]  │        │
│  └──────────────────────────────────┘  └──────────┘        │
│  Current: 8 | Recommended: 50 (for 100 agents at 50%)      │
│  ⚠️ Requires backend restart to take effect.                │
│                                                             │
│  ── Resource Estimator ─────────────────────────────────    │
│                                                             │
│  Planned agents: [  100  ]                                 │
│                                                             │
│  ┌────────────────────────────────────────────────────┐     │
│  │ Estimated Resources for 100 agents (50 concurrent) │     │
│  ├──────────────┬─────────────┬───────────────────────┤     │
│  │ Resource     │ Needed      │ Current Container     │     │
│  ├──────────────┼─────────────┼───────────────────────┤     │
│  │ RAM backend  │ 3.0 GB      │ 1.0 GB (Docker limit) │     │
│  │ RAM postgres │ 700 MB      │ Default (unlimited)   │     │
│  │ CPU          │ 8 cores     │ 1.0 (Docker limit)    │     │
│  │ Disk         │ 150 GB      │ Unknown               │     │
│  │ Semaphore    │ 50          │ 8 (active)            │     │
│  └──────────────┴─────────────┴───────────────────────┘     │
│                                                             │
│  ⚠️ Backend container is limited to 1GB RAM.               │
│     Recommended: at least 3GB for 50 concurrent tasks.      │
│                                                             │
│  [Generate docker-compose.override.yml]                     │
│                                                             │
│  ── Current Status ─────────────────────────────────────    │
│                                                             │
│  Active tasks: 12/50 (24%)                                 │
│  Memory usage: 480MB / 1024MB (46%) ← ❌接近 limite        │
│  CPU usage:    23%                                         │
│  Uptime:       3d 14h                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Backend endpoints

```python
# GET /api/settings/resources — current resource status
{
  "semaphore": {
    "current": 8,
    "recommended": 50,
    "active_tasks": 12,
    "max_tasks": 8,
    "utilization_pct": 100  # 12/8 = over capacity (queued)
  },
  "container": {
    "memory_limit_mb": 1024,
    "memory_usage_mb": 480,
    "cpu_limit": 1.0,
    "cpu_usage_pct": 23.0
  },
  "system": {
    "total_ram_mb": 32768,
    "available_ram_mb": 24576,
    "cpu_cores": 8,
    "disk_available_gb": 500
  },
  "estimate": {
    "agents": 100,
    "concurrent": 50,
    "ram_backend_mb": 3000,
    "ram_postgres_mb": 700,
    "cpu_needed": 8,
    "disk_gb": 150
  }
}
```

```python
# PUT /api/settings/resources/semaphore — update semaphore (requires restart)
{
  "semaphore": 50,
  "restart_required": true
}
```

```python
# POST /api/settings/resources/generate-override — generates docker-compose.override.yml
# Returns the file content for download or auto-apply
{
  "content": "services:\n  backend:\n    deploy:\n...",
  "applied": false,
  "restart_required": true
}
```

### 4.3 Detección de recursos del sistema

El backend detecta recursos desde dentro del container:

```python
# services/resource_monitor.py

class ResourceMonitor:
    def get_container_limits(self) -> dict:
        """Lee cgroups para memory/cpu limits del container."""
        # Memory limit: /sys/fs/cgroup/memory/memory.limit_in_bytes (cgroup v1)
        #             o /sys/fs/cgroup/memory.max (cgroup v2)
        # CPU limit: /sys/fs/cgroup/cpu/cpu.cfs_quota_us / cpu.cfs_period_us
        
    def get_container_usage(self) -> dict:
        """Uso actual de memoria y CPU del proceso."""
        import psutil  # ya disponible en el container
        process = psutil.Process()
        return {
            "memory_mb": process.memory_info().rss / 1024 / 1024,
            "cpu_pct": process.cpu_percent(),
            "num_threads": process.num_threads(),
            "num_children": len(process.children()),
        }
    
    def get_system_resources(self) -> dict:
        """Recursos del host (puede ser limitado por Docker)."""
        import psutil
        return {
            "total_ram_mb": psutil.virtual_memory().total / 1024 / 1024,
            "available_ram_mb": psutil.virtual_memory().available / 1024 / 1024,
            "cpu_cores": psutil.cpu_count(),
            "disk_available_gb": psutil.disk_usage("/").free / 1024 / 1024 / 1024,
        }
```

### 4.4 Frontend — nuevo archivo

`frontend/src/components/settings/ResourcesTab.tsx`

### 4.5 i18n keys

```typescript
// en/settings.ts
resources: "Resources",
semaphore: "Max Concurrent Tasks",
semaphoreDescription: "Maximum number of agent tasks running simultaneously. Each task uses ~50MB RAM.",
semaphoreRecommended: "Recommended: {value} (for {agents} agents at 50% concurrency)",
semaphoreApply: "Apply",
restartRequired: "⚠️ Requires backend restart to take effect.",
resourceEstimator: "Resource Estimator",
plannedAgents: "Planned agents",
estimatedResources: "Estimated Resources for {agents} agents ({concurrent} concurrent)",
resourceBackendRam: "RAM backend",
resourcePostgresRam: "RAM postgres",
resourceCpu: "CPU cores",
resourceDisk: "Disk",
generateOverride: "Generate docker-compose.override.yml",
currentStatus: "Current Status",
activeTasks: "Active tasks",
memoryUsage: "Memory usage",
cpuUsage: "CPU usage",
containerWarning: "⚠️ Backend container is limited to {limit}MB RAM. Recommended: at least {needed}MB for {concurrent} concurrent tasks.",
```

---

## 5. Installer — Detectar y ofrecer resize

### 5.1 Para instalaciones existentes (update)

Cuando el instalador detecta una instalación existente (`FRESH_INSTALL=0`):

```
  ══ Existing Installation Detected ══

  Current configuration:
    CONCURRENCY_SEMAPHORE: 8
    Backend memory limit:  1 GB (from docker-compose.yml)
    
  System resources:
    RAM:  16 GB total / 12 GB available
    CPU:  4 cores
    Disk: 200 GB available

  Your system can support up to ~60 agents (30 concurrent) comfortably.

  Would you like to resize your deployment?
    1) Keep current configuration (8 concurrent)
    2) Resize to recommended (detect optimal)
    3) Resize to specific agent count
    4) Skip

  Choose [1/2/3/4]: 
```

### 5.2 Si elige "Resize to recommended"

El instalador calcula el máximo de agentes que soporta el sistema y reconfigura:

```bash
resize_recommended() {
  local available=$available_ram_mb
  local max_semaphore=$(( (available - 756) / 60 ))  # 50MB per worker + 10MB pg + overhead
  local max_agents=$(( max_semaphore * 2 ))
  
  echo "  Recommended configuration:"
  echo "    Agents:    up to ${max_agents}"
  echo "    Semaphore: ${max_semaphore}"
  echo "    Backend:   $(( max_semaphore * 50 + 500 ))MB RAM"
  echo "    Postgres:  $(( max_semaphore * 10 + 200 ))MB RAM"
  
  # Update .env
  sed -i "s/^CONCURRENCY_SEMAPHORE=.*/CONCURRENCY_SEMAPHORE=${max_semaphore}/" .env
  
  # Generate override
  generate_docker_override "$max_semaphore"
  
  # Restart
  compose up -d
}
```

### 5.3 Si elige "Resize to specific count"

```
  How many agents? (1-{max}): 100

  ┌──────────────┬─────────────┬──────────────┐
  │ Resource     │ Needed      │ Available    │
  ├──────────────┼─────────────┼──────────────┤
  │ RAM          │ 3.0 GB      │ 12 GB     ✅ │
  │ CPU          │ 8 cores     │ 4 cores   ⚠️ │
  │ Disk         │ 150 GB      │ 200 GB    ✅ │
  │ Semaphore    │ 50          │              │
  └──────────────┴─────────────┴──────────────┘

  ⚠️ CPU is below recommended (8 cores needed, 4 available).
     Performance may be degraded under heavy load.

  Proceed anyway? [y/N]: 
```

---

## 6. Archivos a modificar

| # | Archivo | Cambio | Fase |
|---|---------|--------|------|
| 1 | `backend/hermeshq/config.py` | Agregar `concurrency_semaphore: int = 8` | 1 |
| 2 | `backend/hermeshq/services/agent_supervisor.py` | Leer semaphore desde `get_settings()` | 1 |
| 3 | `docker-compose.yml` | Agregar env var `CONCURRENCY_SEMAPHORE` al backend | 1 |
| 4 | `backend/hermeshq/services/resource_monitor.py` | Nuevo servicio: detección de recursos del container/sistema | 2 |
| 5 | `backend/hermeshq/routers/settings.py` | Endpoints: `/resources`, `/resources/semaphore`, `/resources/generate-override` | 2 |
| 6 | `frontend/src/components/settings/ResourcesTab.tsx` | Nueva tab de Resources en Settings | 2 |
| 7 | `frontend/src/api/settings.ts` | Hooks para resource endpoints | 2 |
| 8 | `frontend/src/lib/i18n/locales/en/settings.ts` | i18n EN para resources | 2 |
| 9 | `frontend/src/lib/i18n/locales/es/settings.ts` | i18n ES para resources | 2 |
| 10 | `install.sh` | Prompt de agentes, cálculo, validación, override generation | 3 |
| 11 | `scripts/hermeshq-resize.sh` | Nuevo script de resize standalone | 3 |
| 12 | `.env.example` | Documentar `CONCURRENCY_SEMAPHORE` | 1 |

### Fases de desarrollo

| Fase | Qué | Archivos | Esfuerzo |
|------|-----|----------|----------|
| **1 — Semaphore configurable** | config.py + supervisor + docker-compose + .env | 4 archivos | Pequeño |
| **2 — Settings UI** | ResourceMonitor + endpoints + ResourcesTab + i18n | 6 archivos | Medio |
| **3 — Installer + resize** | install.sh + hermeshq-resize.sh | 2 archivos | Medio |

---

## 7. Consideraciones

### Decisions

| Decisión | Justificación |
|----------|---------------|
| **50MB por worker** | Basado en mediciones reales del stress test + headroom |
| **Concurrencia default 50%** | Consistente con requisito "al menos 50% concurrente" |
| **Concurrencia configurable** | El admin puede ajustar el % si su workload lo requiere |
| **`docker-compose.override.yml`** | No modifica el docker-compose.yml original — merge automático por Docker Compose |
| **Detectar y ofrecer resize** | En updates, no fuerza cambio — pregunta al usuario |
| **ResourceMonitor via psutil** | psutil ya está disponible en el container (dependencia de hermes-agent) |

### Limits del approach

- El sizing es **estimativo** — los requirements reales varían según el tipo de tareas (light vs heavy prompts)
- El semaphore limita subprocess de `hermes_task_runner`, no conexiones WebSocket ni webhooks
- PostgreSQL tuning es básico — para deployments >100 agentes se recomienda DB externa (RDS, Cloud SQL)
- El instalador solo detecta recursos del **host** — no de Docker Desktop VMs (macOS/Windows)
- Container memory limit change requiere **restart del container** (no hot-reload)

### Out of scope (por ahora)

- Auto-scaling dinámico basado en uso real
- Alertas de OOM risk en el dashboard
- Soporte para múltiples backend replicas
- External PostgreSQL detection
- Horizontal scaling (Kubernetes/Swarm)
