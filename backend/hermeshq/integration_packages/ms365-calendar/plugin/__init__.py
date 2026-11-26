from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_ms365_calendar"

_APPROVAL_TTL_SECONDS = 1800


def _current_task_id() -> str:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    try:
        return str(json.loads(raw).get("task_id") or "") if raw else ""
    except (ValueError, AttributeError):
        return ""


def _modo_automatico() -> bool:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not raw:
        return False
    try:
        meta = json.loads(raw).get("metadata") or {}
    except (ValueError, AttributeError):
        return False
    return bool(meta.get("scheduled") or meta.get("background_job"))


def _previews_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".calendar_previews.json"


def _load_previews() -> dict:
    try:
        data = json.loads(_previews_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    limit = time.time() - _APPROVAL_TTL_SECONDS
    return {k: v for k, v in data.items() if isinstance(v, dict) and float(v.get("ts") or 0) >= limit}


def _save_previews(previews: dict) -> None:
    try:
        path = _previews_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(previews), encoding="utf-8")
    except OSError:
        pass


def _action_fingerprint(user_id: str, *parts) -> str:
    return hashlib.sha256(
        "|".join([user_id, *[json.dumps(p, sort_keys=True, default=str) for p in parts]]).encode()
    ).hexdigest()


_PALABRAS_APROBACION = {
    "si",
    "sip",
    "ok",
    "okay",
    "oka",
    "dale",
    "listo",
    "confirmo",
    "confirmado",
    "adelante",
    "perfecto",
    "correcto",
    "procede",
    "hazlo",
    "autorizo",
    "apruebo",
    "aprobado",
    "yes",
    "crea",
    "creala",
    "crealo",
    "crear",
    "agenda",
    "agendala",
    "agendalo",
    "agendar",
    "acepta",
    "aceptala",
    "aceptalo",
    "aceptar",
    "rechaza",
    "rechazala",
    "rechazalo",
    "responde",
    "respondela",
    "actualiza",
    "actualizala",
    "actualizalo",
    "guarda",
    "guardala",
    "guardalo",
    "envia",
    "enviala",
    "envialo",
    "manda",
    "mandala",
    "mandalo",
}
_PALABRAS_RELLENO = {
    "de",
    "acuerdo",
    "asi",
    "tal",
    "cual",
    "ya",
    "por",
    "favor",
    "porfa",
    "nomas",
    "lo",
    "la",
    "le",
    "el",
    "los",
    "las",
    "ahora",
    "reunion",
    "cita",
    "evento",
    "invitacion",
    "correo",
}
_PALABRAS_QUE_ANULAN = {
    "no",
    "nop",
    "nope",
    "nunca",
    "tampoco",
    "ni",
    "nada",
    "stop",
    "alto",
    "todavia",
    "aun",
    "mejor",
    "deten",
    "detente",
    "pero",
    "sino",
    "cambia",
    "cambialo",
    "cambiala",
    "corrige",
    "modifica",
    "error",
    "parar",
    "parale",
    "parala",
    "paralo",
    "parate",
}
_PREFIJOS_QUE_ANULAN = ("olvid", "cancel", "esper", "aguard", "anul", "descart", "dej", "frena")
_BLOQUE_RESPUESTA_CORTA = re.compile(r"(?s)\[Respuesta corta a tu último mensaje:.*?no a temas anteriores\.\]\s*")
_BLOQUES_DEL_SISTEMA = re.compile(
    r"(?s)\[(?:FORMATO PEDIDO EN ESTE MENSAJE|Hora actual|AVISO DEL SISTEMA|Archivos adjuntos|Archivos de turnos"
    r"|B[uú]squeda en SharePoint)[^\]]*(?:\]|$)"
)
_LINEA_ARCHIVO_PREVIO = re.compile(r"(?m)^\s*- .+ \(hace \d+ min\)\s*$")


def _mensaje_del_usuario() -> str:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return ""
    texto = str((datos.get("mensaje") if isinstance(datos, dict) else "") or "")
    texto = _BLOQUE_RESPUESTA_CORTA.sub(" ", texto)
    texto = _BLOQUES_DEL_SISTEMA.sub(" ", texto)
    return _LINEA_ARCHIVO_PREVIO.sub(" ", texto).strip()


def _palabras_planas(texto: str) -> list[str]:
    return re.findall(r"[a-z]+", unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower())


def _anula(palabra: str) -> bool:
    for forma in {palabra, re.sub(r"(.)\1{2,}", r"\1", palabra), re.sub(r"(.)\1+", r"\1", palabra)}:
        if forma in _PALABRAS_QUE_ANULAN or forma.startswith(_PREFIJOS_QUE_ANULAN):
            return True
    return False


def _usuario_confirma(accion: frozenset = frozenset()) -> bool:
    texto = _mensaje_del_usuario()
    if not texto or len(texto) > 60 or "?" in texto or "¿" in texto:
        return False
    palabras = _palabras_planas(texto)
    if not palabras or any(_anula(p) for p in palabras if p not in accion):
        return False
    if not any(p in _PALABRAS_APROBACION for p in palabras):
        return False
    return all(p in _PALABRAS_APROBACION or p in _PALABRAS_RELLENO or p in accion for p in palabras)


VERBOS_ELIMINAR = frozenset(
    {
        "cancela",
        "cancelala",
        "cancelalo",
        "cancelar",
        "elimina",
        "eliminala",
        "eliminalo",
        "eliminar",
        "borra",
        "borrala",
    }
)
VERBOS_MODIFICAR = frozenset({"cambia", "cambiala", "cambialo", "modifica", "modificala", "modificalo"})


def _approval_gate(args: dict, fingerprint: str, preview: dict, accion: frozenset = frozenset()) -> str | None:
    if _modo_automatico():
        return json.dumps(
            {
                "success": False,
                "error": "No puedo crear, editar ni eliminar citas en modo automático (tarea programada o en segundo "
                "plano): no hay un usuario que apruebe la vista previa. Si debe ejecutarse sin intervención, pídele al "
                "dueño que lo configure como una acción con asistentes pre-aprobados.",
            },
            ensure_ascii=False,
        )
    previews = _load_previews()
    entry = previews.get(fingerprint)
    current = _current_task_id()
    previa_de_otra_tarea = bool(entry and current and entry.get("task") and entry["task"] != current)
    if args.get("aprobado_por_el_usuario") and previa_de_otra_tarea and _usuario_confirma(accion):
        return None
    previews[fingerprint] = {
        "task": current,
        "ts": time.time(),
        **({"en_curso": entry["en_curso"]} if entry and entry.get("en_curso") else {}),
    }
    _save_previews(previews)
    result = {
        "success": False,
        "requiere_aprobacion": True,
        "vista_previa": preview,
        "siguiente": "Muéstrale al usuario esta vista previa y pregunta si lo confirmas; termina tu respuesta ahí y "
        "espera su siguiente mensaje. Cuando diga que sí, vuelve a llamar con aprobado_por_el_usuario=true y "
        "EXACTAMENTE los mismos datos. Si cambias cualquier dato, es una vista previa nueva que debe volver a aprobar.",
    }
    if args.get("aprobado_por_el_usuario"):
        result["error"] = (
            "No se ejecutó: el último mensaje del usuario no es una confirmación clara (no dijo «sí», o lo negó, "
            "canceló o pidió esperar). No lo hagas ni digas que se hizo: pregúntale si lo confirma."
            if previa_de_otra_tarea
            else "No se ejecutó: esta vista previa exacta aún no se le mostró al usuario en un mensaje anterior. No "
            "puedes aprobar por él en la misma respuesta."
        )
    return json.dumps(result, ensure_ascii=False)


def _marcar_en_curso(fingerprint: str) -> None:
    previews = _load_previews()
    entrada = previews.get(fingerprint) or {}
    previews[fingerprint] = {**entrada, "en_curso": time.time(), "ts": entrada.get("ts") or time.time()}
    _save_previews(previews)


def _desmarcar_en_curso(fingerprint: str) -> None:
    previews = _load_previews()
    if fingerprint in previews:
        previews[fingerprint].pop("en_curso", None)
        _save_previews(previews)


def _rechazo_firme(resultado: dict) -> bool:
    return bool(
        re.match(
            r"^(Error \d{3}|Sin permiso|Evento no encontrado|Límite de peticiones)", str(resultado.get("error") or "")
        )
    )


