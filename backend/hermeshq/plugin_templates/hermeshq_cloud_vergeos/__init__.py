from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _api_request(method: str, path: str, payload: dict | None = None) -> str:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return json.dumps({"success": False, "error": "HermesHQ cloud broker is not configured in this runtime"})

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
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8") or json.dumps({"success": True})
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


def _cloud_handler(action: str, *, required: list[str] | None = None, param_map: dict | None = None):
    def handler(args, **_kwargs):
        params = dict(args or {})
        for field in required or []:
            if params.get(field) in (None, ""):
                return json.dumps({"success": False, "error": f"'{field}' is required"})
        if param_map:
            mapped = {}
            for target, source in param_map.items():
                if params.get(source) is not None:
                    mapped[target] = params[source]
            params = mapped if mapped else params
        return _api_request("POST", "/control/cloud/request", {"action": action, "params": params})

    return handler


def register(ctx) -> None:  # noqa: ANN001
    specs = [
        {
            "name": "vergeos_list_vms",
            "description": "List the VMs of your cloud tenant (optionally filter with VergeOS OData-like expressions, e.g. \"name ct 'web'\"). Snapshots are excluded by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "VergeOS filter expression (eq/ne/ct/rx...)"},
                    "include_snapshots": {"type": "boolean"},
                },
            },
            "handler": _cloud_handler("vergeos_list_vms"),
            "emoji": "📋",
        },
        {
            "name": "vergeos_get_vm",
            "description": "Get full details of one VM of your tenant by id.",
            "parameters": {"type": "object", "properties": {"vm_id": {"type": "string"}}, "required": ["vm_id"]},
            "handler": _cloud_handler("vergeos_get_vm", required=["vm_id"]),
            "emoji": "🔍",
        },
        {
            "name": "vergeos_list_networks",
            "description": "List the virtual networks (vnets) of your tenant.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _cloud_handler("vergeos_list_networks"),
            "emoji": "🌐",
        },
        {
            "name": "vergeos_usage",
            "description": "Usage summary of your tenant: VM counts, running VMs, snapshots, allocated CPU and RAM.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _cloud_handler("vergeos_usage"),
            "emoji": "📊",
        },
        {
            "name": "vergeos_list_snapshots",
            "description": "List snapshots of your tenant (optionally for a single VM).",
            "parameters": {"type": "object", "properties": {"vm_id": {"type": "string"}}},
            "handler": _cloud_handler("vergeos_list_snapshots"),
            "emoji": "📸",
        },
        {
            "name": "vergeos_events",
            "description": "Recent events of your tenant (optionally filtered).",
            "parameters": {
                "type": "object",
                "properties": {"filter": {"type": "string"}, "limit": {"type": "integer"}},
            },
            "handler": _cloud_handler("vergeos_events"),
            "emoji": "🗓️",
        },
        {
            "name": "vergeos_vm_power",
            "description": "Power operation on a VM: on, off, reset or shutdown (graceful).",
            "parameters": {
                "type": "object",
                "properties": {
                    "vm_id": {"type": "string"},
                    "state": {"type": "string", "enum": ["on", "off", "reset", "shutdown"]},
                },
                "required": ["vm_id", "state"],
            },
            "handler": _cloud_handler("vergeos_vm_power", required=["vm_id", "state"]),
            "emoji": "⚡",
        },
        {
            "name": "vergeos_snapshot_create",
            "description": "Create a named snapshot of a VM.",
            "parameters": {
                "type": "object",
                "properties": {"vm_id": {"type": "string"}, "name": {"type": "string"}},
                "required": ["vm_id", "name"],
            },
            "handler": _cloud_handler("vergeos_snapshot_create", required=["vm_id", "name"]),
            "emoji": "💾",
        },
        {
            "name": "vergeos_snapshot_restore",
            "description": "Restore a VM from one of its snapshots.",
            "parameters": {
                "type": "object",
                "properties": {"vm_id": {"type": "string"}, "snapshot_id": {"type": "string"}},
                "required": ["vm_id", "snapshot_id"],
            },
            "handler": _cloud_handler("vergeos_snapshot_restore", required=["vm_id", "snapshot_id"]),
            "emoji": "⏪",
        },
        {
            "name": "vergeos_vm_create",
            "description": "Create a new VM in your tenant (name, cpu_cores 1-64, ram_mb 256-1048576, os_family, boot_order).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cpu_cores": {"type": "integer"},
                    "ram_mb": {"type": "integer"},
                    "os_family": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "cpu_cores", "ram_mb"],
            },
            "handler": _cloud_handler("vergeos_vm_create", required=["name", "cpu_cores", "ram_mb"]),
            "emoji": "➕",
        },
        {
            "name": "vergeos_vm_resize",
            "description": "Resize a VM (cpu_cores and/or ram_mb). Applies after reboot for running VMs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vm_id": {"type": "string"},
                    "cpu_cores": {"type": "integer"},
                    "ram_mb": {"type": "integer"},
                },
                "required": ["vm_id"],
            },
            "handler": _cloud_handler("vergeos_vm_resize", required=["vm_id"]),
            "emoji": "📐",
        },
        {
            "name": "vergeos_report_inventory",
            "description": "Generate an inventory report of your tenant: markdown table plus CSV attachment.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _cloud_handler("vergeos_report_inventory"),
            "emoji": "📄",
        },
        {
            "name": "vergeos_report_usage",
            "description": "Generate a usage report of your tenant (allocated CPU/RAM, running VMs, snapshots).",
            "parameters": {"type": "object", "properties": {}},
            "handler": _cloud_handler("vergeos_report_usage"),
            "emoji": "📈",
        },
        {
            "name": "vergeos_report_snapshots",
            "description": "Generate a snapshots report of your tenant: markdown plus CSV attachment.",
            "parameters": {"type": "object", "properties": {}},
            "handler": _cloud_handler("vergeos_report_snapshots"),
            "emoji": "🗂️",
        },
    ]

    for spec in specs:
        ctx.register_tool(
            name=spec["name"],
            toolset="hermeshq_cloud_vergeos",
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
