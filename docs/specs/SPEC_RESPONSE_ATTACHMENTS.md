# Spec: Adjuntos en respuestas del agente (HermesHQ)

> **Versión:** 2.0 — cubre backend + app móvil
> **Estado:** Borrador para revisión

---

## 0. Contexto

La app móvil actualmente solo soporta **adjuntos del usuario** (uploads que se guardan en `task.metadata.attachments` y se sirven desde `uploads/`). Se necesita que el **agente** pueda devolver archivos adjuntos en su respuesta — archivos que genera durante la ejecución y guarda en `work/`.

### Problemas identificados en el flujo actual

| # | Gap | Ubicación |
|---|-----|-----------|
| 1 | Nadie escanea `work/` después de la ejecución para detectar archivos generados | `agent_supervisor.py` |
| 2 | `RuntimeExecutionResult` no transporta archivos generados | `hermes_runtime.py` |
| 3 | `hermes_task_runner.py` no reporta archivos generados | `hermes_task_runner.py` |
| 4 | El evento `task.completed` no incluye `metadata` | `agent_supervisor.py` |
| 5 | El endpoint de descarga solo busca en `uploads/`, no en `work/` | `attachments.py` |
| 6 | No hay límites de tamaño, cantidad ni política de limpieza | — |

Este spec define cómo resolver todos estos gaps.

---

## 1. Flujo de extremo a extremo

```
┌─────────────────────────────────────────────────────────────────────┐
│  AGENTE EN EJECUCIÓN                                                 │
│                                                                     │
│  hermes_task_runner.py                                              │
│    1. Snapshot de work/ ANTES de ejecutar (archivos existentes)      │
│    2. Ejecuta el agente (AIAgent.run_conversation)                   │
│    3. Snapshot de work/ DESPUÉS de ejecutar                          │
│    4. Calcula diff = archivos nuevos/modificados                     │
│    5. Emite lista en el evento "result"                              │
│    6. Copia archivos nuevos a uploads/ con file_id                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  BACKEND                                                             │
│                                                                     │
│  hermes_runtime.py (_run_real)                                      │
│    7. Lee generated_files del resultado del subprocess              │
│    8. Construye RuntimeExecutionResult con generated_files          │
│                                                                     │
│  agent_supervisor.py (_execute_task)                                │
│    9. Inyecta response_attachments en task.metadata_json            │
│   10. Incluye metadata en el evento task.completed                   │
│                                                                     │
│  attachments.py (download_attachment)                              │
│   11. Busca en uploads/ (ya copiados en paso 6)                     │
│   12. Sirve el archivo al cliente con FileResponse                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  APP MÓVIL                                                           │
│                                                                     │
│   13. Recibe task.completed con metadata.response_attachments        │
│   14. Muestra adjuntos (preview de imágenes, descarga de otros)      │
│   15. Descarga vía GET /api/agents/{id}/attachments/{file_id}        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Estructura de datos

### 2.1 `response_attachments` en `task.metadata_json`

Cuando un agente genere archivos en su respuesta, se incluyen en `task.metadata_json` bajo la clave **`response_attachments`**:

```json
{
  "task": {
    "id": "task_123",
    "response": "Aquí está el reporte solicitado...",
    "metadata": {
      "attachments": [
        {
          "file_id": "aaa-111",
          "filename": "photo.jpg",
          "media_type": "image/jpeg",
          "size": 102400,
          "caption": "Foto del problema"
        }
      ],
      "response_attachments": [
        {
          "file_id": "bbb-222",
          "filename": "reporte_ventas.xlsx",
          "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "size": 45230,
          "caption": "Reporte de ventas Q2 2026"
        }
      ]
    }
  }
}
```

### 2.2 Campos por archivo

| Campo | Tipo | Enviado al cliente | Descripción |
|-------|------|--------------------|-------------|
| `file_id` | string (UUID) | ✅ Sí | Identificador único del archivo |
| `filename` | string | ✅ Sí | Nombre original con extensión |
| `media_type` | string | ✅ Sí | MIME type (ej. `image/png`, `application/pdf`) |
| `size` | number | ✅ Sí | Tamaño en bytes |
| `caption` | string | ✅ Sí | Descripción opcional (vacío si no aplica) |
| `source_path` | string | ❌ No (interno) | Ruta original en `work/` — solo backend |

> **⚠️ Importante:** El campo `path` del spec v1 **se elimina**. La app móvil usa exclusivamente `file_id` + el endpoint de descarga. El backend mantiene `source_path` internamente pero **nunca lo envía al cliente**.

### 2.3 Clave distinta a `attachments`

- **`attachments`** — archivos que envía el **usuario** (uploads desde la app)
- **`response_attachments`** — archivos que devuelve el **agente** (generados en `work/`)

Nunca reutilizar la clave `attachments` para respuesta del agente.

---

## 3. Cambios en el backend

### 3.1 `hermes_task_runner.py` — Detectar y copiar archivos generados

Después de que el agente termina de ejecutar, comparar el contenido de `work/` antes y después para detectar archivos nuevos o modificados.

```python
# ANTES de ejecutar el agente:
pre_snapshot = _snapshot_directory(Path(payload["cwd"]) / "work")

