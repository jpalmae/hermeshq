from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

MAX_BYTES_DESCARGA = 25 * 1024 * 1024
MAX_BYTES_SUBIDA = 25 * 1024 * 1024
GRAPH_BASE_REAL = "https://graph.microsoft.com/v1.0"


def _graph_base() -> str:
    import os

    if (
        os.environ.get("HERMESHQ_ENTORNO") == "aislado"
        and os.environ.get("HERMESHQ_GRAPH_BASE_SIMULADO")
        and os.environ.get("HERMESHQ_GRAPH_TOKEN_SIMULADO")
    ):
        return os.environ["HERMESHQ_GRAPH_BASE_SIMULADO"].rstrip("/")
    return GRAPH_BASE_REAL


GRAPH_BASE = _graph_base()
OPERACIONES = frozenset({"list", "read", "download", "search", "upload"})
OPERACIONES_CORREO = frozenset(
    {"mail_folders", "mail_list", "mail_read", "mail_search", "mail_mark_read", "directorio"}
)
CLASES_OPERACION = {
    "mail_folders": "lectura",
    "mail_child_folders": "lectura",
    "mail_list": "lectura",
    "mail_read": "lectura",
    "mail_search": "lectura",
    "mail_attachments": "lectura",
    "directorio": "lectura",
    "mail_mark_read": "modificacion_reversible",
    "calendario_lectura": "lectura",
    "teams_lectura": "lectura",
    "upload": "modificacion",
    "session": "modificacion",
}


def clase_de(metodo: str, ruta: str) -> str:
    try:
        return CLASES_OPERACION.get(
            clasificar(metodo, ruta).operacion, "lectura" if metodo.upper() == "GET" else "accion_externa"
        )
    except RutaNoPermitida:
        return "denegada"


def es_subida(metodo: str, ruta: str) -> bool:
    try:
        return clasificar(metodo, ruta).operacion in ("upload", "session")
    except RutaNoPermitida:
        return False


class RutaNoPermitida(PermissionError):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True)
class PoliticaCorreo:
    operaciones: frozenset[str] = frozenset()
    carpetas: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PoliticaGraph:
    correo: PoliticaCorreo = PoliticaCorreo()
    unidades: frozenset[str] = frozenset()
    carpetas_lectura: tuple[str, ...] = ()
    carpetas_escritura: tuple[str, ...] = ()
    sitios: frozenset[str] = frozenset()
    url_sitio: frozenset[str] = frozenset()
    operaciones: frozenset[str] = frozenset()
    calendario: frozenset[str] = frozenset()
    teams: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Clasificacion:
    operacion: str
    unidad: str | None = None
    carpeta: str | None = None
    item_id: str | None = None
    es_me: bool = False
    host: str | None = None
    ruta_sitio: str | None = None
    sitio: str | None = None
    es_sitio_ruta: bool = False
    es_correo: bool = False
    query: str = ""


_RE_UNIDAD = re.compile(
    r"^/drives/(?P<d>[^/:?]+)/root(?::/(?P<p>.*?))?"
    r"(?P<acc>:/children|:/content|:/createUploadSession|/children|/content|/createUploadSession)?$"
)
_RE_UNIDAD_ITEM = re.compile(r"^/drives/(?P<d>[^/:?]+)/items/(?P<i>[^/:?]+)(?P<acc>/children|/content)?$")
_RE_ME = re.compile(
    r"^/me/drive(?:/root(?::/(?P<p>.*?))?"
    r"(?P<acc>:/children|:/content|:/createUploadSession|/children|/content|/createUploadSession)?"
    r"|/items/(?P<i>[^/:?]+)(?P<acc2>/children|/content)?)?$"
)
_RE_CORREO_MENSAJES = re.compile(r"^/me/mailFolders/(?P<f>[^/:?]+)/messages$")
_RE_CORREO_MENSAJE = re.compile(r"^/me/messages/(?P<i>[^/:?]+)$")
_RE_CORREO_SUBCARPETAS = re.compile(r"^/me/mailFolders/(?P<f>[^/:?]+)/childFolders$")
_RE_CORREO_ADJUNTOS = re.compile(r"^/me/messages/(?P<i>[^/:?]+)/attachments(?:/(?P<a>[^/:?]+)(?P<v>/\$value)?)?$")
_RE_FILTRO_DIRECTORIO = re.compile(
    r"^(?:(?:mail|userPrincipalName) eq '[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,120}'(?: or (?!$))?){1,40}$"
)
_SELECT_DIRECTORIO = {
    "mail",
    "userPrincipalName",
    "displayName",
    "jobTitle",
    "department",
    "scoredEmailAddresses",
    "id",
    "officeLocation",
}
_RE_BUSQUEDA_DIRECTORIO = re.compile(
    r'^(?:"mail:[A-Za-z0-9._%+\'-]{1,64}@[A-Za-z0-9.-]{1,120}"'
    r'|"displayName:[\wáéíóúñüÁÉÍÓÚÑÜ.\-]{1,40}"(?: AND "displayName:[\wáéíóúñüÁÉÍÓÚÑÜ.\-]{1,40}"){0,5})$'
)
_ID_MENSAJE = re.compile(r"^[A-Za-z0-9_\-=+/.!]{8,400}$")
_CAMPOS_MENSAJE = (
    "id,subject,from,sender,toRecipients,ccRecipients,receivedDateTime,sentDateTime,"
    "isRead,hasAttachments,importance,conversationId,body,webLink"
)
_MAX_CUERPO = 100_000
_PREFER_PERMITIDOS = {'outlook.body-content-type="text"', 'outlook.body-content-type="html"'}
_RE_SITIO_UNIDADES = re.compile(r"^/sites/(?P<s>[^/:?]+)/drives$")
_RE_SITIO_DESCUBRIR = re.compile(r"^/sites/(?P<host>[^/:?]+):(?P<path>/[^?:]*)$")
_RE_BUSQUEDA_UNIDAD = re.compile(r"^/drives/(?P<d>[^/:?]+)/root/search\(q='(?:[^']|'')*'\)$")
_RE_SITIO_RUTA = re.compile(r"^/sites/(?P<host>[^/:?]+):(?P<path>/[^?:]*):/drive/root(?::/(?P<rest>[^?]*))?$")


