from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.enrolled_device import EnrolledDevice
from hermeshq.models.user import User
from hermeshq.services.enrollment import EnrollmentError, EnrollmentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enrollment", tags=["enrollment"])

DEVICE_TOKEN_HEADER = "X-HermesHQ-Device-Token"


class EnrollRequest(BaseModel):
    agent_id: str
    device_name: str = Field(min_length=1, max_length=128)
    os_info: dict = Field(default_factory=dict)
    guard_fail_mode: str = "fail-open"


class ActivateRequest(BaseModel):
    os_info: dict = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    hermes_version: str | None = None
    bundle_etag: str | None = None


class DeviceRead(BaseModel):
    id: str
    agent_id: str
    user_id: str
    name: str
    os_info: dict
    status: str
    token_version: int
    guard_fail_mode: str
    last_heartbeat: str | None = None
    last_sync_at: str | None = None
    bundle_etag: str | None = None
    hermes_version: str | None = None
    revoked_at: str | None = None
    created_at: str | None = None
    enroll_token: str | None = None


def _service(request: Request) -> EnrollmentService:
    service: EnrollmentService | None = getattr(request.app.state, "enrollment_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Enrollment service unavailable")
    return service


def _device_read(device: EnrolledDevice) -> DeviceRead:
    def iso(value):
        return value.isoformat() if value else None

    return DeviceRead(
        id=device.id,
        agent_id=device.agent_id,
        user_id=device.user_id,
        name=device.name,
        os_info=device.os_info or {},
        status=device.status,
        token_version=device.token_version,
        guard_fail_mode=device.guard_fail_mode,
        last_heartbeat=iso(device.last_heartbeat),
        last_sync_at=iso(device.last_sync_at),
        bundle_etag=device.bundle_etag,
        hermes_version=device.hermes_version,
        revoked_at=iso(device.revoked_at),
        created_at=iso(getattr(device, "created_at", None)),
    )


async def _resolve_device_from_token(
    request: Request,
    device_token: str | None = Header(default=None, alias=DEVICE_TOKEN_HEADER),
) -> EnrolledDevice:
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")
    device = await _service(request).resolve_device_by_token(device_token)
    if not device:
        raise HTTPException(status_code=401, detail="Invalid or revoked device token")
    return device


@router.post("/enroll", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
async def enroll_device(
    payload: EnrollRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    await ensure_agent_access(db, current_user, payload.agent_id)
    service = _service(request)
    try:
        device, enroll_token = await service.create_enrollment(
            payload.agent_id,
            current_user.id,
            payload.device_name,
            os_info=payload.os_info,
            guard_fail_mode=payload.guard_fail_mode,
        )
    except EnrollmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = _device_read(device).model_dump()
    response["enroll_token"] = enroll_token
    return response


@router.post("/devices/{device_id}/activate")
async def activate_device(
    device_id: str,
    payload: ActivateRequest,
    request: Request,
    enroll_token: str | None = Header(default=None, alias="X-HermesHQ-Enroll-Token"),
):
    if not enroll_token:
        raise HTTPException(status_code=401, detail="Missing enrollment token")
    service = _service(request)
    try:
        device = await service.activate_device(device_id, enroll_token, os_info=payload.os_info)
        device_token = await service.issue_device_token(device.id)
    except EnrollmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"device": _device_read(device).model_dump(), "device_token": device_token}


@router.get("/devices")
async def list_devices(
    request: Request,
    agent_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    if agent_id:
        await ensure_agent_access(db, current_user, agent_id)
    devices = await _service(request).list_devices(agent_id)
    return [_device_read(d).model_dump() for d in devices]


@router.get("/devices/bundle")
async def device_bundle(
    request: Request,
    since: str | None = Query(default=None),
    device: EnrolledDevice = Depends(_resolve_device_from_token),
):
    built = await _service(request).build_bundle(device)
    if since and since == built["etag"]:
        from fastapi.responses import Response

        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    return built


@router.post("/devices/heartbeat")
async def device_heartbeat(
    request: Request,
    payload: HeartbeatRequest,
    device: EnrolledDevice = Depends(_resolve_device_from_token),
):
    try:
        result = await _service(request).heartbeat(
            device,
            hermes_version=payload.hermes_version,
            bundle_etag=payload.bundle_etag,
        )
    except EnrollmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["guard_fail_mode"] = device.guard_fail_mode
    return result


@router.get("/devices/{device_id}")
async def get_device(
    device_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    device = await _service(request).get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    async with request.app.state.session_factory() as db:
        await ensure_agent_access(db, current_user, device.agent_id)
    return _device_read(device).model_dump()


@router.delete("/devices/{device_id}", status_code=status.HTTP_200_OK)
async def revoke_device(
    device_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    service = _service(request)
    device = await service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    async with request.app.state.session_factory() as db:
        await ensure_agent_access(db, current_user, device.agent_id)
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required to revoke devices")
    revoked = await service.revoke_device(device_id)
    return _device_read(revoked).model_dump()
