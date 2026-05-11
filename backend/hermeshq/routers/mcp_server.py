from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
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
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column
from hermeshq.versioning import get_app_version

router = APIRouter(tags=["mcp"])


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


async def _list_allowed_agents(db: AsyncSession, access: McpAccessToken) -> list[Agent]:
    allowed_agent_ids = [agent_id for agent_id in (access.allowed_agent_ids or []) if isinstance(agent_id, str)]
    if not allowed_agent_ids:
        return []
    result = await db.execute(
        select(Agent)
        .where(Agent.id.in_(allowed_agent_ids), Agent.is_archived.is_(False))
        .order_by(Agent.friendly_name.asc(), Agent.name.asc())
    )
    agent_map = {agent.id: agent for agent in result.scalars().all()}
    return [agent_map[agent_id] for agent_id in allowed_agent_ids if agent_id in agent_map]


def _tools_definition() -> list[dict]:
    return [
        {
            "name": "list_agents",
            "description": "List HermesHQ agents authorized for this MCP credential.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "invoke_agent",
            "description": "Submit an instruction to an authorized HermesHQ agent and return the created task id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "HermesHQ agent id."},
                    "prompt": {"type": "string", "description": "Instruction or question to send to the agent."},
                    "title": {"type": "string", "description": "Optional task title."},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    "auto_start_stopped": {"type": "boolean", "default": False},
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


async def _call_tool(
    *,
    request: Request,
    db: AsyncSession,
    access: McpAccessToken,
    name: str,
    arguments: dict,
) -> dict:
    if name == "list_agents":
        ensure_mcp_scope(access, "agents:list")
        agents = await _list_allowed_agents(db, access)
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
            ]
        }
        await _log_mcp_event(
            db,
            access,
            "mcp.tool.list_agents",
            message=f"MCP listed {len(agents)} authorized agents",
            details={"count": len(agents)},
        )
        await db.commit()
        return _tool_text_result(f"{len(agents)} authorized HermesHQ agents available.", payload)

    if name == "invoke_agent":
        ensure_mcp_scope(access, "agents:invoke")
        agent_id = str(arguments.get("agent_id") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        title = str(arguments.get("title") or "").strip() or "MCP request"
        priority = int(arguments.get("priority") or 5)
        auto_start_stopped = bool(arguments.get("auto_start_stopped") or False)
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
            db,
            access,
            "mcp.tool.invoke_agent",
            agent=agent,
            task=task,
            message=f"MCP submitted task to {_agent_label(agent)}",
            details={"auto_start_stopped": auto_start_stopped},
        )
        await db.commit()
        await db.refresh(task)
        if agent.status == "running":
            await request.app.state.supervisor.submit_task(task.id)
        payload = {
            "task_id": task.id,
            "agent_id": agent.id,
            "agent_name": _agent_label(agent),
            "status": task.status,
        }
        return _tool_text_result(f"Task {task.id} submitted to {_agent_label(agent)}.", payload)

    if name == "get_agent_task":
        ensure_mcp_scope(access, "tasks:read")
        task_id = str(arguments.get("task_id") or "").strip()
        task = await db.get(Task, task_id)
        if not task:
            return _tool_text_result("Task not found.", {"error": "task_not_found"})
        ensure_mcp_agent_allowed(access, task.agent_id)
        agent = await db.get(Agent, task.agent_id)
        await _log_mcp_event(
            db,
            access,
            "mcp.tool.get_agent_task",
            agent=agent,
            task=task,
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

    return _tool_text_result(f"Unknown tool: {name}", {"error": "unknown_tool", "tool": name})


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

    if request_id is None and method.startswith("notifications/"):
        return Response(status_code=202)

    access = await authenticate_mcp_token(db, authorization)

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "HermesHQ Enterprise MCP", "version": get_app_version()},
                "capabilities": {"tools": {}},
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