def _relativa(p: str | None) -> str:
    if p is None:
        return ""
    if "%" in p:
        p = urllib.parse.unquote(p)
        if "%" in p:
            raise RutaNoPermitida("ruta_con_segmentos_prohibidos")
    if "\\" in p:
        raise RutaNoPermitida("ruta_con_segmentos_prohibidos")
    limpia = p.strip("/")
    if any(seg in ("..", ".") for seg in limpia.split("/")):
        raise RutaNoPermitida("ruta_con_segmentos_prohibidos")
    return limpia


def clasificar(metodo: str, ruta: str) -> Clasificacion:
    metodo = metodo.upper()
    sin_query = ruta.split("?", 1)[0]
    if sin_query == "/search/query":
        raise RutaNoPermitida("busqueda_global_no_permitida")
    if sin_query.endswith("/createLink"):
        raise RutaNoPermitida("compartir_no_permitido")
    if sin_query == "/me/mailFolders":
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("mail_folders", es_correo=True)
    if m := _RE_CORREO_SUBCARPETAS.match(sin_query):
        if metodo != "GET" or not _ID_MENSAJE.match(m.group("f")):
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("mail_child_folders", carpeta=m.group("f"), es_correo=True)
    if m := _RE_CORREO_ADJUNTOS.match(sin_query):
        if metodo != "GET" or not _ID_MENSAJE.match(m.group("i")) or ".." in m.group("i"):
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion(
            "mail_attachments", item_id=m.group("i"), es_correo=True, query=sin_query.split("/attachments", 1)[1]
        )
    if re.match(
        r"^/me/(calendarView|events|calendar/events|calendar)(/[A-Za-z0-9_\-=+/.!:@]{4,400})?$", sin_query
    ) and not re.search(r"/(accept|decline|tentativelyAccept|cancel|forward|attachments)$", sin_query):
        if metodo != "GET" or ".." in sin_query:
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion(
            "calendario_lectura", carpeta=sin_query, query=ruta.split("?", 1)[1] if "?" in ruta else ""
        )
    if re.match(r"^/me/(joinedTeams|chats)(/[A-Za-z0-9_\-=+.!:@]{4,400}(/(messages|members))?)?$", sin_query):
        if metodo != "GET" or ".." in sin_query:
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("teams_lectura", carpeta=sin_query, query=ruta.split("?", 1)[1] if "?" in ruta else "")
    if sin_query in ("/me/people", "/users", "/me"):
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion(
            "directorio", carpeta=sin_query, es_correo=True, query=ruta.split("?", 1)[1] if "?" in ruta else ""
        )
    if m := _RE_CORREO_MENSAJE.match(sin_query):
        if not _ID_MENSAJE.match(m.group("i")) or ".." in m.group("i"):
            raise RutaNoPermitida("identificador_de_mensaje_no_valido")
        if metodo == "PATCH":
            return Clasificacion("mail_mark_read", item_id=m.group("i"), es_correo=True)
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("mail_read", item_id=m.group("i"), es_correo=True)
    if sin_query == "/me/messages":
        consulta = ruta.split("?", 1)[1] if "?" in ruta else ""
        if metodo != "GET" or "$search" not in urllib.parse.parse_qs(consulta, keep_blank_values=True):
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("mail_search", carpeta=BUZON_COMPLETO, es_correo=True, query=consulta)
    if m := _RE_CORREO_MENSAJES.match(sin_query):
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        consulta = ruta.split("?", 1)[1] if "?" in ruta else ""
        op = "mail_search" if "$search" in urllib.parse.parse_qs(consulta, keep_blank_values=True) else "mail_list"
        return Clasificacion(op, carpeta=m.group("f"), es_correo=True, query=consulta)
    if sin_query == "/me/drives":
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("me_drives")
    if m := _RE_SITIO_RUTA.match(sin_query):
        rest = m.group("rest") or ""
        acc = ""
        for suf in ("/children", "/content", "/createUploadSession"):
            if rest.endswith(":" + suf):
                acc, rest = suf, rest[: -len(suf) - 1]
                break
        cl = _ruta(metodo, None, rest or None, acc, False)
        return Clasificacion(
            cl.operacion, None, cl.carpeta, host=m.group("host"), ruta_sitio=m.group("path"), es_sitio_ruta=True
        )
    if m := _RE_SITIO_DESCUBRIR.match(sin_query):
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("site", host=m.group("host"), ruta_sitio=m.group("path"))
    if m := _RE_SITIO_UNIDADES.match(sin_query):
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("sites_drives", sitio=m.group("s"))
    if _RE_BUSQUEDA_UNIDAD.match(sin_query):
        if metodo != "GET":
            raise RutaNoPermitida("operacion_no_reconocida")
        return Clasificacion("search", unidad=_RE_BUSQUEDA_UNIDAD.match(sin_query).group("d"), carpeta="")
    if m := _RE_ME.match(sin_query):
        if m.group("i"):
            return _item(metodo, None, m.group("i"), m.group("acc2") or "", True)
        if sin_query == "/me/drive":
            return _ruta(metodo, None, None, "", True)
        return _ruta(metodo, None, m.group("p"), (m.group("acc") or "").lstrip(":"), True)
    if m := _RE_UNIDAD.match(sin_query):
        return _ruta(metodo, m.group("d"), m.group("p"), (m.group("acc") or "").lstrip(":"), False)
    if m := _RE_UNIDAD_ITEM.match(sin_query):
        return _item(metodo, m.group("d"), m.group("i"), m.group("acc") or "", False)
    raise RutaNoPermitida("ruta_no_reconocida")