def _evento_ya_creado(token: str, asunto: str, desde: float) -> str:
    ruta = "/me/events?$orderby=createdDateTime desc&$top=15&$select=id,subject,createdDateTime,webLink"
    lista = _graph("GET", ruta, token)
    if not isinstance(lista, dict) or "error" in lista:
        return "no_se_pudo"
    for ev in lista.get("value") or []:
        if str(ev.get("subject") or "").strip() != asunto:
            continue
        try:
            creado = datetime.fromisoformat(str(ev.get("createdDateTime") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if creado.tzinfo is None:
            creado = creado.replace(tzinfo=UTC)
        if creado.timestamp() >= desde - 120:
            return "salio"
    return "no_salio"


def _consume_approval(fingerprint: str) -> None:
    previews = _load_previews()
    previews.pop(fingerprint, None)
    _save_previews(previews)


def _resolver_directorio(token: str, correos: list[str]) -> dict:

    def _esc(c: str) -> str:
        return c.replace("'", "''")

    unicos = [c for c in dict.fromkeys(c for c in correos if c)]
    if not unicos:
        return {}
    clausulas = " or ".join(f"mail eq '{_esc(c)}' or userPrincipalName eq '{_esc(c)}'" for c in unicos[:20])
    res = _graph(
        "GET",
        f"/users?$filter={urllib.parse.quote(clausulas, safe='')}&$select=mail,userPrincipalName,displayName",
        token,
    )
    if not isinstance(res, dict) or "error" in res:
        return {}
    encontrados: dict[str, str] = {}
    for u in res.get("value", []) or []:
        for clave in ("mail", "userPrincipalName"):
            valor = str(u.get(clave) or "").lower()
            if valor:
                encontrados[valor] = u.get("displayName") or valor
    etiquetas: dict[str, str] = {}
    for c in unicos:
        nombre = encontrados.get(c.lower())
        etiquetas[c] = f"✓ en directorio: {nombre}" if nombre else "⚠ NO está en el directorio (externo o inexistente)"
    return etiquetas


def _task_user_id() -> str | None:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if raw:
        try:
            payload = json.loads(raw)
            meta = payload.get("metadata") or {}
            uid = str(meta.get("thread_user_id") or meta.get("created_by_user_id") or "").strip()
            if uid:
                return uid
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _get_m365_token(user_id: str) -> tuple[str | None, str]:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return None, "HermesHQ internal control no configurado"

    url = f"{base_url}/control/m365/agent-token?user_id={user_id}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
            "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
            "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("access_token"), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except (json.JSONDecodeError, ValueError):
            detail = body
        return None, str(detail)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, str(exc)


BROKER_TOKEN = "__broker__"


def _central_activa() -> bool:
    return os.environ.get("HERMESHQ_APROBACION_CENTRAL", "").strip() == "1"


def _cabeceras_control() -> dict:
    return {
        "X-HermesHQ-Agent-ID": os.environ.get("HERMESHQ_AGENT_ID", ""),
        "X-HermesHQ-Agent-Token": os.environ.get("HERMESHQ_AGENT_TOKEN", ""),
        "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
        "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
        "Content-Type": "application/json",
    }


def _llamar_control(metodo: str, ruta: str, payload: dict | None = None, timeout: int = 60) -> dict:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    if not base_url or not os.environ.get("HERMESHQ_AGENT_TOKEN"):
        return {"ok": False, "codigo": "sin_control", "error": "HermesHQ internal control no configurado"}
    datos = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base_url}{ruta}", data=datos, method=metodo, headers=_cabeceras_control())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, **json.loads(resp.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        if isinstance(detalle, dict):
            codigo = detalle.get("motivo") or detalle.get("codigo") or "error"
            return {"ok": False, "codigo": codigo, "error": detalle.get("error") or codigo}
        return {"ok": False, "codigo": "error", "error": str(detalle)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "codigo": "sin_respuesta", "error": f"{type(exc).__name__}: {exc}"}


def _token_herramienta(user_id: str) -> tuple[str | None, str]:
    if _central_activa():
        if not os.environ.get("HERMESHQ_INTERNAL_API_URL") or not _current_task_id():
            return None, "HermesHQ internal control no configurado o sin tarea activa."
        return BROKER_TOKEN, ""
    return _get_m365_token(user_id)


def _broker_graph(method: str, path: str, payload: dict | None = None) -> dict:
    respuesta = _llamar_control(
        "POST",
        "/control/m365/graph",
        {
            "tarea_id": _current_task_id(),
            "metodo": method.upper(),
            "ruta": path,
            **({"cuerpo_json": payload} if payload is not None else {}),
        },
    )
    if not respuesta.get("ok"):
        return {
            "error": f"HermesHQ no autorizó la operación ({respuesta.get('codigo')}).",
            "_graph_code": respuesta.get("codigo"),
        }
    return respuesta.get("json") or {}


def _ya_ejecutada_respuesta(r: dict) -> str:
    return json.dumps(
        {
            "success": True,
            "ya_ejecutada": True,
            "resultado": r.get("resultado") or {},
            "nota": "Esta acción ya se ejecutó con la aprobación del usuario; el resultado es el registrado. No se repitió.",
        },
        ensure_ascii=False,
    )


def _escritura_central(args: dict, metodo: str, ruta: str, payload: dict | None, herramienta: str, tipo: str):
    contenido = {"metodo": metodo, "ruta": ruta, "cuerpo": payload or {}}
    aprobacion_id = str(args.get("aprobacion_id") or "").strip()
    if aprobacion_id:
        r = _llamar_control(
            "POST",
            "/control/m365/accion/ejecutar",
            {"tarea_id": _current_task_id(), "aprobacion_id": aprobacion_id, "contenido": contenido},
            timeout=90,
        )
        if r.get("ok"):
            if r.get("ya_ejecutada"):
                return _ya_ejecutada_respuesta(r)
            return r.get("resultado") or {}
        codigo = r.get("codigo")
        if codigo in ("ya_ejecutada", "accion_ya_ejecutada"):
            return json.dumps(
                {
                    "success": False,
                    "ya_ejecutada": True,
                    "error": "Esa acción ya se ejecutó con esta aprobación: no la repetí.",
                },
                ensure_ascii=False,
            )
        if codigo in ("conciliacion_manual_requerida", "conciliacion_prematura"):
            return json.dumps(
                {
                    "success": False,
                    "estado": "desconocido",
                    "codigo": codigo,
                    "error": "No sé todavía si se aplicó y no lo repito. "
                    + (
                        "Hay que revisarlo a mano en Outlook/Teams."
                        if codigo == "conciliacion_manual_requerida"
                        else "Vuelve a intentarlo en unos minutos: primero se verifica en Microsoft 365."
                    ),
                },
                ensure_ascii=False,
            )
        if codigo in ("resultado_incierto", "sin_respuesta", "conciliacion_requerida"):
            return json.dumps(
                {
                    "success": False,
                    "estado": "desconocido",
                    "codigo": codigo,
                    "error": "No sé si se aplicó. No la repito: hay que conciliarla primero.",
                },
                ensure_ascii=False,
            )
        if codigo in ("rechazada", "pendiente"):
            return json.dumps(
                {
                    "success": False,
                    "codigo": codigo,
                    "error": "El usuario rechazó la acción."
                    if codigo == "rechazada"
                    else "El usuario todavía no la aprueba.",
                },
                ensure_ascii=False,
            )
        if codigo == "fallida":
            return json.dumps(
                {
                    "success": False,
                    "codigo": codigo,
                    "error": f"La acción aprobada NO se ejecutó. {r.get('error') or ''} No se repitió. "
                    "Para intentarlo otra vez hay que pedir una aprobación nueva.",
                },
                ensure_ascii=False,
            )
        if codigo not in ("huella_distinta", "expirada"):
            return json.dumps(
                {"success": False, "codigo": codigo, "error": r.get("error") or codigo}, ensure_ascii=False
            )
    creada = _llamar_control(
        "POST",
        "/control/aprobaciones",
        {
            "task_id": _current_task_id(),
            "tipo": tipo,
            "contenido": contenido,
            "parametros": {
                "herramienta": herramienta,
                "args": {k: v for k, v in args.items() if k not in ("aprobado_por_el_usuario", "aprobacion_id")},
            },
        },
    )
    if not creada.get("ok") or not creada.get("id"):
        return json.dumps(
            {"success": False, "codigo": creada.get("codigo"), "error": creada.get("error")}, ensure_ascii=False
        )
    if creada.get("estado") == "ejecutada" and not args.get("_reintento_aprobada"):
        return _escritura_central(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True), metodo, ruta, payload, herramienta, tipo
        )
    if creada.get("estado") == "ejecutando":
        return json.dumps(
            {
                "success": False,
                "estado": "desconocido",
                "aprobacion_id": creada["id"],
                "error": "Esta acción ya está en curso o pendiente de conciliación. No la repito.",
            },
            ensure_ascii=False,
        )
    if creada.get("estado") == "aprobada" and not args.get("_reintento_aprobada"):
        return _escritura_central(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True), metodo, ruta, payload, herramienta, tipo
        )
    salida = {
        "success": False,
        "requiere_aprobacion": True,
        "aprobacion_id": creada["id"],
        "borrador": creada.get("vista_previa"),
        "siguiente": "Muestra el campo 'borrador' TAL CUAL y pregunta si lo apruebas. Solo el usuario aprueba. "
        "Después vuelve a llamar con aprobacion_id y exactamente los mismos datos.",
    }
    if aprobacion_id:
        salida["error"] = "La acción cambió o la aprobación anterior ya no vale: se pidió una aprobación nueva."
    if creada.get("tarjeta"):
        salida["tarjeta_aprobacion"] = creada["tarjeta"]
    return json.dumps(salida, ensure_ascii=False)


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    if access_token == BROKER_TOKEN:
        return _broker_graph(method, path, payload)
    url = _encode_url(path if path.startswith("https://") else f"{GRAPH_BASE}{path}")
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        try:
            parsed = json.loads(body)
            err = parsed.get("error", {})
            code = err.get("code", "")
            msg = err.get("message", body)
        except (json.JSONDecodeError, ValueError):
            code, msg = "", body
        _FRIENDLY = {
            "Authorization_RequestDenied": "Sin permiso para acceder a este recurso (403).",
            "accessDenied": "Sin permiso para acceder a este recurso (403).",
            "itemNotFound": "Evento no encontrado (404).",
            "ActivityLimitReached": f"Límite de peticiones alcanzado. Reintenta en {retry_after or '60'} segundos.",
        }
        friendly = _FRIENDLY.get(code) or (f"Error {exc.code}: {msg}" if msg else f"Error HTTP {exc.code}")
        result: dict = {"error": friendly, "_graph_code": code}
        if retry_after:
            result["retry_after"] = retry_after
        return result


