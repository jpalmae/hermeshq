from __future__ import annotations

import importlib
import shutil


def _check_import(module_name: str) -> str | None:
    try:
        importlib.import_module(module_name)
        return None
    except Exception as exc:
        return str(exc)


async def test_connection(config: dict, resolve_secret):
    errors: dict[str, str] = {}
    faster_whisper_error = _check_import("faster_whisper")
    if faster_whisper_error:
        errors["faster_whisper"] = faster_whisper_error

    piper_import_error = _check_import("piper")
    piper_binary = shutil.which("piper")
    if piper_import_error and not piper_binary:
        errors["piper"] = piper_import_error

    if errors:
        return False, "Voice (Local) dependencies are missing.", {
            "errors": errors,
            "piper_binary": piper_binary,
        }
    return True, "Voice (Local) dependencies are available.", {
        "stt_model": str(config.get("stt_model") or "small"),
        "stt_language": str(config.get("stt_language") or "es"),
        "tts_voice": str(config.get("tts_voice") or "es_MX-voice"),
        "piper_binary": piper_binary,
    }