# ... ejecutar agente ...

# DESPUÉS de ejecutar:
post_snapshot = _snapshot_directory(Path(payload["cwd"]) / "work")
generated_files = _diff_snapshots(pre_snapshot, post_snapshot)

# Filtrar por extensiones permitidas y tamaño máximo
generated_files = [
    f for f in generated_files
    if _is_allowed(f) and f["size"] <= MAX_RESPONSE_FILE_SIZE
]

# Limitar cantidad
generated_files = generated_files[:MAX_RESPONSE_FILES]

# Copiar a uploads/ con file_id UUID
response_attachments = []
uploads_dir = Path(payload["cwd"]) / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)

for f in generated_files:
    file_id = str(uuid.uuid4())
    dest = uploads_dir / f"{file_id}{f['extension']}"
    shutil.copy2(f["path"], dest)
    response_attachments.append({
        "file_id": file_id,
        "filename": f["name"],
        "media_type": f["media_type"],
        "size": f["size"],
        "caption": "",
        "source_path": str(f["path"]),  # interno, no se envía al cliente
    })

# Emitir en el resultado
_emit({
    "event": "result",
    "final_response": final_response,
    "messages": messages,
    "tool_calls": tool_calls,
    "tokens_used": ...,
    "iterations": ...,
    "engine": "hermes-agent",
    "response_attachments": response_attachments,
})
```

**Funciones auxiliares:**

```python
ALLOWED_RESPONSE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".mp3", ".aac", ".ogg", ".wav", ".m4a", ".flac",
    ".mp4", ".webm", ".mov", ".avi", ".mkv",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".csv", ".json", ".md", ".xml", ".html", ".zip",
}

MAX_RESPONSE_FILE_SIZE = 100 * 1024 * 1024   # 100 MB por archivo
MAX_RESPONSE_FILES = 20                       # máximo 20 archivos por tarea