def _auth_error(detail: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"No se pudo obtener token M365: {detail}. Verifica que el usuario haya conectado su cuenta Microsoft 365 en Mi cuenta.",
        }
    )


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _utc_stamp(value, label: str) -> tuple[str | None, str | None]:
    text = (
        str(value or "").strip().replace(" ", "+")
        if re.search(r"\d \d\d:\d\d$", str(value or "").strip())
        else str(value or "").strip()
    )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"{label} debe ser una fecha ISO 8601, por ejemplo 2026-10-08T13:00:00Z."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


_ZONA_POR_DEFECTO = "America/Santiago"


def _instante_en_zona(valor, zona: str, dia_completo: bool):
    if not isinstance(valor, dict) or not valor.get("dateTime"):
        return valor
    texto = str(valor["dateTime"])
    base = texto.split(".")[0]
    if dia_completo:
        return {"dateTime": base, "timeZone": zona}
    try:
        from zoneinfo import ZoneInfo

        origen = ZoneInfo(str(valor.get("timeZone") or "UTC"))
        instante = datetime.fromisoformat(base).replace(tzinfo=origen).astimezone(ZoneInfo(zona))
    except Exception:
        return valor
    return {"dateTime": instante.replace(tzinfo=None).isoformat(), "timeZone": zona}


def _evento_en_zona(evento, zona: str):
    if not isinstance(evento, dict):
        return evento
    dia_completo = bool(evento.get("isAllDay"))
    copia = dict(evento)
    for clave in ("start", "end"):
        if clave in copia:
            copia[clave] = _instante_en_zona(copia[clave], zona, dia_completo)
    return copia


_TU_RESPUESTA = {
    "organizer": "organizas tú",
    "accepted": "aceptada",
    "tentativelyaccepted": "provisional",
    "declined": "rechazada",
    "notresponded": "sin responder",
    "none": "sin responder",
}


def _estado_del_evento(original: dict, convertido: dict, zona: str, ahora: datetime | None = None) -> dict:
    from zoneinfo import ZoneInfo

    local = (ahora or datetime.now(UTC)).astimezone(ZoneInfo(zona)).replace(tzinfo=None)

    def _hora(clave):
        valor = str(((convertido.get(clave) or {}).get("dateTime")) or "")[:19]
        try:
            return datetime.fromisoformat(valor)
        except ValueError:
            return None

    inicio, fin = _hora("start"), _hora("end")
    estado = {}
    if original.get("isCancelled"):
        estado["momento"] = "cancelada"
    elif inicio and fin:
        estado["momento"] = "terminó" if fin <= local else ("en curso" if inicio <= local else "próxima")
    if original.get("isOrganizer"):
        estado["tu_respuesta"] = "organizas tú"
    else:
        respuesta = str(((original.get("responseStatus") or {}).get("response")) or "").lower()
        if respuesta in _TU_RESPUESTA:
            estado["tu_respuesta"] = _TU_RESPUESTA[respuesta]
    return estado


def _zona_pedida(args: dict) -> str:
    zona = str((args or {}).get("timezone") or "").strip() or _ZONA_POR_DEFECTO
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(zona)
    except Exception:
        return _ZONA_POR_DEFECTO
    return zona


_EVENTO_INEXISTENTE = (
    "No existe un evento con ese event_id en el calendario del usuario. No escribas ids de memoria: búscalo con "
    "ms365_calendar_list_events en el rango de fechas y usa el id exacto que devuelve."
)


def _evento_no_existe(info) -> bool:
    if not isinstance(info, dict) or "error" not in info:
        return False
    codigo = str(info.get("_graph_code") or "")
    error = str(info.get("error") or "")
    return (
        codigo in ("ErrorItemNotFound", "itemNotFound", "ErrorInvalidIdMalformed")
        or "(404)" in error
        or error.startswith("Error 404")
    )


def _vista_con_evento_actual(gate: str, token: str, event_id: str, zona: str, info=None) -> str:
    try:
        respuesta = json.loads(gate)
    except (TypeError, ValueError):
        return gate
    if not isinstance(respuesta, dict) or not respuesta.get("requiere_aprobacion"):
        return gate
    if info is None:
        info = _graph("GET", f"/me/events/{event_id}?$select=subject,start,end,isAllDay", token)
    if _evento_no_existe(info):
        return json.dumps({"success": False, "error": _EVENTO_INEXISTENTE}, ensure_ascii=False)
    if isinstance(info, dict) and "error" not in info:
        local = _evento_en_zona(info, zona)
        vista = respuesta.get("vista_previa")
        if isinstance(vista, dict):
            vista["evento_actual"] = {
                "asunto": local.get("subject"),
                "inicio": (local.get("start") or {}).get("dateTime"),
                "fin": (local.get("end") or {}).get("dateTime"),
                "zona_horaria": zona,
            }
    return json.dumps(respuesta, ensure_ascii=False)


_PENDIENTE_TTL = 4 * 3600
_REF_EVENTO = re.compile(r"^#?[Ee](\d{1,3})$")


def _listado_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".calendario_listado.json"


def _guardar_listado(eventos: list) -> None:
    validos = [e for e in eventos if isinstance(e, dict) and e.get("ref") and e.get("id")]
    datos = {
        "ts": time.time(),
        "conv": _conversacion_actual(),
        "task": _current_task_id(),
        "eventos": {e["ref"]: e["id"] for e in validos},
        "titulos": {e["ref"]: str(e.get("subject") or "")[:120] for e in validos},
    }
    try:
        ruta = _listado_path()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(datos), encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError:
        pass