def _ruta(metodo: str, unidad: str | None, p: str | None, acc: str, es_me: bool) -> Clasificacion:
    carpeta = _relativa(p)
    if acc == "/children" and metodo == "GET":
        return Clasificacion("list", unidad, carpeta, es_me=es_me)
    if acc == "/content" and metodo == "GET":
        return Clasificacion("download", unidad, carpeta, es_me=es_me)
    if acc == "/content" and metodo == "PUT":
        return Clasificacion("upload", unidad, carpeta, es_me=es_me)
    if acc == "/createUploadSession" and metodo == "POST":
        return Clasificacion("session", unidad, carpeta, es_me=es_me)
    if acc == "" and metodo == "GET":
        return Clasificacion("read", unidad, carpeta, es_me=es_me)
    raise RutaNoPermitida("operacion_no_reconocida")


def _item(metodo: str, unidad: str | None, item_id: str, acc: str, es_me: bool) -> Clasificacion:
    if metodo != "GET":
        raise RutaNoPermitida("operacion_no_reconocida")
    mapa = {"/children": "list", "/content": "download", "": "read"}
    if acc not in mapa:
        raise RutaNoPermitida("operacion_no_reconocida")
    return Clasificacion(mapa[acc], unidad, None, item_id=item_id, es_me=es_me)


def _dentro(carpeta: str, permitidas: tuple[str, ...]) -> bool:
    return any(base == "" or carpeta == base or carpeta.startswith(base + "/") for base in permitidas)


def _llamar_cabeceras(metodo: str, url: str, token: str, extra: dict, limite: int) -> tuple[int, bytes]:
    cabeceras = {"Authorization": f"Bearer {token}", **extra}
    url = urllib.parse.quote(url, safe=":/?=&$,'()*+;@!~[]#%")
    req = urllib.request.Request(url, method=metodo, headers=cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            datos = resp.read(limite + 1)
            if len(datos) > limite:
                raise RutaNoPermitida("respuesta_supera_el_limite")
            return resp.status, datos
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:4096]


def _llamar_real(
    metodo: str, url: str, token: str, cuerpo: bytes | None, tipo: str | None, limite: int
) -> tuple[int, bytes]:
    cabeceras = {"Authorization": f"Bearer {token}"}
    if tipo:
        cabeceras["Content-Type"] = tipo
    url = urllib.parse.quote(url, safe=":/?=&$,'()*+;@!~[]#%")
    req = urllib.request.Request(url, data=cuerpo, method=metodo, headers=cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            datos = resp.read(limite + 1)
            if len(datos) > limite:
                raise RutaNoPermitida("respuesta_supera_el_limite")
            return resp.status, datos
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:4096]


_llamar = _llamar_real


