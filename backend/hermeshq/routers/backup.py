from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from hermeshq.core.security import require_admin
from hermeshq.models.user import User
from hermeshq.schemas.backup import (
    InstanceBackupCreateRequest,
    InstanceBackupRestoreJobRead,
    InstanceBackupRestoreRead,
    InstanceBackupValidationRead,
)
from hermeshq.services.instance_backup import InstanceBackupError

router = APIRouter(prefix="/backup", tags=["backup"])


def _cleanup_path(path: Path) -> None:
    if path.exists():
        path.unlink()


@router.post("/create")
async def create_instance_backup(
    payload: InstanceBackupCreateRequest,
    request: Request,
    _: User = Depends(require_admin),
):
    try:
        archive_path, filename, _summary = await request.app.state.instance_backup_service.create_backup_archive(payload)
    except InstanceBackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup_path, archive_path),
    )


@router.post("/validate", response_model=InstanceBackupValidationRead)
async def validate_instance_backup(
    request: Request,
    file: UploadFile = File(...),
    passphrase: str | None = Form(default=None),
    _: User = Depends(require_admin),
) -> InstanceBackupValidationRead:
    suffix = Path(file.filename or "backup.zip").suffix or ".zip"
    temp_path = Path(tempfile.NamedTemporaryFile(prefix="hermeshq-validate-", suffix=suffix, delete=False).name)
    try:
        temp_path.write_bytes(await file.read())
        return await request.app.state.instance_backup_service.validate_backup_archive(temp_path, passphrase)
    finally:
        _cleanup_path(temp_path)


@router.post("/restore", response_model=InstanceBackupRestoreJobRead, status_code=202)
async def restore_instance_backup(
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(..., min_length=8, max_length=256),
    mode: str = Form("replace"),
    _: User = Depends(require_admin),
) -> InstanceBackupRestoreJobRead:
    suffix = Path(file.filename or "backup.zip").suffix or ".zip"
    temp_path = Path(tempfile.NamedTemporaryFile(prefix="hermeshq-restore-", suffix=suffix, delete=False).name)
    try:
        temp_path.write_bytes(await file.read())
        return await request.app.state.instance_backup_service.start_restore_job(
            temp_path,
            passphrase,
            mode,
            request.app.state,
        )
    except InstanceBackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        _cleanup_path(temp_path)


@router.get("/restore-jobs/{job_id}", response_model=InstanceBackupRestoreJobRead)
async def get_restore_job(
    job_id: str,
    request: Request,
    _: User = Depends(require_admin),
) -> InstanceBackupRestoreJobRead:
    try:
        return request.app.state.instance_backup_service.get_restore_job(job_id)
    except InstanceBackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
