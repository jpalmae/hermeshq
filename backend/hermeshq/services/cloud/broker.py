from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.agent import Agent
from hermeshq.models.cloud import CloudBinding, CloudPlatform, CloudTenant
from hermeshq.services.cloud.vergeos_client import VergeOSClient, VergeOSClientError
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

VIEWER_ACTIONS = {
    "vergeos_list_vms",
    "vergeos_get_vm",
    "vergeos_list_networks",
    "vergeos_usage",
    "vergeos_list_snapshots",
    "vergeos_events",
    "vergeos_report_inventory",
    "vergeos_report_usage",
    "vergeos_report_snapshots",
}
OPERATOR_ACTIONS = VIEWER_ACTIONS | {
    "vergeos_vm_power",
    "vergeos_snapshot_create",
    "vergeos_snapshot_restore",
}
ADMIN_ACTIONS = OPERATOR_ACTIONS | {
    "vergeos_vm_create",
    "vergeos_vm_resize",
}
KNOWN_ACTIONS = ADMIN_ACTIONS

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
VM_POWER_STATES = {"on", "off", "reset", "shutdown"}


class CloudBrokerError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _ResolvedBinding:
    tenant: CloudTenant
    platform: CloudPlatform
    role: str


class CloudBrokerService:
    def __init__(self, secret_vault: SecretVault, session_factory) -> None:
        self._vault = secret_vault
        self._session_factory = session_factory
        self._clients: dict[str, VergeOSClient] = {}

    async def _resolve_secret(self, ref: str) -> str:
        from hermeshq.models.secret import Secret
        from hermeshq.services.secret_vault import decrypt_value

        async with self._session_factory() as session:
            secret = (await session.execute(select(Secret).where(Secret.name == ref))).scalars().first()
            if secret is None:
                raise CloudBrokerError(f"Secret '{ref}' not found", status_code=503)
            stored = (
                secret.value_enc.decode("utf-8", errors="replace")
                if isinstance(secret.value_enc, bytes)
                else str(secret.value_enc)
            )
            value = decrypt_value(self._vault, stored)
            if not value:
                raise CloudBrokerError(f"Secret '{ref}' could not be decrypted", status_code=503)
            return value

    def _client_for(self, platform: CloudPlatform, tenant: CloudTenant, api_key: str) -> VergeOSClient:
        cache_id = f"{tenant.id}:{platform.id}"
        client = self._clients.get(cache_id)
        if client is None:
            client = VergeOSClient(
                api_url=platform.api_url,
                api_key=api_key,
                insecure_tls=platform.insecure_tls,
                cache_ttl_seconds=tenant.cache_ttl_seconds,
            )
            self._clients[cache_id] = client
        return client

    async def _resolve_bindings(self, db: AsyncSession, agent_id: str) -> list[_ResolvedBinding]:
        result = await db.execute(
            select(CloudBinding, CloudTenant, CloudPlatform)
            .join(CloudTenant, CloudBinding.tenant_id == CloudTenant.id)
            .join(CloudPlatform, CloudTenant.platform_id == CloudPlatform.id)
            .where(CloudBinding.agent_id == agent_id, CloudTenant.active.is_(True), CloudPlatform.active.is_(True))
        )
        return [
            _ResolvedBinding(tenant=tenant, platform=platform, role=binding.cloud_role)
            for binding, tenant, platform in result.all()
        ]

    async def handle(self, db: AsyncSession, agent: Agent, action: str, params: dict | None) -> dict:
        params = dict(params or {})
        action = (action or "").strip()
        if action not in KNOWN_ACTIONS:
            raise CloudBrokerError(f"Unknown cloud action '{action}'")
        bindings = await self._resolve_bindings(db, agent.id)
        if not bindings:
            raise CloudBrokerError("This agent has no cloud tenant bindings", status_code=403)
        requested_tenant = (params.pop("tenant_id", None) or "").strip()
        if requested_tenant:
            binding = next((b for b in bindings if b.tenant.id == requested_tenant), None)
            if binding is None:
                raise CloudBrokerError("Tenant is not bound to this agent", status_code=403)
        else:
            if len(bindings) > 1:
                raise CloudBrokerError(
                    "Agent has multiple cloud tenants — pass tenant_id",
                )
            binding = bindings[0]
        required_rank = self._required_rank(action)
        if ROLE_RANK.get(binding.role, 0) < required_rank:
            raise CloudBrokerError(
                f"Action '{action}' requires cloud role '{self._role_for_rank(required_rank)}' (agent role: '{binding.role}')",
                status_code=403,
            )
        api_key = await self._resolve_secret(binding.tenant.credential_ref)
        client = self._client_for(binding.platform, binding.tenant, api_key)
        handler = _ACTIONS.get(action)
        if handler is None:
            raise CloudBrokerError(f"Action '{action}' is not implemented")
        try:
            payload = await handler(client, params)
        except VergeOSClientError as exc:
            raise CloudBrokerError(str(exc), status_code=502) from exc
        return {
            "success": True,
            "action": action,
            "tenant": {"id": binding.tenant.id, "name": binding.tenant.display_name},
            "platform": {"kind": binding.platform.kind, "name": binding.platform.name},
            "result": payload,
        }

    @staticmethod
    def _required_rank(action: str) -> int:
        if action in ADMIN_ACTIONS - OPERATOR_ACTIONS:
            return 3
        if action in OPERATOR_ACTIONS - VIEWER_ACTIONS:
            return 2
        return 1

    @staticmethod
    def _role_for_rank(rank: int) -> str:
        return {1: "viewer", 2: "operator", 3: "admin"}.get(rank, "viewer")

    async def health(self, db: AsyncSession, agent: Agent) -> list[dict]:
        bindings = await self._resolve_bindings(db, agent.id)
        report = []
        for binding in bindings:
            try:
                api_key = await self._resolve_secret(binding.tenant.credential_ref)
                client = self._client_for(binding.platform, binding.tenant, api_key)
                check = await client.health()
            except CloudBrokerError as exc:
                check = {"ok": False, "error": str(exc)}
            report.append(
                {
                    "tenant": binding.tenant.display_name,
                    "platform": binding.platform.name,
                    "role": binding.role,
                    "healthy": bool(check.get("ok")),
                    "error": check.get("error"),
                }
            )
        return report