def ejecutar(
    metodo: str,
    ruta: str,
    cuerpo_json: dict | None,
    cuerpo_bytes: bytes | None,
    politica: PoliticaGraph,
    token_de: Callable[[], str],
    unidad_propia_de: Callable[[str], str | None],
    prefer: str | None = None,
    contexto: dict | None = None,
    secreto: str | None = None,
) -> dict:
    if (
        politica.operaciones == frozenset()
        and not politica.unidades
        and not politica.url_sitio
        and not politica.sitios
        and not politica.correo.operaciones
        and not politica.calendario
        and not politica.teams
    ):
        raise RutaNoPermitida("politica_vacia_denegar")
    cache: dict[str, str] = {}

    def token() -> str:
        if "t" not in cache:
            cache["t"] = token_de()
        return cache["t"]

    cl = clasificar(metodo, ruta)
    if cl.operacion in ("calendario_lectura", "teams_lectura"):
        permitido = politica.calendario if cl.operacion == "calendario_lectura" else politica.teams
        if "lectura" not in permitido:
            raise RutaNoPermitida("operacion_no_permitida_por_la_politica")
        url = f"{GRAPH_BASE}{cl.carpeta}" + (f"?{cl.query}" if cl.query else "")
        st, datos = _llamar("GET", url, token(), None, None, 5_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}
    if cl.operacion == "directorio" and "lectura" in politica.teams and "directorio" not in politica.correo.operaciones:
        return _directorio(cl, token)
    if cl.operacion == "me_drives":
        st, datos = _llamar("GET", GRAPH_BASE + "/me/drives?$select=id,name,driveType", token(), None, None, 1_000_000)
        cuerpo = json.loads(datos or b"{}")
        cuerpo["value"] = [u for u in cuerpo.get("value", []) if _unidad_permitida(politica, u.get("id"))]
        return {"status": st, "json": cuerpo}
    if cl.es_correo:
        return _ejecutar_correo(cl, politica, token, prefer, contexto, secreto, cuerpo_json)
    if cl.es_sitio_ruta:
        url_sitio_pedida = f"https://{cl.host}{cl.ruta_sitio}".rstrip("/")
        if TODO not in politica.url_sitio and url_sitio_pedida not in {u.rstrip("/") for u in politica.url_sitio}:
            raise RutaNoPermitida("sitio_fuera_de_alcance")
        st, datos = _llamar(
            "GET", f"{GRAPH_BASE}/sites/{cl.host}:{cl.ruta_sitio}?$select=id", token(), None, None, 1_000_000
        )
        if st != 200:
            raise RutaNoPermitida("sitio_no_resuelto")
        sitio_id = json.loads(datos).get("id")
        st, datos = _llamar("GET", f"{GRAPH_BASE}/sites/{sitio_id}/drive?$select=id", token(), None, None, 1_000_000)
        if st != 200:
            raise RutaNoPermitida("unidad_del_sitio_no_resuelta")
        unidad_sitio = json.loads(datos).get("id")
        resto = ruta.split(":/drive/root", 1)[1]
        ruta = f"/drives/{unidad_sitio}/root{resto}"
        cl = clasificar(metodo, ruta)
    if cl.operacion not in OPERACIONES | {"session", "site", "sites_drives"}:
        raise RutaNoPermitida("operacion_no_reconocida")

    if cl.operacion == "site":
        url_sitio = f"https://{cl.host}{cl.ruta_sitio}".rstrip("/")
        if TODO not in politica.url_sitio and url_sitio not in {u.rstrip("/") for u in politica.url_sitio}:
            raise RutaNoPermitida("sitio_fuera_de_alcance")
        st, datos = _llamar("GET", GRAPH_BASE + ruta, token(), None, None, 1_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}

    if cl.operacion == "sites_drives":
        if TODO not in politica.sitios and cl.sitio not in politica.sitios:
            raise RutaNoPermitida("sitio_fuera_de_alcance")
        st, datos = _llamar("GET", GRAPH_BASE + ruta, token(), None, None, 1_000_000)
        cuerpo = json.loads(datos or b"{}")
        cuerpo["value"] = [u for u in cuerpo.get("value", []) if _unidad_permitida(politica, u.get("id"))]
        return {"status": st, "json": cuerpo}

    unidad = cl.unidad
    if not cl.es_me:
        if not unidad:
            raise RutaNoPermitida("unidad_no_identificada")
        if not _unidad_permitida(politica, unidad):
            raise RutaNoPermitida("unidad_fuera_de_alcance")
    else:
        unidad = unidad_propia_de(token())
        if not unidad or not _unidad_permitida(politica, unidad):
            raise RutaNoPermitida("unidad_propia_fuera_de_alcance")
    if cl.operacion not in politica.operaciones and not (
        cl.operacion == "session" and "upload" in politica.operaciones
    ):
        raise RutaNoPermitida("operacion_no_permitida_para_el_agente")

    if cl.item_id:
        base = f"{GRAPH_BASE}/drives/{unidad}/items/{cl.item_id}"
        st, datos = _llamar("GET", base + "?$select=name,parentReference,folder", token(), None, None, 1_000_000)
        if st != 200:
            raise RutaNoPermitida("item_no_resuelto")
        meta = json.loads(datos)
        ref = meta.get("parentReference") or {}
        if ref.get("driveId") and ref["driveId"] != unidad:
            raise RutaNoPermitida("item_de_otra_unidad")
        padre = str(ref.get("path") or "").split("root:", 1)[-1].strip("/")
        carpeta_canonica = "/".join(p for p in (padre, meta.get("name") or "") if p)
        if not _dentro(carpeta_canonica, politica.carpetas_lectura):
            raise RutaNoPermitida("carpeta_fuera_del_alcance_permitido")
        sufijo = {"list": "/children", "download": "/content", "read": ""}[cl.operacion]
        url = base + sufijo
        if cl.operacion == "list" and "folder" not in meta:
            raise RutaNoPermitida("item_no_es_carpeta")
    else:
        carpeta = cl.carpeta or ""
        if cl.operacion in {"upload", "session"}:
            if not carpeta or not _dentro(carpeta, politica.carpetas_escritura):
                raise RutaNoPermitida("escritura_fuera_de_carpeta_de_salida")
        elif cl.operacion == "search":
            if not politica.carpetas_lectura:
                raise RutaNoPermitida("busqueda_sin_alcance")
        else:
            if not carpeta and "" not in politica.carpetas_lectura:
                raise RutaNoPermitida("lectura_sin_carpeta_explicita")
            if not _dentro(carpeta, politica.carpetas_lectura):
                raise RutaNoPermitida("carpeta_fuera_del_alcance_permitido")
        url = GRAPH_BASE + ruta
        if cl.es_me:
            url = url.replace("/me/drive", f"/drives/{unidad}", 1)

    if cl.operacion == "search":
        st, datos = _llamar("GET", GRAPH_BASE + ruta, token(), None, None, 2_000_000)
        cuerpo = json.loads(datos or b"{}")
        cuerpo["value"] = [
            v
            for v in cuerpo.get("value", [])
            if _dentro(
                str((v.get("parentReference") or {}).get("path", "")).split("root:", 1)[-1].strip("/"),
                politica.carpetas_lectura,
            )
        ]
        return {"status": st, "json": cuerpo}
    if cl.operacion == "download":
        st, datos = _llamar("GET", url, token(), None, None, MAX_BYTES_DESCARGA)
        return {"status": st, "base64": base64.b64encode(datos).decode("ascii")}
    if cl.operacion == "upload":
        if cuerpo_bytes is None or len(cuerpo_bytes) > MAX_BYTES_SUBIDA:
            raise RutaNoPermitida("contenido_ausente_o_excesivo")
        st, datos = _llamar("PUT", url, token(), cuerpo_bytes, "application/octet-stream", 1_000_000)
        return {
            "status": st,
            "json": json.loads(datos or b"{}") if st < 300 else {"error": datos.decode(errors="replace")[:300]},
        }
    if cl.operacion == "session":
        st, datos = _llamar("POST", url, token(), json.dumps(cuerpo_json or {}).encode(), "application/json", 1_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}
    st, datos = _llamar("GET", url, token(), None, None, 1_000_000)
    return {"status": st, "json": json.loads(datos or b"{}")}


