"""Voice endpoints: STT, TTS, and config discovery."""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.rate_limit import RateLimiter
from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.services.voice import (
    resolve_active_voice_engine,
    synthesize,
    transcribe,
)

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_TTS_CHARS = 2000

_stt_limiter = RateLimiter(max_requests=20, window_seconds=60)
_tts_limiter = RateLimiter(max_requests=60, window_seconds=60)


class TTSRequest(BaseModel):
    text: str = Field(..., max_length=MAX_TTS_CHARS)
    voice: str | None = None


@router.get("/voice/config")
async def voice_config(
    db: AsyncSession = Depends(get_db_session),
    _user: User = Depends(get_current_user),
):
    """Return voice capabilities for the UI."""
    engine, config = await resolve_active_voice_engine(db)

    if engine is None:
        return {"engine": None, "stt": False, "tts": False, "voices": []}

    stt_enabled = str(config.get("stt_enabled", "true")).lower() == "true"
    tts_enabled = str(config.get("tts_enabled", "true")).lower() == "true"

    voices: list[str] = []
    if engine == "voice-edge":
        voices = [
            "es-MX-JorgeNeural",
            "es-AR-TomasNeural",
            "es-ES-AlvaroNeural",
            "es-CO-GonzaloNeural",
            "en-US-GuyNeural",
            "en-US-ChristopherNeural",
            "en-GB-RyanNeural",
        ]
    else:
        voices = [config.get("tts_voice", "es_MX-voice")]

    return {
        "engine": engine,
        "stt": stt_enabled,
        "tts": tts_enabled,
        "voices": voices,
        "default_voice": config.get("tts_voice"),
    }


@router.post("/voice/stt")
async def speech_to_text(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
):
    """Transcribe an audio file to text."""
    _stt_limiter.check(user.id)
    content_type = file.content_type or ""
    if not content_type.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Only audio files are accepted")

    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file too large (max 25 MB)")
    if len(audio_bytes) < 100:
        raise HTTPException(status_code=400, detail="Audio file is empty or too short")
    _stt_limiter.record(user.id)

    engine, config = await resolve_active_voice_engine(db)
    if engine is None:
        raise HTTPException(status_code=503, detail="Voice engine not available")

    language = str(config.get("stt_language", "es"))
    if language == "auto":
        language = None

    try:
        text, detected_lang = await transcribe(audio_bytes, language=language)
    except Exception:
        logger.error("STT transcription failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Transcription failed")

    return {"text": text, "language": detected_lang}


@router.post("/voice/tts")
async def text_to_speech(
    payload: TTSRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
):
    """Synthesize text to speech audio (MP3)."""
    _tts_limiter.check(user.id)
    if len(payload.text) > MAX_TTS_CHARS:
        raise HTTPException(status_code=400, detail=f"Text too long (max {MAX_TTS_CHARS} characters)")

    engine, config = await resolve_active_voice_engine(db)
    if engine is None:
        raise HTTPException(status_code=503, detail="Voice engine not available")

    try:
        audio = await synthesize(
            payload.text,
            voice=payload.voice,
            engine=engine,
            config=config,
        )
    except Exception:
        logger.error("TTS synthesis failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Synthesis failed")
    _tts_limiter.record(user.id)

    return Response(content=audio, media_type="audio/mpeg")