def _resolver_evento(valor) -> tuple[str | None, str | None]:
    texto = str(valor or "").strip()
    if not texto:
        return None, "Se requiere event_id."
    coincide = _REF_EVENTO.fullmatch(texto)
    if not coincide:
        return texto, None
    ref = f"E{int(coincide.group(1))}"
    try:
        datos = json.loads(_listado_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return texto, None
    vigente = (
        isinstance(datos, dict)
        and time.time() - float(datos.get("ts") or 0) <= _PENDIENTE_TTL
        and str(datos.get("conv") or "") == _conversacion_actual()
    )
    real = (datos.get("eventos") or {}).get(ref) if vigente else None
    if not real:
        return texto, None
    if str(datos.get("task") or "") in ("", _current_task_id()):
        return real, None
    pendiente = _leer_pendiente()
    if (
        pendiente
        and str(pendiente.get("conv") or "") == _conversacion_actual()
        and str((pendiente.get("args") or {}).get("event_id") or "") == real
    ):
        return real, None
    titulo = str((datos.get("titulos") or {}).get(ref) or "")
    era = f" (en ese listado {ref} era «{titulo}»)" if titulo else ""
    return None, (
        f"La referencia {ref} es de un listado de un mensaje anterior{era} y puede no ser la reunión que pide el "
        'usuario. Búscala de nuevo en este mensaje con ms365_calendar_list_events(buscar="<título de la reunión>") y '
        "usa la ref nueva."
    )


def _pendiente_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".calendario_pendiente.json"


def _payload_tarea() -> dict:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _conversacion_actual() -> str:
    meta = _payload_tarea().get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    for campo in ("thread_id", "public_session_id"):
        valor = str(meta.get(campo) or "").strip()
        if valor:
            return valor
    return ""


def _registrar_pendiente(herramienta: str, args: dict, gate: str) -> str:
    try:
        respuesta = json.loads(gate)
    except (TypeError, ValueError):
        return gate
    if not isinstance(respuesta, dict) or not respuesta.get("requiere_aprobacion"):
        return gate
    datos = {
        "herramienta": herramienta,
        "args": {k: v for k, v in args.items() if k not in ("aprobado_por_el_usuario", "aprobacion_id")},
        "task": _current_task_id(),
        "conv": _conversacion_actual(),
        "ts": time.time(),
        "preview": respuesta.get("vista_previa") or {},
    }
    try:
        ruta = _pendiente_path()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError:
        pass
    return gate


def _leer_pendiente() -> dict | None:
    try:
        datos = json.loads(_pendiente_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict) or time.time() - float(datos.get("ts") or 0) > _PENDIENTE_TTL:
        return None
    return datos


def _borrar_pendiente() -> None:
    try:
        _pendiente_path().unlink()
    except OSError:
        pass


def _sin_espacios(texto: str) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


def _vista_mostrada(pendiente: dict) -> bool:
    anterior = _sin_espacios(_payload_tarea().get("respuesta_anterior"))
    mostrada = _sin_espacios(pendiente.get("mostrada"))
    return bool(anterior and mostrada and mostrada in anterior)


def ejecutar_aprobacion_pendiente() -> dict | None:
    if _modo_automatico() or _central_activa():
        return None
    pendiente = _leer_pendiente()
    if not pendiente or pendiente.get("task") == _current_task_id():
        return None
    if str(pendiente.get("conv") or "") != _conversacion_actual():
        return None
    acciones = {
        "create": ("ms365_calendar_create_event", _create_event_tool, frozenset()),
        "update": ("ms365_calendar_update_event", _update_event_tool, VERBOS_MODIFICAR),
        "delete": ("ms365_calendar_delete_event", _delete_event_tool, VERBOS_ELIMINAR),
    }
    if pendiente.get("herramienta") not in acciones:
        return None
    nombre, funcion, verbos = acciones[pendiente["herramienta"]]
    preview = pendiente.get("preview") or {}
    if not _usuario_confirma(verbos) or not _vista_mostrada(pendiente):
        return None
    args = dict(pendiente.get("args") or {}, aprobado_por_el_usuario=True)
    try:
        resultado = json.loads(funcion(args))
    except Exception as exc:  # noqa: BLE001
        resultado = {"success": False, "error": f"Falló la operación de calendario: {exc}"}
    if not resultado.get("requiere_aprobacion"):
        _borrar_pendiente()
    return {"herramienta": nombre, "args": args, "resultado": resultado, "preview": preview}


def _plano_evento(texto) -> str:
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", sin_acentos.lower())).strip()


def _buscar_eventos(token: str, start: str, end: str, fields: str, buscar: str, count: int) -> tuple[list, str | None]:
    desde = datetime.fromisoformat(start.replace("Z", "+00:00"))
    hasta = datetime.fromisoformat(end.replace("Z", "+00:00"))
    hallados, vistos = [], set()
    tramo = desde
    while tramo < hasta and len(hallados) < count:
        fin_tramo = min(tramo + timedelta(days=30), hasta)
        ruta = (
            f"/me/calendarView?startDateTime={tramo.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&endDateTime={fin_tramo.strftime('%Y-%m-%dT%H:%M:%SZ')}&$top=100&$select={fields}"
            "&$orderby=start/dateTime"
        )
        resultado = _graph("GET", ruta, token)
        if "error" in resultado:
            return [], resultado["error"]
        for evento in resultado.get("value", []) or []:
            if (
                isinstance(evento, dict)
                and evento.get("id") not in vistos
                and buscar in _plano_evento(evento.get("subject"))
            ):
                vistos.add(evento.get("id"))
                hallados.append(evento)
        tramo = fin_tramo
    return hallados[:count], None


def _list_events_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    count = min(int(args.get("count") or 10), 50)
    buscar = _plano_evento(args.get("buscar"))
    now = datetime.now(UTC)
    defecto_inicio = now - timedelta(days=60) if buscar else now
    defecto_fin = now + timedelta(days=120 if buscar else 7)
    start, start_error = _utc_stamp(args.get("start") or defecto_inicio.isoformat(), "start")
    end, end_error = _utc_stamp(args.get("end") or defecto_fin.isoformat(), "end")
    if start_error or end_error:
        return json.dumps({"success": False, "error": start_error or end_error}, ensure_ascii=False)
    fields = (
        "id,subject,start,end,location,organizer,isAllDay,bodyPreview,webLink,responseStatus,isOrganizer,isCancelled"
    )
    if buscar:
        originales, error_busqueda = _buscar_eventos(token, start, end, fields, buscar, count)
        if error_busqueda:
            return json.dumps({"success": False, "error": error_busqueda}, ensure_ascii=False)
    else:
        path = f"/me/calendarView?startDateTime={start}&endDateTime={end}&$top={count}&$select={fields}&$orderby=start/dateTime"
        result = _graph("GET", path, token)
        if "error" in result:
            return json.dumps({"success": False, "error": result["error"]})
        originales = [e for e in result.get("value", [])]
    zona = _zona_pedida(args)
    events = []
    for original in originales:
        evento = _evento_en_zona(original, zona)
        if isinstance(evento, dict):
            for clave in ("responseStatus", "isOrganizer", "isCancelled", "@odata.etag"):
                evento.pop(clave, None)
            if evento.get("bodyPreview"):
                evento["bodyPreview"] = str(evento["bodyPreview"])[:160]
            evento.update(_estado_del_evento(original, evento, zona))
        events.append(evento)
    for posicion, evento in enumerate(events, start=1):
        if isinstance(evento, dict):
            evento["ref"] = f"E{posicion}"
    _guardar_listado(events)
    return json.dumps(
        {
            "success": True,
            "count": len(events),
            "zona_horaria": zona,
            "como_referirse": "Para ver, modificar o cancelar un evento pasa su ref (E1, E2…) como event_id; no copies el id largo.",
            "estado": "momento (terminó / en curso / próxima / cancelada) y tu_respuesta salen del calendario: úsalos tal "
            "cual y no inventes otro estado; si un evento no trae tu_respuesta, no la menciones.",
            **(
                {
                    "busqueda": f"Eventos cuyo título contiene «{args.get('buscar')}» entre {start[:10]} y {end[:10]}."
                    + ("" if events else " No hay ninguno: dilo así; no recorras el año por tramos.")
                }
                if buscar
                else {}
            ),
            "events": events,
        },
        ensure_ascii=False,
    )


def _get_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    event_id, error_ref = _resolver_evento(args.get("event_id"))
    if error_ref:
        return json.dumps({"success": False, "error": error_ref}, ensure_ascii=False)
    args = {**args, "event_id": event_id}
    result = _graph("GET", f"/me/events/{event_id}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    zona = _zona_pedida(args)
    return json.dumps(
        {"success": True, "zona_horaria": zona, "event": _evento_para_el_modelo(result, zona)}, ensure_ascii=False
    )


_RESPUESTA_ASISTENTE = {
    "accepted": "aceptó",
    "declined": "rechazó",
    "tentativelyaccepted": "provisional",
    "organizer": "organiza",
    "none": "sin responder",
    "notresponded": "sin responder",
}
_TIPO_ASISTENTE = {"required": "obligatorio", "optional": "opcional", "resource": "sala o recurso"}


def _evento_para_el_modelo(original: dict, zona: str) -> dict:
    evento = _evento_en_zona(original, zona)
    if not isinstance(evento, dict):
        return evento
    evento.pop("@odata.etag", None)
    cuerpo = evento.get("body")
    if isinstance(cuerpo, dict) and cuerpo.get("content"):
        texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(cuerpo["content"]))).strip()
        evento["body"] = {"contentType": "text", "content": texto[:800]}
    asistentes = []
    for a in original.get("attendees") or []:
        if not isinstance(a, dict):
            continue
        correo = (a.get("emailAddress") or {}).get("address")
        respuesta = str(((a.get("status") or {}).get("response")) or "none").lower()
        asistentes.append(
            {
                "nombre": (a.get("emailAddress") or {}).get("name") or correo,
                "correo": correo,
                "tipo": _TIPO_ASISTENTE.get(str(a.get("type") or "").lower(), a.get("type")),
                "respuesta": _RESPUESTA_ASISTENTE.get(respuesta, respuesta),
            }
        )
    if asistentes:
        evento["asistentes_y_respuestas"] = asistentes
        evento.pop("attendees", None)
    for clave in ("responseStatus", "isOrganizer", "isCancelled"):
        evento.pop(clave, None)
    evento.update(_estado_del_evento(original, evento, zona))
    return evento