def _as_list(body: Any) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "results", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _normalize_vm(vm: dict) -> dict:
    return {
        "id": str(vm.get("$key") or vm.get("id") or ""),
        "name": vm.get("name") or "",
        "state": vm.get("state") or vm.get("power_state") or "unknown",
        "cpu_cores": vm.get("cpu_cores"),
        "ram_mb": vm.get("ram"),
        "os_family": vm.get("os_family"),
        "uuid": vm.get("uuid"),
        "is_snapshot": bool(vm.get("is_snapshot")),
        "modified": vm.get("modified"),
    }


async def _list_vms(client: VergeOSClient, params: dict) -> list:
    body = await client.get(
        "/api/v4/vms",
        params=VergeOSClient.build_list_params(
            filter_expr=params.get("filter"), sort=params.get("sort"), limit=params.get("limit")
        ),
    )
    vms = [_normalize_vm(vm) for vm in _as_list(body) if isinstance(vm, dict)]
    if params.get("include_snapshots") is not True:
        vms = [vm for vm in vms if not vm["is_snapshot"]]
    return vms


async def _get_vm(client: VergeOSClient, params: dict) -> dict:
    vm_id = str(params.get("vm_id") or "").strip()
    if not vm_id:
        raise CloudBrokerError("vm_id is required")
    body = await client.get(f"/api/v4/vms/{vm_id}", params={"fields": "all"})
    if not isinstance(body, dict):
        raise CloudBrokerError("Unexpected VM payload")
    normalized = _normalize_vm(body)
    normalized["raw"] = body
    return normalized


async def _list_networks(client: VergeOSClient, params: dict) -> list:
    body = await client.get("/api/v4/vnets", params=VergeOSClient.build_list_params(filter_expr=params.get("filter")))
    return [
        {
            "id": str(net.get("$key") or ""),
            "name": net.get("name"),
            "type": net.get("type"),
            "network": net.get("network"),
            "dhcp_enabled": net.get("dhcp_enabled"),
            "mtu": net.get("mtu"),
        }
        for net in _as_list(body)
        if isinstance(net, dict)
    ]


