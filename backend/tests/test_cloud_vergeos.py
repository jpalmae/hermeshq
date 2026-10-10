from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from hermeshq.services.cloud.broker import (
    ADMIN_ACTIONS,
    ROLE_RANK,
    VIEWER_ACTIONS,
    CloudBrokerError,
    CloudBrokerService,
    _normalize_vm,
)
from hermeshq.services.cloud.vergeos_client import VergeOSClient, VergeOSClientError


def _transport(responses: dict[str, tuple[int, object]], default: tuple[int, object] = (200, [])):
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        status, body = responses.get(key, default)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _client(transport=None, api_key: str = "tenant-key-123") -> VergeOSClient:
    client = VergeOSClient(
        api_url="https://verge.example",
        api_key=api_key,
        cache_ttl_seconds=30,
    )
    client._client = httpx.AsyncClient(
        transport=transport or _transport({}),
        base_url="https://verge.example",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    return client


class TestVergeOSClient:
    async def test_bearer_auth_and_list(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=[{"$key": 1, "name": "web", "ram": 1024}])

        client = _client(httpx.MockTransport(handler))
        body = await client.get("/api/v4/vms", {"fields": "most"})
        assert body[0]["name"] == "web"
        assert seen["auth"] == "Bearer tenant-key-123"
        await client.aclose()

    async def test_cache_hits_within_ttl(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=[])

        client = _client(httpx.MockTransport(handler))
        await client.get("/api/v4/vms", {"fields": "most"})
        await client.get("/api/v4/vms", {"fields": "most"})
        assert calls["n"] == 1
        await client.aclose()

    async def test_mutation_invalidates_cache(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if request.method == "POST":
                return httpx.Response(201, json={"location": "/v4/vms/9"})
            return httpx.Response(200, json=[])

        client = _client(httpx.MockTransport(handler))
        await client.get("/api/v4/vms", {"fields": "most"})
        await client.post("/api/v4/vms", {"name": "x"})
        await client.get("/api/v4/vms", {"fields": "most"})
        assert calls["n"] == 3
        await client.aclose()

    async def test_401_maps_to_credential_error(self) -> None:
        client = _client(_transport({"GET /api/v4/vms": (401, {"err": "denied"})}))
        with pytest.raises(VergeOSClientError, match="[Cc]redential"):
            await client.get("/api/v4/vms")
        await client.aclose()

    async def test_429_maps_to_rate_limit(self) -> None:
        client = _client(_transport({"GET /api/v4/vms": (429, {})}))
        with pytest.raises(VergeOSClientError, match="rate limit"):
            await client.get("/api/v4/vms")
        await client.aclose()

    async def test_error_never_contains_key(self) -> None:
        client = _client(_transport({"GET /api/v4/vms": (500, {"err": "internal boom"})}), api_key="supersecret-key")
        with pytest.raises(VergeOSClientError) as exc_info:
            await client.get("/api/v4/vms")
        assert "supersecret-key" not in str(exc_info.value)
        assert "internal boom" in str(exc_info.value)
        await client.aclose()


class TestNormalizeVm:
    def test_normalize(self) -> None:
        vm = _normalize_vm({"$key": 3, "name": "db", "cpu_cores": 4, "ram": 8192, "os_family": "linux"})
        assert vm["id"] == "3"
        assert vm["ram_mb"] == 8192
        assert vm["state"] == "unknown"


def _binding_row(agent_id: str, role: str, tenant_id: str = "t1"):
    tenant = SimpleNamespace(
        id=tenant_id,
        platform_id="p1",
        display_name="Cliente Uno",
        credential_ref="sec-ref",
        cache_ttl_seconds=5,
        active=True,
    )
    platform = SimpleNamespace(
        id="p1", kind="vergeos", name="Verge Prod", api_url="https://verge.example", insecure_tls=False
    )
    return SimpleNamespace(tenant=tenant, platform=platform, role=role)


def _broker_with_bindings(agent_id: str, rows: list, secret_value: str = "k") -> CloudBrokerService:
    broker = CloudBrokerService.__new__(CloudBrokerService)
    broker._vault = None
    broker._clients = {}
    broker._session_factory = MagicMock()
    broker._resolve_secret = AsyncMock(return_value=secret_value)

    async def _resolve(db, agent_id_):
        return rows

    broker._resolve_bindings = _resolve
    return broker


class TestBroker:
    async def test_unknown_action_rejected(self) -> None:
        broker = _broker_with_bindings("a1", [_binding_row("a1", "viewer")])
        with pytest.raises(CloudBrokerError, match="Unknown cloud action"):
            await broker.handle(MagicMock(), SimpleNamespace(id="a1", name="A"), "rm_rf", {})

    async def test_no_binding_forbidden(self) -> None:
        broker = _broker_with_bindings("a1", [])
        with pytest.raises(CloudBrokerError, match="no cloud tenant"):
            await broker.handle(MagicMock(), SimpleNamespace(id="a1"), "vergeos_usage", {})

    async def test_viewer_cannot_power(self) -> None:
        broker = _broker_with_bindings("a1", [_binding_row("a1", "viewer")])
        with pytest.raises(CloudBrokerError, match="requires cloud role"):
            await broker.handle(
                MagicMock(), SimpleNamespace(id="a1"), "vergeos_vm_power", {"vm_id": "1", "state": "off"}
            )

    async def test_operator_can_power(self) -> None:
        broker = _broker_with_bindings("a1", [_binding_row("a1", "operator")])
        broker._client_for = lambda platform, tenant, key: _client(_transport({"POST /api/v4/vms/1/power": (200, {})}))
        result = await broker.handle(
            MagicMock(), SimpleNamespace(id="a1"), "vergeos_vm_power", {"vm_id": "1", "state": "off"}
        )
        assert result["success"] is True
        assert result["result"]["requested"] == "off"

    async def test_foreign_tenant_rejected(self) -> None:
        broker = _broker_with_bindings("a1", [_binding_row("a1", "viewer", tenant_id="t1")])
        with pytest.raises(CloudBrokerError, match="not bound"):
            await broker.handle(MagicMock(), SimpleNamespace(id="a1"), "vergeos_usage", {"tenant_id": "t-other"})

    async def test_multi_tenant_requires_explicit(self) -> None:
        broker = _broker_with_bindings("a1", [_binding_row("a1", "viewer", "t1"), _binding_row("a1", "viewer", "t2")])
        with pytest.raises(CloudBrokerError, match="tenant_id"):
            await broker.handle(MagicMock(), SimpleNamespace(id="a1"), "vergeos_usage", {})

    async def test_inventory_report_shape(self) -> None:
        vms = [
            {"$key": 1, "name": "web", "cpu_cores": 2, "ram": 2048, "os_family": "linux"},
            {"$key": 2, "name": "snap-old", "is_snapshot": True},
        ]
        broker = _broker_with_bindings("a1", [_binding_row("a1", "viewer")])
        broker._client_for = lambda platform, tenant, key: _client(_transport({"GET /api/v4/vms": (200, vms)}))
        result = await broker.handle(MagicMock(), SimpleNamespace(id="a1"), "vergeos_report_inventory", {})
        payload = result["result"]
        assert "| web |" in payload["markdown"]
        assert "web,unknown,2,2048,linux" in payload["csv"]
        assert payload["suggested_filename"].endswith(".csv")


class TestActionRoles:
    def test_role_hierarchy(self) -> None:
        assert ROLE_RANK["viewer"] < ROLE_RANK["operator"] < ROLE_RANK["admin"]
        assert "vergeos_vm_power" not in VIEWER_ACTIONS
        assert "vergeos_vm_create" not in ADMIN_ACTIONS - {"vergeos_vm_create"}
        assert "vergeos_report_inventory" in VIEWER_ACTIONS


class TestDelegationDeny:
    async def test_deny_delegation_blocks_delegate_task(self) -> None:
        from hermeshq.services.permission_enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer.__new__(PermissionEnforcer)
        policy = SimpleNamespace(
            name="Cloud Viewer",
            tool_rules={"allow": ["vergeos_*"], "deny": [], "deny_delegation": True},
            path_rules={},
            command_rules={},
            network_rules={"allow_domains": [], "deny_all": True},
            approval_rules={},
        )
        enforcer.get_policy = AsyncMock(return_value=policy)
        decision = await enforcer.evaluate(SimpleNamespace(id="a1", name="A"), "delegate_task", {})
        assert decision.allowed is False

    async def test_default_delegation_still_free_without_flag(self) -> None:
        from hermeshq.services.permission_enforcer import PermissionEnforcer

        enforcer = PermissionEnforcer.__new__(PermissionEnforcer)
        policy = SimpleNamespace(
            name="Office",
            tool_rules={"allow": [], "deny": []},
            path_rules={},
            command_rules={},
            network_rules={},
            approval_rules={},
        )
        enforcer.get_policy = AsyncMock(return_value=policy)
        decision = await enforcer.evaluate(SimpleNamespace(id="a1", name="A"), "delegate_task", {})
        assert decision.allowed is True