def _split_attendees(value) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    addresses: list[str] = []
    for item in items:
        for part in re.split(r"[;,\s]+", str(item)):
            part = part.strip().strip("<>\"'")
            if "@" in part and part.lower() not in (a.lower() for a in addresses):
                addresses.append(part)
    return addresses


def _truthy(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "si", "sí", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return None
    return bool(value)


def _teams_fields(args: dict) -> dict:
    wanted = _truthy(args.get("online_meeting"))
    if wanted is None:
        return {}
    if wanted:
        return {"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"}
    return {"isOnlineMeeting": False}


_IMPORTANCE = {"low", "normal", "high"}
_SHOW_AS = {"free", "tentative", "busy", "oof", "workingelsewhere"}


def _extra_fields(args: dict) -> tuple[dict, str | None]:
    fields: dict = {}
    if args.get("importance") is not None:
        value = str(args["importance"]).strip().lower()
        if value not in _IMPORTANCE:
            return {}, "importance debe ser low, normal o high."
        fields["importance"] = value
    if args.get("show_as") is not None:
        value = str(args["show_as"]).strip().lower()
        if value not in _SHOW_AS:
            return {}, "show_as debe ser free, tentative, busy, oof o workingElsewhere."
        fields["showAs"] = "workingElsewhere" if value == "workingelsewhere" else value
    if args.get("reminder_minutes") is not None:
        try:
            fields["reminderMinutesBeforeStart"] = max(0, min(int(args["reminder_minutes"]), 20160))
        except (TypeError, ValueError):
            return {}, "reminder_minutes debe ser un número de minutos."
        fields["isReminderOn"] = True
    all_day = _truthy(args.get("all_day"))
    if all_day is not None:
        fields["isAllDay"] = all_day
    if args.get("categories") is not None:
        raw = args["categories"]
        items = raw if isinstance(raw, list) else re.split(r"[;,]", str(raw))
        fields["categories"] = [str(c).strip() for c in items if str(c).strip()]
    return fields, None


def _parse_graph_datetime(value: str) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_response(result: dict) -> str:
    online = result.get("onlineMeeting") or {}
    out: dict = {"success": True, "event": result}
    if online.get("joinUrl"):
        out["teams_join_url"] = online["joinUrl"]
    return json.dumps(out, ensure_ascii=False)


def _hora_local(valor: str, zona: str) -> str:
    texto = str(valor or "").strip()
    try:
        instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return texto
    if instante.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            instante = instante.astimezone(ZoneInfo(zona)).replace(tzinfo=None)
        except Exception:
            return texto
    return instante.replace(microsecond=0).isoformat()


_PALABRA_DE_SALA = re.compile(r"[\wáéíóúñüÁÉÍÓÚÑÜ.\-]{1,40}")


def _buscar_sala(token: str, nombre) -> tuple[dict | None, str | None]:
    texto = str(nombre or "").strip()
    if not texto:
        return None, None
    if "@" in texto:
        correo = texto.strip("<> ")
        etiqueta = _resolver_directorio(token, [correo]).get(correo, "")
        if etiqueta.startswith("⚠"):
            return None, f"La sala «{correo}» no está en el directorio: pide al usuario el nombre o el correo exacto."
        nombre_dir = etiqueta.split(":", 1)[1].strip() if etiqueta.startswith("✓") and ":" in etiqueta else correo
        return {"address": correo, "name": nombre_dir}, None
    palabras = [p for p in _PALABRA_DE_SALA.findall(texto) if _plano_evento(p) not in ("sala", "de", "la", "el")][:5]
    if not palabras:
        return None, f"«{texto}» no es un nombre de sala: pide al usuario el nombre de la sala."
    termino = " AND ".join(f'"displayName:{p}"' for p in palabras)
    ruta = f"/users?$search={urllib.parse.quote(termino, safe='')}&$top=15&$count=true&$select=displayName,mail"
    res = _graph("GET", ruta, token, headers={"ConsistencyLevel": "eventual"})
    if not isinstance(res, dict) or "error" in res:
        return None, (
            f"No pude buscar la sala «{texto}» en el directorio ({(res or {}).get('error')}). No la escribas como texto "
            "del lugar: dile al usuario que no pude reservarla."
        )
    buscadas = [_plano_evento(p) for p in palabras]
    candidatas = [
        {"address": str(u.get("mail")), "name": str(u.get("displayName") or u.get("mail"))}
        for u in res.get("value") or []
        if isinstance(u, dict)
        and "@" in str(u.get("mail") or "")
        and all(b in _plano_evento(u.get("displayName")) for b in buscadas)
    ]
    salas = [c for c in candidatas if "sala" in _plano_evento(c["name"])] or candidatas
    if len(salas) == 1:
        return salas[0], None
    if not salas:
        return None, (
            f"No encontré la sala «{texto}» en el directorio. No la pongas como texto del lugar: pregúntale al usuario "
            "el nombre exacto o el correo de la sala."
        )
    opciones = "; ".join(f"{c['name']} ({c['address']})" for c in salas[:6])
    return None, f"Hay varias salas que calzan con «{texto}»: {opciones}. Pregúntale al usuario cuál reservar."


def _sala_como_recurso(sala: dict) -> tuple[dict, dict]:
    asistente = {"emailAddress": {"address": sala["address"], "name": sala["name"]}, "type": "resource"}
    lugar = {"displayName": sala["name"], "locationEmailAddress": sala["address"], "locationType": "conferenceRoom"}
    return asistente, lugar


def _create_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    subject = str(args.get("subject") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if not subject or not start or not end:
        return json.dumps({"success": False, "error": "Se requieren: subject, start, end (ISO 8601)."})
    timezone_str = str(args.get("timezone") or _ZONA_POR_DEFECTO)
    start = _hora_local(start, timezone_str)
    end = _hora_local(end, timezone_str)
    error_horas = _fin_antes_del_inicio(start, timezone_str, end, timezone_str)
    if error_horas:
        return json.dumps({"success": False, "error": error_horas}, ensure_ascii=False)
    body_content = str(args.get("body") or "")
    location = str(args.get("location") or "").strip()
    attendees_raw = args.get("attendees") or []
    attendees = [
        {"emailAddress": {"address": address}, "type": "required"} for address in _split_attendees(attendees_raw)
    ]
    attendees += [
        {"emailAddress": {"address": address}, "type": "optional"}
        for address in _split_attendees(args.get("optional_attendees"))
        if address.lower() not in {a["emailAddress"]["address"].lower() for a in attendees}
    ]
    sala, error_sala = _buscar_sala(token, args.get("sala"))
    if error_sala:
        return json.dumps({"success": False, "error": error_sala}, ensure_ascii=False)
    if sala:
        recurso, lugar_sala = _sala_como_recurso(sala)
        attendees = [a for a in attendees if a["emailAddress"]["address"].lower() != sala["address"].lower()]
        attendees.append(recurso)
    extra, extra_error = _extra_fields(args)
    if extra_error:
        return json.dumps({"success": False, "error": extra_error}, ensure_ascii=False)
    payload: dict = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone_str},
        "end": {"dateTime": end, "timeZone": timezone_str},
    }
    if body_content:
        payload["body"] = {"contentType": "HTML", "content": body_content}
    if sala:
        payload["location"] = lugar_sala
    elif location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = attendees
    payload.update(_teams_fields(args))
    payload.update(extra)
    preview = {
        "accion": "crear evento",
        "asunto": subject,
        "inicio": {"dateTime": start, "timeZone": timezone_str},
        "fin": {"dateTime": end, "timeZone": timezone_str},
        "asistentes": [a["emailAddress"]["address"] for a in attendees if a.get("type") != "resource"],
        **({"sala": f"{sala['name']} ({sala['address']})"} if sala else {}),
        **(
            {"opcionales": [a["emailAddress"]["address"] for a in attendees if a.get("type") == "optional"]}
            if any(a.get("type") == "optional" for a in attendees)
            else {}
        ),
        **({"reunion_teams": bool(payload["isOnlineMeeting"])} if "isOnlineMeeting" in payload else {}),
        "ubicacion": (location or None) if not sala else None,
        "descripcion": re.sub(r"<[^>]+>", " ", body_content).strip()[:500] or None,
        **({"importancia": extra["importance"]} if extra.get("importance") else {}),
        **(
            {"recordatorio_minutos": extra["reminderMinutesBeforeStart"]}
            if "reminderMinutesBeforeStart" in extra
            else {}
        ),
    }
    verif = (
        {}
        if args.get("aprobado_por_el_usuario")
        else _resolver_directorio(
            token, [a["emailAddress"]["address"] for a in attendees if a.get("type") != "resource"]
        )
    )
    if verif:
        preview["verificacion_directorio"] = verif
    fingerprint = _action_fingerprint(user_id, "create", "POST", "/me/events", payload)
    if _central_activa():
        result = _escritura_central(args, "POST", "/me/events", payload, "create", "graph.calendario")
        if isinstance(result, str):
            return result
    else:
        gate = _approval_gate(args, fingerprint, preview)
        if gate is not None:
            return _registrar_pendiente("create", args, gate)
        desde = float((_load_previews().get(fingerprint) or {}).get("en_curso") or 0)
        if desde:
            estado = _evento_ya_creado(token, subject, desde)
            if estado == "salio":
                _consume_approval(fingerprint)
                return json.dumps(
                    {
                        "success": True,
                        "ya_creado": True,
                        "message": "Esa cita ya se había creado en el intento anterior (está en el calendario); no la "
                        "creé otra vez.",
                    },
                    ensure_ascii=False,
                )
            if estado == "no_se_pudo":
                return json.dumps(
                    {
                        "success": False,
                        "error": "El intento anterior se cortó y no pude revisar el calendario para saber si la cita "
                        "se creó. No la creo otra vez para no duplicarla: pídele al usuario que mire su calendario "
                        "y diga si hay que crearla.",
                    },
                    ensure_ascii=False,
                )
        _marcar_en_curso(fingerprint)
        result = _graph("POST", "/me/events", token, payload)
    if "error" in result:
        if not _central_activa() and _rechazo_firme(result):
            _desmarcar_en_curso(fingerprint)
        return json.dumps({"success": False, "error": result["error"]})
    _consume_approval(fingerprint)
    return _event_response(result)


