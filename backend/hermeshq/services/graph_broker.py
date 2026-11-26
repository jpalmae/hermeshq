from __future__ import annotations

import asyncio
import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hermeshq.services.autorizacion_datos import (
    MAX_ELEMENTOS_POR_PAGINA,
    MAX_PAGINAS,
    PoliticaDatos,
    Solicitud,
    autorizar,
    cursor_para,
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class AccesoDenegado(PermissionError):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True)
class SolicitudBroker:
    usuario_id: str
    agente_id: str
    tarea_id: str
    operacion: str
    sitio_id: str | None = None
    carpeta: str | None = None
    pagina: int = 0
    cursor: str | None = None
    finalidad: str | None = None
    nombre: str | None = None
    contenido: bytes | None = None


class ProveedorGraphSimulado:
    def __init__(self, archivos: dict[tuple[str, str], list[dict]]):
        self._archivos = archivos
        self._capacidad = secrets.token_hex(16)
        self.llamadas: list[str] = []
        self.escrituras: list[str] = []

    def capacidad_para_broker(self) -> str:
        return self._capacidad

    def _exigir(self, capacidad: str | None) -> None:
        if capacidad != self._capacidad:
            raise PermissionError("el proveedor solo acepta llamadas del broker")

    async def listar(self, capacidad, sitio: str, carpeta: str, pagina: int, tamano: int) -> list[dict]:
        self._exigir(capacidad)
        self.llamadas.append(f"listar:{sitio}:{carpeta}:{pagina}")
        items = self._archivos.get((sitio, carpeta), [])
        inicio = pagina * tamano
        return items[inicio : inicio + tamano + 1]

    async def subir(self, capacidad, sitio: str, carpeta: str, nombre: str, contenido: bytes) -> None:
        self._exigir(capacidad)
        self.escrituras.append(f"subir:{sitio}:{carpeta}:{nombre}:{len(contenido)}")


class ProveedorGraphHttp:
    def __init__(self, token: str):
        self._token = token
        self._capacidad = secrets.token_hex(16)

    def capacidad_para_broker(self) -> str:
        return self._capacidad

    def _exigir(self, capacidad) -> None:
        if capacidad != self._capacidad:
            raise PermissionError("el proveedor solo acepta llamadas del broker")

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def listar(self, capacidad, sitio: str, carpeta: str, pagina: int, tamano: int) -> list[dict]:
        self._exigir(capacidad)
        ruta = urllib.parse.quote(carpeta)
        url = f"{GRAPH_BASE}/sites/{sitio}/drive/root:/{ruta}:/children?$top={min(999, (pagina + 1) * tamano + 1)}"
        datos = await asyncio.to_thread(self._get, url)
        filas = [
            {"id": v.get("id"), "nombre": v.get("name"), "tipo": "folder" if "folder" in v else "file"}
            for v in datos.get("value", [])
        ]
        return filas[pagina * tamano : pagina * tamano + tamano + 1]

    async def subir(self, capacidad, sitio: str, carpeta: str, nombre: str, contenido: bytes) -> None:
        self._exigir(capacidad)
        ruta = urllib.parse.quote(f"{carpeta}/{nombre}")
        url = f"{GRAPH_BASE}/sites/{sitio}/drive/root:/{ruta}:/content"
        req = urllib.request.Request(
            url, data=contenido, method="PUT", headers={"Authorization": f"Bearer {self._token}"}
        )
        await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=60).read())


Fabrica = Callable[[str], Awaitable[object]]


class GraphBroker:
    def __init__(
        self,
        fabrica_proveedor: Fabrica,
        politicas: dict[str, PoliticaDatos],
        secreto_cursor: str,
        tarea_activa: Callable[[str, str, str], bool],
        asignado: Callable[[str, str], bool],
        auditoria: list[dict],
    ):
        self.__fabrica = fabrica_proveedor
        self.__politicas = politicas
        self.__secreto = secreto_cursor
        self.__tarea_activa = tarea_activa
        self.__asignado = asignado
        self.__auditoria = auditoria

    async def ejecutar(self, s: SolicitudBroker, ahora: datetime | None = None) -> dict:
        ahora = ahora or datetime.now(UTC)
        politica = self.__politicas.get(s.agente_id, PoliticaDatos())
        solicitud = Solicitud(
            usuario_id=s.usuario_id,
            agente_id=s.agente_id,
            tarea_id=s.tarea_id,
            operacion=s.operacion,
            sitio_id=s.sitio_id,
            carpeta=s.carpeta,
            finalidad=s.finalidad,
            pagina=s.pagina,
            cursor=s.cursor,
            tarea_activa=self.__tarea_activa(s.usuario_id, s.agente_id, s.tarea_id),
            asignado=self.__asignado(s.usuario_id, s.agente_id),
        )
        decision = autorizar(solicitud, politica, self.__secreto, ahora)
        self.__auditoria.append(decision.registro)
        if not decision.permitido:
            raise AccesoDenegado(decision.motivo)

        proveedor = await self.__fabrica(s.usuario_id)
        cap = proveedor.capacidad_para_broker()
        carpeta = decision.registro["carpeta"]
        if s.operacion == "upload":
            await proveedor.subir(cap, s.sitio_id, carpeta, s.nombre or "", s.contenido or b"")
            return {"ok": True}

        filas = await proveedor.listar(cap, s.sitio_id, carpeta, s.pagina, MAX_ELEMENTOS_POR_PAGINA)
        hay_mas = len(filas) > MAX_ELEMENTOS_POR_PAGINA and s.pagina + 1 < MAX_PAGINAS
        items = [{"id": f["id"], "nombre": f["nombre"], "tipo": f["tipo"]} for f in filas[:MAX_ELEMENTOS_POR_PAGINA]]
        siguiente = None
        if hay_mas:
            siguiente = cursor_para(self.__secreto, s.usuario_id, s.agente_id, carpeta, s.pagina + 1, s.tarea_id)
        return {"items": items, "siguiente_cursor": siguiente}


def decodificar_contenido(texto_b64: str | None) -> bytes | None:
    if not texto_b64:
        return None
    return base64.b64decode(texto_b64.encode("ascii"), validate=True)


def politica_desde_configuracion(cfg) -> PoliticaDatos:
    if not isinstance(cfg, dict):
        return PoliticaDatos()

    def _tupla(valor) -> tuple[str, ...]:
        if not isinstance(valor, list):
            return ()
        return tuple(str(v).strip().strip("/") for v in valor if isinstance(v, str) and v.strip())

    ops = cfg.get("operaciones")
    if not isinstance(ops, list):
        return PoliticaDatos()
    return PoliticaDatos(
        operaciones=frozenset(o for o in ops if o in {"list", "read", "search", "download", "upload"}),
        carpetas_lectura=_tupla(cfg.get("carpetas_lectura")),
        carpetas_escritura=_tupla(cfg.get("carpetas_escritura")),
    )


def proveedor_simulado_desde_json(ruta: str) -> ProveedorGraphSimulado:
    with open(ruta, encoding="utf-8") as fh:
        datos = json.load(fh)
    archivos: dict[tuple[str, str], list[dict]] = {}
    for sitio, carpetas in datos.items():
        for carpeta, items in carpetas.items():
            archivos[(sitio, carpeta)] = items
    return ProveedorGraphSimulado(archivos)
