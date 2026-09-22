from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import decode_access_token_claims, get_user_by_subject
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["desktop-gateway"])

SESSION_TOKEN_HEADER = "X-Hermes-Session-Token"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "authorization",
    "cookie",
}


def _extract_session_token(request: Request) -> str | None:
    token = request.headers.get(SESSION_TOKEN_HEADER)
    if token:
        return token.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie = request.cookies.get("hermeshq_token")
    return cookie.strip() if cookie else None


async def _resolve_user(request: Request, token: str | None, db: AsyncSession) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token")
    claims = decode_access_token_claims(token)
    if not claims or claims.get("sub_kind") not in (None, "id", "username"):
        raise HTTPException(status_code=401, detail="Invalid session token")
    user = await get_user_by_subject(db, claims.get("sub"), claims.get("sub_kind"))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive or unknown user")
    return user


async def _load_bridge_agent(agent_id: str, db: AsyncSession) -> Agent:
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.runtime_type != "hermes":
        raise HTTPException(status_code=400, detail="Desktop access requires a Hermes runtime agent")
    if not agent.desktop_access_enabled:
        raise HTTPException(status_code=403, detail="Desktop access is not enabled for this agent")
    return agent


def _bridge_service(request: Request):
    service = getattr(request.app.state, "desktop_gateway_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Desktop gateway service unavailable")
    return service


def _forwardable_headers(request: Request, inner_token: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS or name.lower() == SESSION_TOKEN_HEADER.lower():
            continue
        headers[name] = value
    if inner_token:
        headers[SESSION_TOKEN_HEADER] = inner_token
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS:
            continue
        headers[name] = value
    return headers


def _proxy_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "desktop_proxy_client", None)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        request.app.state.desktop_proxy_client = client
    return client


# ── Discovery: Desktop probes this to classify the gateway ──────────────────


@router.get("/desktop/{agent_id}/api/status")
async def desktop_status(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    await _resolve_user(request, _extract_session_token(request), db)
    agent = await _load_bridge_agent(agent_id, db)
    service = _bridge_service(request)
    handle = await service.ensure_agent_gateway(agent)
    return JSONResponse(
        {
            "auth_required": True,
            "auth_providers": ["token"],
            "gateway": "hermeshq-desktop-bridge",
            "hermeshq": {"agent_id": agent_id, "port": handle.port},
        }
    )


# ── WebSocket bridge ────────────────────────────────────────────────────────


@router.websocket("/desktop/{agent_id}/api/ws")
async def desktop_ws_bridge(websocket: WebSocket, agent_id: str) -> None:
    token = websocket.query_params.get("token", "").strip()
    if not token:
        await websocket.close(code=4401, reason="Missing session token")
        return
    claims = decode_access_token_claims(token)
    if not claims or claims.get("sub_kind") not in (None, "id", "username"):
        await websocket.close(code=4401, reason="Invalid session token")
        return

    import websockets

    session_factory = websocket.app.state.session_factory
    async with session_factory() as db:
        user = await get_user_by_subject(db, claims.get("sub"), claims.get("sub_kind"))
        if not user or not user.is_active:
            await websocket.close(code=4401, reason="Inactive or unknown user")
            return
        agent = await _load_bridge_agent(agent_id, db)

    service = websocket.app.state.desktop_gateway_service
    try:
        handle = await service.ensure_agent_gateway(agent)
    except Exception as exc:
        logger.exception("Failed to start desktop gateway for agent %s", agent_id)
        await websocket.close(code=1011, reason=f"Gateway startup failed: {exc}"[:120])
        return

    await websocket.accept()
    service.track_ws_connection(agent_id, 1)
    upstream = None
    try:
        upstream = await websockets.connect(handle.ws_url, max_size=2**24, ping_interval=20, ping_timeout=20)
        await _ws_pump_both_directions(websocket, upstream)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Desktop WS bridge error for agent %s", agent_id)
    finally:
        service.track_ws_connection(agent_id, -1)
        if upstream is not None:
            await upstream.close()
        try:
            await websocket.close()
        except Exception:
            pass


async def _ws_pump_both_directions(client_ws: WebSocket, upstream) -> None:
    import asyncio

    async def client_to_upstream() -> None:
        while True:
            message = await client_ws.receive_text()
            await upstream.send(message)

    async def upstream_to_client() -> None:
        while True:
            message = await upstream.recv()
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


# ── HTTP catch-all proxy ────────────────────────────────────────────────────


@router.api_route(
    "/desktop/{agent_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def desktop_http_proxy(
    agent_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    if not path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Only /desktop/{agent_id}/api/* is proxied")
    upstream_path = "/" + path

    await _resolve_user(request, _extract_session_token(request), db)
    agent = await _load_bridge_agent(agent_id, db)
    service = _bridge_service(request)
    handle = await service.ensure_agent_gateway(agent)
    handle.touch()

    body = b""
    if request.method not in ("GET", "HEAD"):
        body = await request.body()

    url = httpx.URL(f"{handle.base_url}{upstream_path}", params=dict(request.query_params))
    upstream_request = httpx.Request(
        request.method,
        url,
        headers=_forwardable_headers(request, handle.inner_token),
        content=body,
    )

    try:
        client = _proxy_client(request)
        upstream_response = await client.send(upstream_request, stream=True)
        if request.method == "HEAD":
            await upstream_response.aread()
            await upstream_response.aclose()
            return Response(
                status_code=upstream_response.status_code,
                headers=_response_headers(upstream_response),
            )

        async def stream_body():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response),
        )
    except httpx.HTTPError as exc:
        logger.warning("Desktop proxy error for agent %s: %s", agent_id, exc)
        return JSONResponse({"error": "desktop gateway unreachable"}, status_code=502)