def _snapshot_directory(directory: Path) -> dict[str, dict]:
    """Retorna {relative_path: {path, name, size, mtime}} para todos los archivos."""
    snapshot = {}
    if not directory.exists():
        return snapshot
    for path in directory.rglob("*"):
        if path.is_file():
            stat = path.stat()
            rel = str(path.relative_to(directory))
            snapshot[rel] = {
                "path": path,
                "name": path.name,
                "extension": path.suffix.lower(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
    return snapshot


def _diff_snapshots(pre: dict, post: dict) -> list[dict]:
    """Retorna archivos nuevos o modificados (comparando mtime + size)."""
    result = []
    for rel, info in post.items():
        if rel not in pre:
            result.append(info)  # archivo nuevo
        elif info["mtime"] != pre[rel]["mtime"] or info["size"] != pre[rel]["size"]:
            result.append(info)  # archivo modificado
    return result


def _is_allowed(file_info: dict) -> bool:
    return file_info.get("extension", "") in ALLOWED_RESPONSE_EXTENSIONS
```

### 3.2 `hermes_runtime.py` — Transportar archivos generados

Agregar campo a `RuntimeExecutionResult`:

```python
@dataclass
class RuntimeExecutionResult:
    final_response: str
    messages: list[dict]
    tool_calls: list[dict]
    tokens_used: int
    iterations: int
    engine: str
    response_attachments: list[dict]  # ← NUEVO
```

En `_run_real()`, leer del `final_result`:

```python
return RuntimeExecutionResult(
    final_response=raw_response,
    messages=list(final_result.get("messages") or []),
    tool_calls=list(final_result.get("tool_calls") or []),
    tokens_used=int(final_result.get("tokens_used") or 0),
    iterations=int(final_result.get("iterations") or 0),
    engine=str(final_result.get("engine") or "hermes-agent"),
    response_attachments=list(final_result.get("response_attachments") or []),
)
```

### 3.3 `agent_supervisor.py` — Persistir y emitir

En `_execute_task`, después de que la ejecución termina exitosamente:

```python
# Guardar response_attachments en task.metadata_json
response_attachments = getattr(execution, "response_attachments", [])
if response_attachments:
    metadata = dict(task.metadata_json or {})
    # Filtrar source_path antes de persistir (no se envía al cliente)
    metadata["response_attachments"] = [
        {k: v for k, v in att.items() if k != "source_path"}
        for att in response_attachments
    ]
    task.metadata_json = metadata
    await session.commit()

# Incluir metadata en el evento task.completed
await self.event_broker.publish({
    "type": "task.completed",
    "task_id": task_id,
    "agent_id": task.agent_id,
    "response": execution.final_response,
    "metadata": task.metadata_json or {},  # ← NUEVO
})
```

### 3.4 `attachments.py` — Sin cambios necesarios

Como los archivos se copian a `uploads/` en el paso 3.1, el endpoint existente ya los encuentra sin modificación:

```
GET /api/agents/{agent_id}/attachments/{file_id}
Authorization: Bearer {token}
```

> **Nota:** Los archivos del agente conviven con los del usuario en `uploads/`, diferenciados por `file_id` (UUID único). El origen (usuario vs agente) se distingue en `task.metadata_json` por la clave (`attachments` vs `response_attachments`).

---

## 4. Eventos WebSocket

El evento `task.completed` ahora incluye `metadata`:

```json
{
  "type": "task.completed",
  "task_id": "task_123",
  "agent_id": "agent_456",
  "response": "Aquí está el reporte...",
  "metadata": {
    "response_attachments": [
      {
        "file_id": "bbb-222",
        "filename": "reporte.xlsx",
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size": 45230,
        "caption": ""
      }
    ]
  }
}
```

**Notar:** `source_path` no se incluye en el evento ni en la API REST. Es estrictamente interno.

---

## 5. Límites y limpieza

### 5.1 Límites por tarea

| Límite | Valor | Ubicación |
|--------|-------|-----------|
| Tamaño máximo por archivo | 100 MB | `hermes_task_runner.py` |
| Cantidad máxima de archivos por tarea | 20 | `hermes_task_runner.py` |
| Extensiones permitidas | ver `ALLOWED_RESPONSE_EXTENSIONS` | `hermes_task_runner.py` |

### 5.2 Limpieza (futuro)

Los archivos copiados a `uploads/` persisten hasta que:
- El usuario los elimine explícitamente (`DELETE /api/agents/{agent_id}/attachments/{file_id}`)
- El agente sea archivado y eliminado (cleanup del workspace)

No se implementa auto-cleanup automático en esta versión.

---

## 6. Compatibilidad

- Si el agente no genera archivos, `response_attachments` se omite de `metadata` (o es array vacío)
- Si un cliente antiguo no lee `metadata` en el evento `task.completed`, simplemente no ve los adjuntos — no hay breaking change
- La clave `response_attachments` nunca colisiona con `attachments` (que ya se usa para uploads del usuario)
- MIME types soportados por la app: imágenes (`image/*`), audio (`audio/*`), video (`video/*`), y cualquier otro tipo como archivo descargable

---

## 7. Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `backend/hermeshq/scripts/hermes_task_runner.py` | Snapshot de `work/`, diff, copia a `uploads/`, emitir `response_attachments` |
| `backend/hermeshq/services/hermes_runtime.py` | Campo `response_attachments` en `RuntimeExecutionResult`, leer del resultado |
| `backend/hermeshq/services/agent_supervisor.py` | Persistir en `task.metadata_json`, incluir `metadata` en evento `task.completed` |
| `backend/hermeshq/routers/attachments.py` | Sin cambios (ya busca en `uploads/`) |

**Sin cambios necesarios en:** `attachments.py`, modelos, migraciones, ni frontend web.

---

## 8. Casos de borde

| Caso | Comportamiento |
|------|----------------|
| Agente no genera archivos | `response_attachments` = `[]` o se omite |
| Agente genera archivo no permitido (ej. `.exe`) | Se filtra, no se incluye |
| Agente genera archivo > 100 MB | Se filtra, no se incluye |
| Agente genera > 20 archivos | Solo los primeros 20 se incluyen |
| Agente sobrescribe archivo existente en `work/` | Se detecta como modificado, se incluye con nuevo `file_id` |
| El mismo archivo aparece en `attachments` y `response_attachments` | No hay conflicto — son listas independientes con `file_id` distintos |
| Archivo generado en subdirectorio de `work/` | Se incluye con `filename` = nombre del archivo (sin ruta) |
