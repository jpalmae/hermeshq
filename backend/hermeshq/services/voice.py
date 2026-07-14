"""Voice service: STT (faster-whisper) and TTS (edge-tts / piper).

Exposes transcription and synthesis using the voice-edge and voice-local
integration packages already bundled with HermesHQ.
"""

import asyncio
import importlib
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.app_settings import AppSettings

logger = logging.getLogger(__name__)

_STT_SEMAPHORE: asyncio.Semaphore | None = None
_TTS_SEMAPHORE: asyncio.Semaphore | None = None
_WHISPER_MODEL: Any = None
_WHISPER_MODEL_NAME: str | None = None


def _get_stt_semaphore() -> asyncio.Semaphore:
    global _STT_SEMAPHORE
    if _STT_SEMAPHORE is None:
        cpu = _cpu_count()
        max_concurrent = max(1, cpu // 2) if cpu > 2 else 1
        _STT_SEMAPHORE = asyncio.Semaphore(max_concurrent)
    return _STT_SEMAPHORE


def _get_tts_semaphore() -> asyncio.Semaphore:
    global _TTS_SEMAPHORE
    if _TTS_SEMAPHORE is None:
        cpu = _cpu_count()
        max_concurrent = max(2, cpu) if cpu > 2 else 2
        _TTS_SEMAPHORE = asyncio.Semaphore(max_concurrent)
    return _TTS_SEMAPHORE


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 2


def _default_whisper_model() -> str:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "base"
    return "small"


async def resolve_active_voice_engine(
    session: AsyncSession,
) -> tuple[str | None, dict[str, Any]]:
    """Return (engine_slug, config_dict) for the active voice package.

    Priority: voice-edge > voice-local. Returns (None, {}) if none available.
    Runs healthcheck before reporting as available.
    """
    row = await session.get(AppSettings, "default")
    if not row:
        return None, {}

    enabled = set(row.enabled_integration_packages or [])

    for slug in ("voice-edge", "voice-local"):
        if slug not in enabled:
            continue

        package_root = _find_package_root(slug)
        if not package_root:
            logger.warning("Voice package %s enabled but not found on disk", slug)
            continue

        config = _read_package_defaults(package_root)
        healthy = await _run_healthcheck(package_root, config)
        if healthy:
            return slug, config
        logger.warning("Voice package %s failed healthcheck, skipping", slug)

    return None, {}


def _find_package_root(slug: str) -> Path | None:
    from hermeshq.services.managed_capabilities import bundled_integration_packages_root

    root = bundled_integration_packages_root() / slug
    if root.is_dir():
        return root
    return None


def _read_package_defaults(package_root: Path) -> dict[str, Any]:
    import yaml

    manifest_path = package_root / "manifest.yaml"
    if not manifest_path.is_file():
        return {}

    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read manifest for %s", package_root.name, exc_info=True)
        return {}

    defaults = manifest.get("defaults") or {}
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64") and defaults.get("stt_model") == "small":
        defaults["stt_model"] = "base"
    return defaults


async def _run_healthcheck(package_root: Path, config: dict) -> bool:
    hc_path = package_root / "healthcheck.py"
    if not hc_path.is_file():
        return False

    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            f"_voice_hc_{package_root.name}", hc_path
        )
        if not spec or not spec.loader:
            return False
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if not hasattr(mod, "test_connection"):
            return False

        async def noop_resolve(_secret_ref: str) -> str:
            return ""

        ok, _msg, _detail = await mod.test_connection(config, noop_resolve)
        return bool(ok)
    except Exception:
        logger.warning("Healthcheck failed for %s", package_root.name, exc_info=True)
        return False


def _load_whisper(model_name: str):
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME

    if _WHISPER_MODEL is not None and _WHISPER_MODEL_NAME == model_name:
        return _WHISPER_MODEL

    from faster_whisper import WhisperModel

    logger.info("Loading Whisper model: %s", model_name)
    _WHISPER_MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")
    _WHISPER_MODEL_NAME = model_name
    return _WHISPER_MODEL


async def transcribe(audio_bytes: bytes, *, language: str | None = None) -> tuple[str, str]:
    """Transcribe audio bytes to text.

    Returns (text, detected_language).
    Runs in a thread via asyncio.to_thread with a semaphore to limit concurrency.
    """
    import io
    import wave

    model_name = _default_whisper_model()
    semaphore = _get_stt_semaphore()

    async with semaphore:
        model = await asyncio.to_thread(_load_whisper, model_name)

        segments, info = await asyncio.to_thread(
            _whisper_transcribe_sync, model, audio_bytes, language
        )

    text = " ".join(seg.text.strip() for seg in segments).strip()
    detected_lang = getattr(info, "language", language or "es")
    return text, detected_lang


def _whisper_transcribe_sync(model, audio_bytes: bytes, language: str | None):
    import numpy as np

    audio_array = _bytes_to_numpy(audio_bytes)
    if audio_array is None:
        raise ValueError("Could not decode audio bytes")

    kwargs: dict[str, Any] = {"beam_size": 5}
    if language and language != "auto":
        kwargs["language"] = language

    return model.transcribe(audio_array, **kwargs)


def _bytes_to_numpy(audio_bytes: bytes):
    """Decode audio bytes (WebM/Opus, WAV, MP3, etc.) to a float32 numpy array at 16kHz.

    faster-whisper expects mono float32 audio sampled at exactly 16kHz.
    We use PyAV's AudioResampler to guarantee the correct sample rate
    regardless of the input container's native rate (Opus=48k, MP3=24k, etc).
    """
    import io

    try:
        import av
        import numpy as np

        container = av.open(io.BytesIO(audio_bytes))

        try:
            # Resample to 16kHz mono float32 — exactly what faster-whisper expects
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)

            audio_frames: list[np.ndarray] = []
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    array = rf.to_ndarray()
                    if array.ndim > 1:
                        array = array.reshape(-1)
                    audio_frames.append(array.astype(np.float32))

            # Flush any samples still buffered in the resampler
            for rf in resampler.resample(None):
                array = rf.to_ndarray()
                if array.ndim > 1:
                    array = array.reshape(-1)
                audio_frames.append(array.astype(np.float32))
        finally:
            container.close()

        if not audio_frames:
            logger.warning("No audio frames decoded from input")
            return None

        audio = np.concatenate(audio_frames).astype(np.float32)
        audio = np.clip(audio, -1.0, 1.0)
        return audio
    except Exception:
        logger.warning("Failed to decode audio with PyAV", exc_info=True)
        return None


async def synthesize(
    text: str,
    *,
    voice: str | None = None,
    engine: str | None = None,
    config: dict | None = None,
) -> bytes:
    """Synthesize text to speech audio (MP3).

    Uses edge-tts for voice-edge engine, piper for voice-local.
    """
    if not engine:
        engine = "voice-edge"

    cfg = config or {}

    if engine == "voice-local":
        return await _synthesize_piper(text, voice or cfg.get("tts_voice", "es_MX-voice"))
    return await _synthesize_edge(text, voice or cfg.get("tts_voice", "es-MX-JorgeNeural"))


async def _synthesize_edge(text: str, voice: str) -> bytes:
    import edge_tts
    import io

    sem = _get_tts_semaphore()
    async with sem:
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        buf.seek(0)
        return buf.read()


async def _synthesize_piper(text: str, voice: str) -> bytes:
    piper_bin = shutil.which("piper")
    if not piper_bin:
        raise RuntimeError("piper binary not found on PATH")

    sem = _get_tts_semaphore()
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            piper_bin,
            "--model", voice,
            "--output-raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate(text.encode("utf-8"))
        return stdout
