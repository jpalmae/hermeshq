"""MCP (Model Context Protocol) server endpoint — JSON-RPC 2.0 over HTTP POST and SSE.

Supports:
- ``POST /mcp``  — stateless JSON-RPC 2.0 requests
- ``GET  /mcp``  — SSE transport for server→client streaming
- Synchronous ``invoke_agent`` with configurable long-poll timeout
- Per-token rate limiting
- Pagination on ``list_agents``
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.mcp_access import McpAccessToken
from hermeshq.models.task import Task
from hermeshq.services.mcp_access import (
    authenticate_mcp_token,
    ensure_mcp_agent_allowed,
    ensure_mcp_scope,
)
from hermeshq.services.mcp_rate_limiter import McpRateLimiter
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column
from hermeshq.versioning import get_app_version

router = APIRouter(tags=["mcp"])

# ---------------------------------------------------------------------------
# Per-token rate limiter (60 requests / 60 s by default)
# ---------------------------------------------------------------------------
_rate_limiter = McpRateLimiter(max_requests=60, window_seconds=60)

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------

def _jsonrpc_result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tool_text_result(text: str, structured: dict | None = None) -> dict:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _agent_label(agent: Agent) -> str:
    return agent.friendly_name or agent.name or agent.slug or agent.id

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

async def _log_mcp_event(
    db: AsyncSession,
    access: McpAccessToken,
    event_type: str,
    *,
    agent: Agent | None = None,
    task: Task | None = None,
    message: str,
    details: dict | None = None,
) -> None:
    db.add(
        ActivityLog(
            agent_id=agent.id if agent else None,
            task_id=task.id if task else None,
            node_id=agent.node_id if agent else None,
            event_type=event_type,
            severity="info",
            message=message,
            details={
                "mcp_access_token_id": access.id,
                "mcp_access_token_name": access.name,
                "mcp_client_name": access.client_name,
                **(details or {}),
            },
        )
    )

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _tools_definition() -> list[dict]:
    return [
        {
            "name": "list_agents",
            "description": "List HermesHQ agents authorized for this MCP credential.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "minimum": 1, "default": 1, "description": "Page number."},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50, "description": "Results per page."},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "invoke_agent",
            "description": (
                "Submit an instruction to an authorized HermesHQ agent. "
                "By default waits synchronously for the result (up to wait_seconds). "
                "Set wait_seconds=0 for fire-and-forget mode."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "HermesHQ agent id."},
                    "prompt": {"type": "string", "description": "Instruction or question to send to the agent."},
                    "title": {"type": "string", "description": "Optional task title."},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    "auto_start_stopped": {"type": "boolean", "default": False},
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 120,
                        "default": 60,
                        "description": "Max seconds to wait for result. 0 = fire-and-forget.",
                    },
                },
                "required": ["agent_id", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_agent_task",
            "description": "Fetch status and result for a task created through HermesHQ.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "HermesHQ task id."}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    ]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _list_allowed_agents(
    db: AsyncSession, access: McpAccessToken, page: int = 1, page_size: int = 50,
) -> tuple[list[Agent], int]:
    """Return (paginated_agents, total_count) respecting the token's allowed list."""
    allowed_agent_ids = [aid for aid in (access.allowed_agent_ids or []) if isinstance(aid, str)]
    if not allowed_agent_ids:
        return [], 0

    # Total count
    count_q = select(func.count(Agent.id)).where(
        Agent.id.in_(allowed_agent_ids), Agent.is_archived.is_(False),
    )
    total = (await db.execute(count_q)).scalar_one()

    # Paginated results (preserve original order from allowed_agent_ids)
    result = await db.execute(
        select(Agent)
        .where(Agent.id.in_(allowed_agent_ids), Agent.is_archived.is_(False))
        .order_by(Agent.friendly_name.asc(), Agent.name.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    agent_map = {agent.id: agent for agent in result.scalars().all()}
    ordered = [agent_map[aid] for aid in allowed_agent_ids if aid in agent_map]
    return ordered, total


async def _wait_for_task_completion(
    db: AsyncSession, task_id: str, max_wait: float = 60.0, poll_interval: float = 1.0,
) -> Task | None:
    """Poll the task until it reaches a terminal state or *max_wait* expires."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        task = await db.get(Task, task_id)
        if task is None:
            return None
        if task.status in ("completed", "failed", "cancelled"):
            return task
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))
    # One final read
    return await db.get(Task, task_id)

# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

async def _call_tool(
    *,
    request: Request,
    db: AsyncSession,
    access: McpAccessToken,
    name: str,
    arguments: dict,
) -> dict:
    if name == "list_agents":
        return await _handle_list_agents(db, access, arguments)

    if name == "invoke_agent":
        return await _handle_invoke_agent(request, db, access, arguments)

    if name == "get_agent_task":
        return await _handle_get_agent_task(db, access, arguments)

    return _tool_text_result(f"Unknown tool: {name}", {"error": "unknown_tool", "tool": name})


async def _handle_list_agents(db: AsyncSession, access: McpAccessToken, arguments: dict) -> dict:
    ensure_mcp_scope(access, "agents:list")
    page = max(1, int(arguments.get("page") or 1))
    page_size = max(1, min(100, int(arguments.get("page_size") or 50)))
    agents, total = await _list_allowed_agents(db, access, page, page_size)
    payload = {
        "agents": [
            {
                "id": agent.id,
                "slug": agent.slug,
                "name": _agent_label(agent),
                "description": agent.description,
                "status": agent.status,
                "runtime_profile": agent.runtime_profile,
                "can_receive_tasks": agent.can_receive_tasks,
            }
            for agent in agents
        ],
        "pagination": {"page": page, "page_size": page_size, "total": total},
    }
    await _log_mcp_event(
        db, access, "mcp.tool.list_agents",
        message=f"MCP listed {len(agents)} authorized agents (page {page})",
        details={"count": len(agents), "total": total},
    )
    await db.commit()
    return _tool_text_result(f"{len(agents)} agents (page {page}, total {total}).", payload)


async def _handle_invoke_agent(
    request: Request, db: AsyncSession, access: McpAccessToken, arguments: dict,
) -> dict:
    ensure_mcp_scope(access, "agents:invoke")
    agent_id = str(arguments.get("agent_id") or "").strip()
    prompt = str(arguments.get("prompt") or "").strip()
    title = str(arguments.get("title") or "").strip() or "MCP request"
    priority = int(arguments.get("priority") or 5)
    auto_start_stopped = bool(arguments.get("auto_start_stopped") or False)
    wait_seconds = max(0, min(120, int(arguments.get("wait_seconds") if arguments.get("wait_seconds") is not None else 60)))

    if not agent_id or not prompt:
        return _tool_text_result("agent_id and prompt are required.", {"error": "agent_id and prompt are required"})
    ensure_mcp_agent_allowed(access, agent_id)

    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        return _tool_text_result("Agent not found or archived.", {"error": "agent_not_found"})
    if not agent.can_receive_tasks:
        return _tool_text_result("Agent is not configured to receive tasks.", {"error": "agent_cannot_receive_tasks"})

    if agent.status != "running" and auto_start_stopped:
        await request.app.state.supervisor.start_agent(agent.id)
        await db.refresh(agent)

    task = Task(
        agent_id=agent.id,
        title=title[:512],
        prompt=prompt,
        priority=max(1, min(priority, 10)),
        metadata_json={
            "source": "mcp",
            "mcp_access_token_id": access.id,
            "mcp_access_token_name": access.name,
            "mcp_client_name": access.client_name,
        },
    )
    task.board_column = runtime_status_to_board_column(task.status)
    task.board_order = next_board_order()
    task.board_manual = False
    db.add(task)
    await db.flush()

    await _log_mcp_event(
        db, access, "mcp.tool.invoke_agent", agent=agent, task=task,
        message=f"MCP submitted task to {_agent_label(agent)}",
        details={"auto_start_stopped": auto_start_stopped, "wait_seconds": wait_seconds},
    )
    await db.commit()
    await db.refresh(task)

    if agent.status == "running":
        await request.app.state.supervisor.submit_task(task.id)

    # ── Synchronous wait ────────────────────────────────────────────────
    if wait_seconds > 0:
        final_task = await _wait_for_task_completion(db, task.id, max_wait=float(wait_seconds))
        if final_task:
            payload = {
                "task_id": final_task.id,
                "agent_id": agent.id,
                "agent_name": _agent_label(agent),
                "status": final_task.status,
                "response": final_task.response,
                "error_message": final_task.error_message,
                "completed": final_task.status in ("completed", "failed", "cancelled"),
                "completed_at": final_task.completed_at.isoformat() if final_task.completed_at else None,
            }
            summary = final_task.response or final_task.error_message or f"Task {final_task.status}"
            return _tool_text_result(summary, payload)

    # ── Fire-and-forget fallback ────────────────────────────────────────
    payload = {
        "task_id": task.id,
        "agent_id": agent.id,
        "agent_name": _agent_label(agent),
        "status": task.status,
        "completed": False,
    }
    return _tool_text_result(f"Task {task.id} submitted to {_agent_label(agent)}.", payload)


async def _handle_get_agent_task(db: AsyncSession, access: McpAccessToken, arguments: dict) -> dict:
    ensure_mcp_scope(access, "tasks:read")
    task_id = str(arguments.get("task_id") or "").strip()
    task = await db.get(Task, task_id)
    if not task:
        return _tool_text_result("Task not found.", {"error": "task_not_found"})
    ensure_mcp_agent_allowed(access, task.agent_id)
    agent = await db.get(Agent, task.agent_id)
    await _log_mcp_event(
        db, access, "mcp.tool.get_agent_task", agent=agent, task=task,
        message=f"MCP read task {task.id}",
    )
    await db.commit()
    payload = {
        "task_id": task.id,
        "agent_id": task.agent_id,
        "status": task.status,
        "response": task.response,
        "error_message": task.error_message,
        "queued_at": task.queued_at.isoformat() if task.queued_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    return _tool_text_result(task.response or task.error_message or f"Task status: {task.status}", payload)

# ---------------------------------------------------------------------------
# POST /mcp  — JSON-RPC 2.0 handler
# ---------------------------------------------------------------------------

@router.post("/mcp")
async def mcp_http_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    payload = await request.json()
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    # Accept client-side notifications (no id) with 202 Accepted.
    if request_id is None and method.startswith("notifications/"):
        return Response(status_code=202)

    access = await authenticate_mcp_token(db, authorization)

    # Rate limiting
    await _rate_limiter.check(access.id)

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "HermesHQ Enterprise MCP", "version": get_app_version()},
                "capabilities": {
                    "tools": {},
                    "streaming": {},
                },
            }
            await db.commit()
            return JSONResponse(_jsonrpc_result(request_id, result))

        if method == "tools/list":
            ensure_mcp_scope(access, "agents:list")
            await db.commit()
            return JSONResponse(_jsonrpc_result(request_id, {"tools": _tools_definition()}))

        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            result = await _call_tool(request=request, db=db, access=access, name=tool_name, arguments=arguments)
            return JSONResponse(_jsonrpc_result(request_id, result))

        await db.commit()
        return JSONResponse(_jsonrpc_error(request_id, -32601, f"Method not found: {method}"))

    except Exception as exc:
        await db.rollback()
        return JSONResponse(_jsonrpc_error(request_id, -32000, str(exc)), status_code=200)

# ---------------------------------------------------------------------------
# GET /mcp  — SSE transport
# ---------------------------------------------------------------------------

@router.get("/mcp")
async def mcp_sse_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """SSE transport for MCP clients that support it.

    After establishing the EventSource connection the server sends an
    ``endpoint`` event whose data is the URL the client should POST to.
    """
    access = await authenticate_mcp_token(db, authorization)
    await _rate_limiter.check(access.id)
    await db.commit()

    async def _sse_generator():
        # Per the MCP spec the first event advertises the POST endpoint.
        yield f"event: endpoint\ndata: /mcp\n\n"
        # Keep the connection alive with periodic heartbeats.
        try:
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(15)
                yield f": heartbeat\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        content=_sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