_FILTRO_CORREO = re.compile(r"(receivedDateTime ge \d{4}-\d{2}-\d{2}T[\d:.]+Z)?( and )?(isRead eq (true|false))?")


_CONSULTA_CORREO = re.compile(r'^"([\w\s\-.@áéíóúñüÁÉÍÓÚÑÜ]{2,100})"$')
_MAX_RESULTADOS = 50
BUZON_COMPLETO = "*"
_MAX_VENTANA = 200


def _buscar_correo(
    cl: Clasificacion, corr: PoliticaCorreo, token: Callable[[], str], contexto: dict | None, secreto: str | None
) -> dict:
    from hermeshq.services import cursor_seguro as cs

    if cl.operacion not in corr.operaciones:
        raise RutaNoPermitida("operacion_de_correo_no_permitida")
    carpeta = cl.carpeta or ""
    if carpeta == BUZON_COMPLETO:
        if TODO not in corr.carpetas:
            raise RutaNoPermitida("carpeta_de_correo_fuera_del_alcance")
    elif not _carpeta_permitida(corr, carpeta):
        raise RutaNoPermitida("carpeta_de_correo_fuera_del_alcance")
    parametros = urllib.parse.parse_qs(cl.query, keep_blank_values=True)
    if set(parametros) - {"$search", "$top", "cursor"}:
        raise RutaNoPermitida("parametro_no_permitido")
    if len(parametros.get("$search", [])) != 1:
        raise RutaNoPermitida("consulta_no_valida")
    crudo = parametros["$search"][0]
    if len(crudo) < 2 or not (crudo.startswith('"') and crudo.endswith('"')):
        raise RutaNoPermitida("consulta_no_valida")
    from hermeshq.services import kql_correo as kql

    try:
        consulta = kql.validar(crudo[1:-1])
    except kql.KqlNoPermitido as exc:
        raise RutaNoPermitida(exc.motivo)
    try:
        top = int((parametros.get("$top") or ["10"])[0])
    except ValueError:
        raise RutaNoPermitida("parametro_top_no_valido")
    if not 1 <= top <= _MAX_RESULTADOS:
        raise RutaNoPermitida("parametro_top_fuera_de_limite")
    offset = 0
    if parametros.get("cursor"):
        if not contexto or not secreto:
            raise RutaNoPermitida("cursor_no_valido")
        try:
            offset = cs.validar(secreto, parametros["cursor"][0], contexto, carpeta.lower(), consulta, top)
        except cs.CursorNoValido:
            raise RutaNoPermitida("cursor_no_valido")
    if offset + top + 1 > _MAX_VENTANA:
        raise RutaNoPermitida("ventana_de_busqueda_excedida")

    todas = TODO in corr.carpetas
    ids_permitidos = set() if todas else _ids_de_carpetas_permitidas(corr, token)
    literal = '"' + consulta.replace('"', '\\"') + '"'
    query = (
        f"?$search={urllib.parse.quote(literal, safe='')}&$top={offset + top + 1}"
        "&$select=id,subject,from,receivedDateTime,isRead,hasAttachments,parentFolderId,toRecipients,ccRecipients,"
        "conversationId,bodyPreview"
    )
    st, datos = _llamar(
        "GET",
        f"{GRAPH_BASE}/me/messages{query}"
        if carpeta == BUZON_COMPLETO
        else f"{GRAPH_BASE}/me/mailFolders/{urllib.parse.quote(carpeta)}/messages{query}",
        token(),
        None,
        None,
        2_000_000,
    )
    if st != 200:
        raise RutaNoPermitida("busqueda_no_disponible")
    filas = json.loads(datos or b"{}").get("value", [])
    filas = [f for f in filas if todas or f.get("parentFolderId") in ids_permitidos]
    filas.sort(key=lambda f: str(f.get("receivedDateTime") or ""), reverse=True)
    pagina = filas[offset : offset + top]
    items = [{k: v for k, v in f.items() if k != "parentFolderId"} for f in pagina]
    siguiente = None
    if len(filas) > offset + top and contexto and secreto:
        siguiente = cs.emitir(secreto, contexto, carpeta.lower(), consulta, top, offset + top)
    return {"status": 200, "json": {"items": items, "siguiente_cursor": siguiente}}