def _attendee_entry(address: str, existing: dict | None = None) -> dict:
    if existing:
        return existing
    return {"emailAddress": {"address": address}, "type": "required"}


def _build_attendee_patch(args: dict, current: list[dict] | None) -> list[dict] | None:
    full = args.get("attendees")
    add = _split_attendees(args.get("add_attendees"))
    add_optional = _split_attendees(args.get("add_optional_attendees"))
    remove = {a.lower() for a in _split_attendees(args.get("remove_attendees"))}
    if full is None and not add and not add_optional and not remove:
        return None
    if full is not None:
        base = [_attendee_entry(a) for a in _split_attendees(full)]
        if current:
            known = {((c.get("emailAddress") or {}).get("address") or "").lower(): c for c in current}
            base = [
                _attendee_entry(b["emailAddress"]["address"], known.get(b["emailAddress"]["address"].lower()))
                for b in base
            ]
    else:
        base = list(current or [])
    present = {((b.get("emailAddress") or {}).get("address") or "").lower() for b in base}
    for address in add:
        if address.lower() not in present:
            base.append(_attendee_entry(address))
            present.add(address.lower())
    for address in add_optional:
        if address.lower() not in present:
            base.append({"emailAddress": {"address": address}, "type": "optional"})
            present.add(address.lower())
    return [b for b in base if ((b.get("emailAddress") or {}).get("address") or "").lower() not in remove]


def _instante(valor, zona: str) -> datetime | None:
    texto = str(valor or "").strip()
    try:
        momento = datetime.fromisoformat(texto[:26].replace("Z", "+00:00"))
    except ValueError:
        return None
    if momento.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo

            momento = momento.replace(tzinfo=ZoneInfo(zona))
        except Exception:  # noqa: BLE001
            return None
    return momento


def _fin_antes_del_inicio(inicio, zona_inicio: str, fin, zona_fin: str) -> str | None:
    a, b = _instante(inicio, zona_inicio), _instante(fin, zona_fin)
    if a and b and b <= a:
        return "La hora de fin queda antes (o igual) que la de inicio: revisa las horas (son hora de Chile) y repite."
    return None