async def _usage(client: VergeOSClient, params: dict) -> dict:
    vms = await _list_vms(client, {"include_snapshots": True})
    active = [vm for vm in vms if not vm["is_snapshot"]]
    total_cpu = sum(vm.get("cpu_cores") or 0 for vm in active)
    total_ram = sum(vm.get("ram_mb") or 0 for vm in active)
    return {
        "vms_total": len(active),
        "vms_running": sum(1 for vm in active if str(vm.get("state")).lower() in ("running", "on", "active")),
        "snapshots": len(vms) - len(active),
        "cpu_cores_allocated": total_cpu,
        "ram_mb_allocated": total_ram,
        "collected_at": int(time.time()),
    }


async def _list_snapshots(client: VergeOSClient, params: dict) -> list:
    vm_id = str(params.get("vm_id") or "").strip()
    if vm_id:
        body = await client.get(
            "/api/v4/vms",
            params=VergeOSClient.build_list_params(filter_expr=f"parent eq {vm_id}"),
        )
    else:
        body = await client.get("/api/v4/vms", params=VergeOSClient.build_list_params(filter_expr=None))
    return [
        {
            "id": str(vm.get("$key") or ""),
            "name": vm.get("name"),
            "parent": vm.get("parent") or vm.get("machine"),
            "created": vm.get("created") or vm.get("modified"),
        }
        for vm in _as_list(body)
        if isinstance(vm, dict) and vm.get("is_snapshot")
    ]


async def _events(client: VergeOSClient, params: dict) -> list:
    body = await client.get(
        "/api/v4/events",
        params=VergeOSClient.build_list_params(filter_expr=params.get("filter"), limit=params.get("limit") or 100),
    )
    return _as_list(body)


async def _vm_power(client: VergeOSClient, params: dict) -> dict:
    vm_id = str(params.get("vm_id") or "").strip()
    state = str(params.get("state") or "").strip().lower()
    if not vm_id:
        raise CloudBrokerError("vm_id is required")
    if state not in VM_POWER_STATES:
        raise CloudBrokerError(f"state must be one of {sorted(VM_POWER_STATES)}")
    await client.post(f"/api/v4/vms/{vm_id}/power", {"state": state})
    return {"vm_id": vm_id, "requested": state}


async def _snapshot_create(client: VergeOSClient, params: dict) -> dict:
    vm_id = str(params.get("vm_id") or "").strip()
    name = str(params.get("name") or "").strip()
    if not vm_id:
        raise CloudBrokerError("vm_id is required")
    if not name:
        raise CloudBrokerError("name is required")
    await client.post(f"/api/v4/vms/{vm_id}/snapshots", {"name": name})
    return {"vm_id": vm_id, "snapshot": name}


async def _snapshot_restore(client: VergeOSClient, params: dict) -> dict:
    vm_id = str(params.get("vm_id") or "").strip()
    snapshot_id = str(params.get("snapshot_id") or "").strip()
    if not vm_id or not snapshot_id:
        raise CloudBrokerError("vm_id and snapshot_id are required")
    await client.post(f"/api/v4/vms/{vm_id}/snapshots/{snapshot_id}/restore", {})
    return {"vm_id": vm_id, "restored": snapshot_id}


async def _vm_create(client: VergeOSClient, params: dict) -> dict:
    name = str(params.get("name") or "").strip()
    if not name:
        raise CloudBrokerError("name is required")
    cpu = params.get("cpu_cores")
    ram = params.get("ram_mb")
    if not isinstance(cpu, int) or cpu < 1 or cpu > 64:
        raise CloudBrokerError("cpu_cores must be an integer between 1 and 64")
    if not isinstance(ram, int) or ram < 256 or ram > 1024 * 1024:
        raise CloudBrokerError("ram_mb must be an integer between 256 and 1048576")
    payload = {
        "name": name,
        "cpu_cores": cpu,
        "ram": ram,
        "os_family": params.get("os_family") or "linux",
        "boot_order": params.get("boot_order") or "hd",
        "description": params.get("description") or "",
    }
    body = await client.post("/api/v4/vms", payload)
    location = body.get("location") if isinstance(body, dict) else None
    return {"created": name, "location": location}


