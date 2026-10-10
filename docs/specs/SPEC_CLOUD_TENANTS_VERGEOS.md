# SPEC F1 — Integración cloud multi-tenant: VergeOS

## Objetivo
Clientes controlan su tenant VergeOS (vDC) a través de un agente HermesHQ, con aislamiento por credencial de tenant, policies por rol y reportes.

## Fundamentos de la API VergeOS (documentación oficial, v4.12+)
- Base: `https://<instancia>/api/v4/`
- Auth: API key como Bearer (`Authorization: Bearer <key>`) — hereda permisos del usuario dueño (Tenant Admin → scope tenant). Alternativa: token de sesión (`POST /api/sys/tokens` → header `x-yottabyte-token`).
- Convenciones GET: `fields=all|most|summary|<campos>`, `filter` (OData-like: eq/ne/gt/ct/rx/and/or), `sort=±campo`, `limit`.
- Esquemas: `<tabla>$table` devuelve el schema completo.
- Rate limit: **1000 req/hora por key** → el broker DEBE cachear.
- Errores: HTTP estándar; 422 con `{"err": ...}`.

Endpoints canónicos (según doc; validar contra el swagger de la instancia real):
- `GET /v4/vms?fields=most` → VMs ($key, name, cpu_cores, ram, os_family, uuid, state...)
- `GET /v4/vms/{key}` / `POST /v4/vms` / `PUT /v4/vms/{key}`
- `GET /v4/vnets?fields=most` → redes internas/externas
- `GET /v4/machines?fields=all` → hosts
- Snapshots/power: descubrir vía `$table` en la instancia (nombres exactos varían por versión)

## Modelo de datos (migración `cloud_f1`)
```
cloud_platform:  id, kind ('vergeos'), name, api_url, admin_credential_ref (vault, opcional),
                 version_info (json), active, timestamps
cloud_tenant:    id, platform_id FK, tenant_ref (display/nativo), display_name,
                 credential_ref (vault — API key del usuario tenant-admin), 
                 cache_ttl_seconds (default 30), active, timestamps
cloud_binding:   id, tenant_id FK, agent_id FK (UNIQUE(tenant_id, agent_id)),
                 cloud_role ('viewer'|'operator'|'admin')
```
Invariante: **el agente nunca pasa tenant/credenciales** — el broker resuelve por identidad del agente (binding). Un agente puede tener N tenants (multi-cloud de un cliente).

## CloudBrokerService (backend)
- `POST /internal/control/cloud/request` (auth agente + audit `cloud.request`):
  body: `{action: 'vergeos_list_vms'|..., params: {...}}`
- Flujo: agente → binding(s) → si 1 tenant: ejecuta; si N: exige `tenant_id` en params y valida que pertenezca al agente
- Cliente VergeOS (`services/cloud/vergeos_client.py`): httpx AsyncClient, Bearer key, timeouts cortos, **cache por (tenant, path+query) con TTL 30s** (respeta rate-limit), revalidación tras mutación (invalidar cache del tenant)
- Redacción: nunca loguear keys; errores mapeados (401→"credencial de tenant inválida", 429→backoff)
- Rate-limit propio por tenant (protección extra: p.ej. 300 req/h < límite VergeOS)
- Health-check: `GET /v4/system?fields=summary` con la key del tenant → estado de credencial

## Integration package `hermeshq_cloud_vergeos` (+ tools)
### Lectura (viewer)
- `vergeos_list_vms(filter?)` — lista normalizada: id, name, state, cpu, ram_mb, os, tags
- `vergeos_get_vm(vm_id)` — detalle + drives + nics
- `vergeos_list_networks()` — vnets del tenant
- `vergeos_usage()` — recursos del tenant (uso vs asignado)
- `vergeos_list_snapshots(vm_id?)`
- `vergeos_events(filter?)` — log de eventos
### Operación (operator)
- `vergeos_vm_power(vm_id, on|off|reset|shutdown)`
- `vergeos_snapshot_create(vm_id, name)` / `vergeos_snapshot_restore(vm_id, snapshot_id)`
### Administración (admin)
- `vergeos_vm_create(name, cpu, ram_mb, ...)`, `vergeos_vm_resize(vm_id, cpu?, ram_mb?)`
- (delete NO se expone en F1 — por diseño)
### Reportes (viewer) — requisito explícito
- `vergeos_report_inventory()` — inventario completo en markdown + CSV adjunto
- `vergeos_report_usage(period?)` — uso/percentiles (datos de usage history del tenant)
- `vergeos_report_snapshots()` — snapshots por VM con antigüedad
- Entrega: respuesta markdown en el chat + **attachment** (mecanismo existente de response attachments); formato CSV y/o PDF (plugin pdf existente)
- Patrón: cada reporte se genera desde las tools de lectura (composición) → cacheable y auditable

## Policies (seed, migración)
- `sys-role-cloud-viewer`: allow `vergeos_list_*,vergeos_get_*,vergeos_usage,vergeos_events,vergeos_report_*`
- `sys-role-cloud-operator`: + `vergeos_vm_power,vergeos_snapshot_*`
- `sys-role-cloud-admin`: + `vergeos_vm_create,vergeos_vm_resize`
Doble capa: toolset (si el agente no tiene `hermeshq_cloud_vergeos`, las tools no existen) + policy (qué permite) + broker (a qué tenant llega).

## UI mínima
- Settings → Cloud Platforms: plataforma (URL + key admin opcional) / tenants (nombre, key, TTL) / bindings (agente + rol)
- Agent detail → tab Cloud: binding(s) read-only

## Red
- El BROKER (backend) hace las llamadas — los contenedores de agentes NO necesitan egress a VergeOS (no cambia squid)
- Requisito: el host de HermesHQ alcanza la API de VergeOS (IP/LAN). TLS auto-firmado: soportar `verify=false` por plataforma (flag `insecure_tls`, default false)

## Testing (sin instancia real)
- Unit: vergeos_client con `httpx.MockTransport` (auth, cache TTL, 429 backoff, redacción), broker (resolución por identidad, multi-tenant, auditoría), policies
- E2E VM hq-test: contenedor **stub VergeOS** (FastAPI que imita /v4/vms, /v4/vnets, power con estado en memoria) → alta de plataforma+tenant+binding → tarea real de agente lista VMs → power off → reporte inventory con attachment
- E2E live (futuro): con instancia real cuando exista

## Fuera de alcance F1
Delete de VMs, gestión de tenants (crear/borrar tenants VergeOS — admin platform), VCF/VMware, OLVM, quotas duras (solo lectura de uso), schedules de reportes (F4)

## Entregables
1. Migración + modelos + policies seed
2. vergeos_client + broker + endpoints control + admin CRUD cloud
3. Plugin + tools (incl. reportes con attachments)
4. UI Settings + tab agente
5. Tests unit + stub E2E en VM
6. Docs manual (ES/EN) sección Cloud
