"""Mobile app attachment endpoints – upload, download, delete media files."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.bot_channel import BotChannel
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.attachment import AttachmentDeleteRead, AttachmentUploadRead
from hermeshq.services.delegacion_entre_usuarios import es_cuenta_de_servicio
from hermeshq.services.task_visibility import task_visibility_clause

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["attachments"])

ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".mp3",
    ".aac",
    ".ogg",
    ".wav",
    ".m4a",
    ".flac",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".mkv",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".json",
    ".md",
    ".xml",
    ".html",
    ".zip",
    ".eml",
}

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".xml": "application/xml",
    ".html": "text/html",
    ".zip": "application/zip",
    ".eml": "message/rfc822",
}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _resolve_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return MIME_MAP.get(ext, "application/octet-stream")


def _uploads_dir(workspace_manager, agent_id: str) -> Path:
    workspace = workspace_manager.build_workspace_path(agent_id)
    uploads = workspace / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _metadata_referencia_adjunto(metadata: dict | None, file_id: str) -> bool:
    for clave in ("attachments", "response_attachments"):
        entradas = (metadata or {}).get(clave) or []
        if not isinstance(entradas, list):
            continue
        for att in entradas:
            if not isinstance(att, dict):
                continue
            if str(att.get("file_id") or "").lower() == file_id:
                return True
            if Path(str(att.get("path") or "")).stem.lower() == file_id:
                return True
    return False


async def _tarea_del_usuario_referencia(db: AsyncSession, user_id: str, agent_id: str, file_id: str) -> bool:
    result = await db.execute(
        select(Task.metadata_json).where(
            Task.agent_id == agent_id,
            task_visibility_clause(user_id),
            cast(Task.metadata_json, Text).contains(file_id),
        )
    )
    return any(_metadata_referencia_adjunto(metadata, file_id) for metadata in result.scalars())


async def _canal_admite_cuenta_de_servicio(db: AsyncSession, user_id: str, agent_id: str | None = None):
    filas = (await db.execute(select(BotChannel.id, BotChannel.service_user_id, BotChannel.is_default))).all()
    defecto = next((str(f[0]) for f in filas if f[2]), None)
    duenos = {str(f[0]): (str(f[1]) if f[1] else None) for f in filas}
    servidos = {canal for canal, dueno in duenos.items() if dueno == str(user_id)}
    dueno_del_defecto = duenos.get(defecto) if defecto else None
    defecto_no_atiende_al_agente = False
    if agent_id and dueno_del_defecto and dueno_del_defecto != str(user_id):
        asignado = await db.execute(
            select(AgentAssignment.id).where(
                AgentAssignment.user_id == dueno_del_defecto, AgentAssignment.agent_id == agent_id
            )
        )
        defecto_no_atiende_al_agente = asignado.first() is None

    def admite(metadata: dict | None) -> bool:
        if defecto_no_atiende_al_agente and not str((metadata or {}).get("bot_channel_id") or ""):
            return True
        canal = str((metadata or {}).get("bot_channel_id") or "") or defecto
        if servidos:
            return canal in servidos
        return canal is None or duenos.get(canal) in (None, str(user_id))

    return admite


async def _tarea_de_chat_publico_referencia(db: AsyncSession, user_id: str, agent_id: str, file_id: str) -> bool:
    result = await db.execute(
        select(Task.metadata_json).where(
            Task.agent_id == agent_id,
            Task.metadata_json["source"].as_string() == "public_chat",
            cast(Task.metadata_json, Text).contains(file_id),
        )
    )
    admite = await _canal_admite_cuenta_de_servicio(db, user_id, agent_id)
    return any(_metadata_referencia_adjunto(metadata, file_id) and admite(metadata) for metadata in result.scalars())


async def _ensure_propietario_adjunto(
    db: AsyncSession, workspace_manager, current_user: User, agent_id: str, file_id: str
) -> None:
    if is_admin(current_user):
        return
    propietario = workspace_manager.propietario_adjunto(agent_id, file_id)
    if propietario is not None:
        if propietario == current_user.id:
            return
        raise HTTPException(status_code=404, detail="Attachment not found")
    if await _tarea_del_usuario_referencia(db, current_user.id, agent_id, file_id):
        return
    if es_cuenta_de_servicio(current_user.username) and await _tarea_de_chat_publico_referencia(
        db, current_user.id, agent_id, file_id
    ):
        return
    raise HTTPException(status_code=404, detail="Attachment not found")


@router.post("/{agent_id}/attachments", response_model=AttachmentUploadRead)
async def upload_attachment(
    agent_id: str,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Upload a media file for an agent. Returns file_id and metadata."""
    await ensure_agent_access(db, current_user, agent_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed.",
        )

    file_id = str(uuid.uuid4())
    safe_filename = f"{file_id}{ext}"
    uploads = _uploads_dir(request.app.state.workspace_manager, agent_id)
    file_path = uploads / safe_filename

    total_size = 0
    chunk_size = 1024 * 1024  # 1 MB
    try:
        async with aiofiles.open(file_path, "wb") as f:
            while chunk := await file.read(chunk_size):
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    break
                await f.write(chunk)
    except Exception:
        file_path.unlink(missing_ok=True)
        raise

    if total_size > MAX_FILE_SIZE:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_FILE_SIZE // (1024 * 1024)}MB")

    original = Path(str(file.filename or "")).name.strip()
    media_type = _resolve_media_type(file.filename)
    if ext == ".eml":
        from hermeshq.services.correo_adjunto import CorreoIlegible, eml_a_texto

        try:
            texto = eml_a_texto(file_path.read_bytes())
        except CorreoIlegible as exc:
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"No pude leer el correo: {exc}")
        file_path.unlink(missing_ok=True)
        safe_filename = f"{file_id}.txt"
        file_path = uploads / safe_filename
        file_path.write_text(texto, encoding="utf-8")
        total_size = file_path.stat().st_size
        media_type = "text/plain"
        original = f"{original}.txt" if original else safe_filename

    request.app.state.workspace_manager.registrar_propietario_adjunto(agent_id, file_id, current_user.id)
    from hermeshq.services.hermes_runtime import registrar_nombre_original

    if original:
        registrar_nombre_original(uploads, safe_filename, original)

    relative_path = f"uploads/{safe_filename}"

    return {
        "file_id": file_id,
        "filename": file.filename,
        "media_type": media_type,
        "size": total_size,
        "path": relative_path,
    }