def _ejecutar_correo(
    cl: Clasificacion,
    politica: PoliticaGraph,
    token: Callable[[], str],
    prefer: str | None = None,
    contexto: dict | None = None,
    secreto: str | None = None,
    cuerpo_json: dict | None = None,
) -> dict:
    corr = politica.correo
    if cl.operacion == "mail_search":
        return _buscar_correo(cl, corr, token, contexto, secreto)
    if cl.operacion == "mail_child_folders":
        if "mail_folders" not in corr.operaciones:
            raise RutaNoPermitida("operacion_de_correo_no_permitida")
        url = (
            f"{GRAPH_BASE}/me/mailFolders/{urllib.parse.quote(cl.carpeta or '')}/childFolders"
            "?$top=100&$select=id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount"
        )
        st, datos = _llamar("GET", url, token(), None, None, 1_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}
    if cl.operacion == "mail_attachments":
        if "mail_read" not in corr.operaciones:
            raise RutaNoPermitida("operacion_de_correo_no_permitida")
        _exigir_mensaje_autorizado(cl.item_id or "", corr, token)
        url = f"{GRAPH_BASE}/me/messages/{cl.item_id}/attachments{cl.query}"
        if not cl.query:
            url += "?$select=id,name,contentType,size,isInline"
        st, datos = _llamar("GET", url, token(), None, None, MAX_BYTES_DESCARGA)
        if cl.query.endswith("/$value"):
            return {"status": st, "base64": base64.b64encode(datos or b"").decode("ascii")}
        return {"status": st, "json": json.loads(datos or b"{}")}
    if cl.operacion == "mail_mark_read":
        if "mail_mark_read" not in corr.operaciones:
            raise RutaNoPermitida("operacion_de_correo_no_permitida")
        if (
            not isinstance(cuerpo_json, dict)
            or set(cuerpo_json) != {"isRead"}
            or not isinstance(cuerpo_json.get("isRead"), bool)
        ):
            raise RutaNoPermitida("solo_se_permite_isRead")
        _exigir_mensaje_autorizado(cl.item_id or "", corr, token)
        st, datos = _llamar(
            "PATCH",
            f"{GRAPH_BASE}/me/messages/{cl.item_id}",
            token(),
            json.dumps({"isRead": cuerpo_json["isRead"]}).encode(),
            "application/json",
            200_000,
        )
        return {"status": st, "json": json.loads(datos or b"{}") if datos else {}}
    if cl.operacion == "directorio":
        if "directorio" not in corr.operaciones:
            raise RutaNoPermitida("operacion_de_correo_no_permitida")
        return _directorio(cl, token)
    if cl.operacion not in corr.operaciones:
        raise RutaNoPermitida("operacion_de_correo_no_permitida")
    if cl.operacion == "mail_read":
        return _leer_mensaje(cl.item_id or "", corr, token, prefer)
    if cl.operacion == "mail_list":
        carpeta = cl.carpeta or ""
        if not _carpeta_permitida(corr, carpeta):
            raise RutaNoPermitida("carpeta_de_correo_fuera_del_alcance")
        url = f"{GRAPH_BASE}/me/mailFolders/{urllib.parse.quote(carpeta)}/messages"
        parametros = urllib.parse.parse_qs(cl.query, keep_blank_values=False)
        try:
            top = max(1, min(50, int((parametros.get("$top") or ["10"])[0])))
        except ValueError:
            raise RutaNoPermitida("parametro_top_no_valido")
        filtro = (parametros.get("$filter") or [""])[0]
        if filtro and not _FILTRO_CORREO.fullmatch(filtro):
            raise RutaNoPermitida("filtro_de_correo_no_permitido")
        query = (
            f"?$top={top}&$select=id,internetMessageId,subject,from,receivedDateTime,sentDateTime,isRead,"
            "hasAttachments,bodyPreview,conversationId,toRecipients,ccRecipients&$orderby=receivedDateTime%20desc"
        )
        if filtro:
            query += "&$filter=" + urllib.parse.quote(filtro)
        st, datos = _llamar("GET", url + query, token(), None, None, 2_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}
    url = f"{GRAPH_BASE}/me/mailFolders?$top=100&$select=id,displayName,childFolderCount,totalItemCount,unreadItemCount"
    st, datos = _llamar("GET", url, token(), None, None, 1_000_000)
    return {"status": st, "json": json.loads(datos or b"{}")}


