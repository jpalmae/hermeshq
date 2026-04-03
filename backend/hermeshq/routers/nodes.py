import socket

from fastapi import APIRouter, Depends, HTTPException
import psutil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.schemas.node import NodeCreate, NodeRead, NodeUpdate

router = APIRouter(prefix="/nodes", tags=["nodes"])


def _is_local_node(node: Node) -> bool:
    return node.node_type == "local"


@router.get("", response_model=list[NodeRead])
async def list_nodes(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[NodeRead]:
    result = await db.execute(select(Node).order_by(Node.created_at.asc()))
    return [NodeRead.model_validate(node) for node in result.scalars().all()]


@router.post("", response_model=NodeRead)
async def create_node(
    payload: NodeCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> NodeRead:
    node = Node(**payload.model_dump())
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return NodeRead.model_validate(node)


@router.get("/{node_id}", response_model=NodeRead)
async def get_node(
    node_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> NodeRead:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeRead.model_validate(node)


@router.put("/{node_id}", response_model=NodeRead)
async def update_node(
    node_id: str,
    payload: NodeUpdate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> NodeRead:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(node, field, value)
    await db.commit()
    await db.refresh(node)
    return NodeRead.model_validate(node)


@router.post("/{node_id}/test")
async def test_node(
    node_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if _is_local_node(node):
        return {
            "status": "ok",
            "node_id": node_id,
            "message": "Local node is reachable",
            "hostname": node.hostname,
        }
    try:
        with socket.create_connection((node.hostname, node.ssh_port), timeout=3):
            pass
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"SSH connectivity test failed: {exc}") from exc
    return {
        "status": "ok",
        "node_id": node_id,
        "message": "SSH port is reachable",
        "hostname": node.hostname,
        "port": node.ssh_port,
    }


@router.post("/{node_id}/provision")
async def provision_node(
    node_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if not _is_local_node(node):
        raise HTTPException(status_code=501, detail="Remote node provisioning is not implemented yet")
    node.status = "online"
    node.system_info = {
        **(node.system_info or {}),
        "runtime": "local",
        "provisioned": True,
    }
    await db.commit()
    return {"status": "ok", "node_id": node_id, "message": "Local node is provisioned"}


@router.get("/{node_id}/metrics")
async def node_metrics(
    node_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if not _is_local_node(node):
        raise HTTPException(status_code=501, detail="Remote node metrics are not implemented yet")
    disk_usage = psutil.disk_usage("/")
    vm = psutil.virtual_memory()
    return {
        "node_id": node_id,
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_percent": vm.percent,
        "disk_percent": disk_usage.percent,
        "memory_total": vm.total,
        "memory_available": vm.available,
        "disk_total": disk_usage.total,
        "disk_free": disk_usage.free,
        "system_info": {
            **(node.system_info or {}),
            "cpu_count": psutil.cpu_count(),
            "boot_time": psutil.boot_time(),
        },
    }
