from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


def _api_request(method: str, path: str, payload: dict | None = None) -> str:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return json.dumps({"success": False, "error": "HermesHQ internal control is not configured in this runtime"})

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method.upper(),
        headers={
            "Content-Type": "application/json",
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return body or json.dumps({"success": True})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        return json.dumps(
            {
                "success": False,
                "status_code": exc.code,
                "error": parsed.get("detail") or parsed.get("error") or body or str(exc),
            }
        )
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return json.dumps({"success": False, "error": str(exc)})


def _check_requirements():
    return bool(
        os.environ.get("HERMESHQ_INTERNAL_API_URL")
        and os.environ.get("HERMESHQ_AGENT_ID")
        and os.environ.get("HERMESHQ_AGENT_TOKEN")
    )


def _payload_handler(method: str, path_builder, *, required: list[str] | None = None, payload_builder=None):
    def handler(args, **_kwargs):
        required_fields = required or []
        for field in required_fields:
            value = args.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                return json.dumps({"success": False, "error": f"'{field}' is required"})
        payload = payload_builder(args) if payload_builder else dict(args or {})
        return _api_request(method, path_builder(args), payload if method.upper() != "GET" else None)

    return handler


def _delete_handler(path_builder, *, required: list[str]):
    def handler(args, **_kwargs):
        for field in required:
            value = args.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                return json.dumps({"success": False, "error": f"'{field}' is required"})
        return _api_request("DELETE", path_builder(args))

    return handler


ACCIONES_QUE_PIDEN_CONFIRMACION = frozenset(
    {
        "hq_control_delete_user",
        "hq_control_delete_provider",
        "hq_control_delete_secret",
        "hq_control_delete_schedule",
        "hq_control_create_schedule",
        "hq_control_update_schedule",
        "hq_control_archive_agent",
        "hq_control_uninstall_integration",
        "hq_control_delete_integration_draft",
    }
)
_VIGENCIA_CONFIRMACION = 1800
_PALABRAS_SI = {
    "si",
    "sip",
    "ok",
    "okay",
    "dale",
    "confirmo",
    "confirmado",
    "adelante",
    "procede",
    "hazlo",
    "apruebo",
    "yes",
}
_PALABRAS_RELLENO = {
    "de",
    "acuerdo",
    "por",
    "favor",
    "porfa",
    "lo",
    "la",
    "el",
    "ahora",
    "elimina",
    "eliminalo",
    "borra",
    "borralo",
    "crea",
    "crealo",
    "archiva",
    "archivalo",
    "actualiza",
    "actualizalo",
    "desinstala",
}
_PALABRAS_QUE_ANULAN = {
    "no",
    "nunca",
    "tampoco",
    "espera",
    "mejor",
    "cancela",
    "cancelar",
    "cambia",
    "pero",
    "todavia",
    "aun",
}


def _payload_tarea() -> dict:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _id_tarea() -> str:
    return str(_payload_tarea().get("task_id") or "")


def _usuario_confirma() -> bool:
    texto = str(_payload_tarea().get("mensaje") or "").strip()
    if not texto or len(texto) > 60 or "?" in texto:
        return False
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    palabras = re.findall(r"[a-z]+", plano)
    if not palabras or any(p in _PALABRAS_QUE_ANULAN for p in palabras) or not any(p in _PALABRAS_SI for p in palabras):
        return False
    return all(p in _PALABRAS_SI or p in _PALABRAS_RELLENO for p in palabras)


def _pendiente_path():
    from pathlib import Path

    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".control_pendiente.json"


def _huella_accion(nombre: str, args: dict) -> str:
    limpio = {k: v for k, v in (args or {}).items() if k != "confirmado_por_el_usuario"}
    return hashlib.sha256(
        json.dumps([nombre, limpio], sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _con_confirmacion(nombre: str, handler):

    def envuelto(args, **kwargs):
        args = dict(args or {})
        huella = _huella_accion(nombre, args)
        ruta = _pendiente_path()
        if args.get("confirmado_por_el_usuario"):
            try:
                pendiente = json.loads(ruta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pendiente = {}
            vigente = time.time() - float(pendiente.get("ts") or 0) <= _VIGENCIA_CONFIRMACION
            if (
                pendiente.get("huella") == huella
                and pendiente.get("task") != _id_tarea()
                and vigente
                and _usuario_confirma()
            ):
                resultado = handler({k: v for k, v in args.items() if k != "confirmado_por_el_usuario"}, **kwargs)
                try:
                    ruta.unlink()
                except OSError:
                    pass
                return resultado
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(
                json.dumps({"huella": huella, "task": _id_tarea(), "ts": time.time(), "accion": nombre}),
                encoding="utf-8",
            )
        except OSError:
            pass
        limpio = {k: v for k, v in args.items() if k != "confirmado_por_el_usuario"}
        return json.dumps(
            {
                "success": False,
                "requiere_confirmacion": True,
                "accion": nombre,
                "parametros": limpio,
                "error": "No se ejecutó: esta acción necesita la confirmación del usuario en un mensaje posterior.",
                "siguiente": "Muéstrale al usuario exactamente qué vas a hacer (la acción y sus parámetros), pregunta si "
                "confirma y termina tu respuesta. Cuando responda «sí», vuelve a llamar con los MISMOS argumentos y "
                "confirmado_por_el_usuario=true. No lo ejecutes sin su «sí».",
            },
            ensure_ascii=False,
        )

    return envuelto


def register(ctx):
    specs = [
        {
            "name": "hq_control_list_agents",
            "description": "List HermesHQ agents, including archived agents when requested.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_archived": {
                        "type": "boolean",
                        "description": "Set true to include archived agents in the result.",
                    }
                },
            },
            "handler": _payload_handler(
                "GET",
                lambda args: f"/control/agents?include_archived={'true' if args.get('include_archived') else 'false'}",
            ),
            "emoji": "🛰️",
        },
        {
            "name": "hq_control_create_agent",
            "description": "Create a new HermesHQ agent. Provide the same payload fields used by the admin UI, including node_id, friendly_name or name, runtime_profile, provider/model overrides if needed, and optional integration_configs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "friendly_name": {"type": "string"},
                    "name": {"type": "string"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "run_mode": {"type": "string"},
                    "runtime_profile": {"type": "string"},
                    "hermes_version": {"type": "string"},
                    "model": {"type": "string"},
                    "provider": {"type": "string"},
                    "api_key_ref": {"type": "string"},
                    "base_url": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "soul_md": {"type": "string"},
                    "enabled_toolsets": {"type": "array", "items": {"type": "string"}},
                    "disabled_toolsets": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "integration_configs": {"type": "object"},
                    "team_tags": {"type": "array", "items": {"type": "string"}},
                    "supervisor_agent_id": {"type": "string"},
                    "runtime_type": {"type": "string", "enum": ["hermes", "pi"]},
                    "pi_config": {"type": "object"},
                    "permission_policy_id": {"type": "string"},
                },
                "required": ["node_id"],
            },
            "handler": _payload_handler("POST", lambda _args: "/control/agents", required=["node_id"]),
            "emoji": "🧩",
        },
        {
            "name": "hq_control_update_agent",
            "description": "Update an existing HermesHQ agent by id. Pass agent_id plus any editable agent fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "friendly_name": {"type": "string"},
                    "name": {"type": "string"},
                    "slug": {"type": "string"},
                    "description": {"type": "string"},
                    "run_mode": {"type": "string"},
                    "runtime_profile": {"type": "string"},
                    "hermes_version": {"type": "string"},
                    "model": {"type": "string"},
                    "provider": {"type": "string"},
                    "api_key_ref": {"type": "string"},
                    "base_url": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "soul_md": {"type": "string"},
                    "enabled_toolsets": {"type": "array", "items": {"type": "string"}},
                    "disabled_toolsets": {"type": "array", "items": {"type": "string"}},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "integration_configs": {"type": "object"},
                    "team_tags": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string"},
                    "supervisor_agent_id": {"type": "string"},
                    "runtime_type": {"type": "string", "enum": ["hermes", "pi"]},
                    "pi_config": {"type": "object"},
                    "permission_policy_id": {"type": "string"},
                    "permission_policy_ids": {"type": "array", "items": {"type": "string"}},
                    "desktop_access_enabled": {"type": "boolean"},
                },
                "required": ["agent_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/agents/{args.get('agent_id')}",
                required=["agent_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "agent_id"
                },
            ),
            "emoji": "🛠️",
        },
        {
            "name": "hq_control_archive_agent",
            "description": "Archive an agent by id. Archived agents keep logs and audit history but leave active operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["agent_id"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/agents/{args.get('agent_id')}/archive",
                required=["agent_id"],
                payload_builder=lambda args: {"reason": args.get("reason")},
            ),
            "emoji": "🗄️",
        },
        {
            "name": "hq_control_agent_runtime",
            "description": "Start, stop, or restart an agent runtime by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["start", "stop", "restart"]},
                },
                "required": ["agent_id", "action"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/agents/{args.get('agent_id')}/runtime/{args.get('action')}",
                required=["agent_id", "action"],
            ),
            "emoji": "⚙️",
        },
        {
            "name": "hq_control_list_users",
            "description": "List HermesHQ users and their assigned agents.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/users"),
            "emoji": "👥",
        },
        {
            "name": "hq_control_create_user",
            "description": "Create a HermesHQ user with password, role, active state, and optional assigned_agent_ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "display_name": {"type": "string"},
                    "password": {"type": "string"},
                    "role": {"type": "string", "enum": ["admin", "user"]},
                    "is_active": {"type": "boolean"},
                    "assigned_agent_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["username", "display_name", "password"],
            },
            "handler": _payload_handler(
                "POST", lambda _args: "/control/users", required=["username", "display_name", "password"]
            ),
            "emoji": "➕",
        },
        {
            "name": "hq_control_update_user",
            "description": "Update a HermesHQ user by id. Supports display_name, password, role, is_active, and assigned_agent_ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "password": {"type": "string"},
                    "role": {"type": "string", "enum": ["admin", "user"]},
                    "is_active": {"type": "boolean"},
                    "assigned_agent_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["user_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/users/{args.get('user_id')}",
                required=["user_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "user_id"
                },
            ),
            "emoji": "✏️",
        },
        {
            "name": "hq_control_delete_user",
            "description": "Delete a HermesHQ user by id.",
            "parameters": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
            "handler": _delete_handler(lambda args: f"/control/users/{args.get('user_id')}", required=["user_id"]),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_list_providers",
            "description": "List HermesHQ provider definitions.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/providers"),
            "emoji": "🔌",
        },
        {
            "name": "hq_control_create_provider",
            "description": "Create a provider definition in HermesHQ.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "name": {"type": "string"},
                    "runtime_provider": {"type": "string"},
                    "auth_type": {"type": "string"},
                    "base_url": {"type": "string"},
                    "default_model": {"type": "string"},
                    "description": {"type": "string"},
                    "docs_url": {"type": "string"},
                    "secret_placeholder": {"type": "string"},
                    "supports_secret_ref": {"type": "boolean"},
                    "supports_custom_base_url": {"type": "boolean"},
                    "enabled": {"type": "boolean"},
                    "sort_order": {"type": "integer"},
                },
                "required": ["slug", "name", "runtime_provider"],
            },
            "handler": _payload_handler(
                "POST", lambda _args: "/control/providers", required=["slug", "name", "runtime_provider"]
            ),
            "emoji": "🧱",
        },
        {
            "name": "hq_control_update_provider",
            "description": "Update a provider definition by slug.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_slug": {"type": "string"},
                    "name": {"type": "string"},
                    "base_url": {"type": "string"},
                    "default_model": {"type": "string"},
                    "description": {"type": "string"},
                    "docs_url": {"type": "string"},
                    "secret_placeholder": {"type": "string"},
                    "supports_secret_ref": {"type": "boolean"},
                    "supports_custom_base_url": {"type": "boolean"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["provider_slug"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/providers/{args.get('provider_slug')}",
                required=["provider_slug"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "provider_slug"
                },
            ),
            "emoji": "🔧",
        },
        {
            "name": "hq_control_delete_provider",
            "description": "Delete a provider definition by slug.",
            "parameters": {
                "type": "object",
                "properties": {"provider_slug": {"type": "string"}},
                "required": ["provider_slug"],
            },
            "handler": _delete_handler(
                lambda args: f"/control/providers/{args.get('provider_slug')}", required=["provider_slug"]
            ),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_list_secrets",
            "description": "List HermesHQ secrets metadata. Secret values are never returned.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/secrets"),
            "emoji": "🔐",
        },
        {
            "name": "hq_control_create_secret",
            "description": "Create a secret in the HermesHQ vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "provider": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
            },
            "handler": _payload_handler("POST", lambda _args: "/control/secrets", required=["name", "value"]),
            "emoji": "🗝️",
        },
        {
            "name": "hq_control_update_secret",
            "description": "Update a secret metadata or value by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "secret_id": {"type": "string"},
                    "provider": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["secret_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/secrets/{args.get('secret_id')}",
                required=["secret_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "secret_id"
                },
            ),
            "emoji": "✏️",
        },
        {
            "name": "hq_control_delete_secret",
            "description": "Delete a secret by id.",
            "parameters": {
                "type": "object",
                "properties": {"secret_id": {"type": "string"}},
                "required": ["secret_id"],
            },
            "handler": _delete_handler(
                lambda args: f"/control/secrets/{args.get('secret_id')}", required=["secret_id"]
            ),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_list_integrations",
            "description": "List managed integrations available in this HermesHQ instance.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/integrations"),
            "emoji": "🧰",
        },
        {
            "name": "hq_control_install_integration",
            "description": "Install a managed integration package by slug.",
            "parameters": {
                "type": "object",
                "properties": {"integration_slug": {"type": "string"}},
                "required": ["integration_slug"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/integrations/{args.get('integration_slug')}/install",
                required=["integration_slug"],
            ),
            "emoji": "📦",
        },
        {
            "name": "hq_control_uninstall_integration",
            "description": "Uninstall a managed integration package by slug.",
            "parameters": {
                "type": "object",
                "properties": {"integration_slug": {"type": "string"}},
                "required": ["integration_slug"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/integrations/{args.get('integration_slug')}/uninstall",
                required=["integration_slug"],
            ),
            "emoji": "📤",
        },
        {
            "name": "hq_control_configure_agent_integration",
            "description": "Enable or disable an installed integration for an agent and store the agent-level config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "integration_slug": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "config": {"type": "object"},
                },
                "required": ["agent_id", "integration_slug"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/agents/{args.get('agent_id')}/integrations/{args.get('integration_slug')}",
                required=["agent_id", "integration_slug"],
                payload_builder=lambda args: {"enabled": args.get("enabled", True), "config": args.get("config") or {}},
            ),
            "emoji": "🔗",
        },
        {
            "name": "hq_control_test_agent_integration",
            "description": "Run the managed integration health check for an agent using the provided config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "integration_slug": {"type": "string"},
                    "config": {"type": "object"},
                },
                "required": ["agent_id", "integration_slug"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/agents/{args.get('agent_id')}/integrations/{args.get('integration_slug')}/test",
                required=["agent_id", "integration_slug"],
                payload_builder=lambda args: {"enabled": True, "config": args.get("config") or {}},
            ),
            "emoji": "🩺",
        },
        {
            "name": "hq_control_list_schedules",
            "description": "List scheduled tasks across the instance.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/scheduled-tasks"),
            "emoji": "🗓️",
        },
        {
            "name": "hq_control_create_schedule",
            "description": "Create a scheduled task for an agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "name": {"type": "string"},
                    "cron_expression": {"type": "string"},
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["agent_id", "name", "cron_expression", "prompt"],
            },
            "handler": _payload_handler(
                "POST",
                lambda _args: "/control/scheduled-tasks",
                required=["agent_id", "name", "cron_expression", "prompt"],
            ),
            "emoji": "⏱️",
        },
        {
            "name": "hq_control_update_schedule",
            "description": "Update a scheduled task by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scheduled_task_id": {"type": "string"},
                    "name": {"type": "string"},
                    "cron_expression": {"type": "string"},
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["scheduled_task_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/scheduled-tasks/{args.get('scheduled_task_id')}",
                required=["scheduled_task_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "scheduled_task_id"
                },
            ),
            "emoji": "📝",
        },
        {
            "name": "hq_control_delete_schedule",
            "description": "Delete a scheduled task by id.",
            "parameters": {
                "type": "object",
                "properties": {"scheduled_task_id": {"type": "string"}},
                "required": ["scheduled_task_id"],
            },
            "handler": _delete_handler(
                lambda args: f"/control/scheduled-tasks/{args.get('scheduled_task_id')}", required=["scheduled_task_id"]
            ),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_list_integration_drafts",
            "description": "List integration factory drafts available for editing and publication.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/integration-drafts"),
            "emoji": "🏗️",
        },
        {
            "name": "hq_control_create_integration_draft",
            "description": "Create a new managed integration draft scaffold. Use template 'rest-api' for a working HTTP integration skeleton or 'empty' for a minimal package.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "template": {"type": "string", "enum": ["rest-api", "empty"]},
                    "version": {"type": "string"},
                },
                "required": ["slug", "name"],
            },
            "handler": _payload_handler("POST", lambda _args: "/control/integration-drafts", required=["slug", "name"]),
            "emoji": "🧱",
        },
        {
            "name": "hq_control_get_integration_draft",
            "description": "Get integration draft metadata, status, files, and current scaffold details by draft id.",
            "parameters": {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            "handler": _payload_handler(
                "GET", lambda args: f"/control/integration-drafts/{args.get('draft_id')}", required=["draft_id"]
            ),
            "emoji": "📘",
        },
        {
            "name": "hq_control_update_integration_draft",
            "description": "Update integration draft metadata such as name, description, version, or notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "version": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["draft_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/integration-drafts/{args.get('draft_id')}",
                required=["draft_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "draft_id"
                },
            ),
            "emoji": "📝",
        },
        {
            "name": "hq_control_get_integration_draft_file",
            "description": "Read a draft file by relative path, for example manifest.yaml or plugin/__init__.py.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["draft_id", "path"],
            },
            "handler": _payload_handler(
                "GET",
                lambda args: (
                    f"/control/integration-drafts/{args.get('draft_id')}/file?path={urllib.parse.quote(str(args.get('path') or ''))}"
                ),
                required=["draft_id", "path"],
            ),
            "emoji": "📄",
        },
        {
            "name": "hq_control_put_integration_draft_file",
            "description": "Create or replace a draft file by relative path with the provided content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["draft_id", "path", "content"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: (
                    f"/control/integration-drafts/{args.get('draft_id')}/file?path={urllib.parse.quote(str(args.get('path') or ''))}"
                ),
                required=["draft_id", "path", "content"],
                payload_builder=lambda args: {"content": args.get("content", "")},
            ),
            "emoji": "✍️",
        },
        {
            "name": "hq_control_delete_integration_draft_file",
            "description": "Delete a draft file by relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["draft_id", "path"],
            },
            "handler": _delete_handler(
                lambda args: (
                    f"/control/integration-drafts/{args.get('draft_id')}/file?path={urllib.parse.quote(str(args.get('path') or ''))}"
                ),
                required=["draft_id", "path"],
            ),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_validate_integration_draft",
            "description": "Run structural validation for an integration draft and return checks for manifest, plugin, and Python files.",
            "parameters": {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/integration-drafts/{args.get('draft_id')}/validate",
                required=["draft_id"],
            ),
            "emoji": "🩺",
        },
        {
            "name": "hq_control_publish_integration_draft",
            "description": "Publish an integration draft into Managed integrations and install it at instance level.",
            "parameters": {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/integration-drafts/{args.get('draft_id')}/publish",
                required=["draft_id"],
            ),
            "emoji": "🚀",
        },
        {
            "name": "hq_control_delete_integration_draft",
            "description": "Delete an integration draft and its scaffold files.",
            "parameters": {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            "handler": _delete_handler(
                lambda args: f"/control/integration-drafts/{args.get('draft_id')}", required=["draft_id"]
            ),
            "emoji": "🧹",
        },
        {
            "name": "hq_control_list_policies",
            "description": "List permission policies available for Pi agents.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda _args: "/control/permission-policies"),
            "emoji": "🛡️",
        },
        {
            "name": "hq_control_create_policy",
            "description": "Create a new permission policy for Pi agents. Specify name, tool_rules, path_rules, command_rules, network_rules, and approval_rules as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tool_rules": {"type": "object"},
                    "path_rules": {"type": "object"},
                    "command_rules": {"type": "object"},
                    "network_rules": {"type": "object"},
                    "approval_rules": {"type": "object"},
                },
                "required": ["name"],
            },
            "handler": _payload_handler("POST", lambda _args: "/control/permission-policies", required=["name"]),
            "emoji": "📜",
        },
        {
            "name": "hq_control_update_policy",
            "description": "Update a permission policy by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "policy_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tool_rules": {"type": "object"},
                    "path_rules": {"type": "object"},
                    "command_rules": {"type": "object"},
                    "network_rules": {"type": "object"},
                    "approval_rules": {"type": "object"},
                },
                "required": ["policy_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/permission-policies/{args.get('policy_id')}",
                required=["policy_id"],
                payload_builder=lambda args: {
                    key: value for key, value in dict(args or {}).items() if key != "policy_id"
                },
            ),
            "emoji": "✏️",
        },
        {
            "name": "hq_control_delete_policy",
            "description": "Delete a permission policy by id. System policies cannot be deleted.",
            "parameters": {
                "type": "object",
                "properties": {"policy_id": {"type": "string"}},
                "required": ["policy_id"],
            },
            "handler": _delete_handler(
                lambda args: f"/control/permission-policies/{args.get('policy_id')}", required=["policy_id"]
            ),
            "emoji": "🗑️",
        },
        {
            "name": "hq_control_assign_policy",
            "description": "Assign a primary permission policy to any agent by agent_id and policy_id. Pass empty policy_id to remove it. For chained policies use hq_control_update_agent with permission_policy_ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "policy_id": {"type": "string"},
                },
                "required": ["agent_id"],
            },
            "handler": _payload_handler(
                "PUT",
                lambda args: f"/control/agents/{args.get('agent_id')}",
                required=["agent_id"],
                payload_builder=lambda args: {"permission_policy_id": args.get("policy_id") or None},
            ),
            "emoji": "🔗",
        },
        {
            "name": "hq_control_test_permission",
            "description": "Test whether a tool call, path, or command would be allowed by an agent's permission policies (own plus any inherited master policies when delegated).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "tool": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["agent_id", "tool"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: f"/control/agents/{args.get('agent_id')}/test-permission",
                required=["agent_id", "tool"],
                payload_builder=lambda args: {"tool": args.get("tool"), "input": args.get("input") or {}},
            ),
            "emoji": "🧪",
        },
        {
            "name": "hq_control_list_devices",
            "description": "List enrolled Hermes Desktop devices with status, heartbeat, and last sync. Filter by agent_id optionally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                },
            },
            "handler": _payload_handler(
                "GET",
                lambda args: (
                    "/control/enrollment/devices" + (("?agent_id=" + args["agent_id"]) if args.get("agent_id") else "")
                ),
            ),
            "emoji": "💻",
        },
        {
            "name": "hq_control_enroll_device",
            "description": "Enroll a new Hermes Desktop device for an agent. Returns a one-time enrollment token plus the exact activation command to give the user. guard_fail_mode: fail-open | fail-closed | fail-grace:<seconds>.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "device_name": {"type": "string"},
                    "guard_fail_mode": {"type": "string"},
                },
                "required": ["agent_id", "device_name"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: "/control/enrollment/devices",
                required=["agent_id", "device_name"],
                payload_builder=lambda args: {
                    "agent_id": args.get("agent_id"),
                    "device_name": args.get("device_name"),
                    "guard_fail_mode": args.get("guard_fail_mode") or "fail-open",
                },
            ),
            "emoji": "📲",
        },
        {
            "name": "hq_control_revoke_device",
            "description": "Revoke an enrolled device by device_id. The device's guard blocks all tools immediately on the next evaluation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                },
                "required": ["device_id"],
            },
            "handler": _payload_handler(
                "DELETE",
                lambda args: f"/control/enrollment/devices/{args.get('device_id')}",
                required=["device_id"],
            ),
            "emoji": "🚫",
        },
        {
            "name": "hq_control_list_agent_skills",
            "description": "List installed skills of any agent (name, description, path, managed flag). Use before transferring a skill between agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                },
                "required": ["agent_id"],
            },
            "handler": _payload_handler(
                "GET",
                lambda args: f"/control/agents/{args.get('agent_id')}/skills",
                required=["agent_id"],
            ),
            "emoji": "🗂️",
        },
        {
            "name": "hq_control_transfer_skill",
            "description": "Copy a skill from one agent to another (validated: text files only, size-capped, path-safe). Collision requires overwrite=true. Returns the copied skill summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_agent_id": {"type": "string"},
                    "target_agent_id": {"type": "string"},
                    "skill_path": {"type": "string"},
                    "target_name": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["source_agent_id", "target_agent_id", "skill_path"],
            },
            "handler": _payload_handler(
                "POST",
                lambda args: "/control/skills/transfer",
                required=["source_agent_id", "target_agent_id", "skill_path"],
                payload_builder=lambda args: {
                    "source_agent_id": args.get("source_agent_id"),
                    "target_agent_id": args.get("target_agent_id"),
                    "skill_path": args.get("skill_path"),
                    "target_name": args.get("target_name"),
                    "overwrite": bool(args.get("overwrite")),
                },
            ),
            "emoji": "📤",
        },
        {
            "name": "hq_control_list_ssh_destinations",
            "description": "List active SSH destinations (host, port, listen_port, allowed agent). Authorized agents reach them from tasks as ssh-relay:<listen_port>.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _payload_handler("GET", lambda args: "/control/ssh/destinations"),
            "emoji": "🔐",
        },
    ]

    for spec in specs:
        if spec["name"] in ACCIONES_QUE_PIDEN_CONFIRMACION:
            spec["handler"] = _con_confirmacion(spec["name"], spec["handler"])
            propiedades = spec["parameters"].setdefault("properties", {})
            propiedades["confirmado_por_el_usuario"] = {
                "type": "boolean",
                "description": "true solo cuando el usuario ya confirmó, en un mensaje posterior, la acción mostrada.",
            }
            spec["description"] = (
                f"{spec['description']} Pide la confirmación del usuario: la primera llamada no ejecuta nada."
            )
        ctx.register_tool(
            name=spec["name"],
            toolset="hermeshq_control",
            schema={
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
            handler=spec["handler"],
            check_fn=_check_requirements,
            description=spec["description"],
            emoji=spec["emoji"],
        )
