from __future__ import annotations

import json
import os
import time

import requests

DEFAULT_BASE_URL = "https://public-api.gamma.app/v1.0"
REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "HermesHQ-Gamma/1.0",
}

SIXMANAGER_LOGOS = {
    "horizontal": "https://media.sixmanager.io/media/logos/Logo_Sixmanager_horizontal_color.png",
    "vertical": "https://media.sixmanager.io/media/logos/Logo_Sixmanager_vertical_color.png",
    "iso": "https://media.sixmanager.io/media/logos/logowebiso.png",
}


def _configured() -> bool:
    return bool(os.environ.get("GAMMA_API_KEY"))


def _base_url() -> str:
    return os.environ.get("GAMMA_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    api_key = os.environ.get("GAMMA_API_KEY", "").strip()
    return {
        **REQUEST_HEADERS,
        "X-API-KEY": api_key,
    }


def _request(method: str, path: str, payload: dict | None = None, query: dict | None = None) -> str:
    if not _configured():
        return json.dumps({"success": False, "error": "Gamma integration is not configured. Set a Gamma API key secret first."})

    url = f"{_base_url()}{path}"
    try:
        response = requests.request(
            method.upper(),
            url,
            params={k: v for k, v in (query or {}).items() if v is not None},
            json=payload,
            headers=_headers(),
            timeout=120,
        )
        raw = response.text or ""
        try:
            parsed = response.json() if raw else {}
        except Exception:
            parsed = {"raw": raw}
        if response.status_code >= 400:
            return json.dumps(
                {
                    "success": False,
                    "status_code": response.status_code,
                    "error": parsed.get("error") or parsed.get("message") or raw or f"HTTP {response.status_code}",
                    "data": parsed,
                }
            )
        return json.dumps({"success": True, "status_code": response.status_code, "data": parsed})
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def _compose_branding_instructions(args: dict) -> str:
    additional = str(args.get("additional_instructions") or "").strip()
    if not args.get("sixmanager_branding"):
        return additional
    logo_type = str(args.get("logo_type") or "horizontal").strip().lower()
    logo = SIXMANAGER_LOGOS.get(logo_type, SIXMANAGER_LOGOS["horizontal"])
    branding = (
        "Use Sixmanager corporate branding. "
        f"Primary logo: {logo}. "
        f"Alternative logos: horizontal={SIXMANAGER_LOGOS['horizontal']}, vertical={SIXMANAGER_LOGOS['vertical']}, iso={SIXMANAGER_LOGOS['iso']}. "
        "Colors: primary #252E5A, secondary #4689C8, tertiary #58585B. "
        "Typography: Gotham for titles, Gotham Rounded for body, Century Gothic fallback. "
        "Include the logo on the cover and closing slides. Keep the tone professional and technical."
    )
    if additional:
        return f"{branding}\n\n{additional}"
    return branding


def _generation_payload(args: dict, *, content_format: str) -> dict:
    payload: dict = {
        "inputText": str(args.get("input_text") or "").strip(),
        "textMode": str(args.get("text_mode") or "generate"),
        "format": content_format,
    }
    title = str(args.get("title") or "").strip()
    if title and content_format == "presentation":
        payload["inputText"] = f"{title}\n\n{payload['inputText']}"
    if content_format == "presentation":
        payload["numCards"] = int(args.get("num_cards") or 10)
    if args.get("theme_id"):
        payload["themeId"] = str(args["theme_id"])
    if args.get("export_as"):
        payload["exportAs"] = str(args["export_as"])
    instructions = _compose_branding_instructions(args)
    if instructions:
        payload["additionalInstructions"] = instructions
    language = str(args.get("language") or "").strip()
    tone = str(args.get("tone") or "").strip()
    text_options: dict[str, str] = {}
    if language:
        text_options["language"] = language
    if tone:
        text_options["tone"] = tone
    if text_options:
        payload["textOptions"] = text_options
    image_source = str(args.get("image_source") or "").strip()
    image_model = str(args.get("image_model") or "").strip()
    image_options: dict[str, str] = {}
    if image_source:
        image_options["source"] = image_source
    if image_model:
        image_options["model"] = image_model
    if image_options:
        payload["imageOptions"] = image_options
    return payload


def gamma_create_presentation_tool(args, **_kwargs):
    input_text = str(args.get("input_text") or "").strip()
    if not input_text:
        return json.dumps({"success": False, "error": "'input_text' is required"})
    return _request("POST", "/generations", _generation_payload(args, content_format="presentation"))


def gamma_create_document_tool(args, **_kwargs):
    input_text = str(args.get("input_text") or "").strip()
    if not input_text:
        return json.dumps({"success": False, "error": "'input_text' is required"})
    return _request("POST", "/generations", _generation_payload(args, content_format="document"))


def gamma_create_webpage_tool(args, **_kwargs):
    input_text = str(args.get("input_text") or "").strip()
    if not input_text:
        return json.dumps({"success": False, "error": "'input_text' is required"})
    return _request("POST", "/generations", _generation_payload(args, content_format="webpage"))


def gamma_create_social_post_tool(args, **_kwargs):
    input_text = str(args.get("input_text") or "").strip()
    if not input_text:
        return json.dumps({"success": False, "error": "'input_text' is required"})
    return _request("POST", "/generations", _generation_payload(args, content_format="social"))


def gamma_create_from_template_tool(args, **_kwargs):
    template_id = str(args.get("template_id") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if not template_id or not prompt:
        return json.dumps({"success": False, "error": "'template_id' and 'prompt' are required"})
    payload = {
        "gammaId": template_id,
        "prompt": prompt,
    }
    if args.get("theme_id"):
        payload["themeId"] = str(args["theme_id"])
    if args.get("export_as"):
        payload["exportAs"] = str(args["export_as"])
    return _request("POST", "/generations/from-template", payload)


def gamma_get_generation_status_tool(args, **_kwargs):
    generation_id = str(args.get("generation_id") or "").strip()
    if not generation_id:
        return json.dumps({"success": False, "error": "'generation_id' is required"})
    return _request("GET", f"/generations/{generation_id}")


def gamma_wait_for_generation_tool(args, **_kwargs):
    generation_id = str(args.get("generation_id") or "").strip()
    if not generation_id:
        return json.dumps({"success": False, "error": "'generation_id' is required"})
    max_attempts = max(1, min(int(args.get("max_attempts") or 30), 120))
    interval_seconds = max(1, min(int(args.get("interval_seconds") or 5), 60))
    attempts: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        parsed = json.loads(_request("GET", f"/generations/{generation_id}"))
        attempts.append(
            {
                "attempt": attempt,
                "success": parsed.get("success", False),
                "status": ((parsed.get("data") or {}) if isinstance(parsed.get("data"), dict) else {}).get("status"),
            }
        )
        if not parsed.get("success", False):
            parsed["attempts"] = attempts
            return json.dumps(parsed)
        data = parsed.get("data") or {}
        status = str(data.get("status") or "").lower()
        if status in {"completed", "failed"}:
            parsed["attempts"] = attempts
            return json.dumps(parsed)
        time.sleep(interval_seconds)
    return json.dumps(
        {
            "success": False,
            "error": "Gamma generation did not finish before the polling limit.",
            "generation_id": generation_id,
            "attempts": attempts,
        }
    )


def register(ctx):
    common_generation_fields = {
        "input_text": {"type": "string", "description": "Base content or prompt to turn into Gamma output."},
        "title": {"type": "string", "description": "Optional title, mainly useful for presentations."},
        "text_mode": {"type": "string", "enum": ["generate", "condense", "preserve"], "description": "How Gamma should treat the provided text."},
        "theme_id": {"type": "string", "description": "Optional Gamma theme id."},
        "export_as": {"type": "string", "enum": ["pdf", "pptx"], "description": "Optional export format."},
        "additional_instructions": {"type": "string", "description": "Additional design or content instructions."},
        "language": {"type": "string", "description": "Language code, for example es-419 or en."},
        "tone": {"type": "string", "description": "Optional content tone such as professional or casual."},
        "image_source": {"type": "string", "description": "Optional Gamma image source, for example aiGenerated or stock."},
        "image_model": {"type": "string", "description": "Optional Gamma image model, for example imagen-3-pro."},
        "sixmanager_branding": {"type": "boolean", "description": "When true, append Sixmanager branding instructions."},
        "logo_type": {"type": "string", "enum": ["horizontal", "vertical", "iso"], "description": "Preferred Sixmanager logo when branding is enabled."},
    }
    ctx.register_tool(
        name="gamma_create_presentation",
        toolset="hermeshq_gamma_app",
        schema={
            "name": "gamma_create_presentation",
            "description": "Create a Gamma presentation and optionally request PDF or PPTX export.",
            "parameters": {
                "type": "object",
                "properties": {
                    **common_generation_fields,
                    "num_cards": {"type": "integer", "description": "Target number of slides.", "minimum": 1, "maximum": 75},
                },
                "required": ["input_text"],
            },
        },
        handler=gamma_create_presentation_tool,
        check_fn=_configured,
        description="Create Gamma presentations",
        emoji="🎞️",
    )
    for name, description, handler, content_format in [
        ("gamma_create_document", "Create a Gamma document.", gamma_create_document_tool, "document"),
        ("gamma_create_webpage", "Create a Gamma webpage.", gamma_create_webpage_tool, "webpage"),
        ("gamma_create_social_post", "Create Gamma social content.", gamma_create_social_post_tool, "social"),
    ]:
        ctx.register_tool(
            name=name,
            toolset="hermeshq_gamma_app",
            schema={
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": common_generation_fields,
                    "required": ["input_text"],
                },
            },
            handler=handler,
            check_fn=_configured,
            description=description,
            emoji="📝" if content_format == "document" else "🌐" if content_format == "webpage" else "📣",
        )
    ctx.register_tool(
        name="gamma_create_from_template",
        toolset="hermeshq_gamma_app",
        schema={
            "name": "gamma_create_from_template",
            "description": "Create Gamma content from an existing template id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string", "description": "Gamma template id."},
                    "prompt": {"type": "string", "description": "Instructions to adapt the template."},
                    "theme_id": {"type": "string", "description": "Optional Gamma theme id."},
                    "export_as": {"type": "string", "enum": ["pdf", "pptx"], "description": "Optional export format."},
                },
                "required": ["template_id", "prompt"],
            },
        },
        handler=gamma_create_from_template_tool,
        check_fn=_configured,
        description="Create Gamma content from template",
        emoji="🧩",
    )
    ctx.register_tool(
        name="gamma_get_generation_status",
        toolset="hermeshq_gamma_app",
        schema={
            "name": "gamma_get_generation_status",
            "description": "Fetch the current status of a Gamma generation id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "generation_id": {"type": "string", "description": "Gamma generation id."},
                },
                "required": ["generation_id"],
            },
        },
        handler=gamma_get_generation_status_tool,
        check_fn=_configured,
        description="Check Gamma generation status",
        emoji="📡",
    )
    ctx.register_tool(
        name="gamma_wait_for_generation",
        toolset="hermeshq_gamma_app",
        schema={
            "name": "gamma_wait_for_generation",
            "description": "Poll Gamma until a generation completes or fails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "generation_id": {"type": "string", "description": "Gamma generation id."},
                    "max_attempts": {"type": "integer", "description": "Maximum polling attempts."},
                    "interval_seconds": {"type": "integer", "description": "Seconds to wait between polling attempts."},
                },
                "required": ["generation_id"],
            },
        },
        handler=gamma_wait_for_generation_tool,
        check_fn=_configured,
        description="Wait for Gamma generation completion",
        emoji="⏳",
    )