def _update_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    event_id, error_ref = _resolver_evento(args.get("event_id"))
    if error_ref:
        return json.dumps({"success": False, "error": error_ref}, ensure_ascii=False)
    args = {**args, "event_id": event_id}
    patch: dict = {}
    timezone_str = str(args.get("timezone") or _ZONA_POR_DEFECTO)
    if args.get("subject"):
        patch["subject"] = str(args["subject"]).strip()
    if args.get("end"):
        patch["end"] = {"dateTime": _hora_local(str(args["end"]), timezone_str), "timeZone": timezone_str}
    if args.get("start"):
        patch["start"] = {"dateTime": _hora_local(str(args["start"]), timezone_str), "timeZone": timezone_str}
        duration = None
        if args.get("duration_minutes") is not None:
            try:
                duration = timedelta(minutes=max(1, int(args["duration_minutes"])))
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "duration_minutes debe ser un número."})
        elif not args.get("end"):
            current = _graph("GET", f"/me/events/{event_id}?$select=start,end", token)
            if "error" in current:
                return json.dumps({"success": False, "error": current["error"]})
            old_start = _parse_graph_datetime((current.get("start") or {}).get("dateTime"))
            old_end = _parse_graph_datetime((current.get("end") or {}).get("dateTime"))
            if old_start and old_end and old_end > old_start:
                duration = old_end - old_start
        new_start = _parse_graph_datetime(str(args["start"]))
        if duration is not None and new_start is not None and not args.get("end"):
            zone = timezone_str
            if new_start.tzinfo is not None:
                new_start = new_start.astimezone(UTC)
                zone = "UTC"
            patch["start"] = {"dateTime": new_start.replace(tzinfo=None).isoformat(), "timeZone": zone}
            patch["end"] = {"dateTime": (new_start + duration).replace(tzinfo=None).isoformat(), "timeZone": zone}
    elif args.get("duration_minutes") is not None:
        current = _graph("GET", f"/me/events/{event_id}?$select=start", token)
        if "error" in current:
            return json.dumps({"success": False, "error": current["error"]})
        cur_start = current.get("start") or {}
        base = _parse_graph_datetime(cur_start.get("dateTime"))
        try:
            minutes = max(1, int(args["duration_minutes"]))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "duration_minutes debe ser un número."})
        if base is None:
            return json.dumps({"success": False, "error": "No se pudo leer la hora de inicio actual del evento."})
        patch["end"] = {
            "dateTime": (base + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(),
            "timeZone": cur_start.get("timeZone") or "UTC",
        }
    if patch.get("end") and patch.get("start"):
        error_horas = _fin_antes_del_inicio(
            patch["start"]["dateTime"], patch["start"]["timeZone"], patch["end"]["dateTime"], patch["end"]["timeZone"]
        )
        if error_horas:
            return json.dumps({"success": False, "error": error_horas}, ensure_ascii=False)
    elif patch.get("end"):
        current = _graph("GET", f"/me/events/{event_id}?$select=start", token)
        if "error" not in current:
            inicio_actual = current.get("start") or {}
            error_horas = _fin_antes_del_inicio(
                inicio_actual.get("dateTime"),
                inicio_actual.get("timeZone") or "UTC",
                patch["end"]["dateTime"],
                patch["end"]["timeZone"],
            )
            if error_horas:
                return json.dumps({"success": False, "error": error_horas}, ensure_ascii=False)
    if args.get("body") is not None:
        patch["body"] = {"contentType": "HTML", "content": str(args["body"])}
    sala, error_sala = _buscar_sala(token, args.get("sala"))
    if error_sala:
        return json.dumps({"success": False, "error": error_sala}, ensure_ascii=False)
    if sala:
        recurso, patch["location"] = _sala_como_recurso(sala)
    elif args.get("location") is not None:
        patch["location"] = {"displayName": str(args["location"])}
    extra, extra_error = _extra_fields(args)
    if extra_error:
        return json.dumps({"success": False, "error": extra_error}, ensure_ascii=False)
    patch.update(extra)
    patch.update(_teams_fields(args))
    wants_attendees = (
        args.get("attendees") is not None
        or args.get("add_attendees") is not None
        or args.get("add_optional_attendees") is not None
        or args.get("remove_attendees")
        or sala
    )
    if wants_attendees:
        existing = _graph("GET", f"/me/events/{event_id}?$select=attendees", token)
        if "error" in existing:
            return json.dumps({"success": False, "error": existing["error"]})
        current = existing.get("attendees") or []
        attendees = _build_attendee_patch(args, current)
        if sala:
            base = attendees if attendees is not None else list(current)
            base = [
                a
                for a in base
                if ((a.get("emailAddress") or {}).get("address") or "").lower() != sala["address"].lower()
                and a.get("type") != "resource"
            ]
            attendees = base + [recurso]
        if attendees is not None:
            patch["attendees"] = attendees
    if not patch:
        return json.dumps({"success": False, "error": "No se proporcionaron campos para actualizar."})
    preview: dict = {"accion": "actualizar evento", "event_id": event_id}
    if "subject" in patch:
        preview["nuevo_asunto"] = patch["subject"]
    if "start" in patch:
        preview["nuevo_inicio"] = patch["start"]
    if "end" in patch:
        preview["nuevo_fin"] = patch["end"]
    if sala:
        preview["nueva_sala"] = f"{sala['name']} ({sala['address']})"
    elif "location" in patch:
        preview["nueva_ubicacion"] = (patch["location"] or {}).get("displayName")
    if "importance" in patch:
        preview["nueva_importancia"] = patch["importance"]
    if "reminderMinutesBeforeStart" in patch:
        preview["nuevo_recordatorio_minutos"] = patch["reminderMinutesBeforeStart"]
    if "attendees" in patch:
        cur_addr = {((c.get("emailAddress") or {}).get("address") or "").lower() for c in current}
        new_addr = {((a.get("emailAddress") or {}).get("address") or "").lower() for a in patch["attendees"]}
        preview["asistentes_agregados"] = sorted(new_addr - cur_addr - ({sala["address"].lower()} if sala else set()))
        preview["asistentes_QUITADOS_reciben_cancelacion"] = sorted(cur_addr - new_addr)
        preview["asistentes_finales"] = sorted(new_addr)
        verif = (
            {}
            if args.get("aprobado_por_el_usuario")
            else _resolver_directorio(
                token, [((a.get("emailAddress") or {}).get("address")) for a in patch["attendees"]]
            )
        )
        if verif:
            preview["verificacion_directorio"] = verif
    fingerprint = _action_fingerprint(user_id, "update", event_id, patch)
    if _central_activa():
        result = _escritura_central(args, "PATCH", f"/me/events/{event_id}", patch, "update", "graph.calendario")
        if isinstance(result, str):
            return result
    else:
        gate = _approval_gate(args, fingerprint, preview, VERBOS_MODIFICAR)
        if gate is not None:
            return _registrar_pendiente(
                "update", args, _vista_con_evento_actual(gate, token, event_id, _zona_pedida(args))
            )
        result = _graph("PATCH", f"/me/events/{event_id}", token, patch)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    _consume_approval(fingerprint)
    return _event_response(result)


def _delete_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    event_id, error_ref = _resolver_evento(args.get("event_id"))
    if error_ref:
        return json.dumps({"success": False, "error": error_ref}, ensure_ascii=False)
    args = {**args, "event_id": event_id}
    info = _graph("GET", f"/me/events/{event_id}?$select=subject,start,end,isAllDay,attendees", token)
    preview = {
        "accion": "ELIMINAR evento (cancela la reunión a todos los asistentes)",
        "event_id": event_id,
        "asunto": info.get("subject") if isinstance(info, dict) else None,
        "inicio": ((info.get("start") or {}) if isinstance(info, dict) else {}).get("dateTime"),
        "asistentes_que_reciben_cancelacion": [
            ((a.get("emailAddress") or {}).get("address"))
            for a in ((info.get("attendees") or []) if isinstance(info, dict) else [])
        ],
    }
    fingerprint = _action_fingerprint(user_id, "delete", event_id)
    if _central_activa():
        result = _escritura_central(args, "DELETE", f"/me/events/{event_id}", None, "delete", "graph.calendario")
        if isinstance(result, str):
            return result
    else:
        gate = _approval_gate(args, fingerprint, preview, VERBOS_ELIMINAR)
        if gate is not None:
            return _registrar_pendiente(
                "delete", args, _vista_con_evento_actual(gate, token, event_id, _zona_pedida(args), info)
            )
        result = _graph("DELETE", f"/me/events/{event_id}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    _consume_approval(fingerprint)
    return json.dumps({"success": True, "message": "Evento eliminado correctamente."})


_RESPUESTA_GRAPH = {
    "aceptar": ("accept", "ACEPTAR la invitación"),
    "acepto": ("accept", "ACEPTAR la invitación"),
    "si": ("accept", "ACEPTAR la invitación"),
    "sí": ("accept", "ACEPTAR la invitación"),
    "tentativa": ("tentativelyAccept", "aceptar PROVISIONALMENTE (tentativa) la invitación"),
    "provisional": ("tentativelyAccept", "aceptar PROVISIONALMENTE (tentativa) la invitación"),
    "quizas": ("tentativelyAccept", "aceptar PROVISIONALMENTE (tentativa) la invitación"),
    "rechazar": ("decline", "RECHAZAR la invitación"),
    "declinar": ("decline", "RECHAZAR la invitación"),
    "no": ("decline", "RECHAZAR la invitación"),
}


def _resolver_evento_de_mensaje(token: str, message_id: str) -> tuple[str | None, str | None]:
    path = f"/me/messages/{message_id}?$expand=microsoft.graph.eventMessage/event&$select=id,subject"
    res = _graph("GET", path, token)
    if isinstance(res, dict) and "error" in res:
        return None, res["error"]
    evento = (res.get("event") or {}) if isinstance(res, dict) else {}
    event_id = str(evento.get("id") or "").strip()
    if not event_id:
        return None, (
            "Ese correo no trae una cita de Outlook asociada (quizá no es una invitación de calendario o ya no "
            "está activa). Busca el evento con ms365_calendar_list_events y pásame su event_id."
        )
    return event_id, None


def _respond_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    mapeo = _RESPUESTA_GRAPH.get(str(args.get("respuesta") or "").strip().lower())
    if not mapeo:
        return json.dumps(
            {"success": False, "error": "respuesta debe ser 'aceptar', 'tentativa' o 'rechazar'."},
            ensure_ascii=False,
        )
    accion_graph, accion_texto = mapeo
    event_id = str(args.get("event_id") or "").strip()
    if event_id:
        event_id, error_ref = _resolver_evento(event_id)
        if error_ref:
            return json.dumps({"success": False, "error": error_ref}, ensure_ascii=False)
    if not event_id:
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            return json.dumps(
                {"success": False, "error": "Se requiere event_id (o message_id del correo de invitación)."},
                ensure_ascii=False,
            )
        event_id, resolve_err = _resolver_evento_de_mensaje(token, message_id)
        if not event_id:
            return json.dumps({"success": False, "error": resolve_err}, ensure_ascii=False)
    info = _graph(
        "GET",
        f"/me/events/{event_id}?$select=subject,start,end,organizer,responseStatus,isOnlineMeeting",
        token,
    )
    if isinstance(info, dict) and "error" in info:
        return json.dumps({"success": False, "error": info["error"]}, ensure_ascii=False)
    enviar = _truthy(args.get("enviar_respuesta"))
    enviar = True if enviar is None else enviar
    comentario = str(args.get("comentario") or "").strip()
    info = info if isinstance(info, dict) else {}
    preview = {
        "accion": accion_texto,
        "event_id": event_id,
        "asunto": info.get("subject"),
        "inicio": (info.get("start") or {}).get("dateTime"),
        "fin": (info.get("end") or {}).get("dateTime"),
        "organizador": ((info.get("organizer") or {}).get("emailAddress") or {}).get("address"),
        "mi_respuesta_actual": (info.get("responseStatus") or {}).get("response"),
        "avisa_al_organizador": bool(enviar),
        "comentario": comentario or None,
    }
    payload: dict = {"sendResponse": bool(enviar)}
    if comentario:
        payload["comment"] = comentario
    fingerprint = _action_fingerprint(user_id, "respond", event_id, accion_graph, payload)
    if _central_activa():
        result = _escritura_central(
            args, "POST", f"/me/events/{event_id}/{accion_graph}", payload, "respond", "graph.calendario"
        )
        if isinstance(result, str):
            return result
    else:
        gate = _approval_gate(args, fingerprint, preview)
        if gate is not None:
            return gate
        result = _graph("POST", f"/me/events/{event_id}/{accion_graph}", token, payload)
    if isinstance(result, dict) and "error" in result:
        return json.dumps({"success": False, "error": result["error"]}, ensure_ascii=False)
    _consume_approval(fingerprint)
    return json.dumps(
        {
            "success": True,
            "respuesta": accion_texto,
            "asunto": preview["asunto"],
            "inicio": preview["inicio"],
            "aviso_al_organizador": bool(enviar),
        },
        ensure_ascii=False,
    )


# ── Plugin registration ───────────────────────────────────────────────────────


def register(ctx):
    ctx.register_tool(
        name="ms365_calendar_list_events",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_list_events",
            "description": "Lista eventos del calendario Microsoft 365 del usuario en un rango de fechas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "buscar": {
                        "type": "string",
                        "description": "Texto del título de la reunión que buscas (p. ej. «Prueba HermesHQ»): devuelve "
                        "solo esas, entre 60 días atrás y 120 adelante, en una llamada. No recorras el calendario por tramos.",
                    },
                    "start": {"type": "string", "description": "Inicio del rango en ISO 8601 (default: ahora)"},
                    "end": {"type": "string", "description": "Fin del rango en ISO 8601 (default: +7 días)"},
                    "count": {"type": "integer", "description": "Número de eventos (máx 50, default 10)"},
                    "timezone": {
                        "type": "string",
                        "description": "Zona en la que se devuelven las horas (default: America/Santiago); ya vienen convertidas.",
                    },
                },
            },
        },
        handler=_list_events_tool,
        description="Listar eventos de calendario M365",
        emoji="📅",
    )
    ctx.register_tool(
        name="ms365_calendar_get_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_get_event",
            "description": "Obtiene el detalle completo de un evento del calendario M365 por su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Ref del listado (E1, E2…) o ID del evento."},
                },
                "required": ["event_id"],
            },
        },
        handler=_get_event_tool,
        description="Obtener evento de calendario M365",
        emoji="🗓️",
    )
    ctx.register_tool(
        name="ms365_calendar_create_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_create_event",
            "description": (
                "Crea un nuevo evento en el calendario Microsoft 365 del usuario. La primera llamada devuelve "
                "una vista previa (asistentes y fecha/hora) SIN crear; muéstrasela al usuario y solo con "
                "aprobado_por_el_usuario=true y los mismos datos, en un mensaje posterior, se crea."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Título del evento"},
                    "start": {"type": "string", "description": "Fecha/hora de inicio en ISO 8601"},
                    "end": {"type": "string", "description": "Fecha/hora de fin en ISO 8601"},
                    "timezone": {
                        "type": "string",
                        "description": "Zona horaria (default: America/Santiago: una hora sin zona es hora de Chile)",
                    },
                    "body": {"type": "string", "description": "Descripción del evento (HTML)"},
                    "location": {"type": "string", "description": "Ubicación del evento (texto; no reserva salas)"},
                    "sala": {
                        "type": "string",
                        "description": "Sala de reuniones a reservar, por su nombre (ej. «Sala Sur») o su correo. "
                        "La herramienta la busca en el directorio y la invita como recurso; no la pongas en location.",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Importancia del evento (high = alta).",
                    },
                    "reminder_minutes": {
                        "type": "integer",
                        "description": "Recordatorio: minutos antes del inicio (ej. 15, 60, 1440 = un día).",
                    },
                    "online_meeting": {
                        "type": "boolean",
                        "description": (
                            "true para habilitar una reunión de Microsoft Teams en el evento. "
                            "La respuesta trae teams_join_url con el enlace para unirse."
                        ),
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de emails de asistentes (obligatorios)",
                    },
                    "optional_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de emails de asistentes opcionales",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["subject", "start", "end"],
            },
        },
        handler=_create_event_tool,
        description="Crear evento en calendario M365",
        emoji="➕",
    )
    ctx.register_tool(
        name="ms365_calendar_update_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_update_event",
            "description": (
                "Actualiza un evento existente en el calendario M365. La primera llamada devuelve una vista "
                "previa con el diff (incluidos los asistentes que reciben cancelación) SIN aplicar; solo con "
                "aprobado_por_el_usuario=true y los mismos datos, en un mensaje posterior, se aplica."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Ref del listado (E1, E2…) del evento a actualizar; no copies el id largo.",
                    },
                    "subject": {"type": "string", "description": "Nuevo título"},
                    "start": {"type": "string", "description": "Nueva fecha/hora de inicio ISO 8601"},
                    "end": {"type": "string", "description": "Nueva fecha/hora de fin ISO 8601"},
                    "timezone": {"type": "string", "description": "Zona horaria"},
                    "body": {"type": "string", "description": "Nueva descripción"},
                    "location": {"type": "string", "description": "Nueva ubicación (texto; no reserva salas)"},
                    "sala": {
                        "type": "string",
                        "description": "Sala de reuniones a reservar, por su nombre o su correo: se busca en el "
                        "directorio, se invita como recurso y queda como lugar. No la pongas en location ni en "
                        "add_attendees.",
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Lista COMPLETA de asistentes que deben quedar (reemplaza la actual; los omitidos "
                            "reciben cancelación). Prefiere add_attendees / remove_attendees para no perder a nadie."
                        ),
                    },
                    "add_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a AGREGAR a los asistentes actuales (conserva a los que ya están)",
                    },
                    "add_optional_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a AGREGAR como asistentes opcionales",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": (
                            "Nueva duración en minutos. Con start nuevo y sin end, se conserva la duración "
                            "actual salvo que indiques esta."
                        ),
                    },
                    "remove_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a QUITAR de los asistentes actuales (reciben cancelación)",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Importancia del evento (high = alta).",
                    },
                    "reminder_minutes": {
                        "type": "integer",
                        "description": "Recordatorio: minutos antes del inicio (ej. 15, 60, 1440 = un día).",
                    },
                    "online_meeting": {
                        "type": "boolean",
                        "description": (
                            "true para habilitar una reunión de Microsoft Teams en el evento. "
                            "La respuesta trae teams_join_url con el enlace para unirse."
                        ),
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["event_id"],
            },
        },
        handler=_update_event_tool,
        description="Actualizar evento en calendario M365",
        emoji="✏️",
    )
    ctx.register_tool(
        name="ms365_calendar_delete_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_delete_event",
            "description": (
                "Elimina un evento del calendario Microsoft 365 del usuario (cancela la reunión a los "
                "asistentes). La primera llamada devuelve una vista previa SIN eliminar; solo con "
                "aprobado_por_el_usuario=true, en un mensaje posterior, se elimina."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Ref del listado (E1, E2…) del evento a eliminar; no copies el id largo.",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["event_id"],
            },
        },
        handler=_delete_event_tool,
        description="Eliminar evento de calendario M365",
        emoji="🗑️",
    )
    ctx.register_tool(
        name="ms365_calendar_respond_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_respond_event",
            "description": (
                "Responde una invitación de reunión del calendario Microsoft 365 del usuario: aceptar, tentativa "
                "(provisional) o rechazar. Pasa event_id, o message_id del correo de invitación (se resuelve a la "
                "cita asociada). La primera llamada devuelve una vista previa SIN responder; solo con "
                "aprobado_por_el_usuario=true, en un mensaje posterior, se envía la respuesta al organizador. Úsala "
                "en vez de crear un evento 'de respaldo' a mano."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "respuesta": {
                        "type": "string",
                        "description": "aceptar | tentativa | rechazar",
                    },
                    "event_id": {"type": "string", "description": "ID de la cita en el calendario (si lo tienes)."},
                    "message_id": {
                        "type": "string",
                        "description": (
                            "ID del correo de invitación; se resuelve a la cita asociada. Úsalo si no tienes event_id."
                        ),
                    },
                    "comentario": {
                        "type": "string",
                        "description": "Nota opcional para el organizador (p. ej. 'Confirmo asistencia').",
                    },
                    "enviar_respuesta": {
                        "type": "boolean",
                        "description": "true (default) avisa al organizador; false responde sin notificarle.",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["respuesta"],
            },
        },
        handler=_respond_event_tool,
        description="Responder invitación de calendario M365",
        emoji="✅",
    )