@router.get("/{agent_id}/attachments/{file_id}")
async def download_attachment(
    agent_id: str,
    file_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    """Download an attachment by file_id."""
    await ensure_agent_access(db, current_user, agent_id)

    if not _UUID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="Invalid file ID format")
    file_id = file_id.lower()
    await _ensure_propietario_adjunto(db, request.app.state.workspace_manager, current_user, agent_id, file_id)

    uploads = _uploads_dir(request.app.state.workspace_manager, agent_id)
    matches = list(uploads.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = matches[0]
    media_type = _resolve_media_type(file_path.name)
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=file_path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{agent_id}/attachments/{file_id}", response_model=AttachmentDeleteRead)
async def delete_attachment(
    agent_id: str,
    file_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Delete an attachment by file_id."""
    await ensure_agent_access(db, current_user, agent_id)

    if not _UUID_RE.match(file_id):
        raise HTTPException(status_code=400, detail="Invalid file ID format")
    file_id = file_id.lower()
    await _ensure_propietario_adjunto(db, request.app.state.workspace_manager, current_user, agent_id, file_id)

    uploads = _uploads_dir(request.app.state.workspace_manager, agent_id)
    matches = list(uploads.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Attachment not found")

    for match in matches:
        match.unlink()
    request.app.state.workspace_manager.borrar_propietario_adjunto(agent_id, file_id)

    return {"status": "deleted", "file_id": file_id}
