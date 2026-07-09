from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


SUPPORTED_EXTS = {".m4a", ".ogg", ".mp3", ".wav", ".webm", ".flac", ".aac", ".opus", ".wma"}
MAX_AUDIO_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

_TRANSCRIBE_SCRIPT = """
import sys, json, asyncio, os
for _p in ["/app", os.environ.get("HERMESHQ_BACKEND_ROOT", "")]:
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from hermeshq.services.voice import transcribe

file_path = sys.argv[1]
language = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

with open(file_path, "rb") as f:
    audio_bytes = f.read()

text, lang = asyncio.run(transcribe(audio_bytes, language=language))
print(json.dumps({"text": text, "language": lang}))
"""


def _find_backend_python() -> str:
    """Find the backend Python that has faster_whisper + av installed."""
    for candidate in ("/usr/local/bin/python3.11", "/usr/local/bin/python3", "/usr/bin/python3"):
        if os.path.isfile(candidate):
            return candidate
    return sys.executable


def _handle_transcribe_audio(args, **_kwargs):
    file_path = args.get("file_path", "")
    language = args.get("language", "")

    if not file_path:
        return json.dumps({"success": False, "error": "file_path is required"})

    ext = Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return json.dumps({
            "success": False,
            "error": f"Unsupported audio format: '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTS))}",
        })

    if not os.path.isabs(file_path):
        cwd = os.environ.get("HERMES_SESSION_CWD", os.getcwd())
        file_path = os.path.join(cwd, file_path)

    if not os.path.isfile(file_path):
        return json.dumps({"success": False, "error": f"File not found: {file_path}"})

    file_size = os.path.getsize(file_path)
    if file_size > MAX_AUDIO_FILE_SIZE:
        return json.dumps({"success": False, "error": f"Audio file too large ({file_size // 1024 // 1024}MB). Max {MAX_AUDIO_FILE_SIZE // 1024 // 1024}MB."})
    if file_size < 100:
        return json.dumps({"success": False, "error": "Audio file is empty or too small"})

    lang = language.strip().lower() if language else ""

    try:
        result = subprocess.run(
            [_find_backend_python(), "-c", _TRANSCRIBE_SCRIPT, file_path, lang],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Transcription timed out (>120s)"})
    except Exception as exc:
        return json.dumps({"success": False, "error": f"Failed to run transcription: {exc}"})

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        return json.dumps({"success": False, "error": f"Transcription failed: {stderr}"})

    try:
        data = json.loads(result.stdout.strip().split("\n")[-1])
    except Exception:
        return json.dumps({"success": False, "error": f"Unexpected output: {result.stdout[:200]}"})

    text = data.get("text", "").strip()
    detected_lang = data.get("language", lang or "unknown")

    if not text:
        return json.dumps({"success": False, "error": "No speech detected in audio file"})

    return json.dumps({
        "success": True,
        "text": text,
        "language": detected_lang,
        "file": file_path,
    })


def register(ctx):
    spec = {
        "name": "hq_transcribe_audio",
        "description": (
            "Transcribe an audio file to text. Supports m4a, ogg, mp3, wav, webm, flac, aac, opus, and wma. "
            "Use this when the user sends an audio file or voice message and you need to know what's in it. "
            "The file_path should be relative to the workspace root (e.g. 'uploads/audio.m4a')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the audio file (relative to workspace or absolute)",
                },
                "language": {
                    "type": "string",
                    "description": "Language code (e.g. 'es' for Spanish, 'en' for English). Use 'auto' for auto-detection.",
                },
            },
            "required": ["file_path"],
        },
        "handler": _handle_transcribe_audio,
    }

    ctx.register_tool(
        name=spec["name"],
        toolset="hermeshq_audio",
        schema={
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
        handler=spec["handler"],
        emoji="🎵",
    )