def _exigir_mensaje_autorizado(mensaje_id: str, corr: PoliticaCorreo, token: Callable[[], str]) -> None:
    st, datos = _llamar(
        "GET", f"{GRAPH_BASE}/me/messages/{mensaje_id}?$select=parentFolderId", token(), None, None, 100_000
    )
    if st != 200:
        raise RutaNoPermitida("mensaje_no_autorizado")
    padre = json.loads(datos or b"{}").get("parentFolderId", "")
    if not padre or (TODO not in corr.carpetas and padre not in _ids_de_carpetas_permitidas(corr, token)):
        raise RutaNoPermitida("mensaje_fuera_de_carpeta_autorizada")


def _directorio(cl: Clasificacion, token: Callable[[], str]) -> dict:
    parametros = urllib.parse.parse_qs(cl.query, keep_blank_values=False)
    seleccion = (parametros.get("$select") or [""])[0]
    if seleccion and not set(seleccion.split(",")) <= _SELECT_DIRECTORIO:
        raise RutaNoPermitida("campos_de_directorio_no_permitidos")
    if cl.carpeta == "/me":
        if set(parametros) - {"$select"}:
            raise RutaNoPermitida("parametro_no_permitido")
        url = f"{GRAPH_BASE}/me?$select=mail,userPrincipalName,displayName"
    elif cl.carpeta == "/me/people":
        if set(parametros) - {"$search", "$top", "$select"}:
            raise RutaNoPermitida("parametro_no_permitido")
        busqueda = (parametros.get("$search") or [""])[0]
        if not _CONSULTA_CORREO.fullmatch(busqueda):
            raise RutaNoPermitida("consulta_no_valida")
        top = max(1, min(25, int((parametros.get("$top") or ["10"])[0])))
        url = (
            f"{GRAPH_BASE}/me/people?$search={urllib.parse.quote(busqueda, safe='')}&$top={top}"
            f"&$select={seleccion or 'displayName,scoredEmailAddresses,jobTitle,department'}"
        )
    elif "$search" in parametros:
        if set(parametros) - {"$search", "$top", "$count", "$select"}:
            raise RutaNoPermitida("parametro_no_permitido")
        busqueda = parametros["$search"][0]
        if not _RE_BUSQUEDA_DIRECTORIO.fullmatch(busqueda):
            raise RutaNoPermitida("busqueda_de_directorio_no_permitida")
        top = max(1, min(25, int((parametros.get("$top") or ["15"])[0])))
        url = (
            f"{GRAPH_BASE}/users?$search={urllib.parse.quote(busqueda, safe='')}&$top={top}&$count=true"
            f"&$select={seleccion or 'mail,userPrincipalName,displayName'}"
        )
        st, datos = _llamar_cabeceras("GET", url, token(), {"ConsistencyLevel": "eventual"}, 1_000_000)
        return {"status": st, "json": json.loads(datos or b"{}")}
    else:
        if set(parametros) - {"$filter", "$select"}:
            raise RutaNoPermitida("parametro_no_permitido")
        filtro = (parametros.get("$filter") or [""])[0]
        if not _RE_FILTRO_DIRECTORIO.fullmatch(filtro):
            raise RutaNoPermitida("filtro_de_directorio_no_permitido")
        url = (
            f"{GRAPH_BASE}/users?$filter={urllib.parse.quote(filtro, safe='')}"
            f"&$select={seleccion or 'mail,userPrincipalName,displayName'}"
        )
    st, datos = _llamar("GET", url, token(), None, None, 1_000_000)
    return {"status": st, "json": json.loads(datos or b"{}")}


def _ids_de_carpetas_permitidas(corr: PoliticaCorreo, token: Callable[[], str]) -> set[str]:
    ids: set[str] = set()
    for carpeta in corr.carpetas:
        st, datos = _llamar(
            "GET", f"{GRAPH_BASE}/me/mailFolders/{urllib.parse.quote(carpeta)}?$select=id", token(), None, None, 100_000
        )
        if st == 200:
            ids.add(json.loads(datos).get("id", ""))
    return {i for i in ids if i}


