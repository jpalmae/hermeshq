from __future__ import annotations

import importlib


def _check_import(module_name: str) -> str | None:
    try:
        importlib.import_module(module_name)
        return None
    except Exception as exc:
        return str(exc)


async def test_connection(config: dict, resolve_secret):
    errors: dict[str, str] = {}
    for module_name in ("faster_whisper", "edge_tts"):
        error = _check_import(module_name)
        if error:
            errors[module_name] = error
    if errors:
        return False, "Voice (Edge TTS) dependencies are missing.", {"errors": errors}
    return True, "Voice (Edge TTS) dependencies are available.", {
        "stt_model": str(config.get("stt_model") or "small"),
        "stt_language": str(config.get("stt_language") or "es"),
        "tts_voice": str(config.get("tts_voice") or "es-MX-JorgeNeural"),
    }