async def _vm_resize(client: VergeOSClient, params: dict) -> dict:
    vm_id = str(params.get("vm_id") or "").strip()
    if not vm_id:
        raise CloudBrokerError("vm_id is required")
    changes: dict = {}
    cpu = params.get("cpu_cores")
    ram = params.get("ram_mb")
    if cpu is not None:
        if not isinstance(cpu, int) or cpu < 1 or cpu > 64:
            raise CloudBrokerError("cpu_cores must be an integer between 1 and 64")
        changes["cpu_cores"] = cpu
    if ram is not None:
        if not isinstance(ram, int) or ram < 256 or ram > 1024 * 1024:
            raise CloudBrokerError("ram_mb must be an integer between 256 and 1048576")
        changes["ram"] = ram
    if not changes:
        raise CloudBrokerError("Nothing to resize — pass cpu_cores and/or ram_mb")
    await client.put(f"/api/v4/vms/{vm_id}", changes)
    return {"vm_id": vm_id, "changes": changes}


def _fmt_gb(mb: int | None) -> str:
    if not mb:
        return "-"
    return f"{mb / 1024:.1f} GB"


async def _report_inventory(client: VergeOSClient, params: dict) -> dict:
    vms = await _list_vms(client, {})
    usage = await _usage(client, {})
    lines = [
        "# Inventario de tenant",
        "",
        f"- VMs activas: **{usage['vms_total']}** (corriendo: {usage['vms_running']})",
        f"- Snapshots: {usage['snapshots']}",
        f"- CPU asignado: {usage['cpu_cores_allocated']} cores",
        f"- RAM asignada: {_fmt_gb(usage['ram_mb_allocated'])}",
        "",
        "| VM | Estado | CPU | RAM | SO |",
        "|---|---|---|---|---|",
    ]
    for vm in vms:
        lines.append(
            f"| {vm['name']} | {vm['state']} | {vm['cpu_cores'] or '-'} | {_fmt_gb(vm['ram_mb'])} | {vm['os_family'] or '-'} |"
        )
    csv = ["name,state,cpu_cores,ram_mb,os_family"]
    for vm in vms:
        csv.append(f"{vm['name']},{vm['state']},{vm['cpu_cores'] or ''},{vm['ram_mb'] or ''},{vm['os_family'] or ''}")
    return {
        "markdown": "\n".join(lines),
        "csv": "\n".join(csv),
        "suggested_filename": "inventario_tenant.csv",
    }


async def _report_usage(client: VergeOSClient, params: dict) -> dict:
    usage = await _usage(client, {})
    lines = [
        "# Uso del tenant",
        "",
        f"- CPU asignado: **{usage['cpu_cores_allocated']} cores** en {usage['vms_total']} VMs",
        f"- RAM asignada: **{_fmt_gb(usage['ram_mb_allocated'])}**",
        f"- VMs corriendo: {usage['vms_running']} / {usage['vms_total']}",
        f"- Snapshots almacenados: {usage['snapshots']}",
        "",
        "_Percentiles históricos (95th billing) requieren la instancia real — se incluyen cuando la API los expone._",
    ]
    return {"markdown": "\n".join(lines), "csv": None}


async def _report_snapshots(client: VergeOSClient, params: dict) -> dict:
    snapshots = await _list_snapshots(client, {})
    lines = [
        "# Snapshots del tenant",
        "",
        f"Total: **{len(snapshots)}**",
        "",
        "| Snapshot | VM padre | Creado |",
        "|---|---|---|",
    ]
    for snap in snapshots:
        lines.append(f"| {snap['name']} | {snap['parent']} | {snap['created'] or '-'} |")
    csv = ["name,parent,created"]
    for snap in snapshots:
        csv.append(f"{snap['name']},{snap['parent']},{snap['created'] or ''}")
    return {"markdown": "\n".join(lines), "csv": "\n".join(csv), "suggested_filename": "snapshots_tenant.csv"}


_ACTIONS: dict[str, Callable[[VergeOSClient, dict], Awaitable[object]]] = {
    "vergeos_list_vms": _list_vms,
    "vergeos_get_vm": _get_vm,
    "vergeos_list_networks": _list_networks,
    "vergeos_usage": _usage,
    "vergeos_list_snapshots": _list_snapshots,
    "vergeos_events": _events,
    "vergeos_vm_power": _vm_power,
    "vergeos_snapshot_create": _snapshot_create,
    "vergeos_snapshot_restore": _snapshot_restore,
    "vergeos_vm_create": _vm_create,
    "vergeos_vm_resize": _vm_resize,
    "vergeos_report_inventory": _report_inventory,
    "vergeos_report_usage": _report_usage,
    "vergeos_report_snapshots": _report_snapshots,
}