def _leer_mensaje(mensaje_id: str, corr: PoliticaCorreo, token: Callable[[], str], prefer: str | None) -> dict:
    st, datos = _llamar(
        "GET", f"{GRAPH_BASE}/me/messages/{mensaje_id}?$select=parentFolderId", token(), None, None, 100_000
    )
    if st != 200:
        raise RutaNoPermitida("mensaje_no_autorizado")
    padre = json.loads(datos or b"{}").get("parentFolderId", "")
    if not padre or (TODO not in corr.carpetas and padre not in _ids_de_carpetas_permitidas(corr, token)):
        raise RutaNoPermitida("mensaje_fuera_de_carpeta_autorizada")
    cabeceras_prefer = prefer if prefer in _PREFER_PERMITIDOS else None
    st, datos = _llamar_con_prefer(
        f"{GRAPH_BASE}/me/messages/{mensaje_id}?$select={_CAMPOS_MENSAJE}", token(), cabeceras_prefer
    )
    mensaje = json.loads(datos or b"{}")
    cuerpo = mensaje.get("body") or {}
    if isinstance(cuerpo.get("content"), str) and len(cuerpo["content"]) > _MAX_CUERPO:
        cuerpo["content"] = cuerpo["content"][:_MAX_CUERPO]
        cuerpo["truncado"] = True
    return {"status": st, "json": mensaje}


def _llamar_con_prefer(url: str, token: str, prefer: str | None) -> tuple[int, bytes]:
    if prefer is None:
        return _llamar("GET", url, token, None, None, 2_000_000)
    return _llamar_cabeceras("GET", url, token, {"Prefer": prefer}, 2_000_000)


TODO = "*"
INTEGRACIONES_M365 = ("ms365-mail", "ms365-calendar", "ms365-teams", "sharepoint")


def _carpeta_permitida(corr: PoliticaCorreo, carpeta: str) -> bool:
    return TODO in corr.carpetas or carpeta.lower() in {c.lower() for c in corr.carpetas}


def _unidad_permitida(politica: PoliticaGraph, unidad: str | None) -> bool:
    return bool(unidad) and (TODO in politica.unidades or unidad in politica.unidades)


def politica_de_compatibilidad(integraciones: dict) -> PoliticaGraph:
    correo = PoliticaCorreo()
    if "ms365-mail" in integraciones:
        correo = PoliticaCorreo(operaciones=OPERACIONES_CORREO, carpetas=frozenset({TODO}))
    sp = "sharepoint" in integraciones
    return PoliticaGraph(
        correo=correo,
        calendario=frozenset({"lectura"}) if "ms365-calendar" in integraciones else frozenset(),
        teams=frozenset({"lectura"}) if "ms365-teams" in integraciones else frozenset(),
        unidades=frozenset({TODO}) if sp else frozenset(),
        sitios=frozenset({TODO}) if sp else frozenset(),
        url_sitio=frozenset({TODO}) if sp else frozenset(),
        operaciones=OPERACIONES if sp else frozenset(),
        carpetas_lectura=("",) if sp else (),
        carpetas_escritura=("",) if sp else (),
    )


def politica_de_agente(
    integration_configs: dict | None, compatibilidad: bool | None = None
) -> tuple[PoliticaGraph, str]:
    configs = integration_configs or {}
    if isinstance(configs.get("m365_graph"), dict):
        return politica_desde_configuracion(configs["m365_graph"]), "m365_graph"
    if compatibilidad is None:
        from hermeshq.config import get_settings

        compatibilidad = bool(get_settings().m365_politica_compatibilidad)
    if compatibilidad and any(k in configs for k in INTEGRACIONES_M365):
        return politica_de_compatibilidad(configs), "compatibilidad"
    return politica_desde_configuracion(None), "sin_politica"


def politica_desde_configuracion(cfg) -> PoliticaGraph:
    if not isinstance(cfg, dict):
        return PoliticaGraph()

    def _lista(v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if isinstance(x, str) and str(x).strip()]

    corr_cfg = cfg.get("correo") if isinstance(cfg.get("correo"), dict) else {}
    return PoliticaGraph(
        correo=PoliticaCorreo(
            operaciones=frozenset(o for o in _lista(corr_cfg.get("operaciones")) if o in OPERACIONES_CORREO),
            carpetas=frozenset(_lista(corr_cfg.get("carpetas"))),
        ),
        unidades=frozenset(_lista(cfg.get("unidades"))),
        carpetas_lectura=tuple(c.strip("/") for c in _lista(cfg.get("carpetas_lectura"))),
        carpetas_escritura=tuple(c.strip("/") for c in _lista(cfg.get("carpetas_escritura"))),
        sitios=frozenset(_lista(cfg.get("sitios"))),
        url_sitio=frozenset(_lista(cfg.get("url_sitio"))),
        operaciones=frozenset(o for o in _lista(cfg.get("operaciones")) if o in OPERACIONES),
        calendario=frozenset(
            o
            for o in _lista(
                (cfg.get("calendario") or {}).get("operaciones") if isinstance(cfg.get("calendario"), dict) else []
            )
            if o == "lectura"
        ),
        teams=frozenset(
            o
            for o in _lista((cfg.get("teams") or {}).get("operaciones") if isinstance(cfg.get("teams"), dict) else [])
            if o == "lectura"
        ),
    )
