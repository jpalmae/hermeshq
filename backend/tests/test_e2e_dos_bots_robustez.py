from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import select, text
from test_aprobacion_central import _correo
from test_e2e_concurrencia_cron_chat import _chat, _esperar, _llamada, _llamadas, _maximo_simultaneo
from test_e2e_dos_bots import (
    AAD,
    CANAL_1,
    CANAL_2,
    EXTERNO,
    _BotFalso,
    _cargar_bridge,
    _codigo,
    _contexto,
    _corridas,
    _cron,
    _decidir,
    _demora_entrega,
    _esperar_entregas,
    _medicion,
    _notificaciones,
    _ruta_bridge,
    _tarea_con,
    dos_bots,  # noqa: F401
)

from hermeshq.core.security import hash_password
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.bot_channel import BotChannel, UserChannelIdentity
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac
from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, _hash_key

pytestmark = [pytest.mark.integration, requires_database]

CLAVE_SERVICIO = "clave-servicio-pruebas"
LATIDO_HOLGADO = {
    "EXECUTION_LEASE_TTL_SECONDS": "12",
    "RUNNER_HEARTBEAT_STALE_SECONDS": "10",
}
_IP = iter(range(1, 10_000))


def _ip() -> str:
    n = next(_IP)
    return f"10.77.{n // 250}.{n % 250 + 1}"


async def _crear_clave(maker, m, nombre: str, agente: str, bot: int | None, *, usuario=None, confianza=False):
    cruda = f"hqk-{uuid.uuid4().hex}"
    async with maker() as s:
        clave = PublicChatApiKey(
            key_hash=_hash_key(cruda),
            key_prefix=cruda[:8],
            label="bridge-teams-2" if bot == 2 else "bridge-teams",
            agent_id=m["agentes"][agente],
            allowed_domains=["*"],
            is_active=True,
            bound_user_id=m["usuarios"][usuario] if usuario else None,
            trust_sender_id=confianza,
            bot_channel_id=m["canales"][bot] if bot else None,
        )
        s.add(clave)
        await s.commit()
    m["claves"][nombre] = cruda
    m["id_clave"][nombre] = clave.id
    return cruda


async def _sesion_cruda(base: str, clave: str, canal: str | None, remitente: str | None, aad: str | None = None):
    cabeceras = {"X-Api-Key": clave, "X-Real-IP": _ip(), **({"X-Bot-Channel": canal} if canal else {})}
    cuerpo = {k: v for k, v in (("sender_id", remitente), ("aad_object_id", aad)) if v}
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.post(f"{base}/api/public/chat/sessions", headers=cabeceras, json=cuerpo)


async def _cuenta_de_servicio(maker, m, nombre: str, agentes: tuple[str, ...], bot: int) -> str:
    async with maker() as s:
        usuario = (await s.execute(select(User).where(User.username == nombre))).scalar_one_or_none()
        if usuario is None:
            usuario = User(username=nombre, display_name=nombre, password_hash="", role="user")
            s.add(usuario)
        usuario.password_hash = hash_password(CLAVE_SERVICIO)
        usuario.is_active = True
        await s.flush()
        for agente in agentes:
            s.add(AgentAssignment(user_id=usuario.id, agent_id=m["agentes"][agente]))
        await s.execute(
            text("UPDATE bot_channels SET service_user_id = :u WHERE id = :c"),
            {"u": usuario.id, "c": m["canales"][bot]},
        )
        await s.commit()
        return usuario.id


async def _entrar(base: str, usuario: str, clave: str = CLAVE_SERVICIO) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{base}/api/auth/login", json={"username": usuario, "password": clave})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _usuario_de(maker, user_id: str) -> str:
    async with maker() as s:
        return (await s.get(User, user_id)).username


async def _raiz_de_workspaces(maker, m) -> Path:
    async with maker() as s:
        return Path((await s.get(Agent, m["agentes"]["a1"])).workspace_path).parent


async def _adjuntar_documento(maker, m, tarea_id: str, agente: str, contenido: bytes) -> str:
    file_id = str(uuid.uuid4())
    uploads = (await _raiz_de_workspaces(maker, m)) / f"agent-{m['agentes'][agente]}" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / f"{file_id}.pdf").write_bytes(contenido)
    ficha = [{"file_id": file_id, "filename": "informe.pdf", "media_type": "application/pdf", "size": len(contenido)}]
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE tasks SET metadata = (metadata::jsonb || jsonb_build_object('response_attachments', "
                "CAST(:f AS jsonb)))::json WHERE id = :i"
            ),
            {"f": json.dumps(ficha), "i": tarea_id},
        )
        await s.commit()
    return file_id


async def _descargar(base: str, cabeceras: dict, agente_id: str, file_id: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.get(f"{base}/api/agents/{agente_id}/attachments/{file_id}", headers=cabeceras)


async def _ack(base: str, clave: str, canal: str | None, notificacion_id: str) -> httpx.Response:
    cabeceras = {"X-Api-Key": clave, **({"X-Bot-Channel": canal} if canal else {})}
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.post(f"{base}/api/public/chat/notifications/{notificacion_id}/ack", headers=cabeceras)


async def _pendientes(base: str, clave: str, canal: str | None, sesion: tuple[str, str]) -> httpx.Response:
    cabeceras = {"X-Api-Key": clave, **({"X-Bot-Channel": canal} if canal else {})}
    async with httpx.AsyncClient(timeout=15) as c:
        return await c.get(
            f"{base}/api/public/chat/aprobaciones/pendientes",
            headers=cabeceras,
            params={"session_id": sesion[0], "session_token": sesion[1]},
        )


async def _aprobacion(maker, m, agente: str, usuario: str, sesion_id: str, task_id: str | None = None):
    async with maker() as s:
        ctx = ac.Contexto(
            agent_id=m["agentes"][agente],
            user_id=m["usuarios"][usuario],
            conversacion_id=sesion_id,
            task_id=task_id,
            canal="public_chat",
        )
        return await ac.crear(s, ac.TIPO_CORREO, _correo(asunto=f"Informe {uuid.uuid4().hex[:6]}"), ctx)


async def _filas_de(maker, *, schedule_id=None, task_id=None) -> list[PublicChatNotification]:
    filtro = {"schedule_id": schedule_id} if schedule_id else {"task_id": task_id}
    return await _notificaciones(maker, **filtro)


async def _crons_terminados(maker, ids: dict[str, str], esperadas: dict[str, int]):
    listas = {k: await _corridas(maker, v) for k, v in ids.items()}
    listo = all(len(listas[k]) == esperadas.get(k, 1) and listas[k][-1].status in ("completed", "failed") for k in ids)
    return listas if listo else None


def _todas(*bridges) -> list[dict]:
    return [n for b in bridges for n in b.recibidas]


class TestDosAplicacionesAzure:
    async def test_el_29_de_un_bot_no_resuelve_a_la_persona_en_el_otro(self, dos_bots):  # noqa: F811
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        u2 = m["usuarios"]["u2"]
        async with maker() as s:
            for bot, app, tenant in ((1, "app-bot-1", "tenant-1"), (2, "app-bot-2", "tenant-2")):
                await s.execute(
                    text("UPDATE bot_channels SET azure_app_id = :a, tenant_id = :t WHERE id = :i"),
                    {"a": app, "t": tenant, "i": m["canales"][bot]},
                )
            await s.execute(
                text("UPDATE user_channel_identities SET tenant_id = 'tenant-2' WHERE bot_channel_id = :c"),
                {"c": m["canales"][2]},
            )
            await s.commit()
            canales = (await s.execute(select(BotChannel))).scalars().all()
            identidades = (
                (await s.execute(select(UserChannelIdentity).where(UserChannelIdentity.user_id == u2))).scalars().all()
            )
        assert len({(c.azure_app_id, c.tenant_id) for c in canales}) == 2 and all(c.azure_app_id for c in canales)
        assert {(i.bot_channel_id, i.external_user_id) for i in identidades} == {
            (m["canales"][1], EXTERNO[("u2", 1)]),
            (m["canales"][2], EXTERNO[("u2", 2)]),
        }
        k_b1 = await _crear_clave(maker, m, "compartida-b1", "a2", None, confianza=True)
        k_b2 = await _crear_clave(maker, m, "compartida-b2", "a2", 2, confianza=True)
        pila = await dos_bots["levantar"]("robustez-azure")
        inst = pila["inst"]

        casos = [
            ("bot 2, su 29:", k_b2, CANAL_2, EXTERNO[("u2", 2)], u2),
            ("bot 2, 29: del bot 1", k_b2, CANAL_2, EXTERNO[("u2", 1)], None),
            ("bot 2, 29: legado de users.teams_id", k_b2, CANAL_2, "29:u1-b1", None),
            ("bot 1, su 29:", k_b1, CANAL_1, EXTERNO[("u2", 1)], u2),
            ("bot 1, 29: del bot 2", k_b1, CANAL_1, EXTERNO[("u2", 2)], None),
            ("bot 1, 29: de U3 en el bot 2", k_b1, CANAL_1, EXTERNO[("u3", 2)], None),
        ]
        sesiones = {}
        for nombre, clave, canal, remitente, esperado in casos:
            r = await _sesion_cruda(inst.base, clave, canal, remitente)
            assert r.status_code == 200, (nombre, r.text)
            async with maker() as s:
                fila = await s.get(PublicChatSession, r.json()["session_id"])
            assert fila.user_id == esperado, nombre
            sesiones[nombre] = (r.json()["session_id"], r.json()["session_token"])

        r = await _chat(inst.base, *sesiones["bot 2, su 29:"], "Mi dato es SECRETO-AZURE-B2. MARCA-AZB2 DEMORA=0.2")
        assert r["type"] == "done"
        assert (await _tarea_con(maker, "MARCA-AZB2")).metadata_json["thread_user_id"] == u2
        for nombre, marca in (("bot 2, 29: del bot 1", "MARCA-AZCRUCE2"), ("bot 1, 29: del bot 2", "MARCA-AZCRUCE1")):
            r = await _chat(inst.base, *sesiones[nombre], f"¿Qué dato te di? {marca} DEMORA=0.2")
            assert r["type"] in ("done", "error"), r
            tarea = await _tarea_con(maker, marca)
            assert tarea.metadata_json["thread_user_id"] == PUBLIC_CHAT_USER_ID, nombre
            assert "SECRETO-AZURE-B2" not in _contexto(handler, marca), f"{nombre}: recibió el historial de U2"
        r = await _chat(inst.base, *sesiones["bot 1, su 29:"], "¿Qué dato te di? MARCA-AZB1 DEMORA=0.2")
        assert (await _tarea_con(maker, "MARCA-AZB1")).metadata_json["thread_user_id"] == u2
        assert "SECRETO-AZURE-B2" not in _contexto(handler, "MARCA-AZB1"), "el historial del bot 2 llegó al bot 1"


class TestReinicioReal:
    async def test_tras_reiniciar_cada_entrega_sale_una_vez_por_su_bot(self, dos_bots):  # noqa: F811
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        u1, u2, u3 = (m["usuarios"][x] for x in ("u1", "u2", "u3"))
        a = await dos_bots["levantar"]("robustez-reinicio-a", LATIDO_HOLGADO)
        s21, t21 = await a["b1"].sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        ids = {
            "pendiente": await _cron(maker, m, "a2", "u2", "pend", "Informe DEMORA=0.2 MARCA-RPEND", clave="u2-a2-b2")
        }

        async def _fila_pendiente():
            return await _filas_de(maker, schedule_id=ids["pendiente"])

        await _esperar(_fila_pendiente, 60, "aviso del cron listo antes del reinicio (sin bridge)")
        ids["vuelo_b2"] = await _cron(maker, m, "a2", "u2", "vuelo", "Informe DEMORA=10 MARCA-RVUELO", clave="u2-a2-b2")
        ids["vuelo_u3"] = await _cron(maker, m, "a3", "u3", "u3", "Informe DEMORA=10 MARCA-RU3")
        ids["vuelo_u1"] = await _cron(maker, m, "a1", "u1", "u1", "Informe DEMORA=10 MARCA-RU1")
        chat = asyncio.create_task(_chat(a["inst"].base, s21, t21, "Trabajo MARCA-RCHATB1 DEMORA=10"))
        marcas = {"vuelo_b2": "MARCA-RVUELO", "vuelo_u3": "MARCA-RU3", "vuelo_u1": "MARCA-RU1", "chat": "MARCA-RCHATB1"}

        async def _todas_en_curso():
            return all([await _llamada(handler, x) for x in marcas.values()])

        try:
            await _esperar(_todas_en_curso, 120, "tres crons y un chat en curso a la vez")
        except AssertionError:
            estado = {
                k: [(c.status, c.error_message, str(c.started_at)) for c in await _corridas(maker, ids[k])] for k in ids
            }
            llamadas = {x: len(_llamadas(handler, x)) for x in marcas.values()}
            raise AssertionError(f"no arrancaron a tiempo: {estado} {llamadas}") from None
        chat_id = (await _tarea_con(maker, "MARCA-RCHATB1")).id
        caida = time.time()
        a["inst"].kill_group()
        with pytest.raises((httpx.HTTPError, AssertionError)):
            await asyncio.wait_for(chat, 30)

        b = await dos_bots["levantar"]("robustez-reinicio-b", LATIDO_HOLGADO)
        listo = time.time()
        b1, b2 = b["b1"], b["b2"]
        b1.arrancar()
        b2.arrancar()
        esperado = {
            "pendiente": ("u2-a2-b2", u2, EXTERNO[("u2", 2)]),
            "vuelo_b2": ("u2-a2-b2", u2, EXTERNO[("u2", 2)]),
            "vuelo_u3": ("u3-a3-b2", u3, EXTERNO[("u3", 2)]),
            "vuelo_u1": ("u1-a1-b1", u1, None),
        }

        def _crons_entregados():
            return all(b1.de(schedule_id=ids[k]) or b2.de(schedule_id=ids[k]) for k in ids)

        try:
            await _esperar_entregas(None, _crons_entregados, 150, "avisos de los crons tras el reinicio")
        except AssertionError:
            estado = {
                k: {
                    "corridas": [
                        (c.status, c.error_message, c.metadata_json.get("retry_of")) for c in await _corridas(maker, v)
                    ],
                    "filas": [(f.api_key_id, f.status) for f in await _filas_de(maker, schedule_id=v)],
                    "vistas": [n["clave"] for n in _todas(b1, b2) if n.get("schedule_id") == v],
                }
                for k, v in ids.items()
            }
            raise AssertionError(f"avisos de los crons tras el reinicio: {json.dumps(estado, default=str)}") from None
        await asyncio.sleep(5)

        for k, (clave, usuario, externo) in esperado.items():
            vistas = [n for n in _todas(b1, b2) if n.get("schedule_id") == ids[k]]
            assert len(vistas) == 1, (k, vistas)
            assert vistas[0]["clave"] == clave and vistas[0]["recipient_external_id"] == externo, k
            filas = await _filas_de(maker, schedule_id=ids[k])
            assert [(f.api_key_id, f.recipient_user_id, f.status) for f in filas] == [
                (m["id_clave"][clave], usuario, "delivered")
            ], k
            _medicion(
                caso="reinicio",
                que=k,
                entrega_s=await _demora_entrega(maker, vistas[0]),
                desde_arranque_s=round(vistas[0]["visto"] - listo, 2),
            )
        for k in ("vuelo_b2", "vuelo_u3", "vuelo_u1"):
            corridas = await _corridas(maker, ids[k])
            estados = [(c.status, c.error_message) for c in corridas]
            assert [c.status for c in corridas] == ["failed", "completed"], (k, estados)
            assert corridas[1].metadata_json["retry_of"] == corridas[0].id
            (vista,) = [n for n in _todas(b1, b2) if n.get("schedule_id") == ids[k]]
            assert marcas[k] in vista["body"] and len(_llamadas(handler, marcas[k])) == 2, k
        assert len(await _corridas(maker, ids["pendiente"])) == 1
        await _esperar_entregas(None, lambda: b1.de(task_id=chat_id) or b2.de(task_id=chat_id), 90, "chat recuperado")
        await asyncio.sleep(3)
        vistas_chat = [n for n in _todas(b1, b2) if n.get("task_id") == chat_id]
        assert len(vistas_chat) == 1 and vistas_chat[0]["bridge"] == "bridge-1", vistas_chat
        assert vistas_chat[0]["clave"] == "u2-a2-b1" and vistas_chat[0]["origin"] == "respuesta_tardia"
        assert "MARCA-RCHATB1" in vistas_chat[0]["body"]
        filas = await _filas_de(maker, task_id=chat_id)
        assert [(f.api_key_id, f.recipient_user_id) for f in filas] == [(m["id_clave"]["u2-a2-b1"], u2)]
        async with maker() as s:
            tarea = await s.get(Task, chat_id)
        assert tarea.status == "completed" and tarea.metadata_json.get("recovery_reintentos") == 1
        _medicion(
            caso="reinicio",
            que="chat bot 1",
            entrega_s=await _demora_entrega(maker, vistas_chat[0]),
            caida_a_arranque_s=round(listo - caida, 2),
        )


class TestDuplicados:
    async def test_sondeos_concurrentes_repetidos_y_ack_repetido_entregan_una_vez_por_su_clave(self, dos_bots):  # noqa: F811
        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("robustez-duplicados")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        claves = {
            "u1-a1-b1": ("a1", "u1"),
            "u2-a2-b1": ("a2", "u2"),
            "u2-a2-b2": ("a2", "u2"),
            "u3-a3-b2": ("a3", "u3"),
            "u3-a4-b2": ("a4", "u3"),
        }

        async def _ronda(etiqueta: str) -> dict[str, set[str]]:
            ids = {}
            for clave, (agente, usuario) in claves.items():
                for n in range(2):
                    ids[(clave, n)] = await _cron(
                        maker,
                        m,
                        agente,
                        usuario,
                        f"{etiqueta}-{clave}-{n}",
                        f"Informe DEMORA=0.2 MARCA-DUP{etiqueta}",
                        clave=clave,
                    )

            async def _filas():
                todas = {k: await _filas_de(maker, schedule_id=v) for k, v in ids.items()}
                return todas if all(len(x) == 1 for x in todas.values()) else None

            filas = await _esperar(_filas, 90, f"avisos de la ronda {etiqueta}")
            por_clave: dict[str, set[str]] = {k: set() for k in claves}
            for (clave, _), (fila,) in filas.items():
                assert fila.api_key_id == m["id_clave"][clave]
                por_clave[clave].add(fila.id)
            return por_clave

        por_clave = await _ronda("A")
        duenos = {nid: clave for clave, nids in por_clave.items() for nid in nids}
        llamadas = [(b, k) for b in (b1, b2) for k in b.claves for _ in range(3)]
        respuestas = await asyncio.gather(*(b.sondear(k) for b, k in llamadas))
        assert [r.status_code for r in respuestas] == [200] * len(llamadas)
        for (_, clave), r in zip(llamadas, respuestas, strict=True):
            vistos = {n["id"] for n in r.json()}
            assert vistos == por_clave[clave], f"la clave {clave} vio avisos de otra o le faltaron"
            assert all(duenos[n] == clave for n in vistos)

        nid_b2 = next(iter(por_clave["u2-a2-b2"]))
        ajeno = await _ack(inst.base, m["claves"]["u2-a2-b1"], CANAL_1, nid_b2)
        ajeno_mismo_canal = await _ack(inst.base, m["claves"]["u3-a3-b2"], CANAL_2, nid_b2)
        assert ajeno.status_code == ajeno_mismo_canal.status_code == 404
        async with maker() as s:
            assert (await s.get(PublicChatNotification, nid_b2)).status == "pending", "un ack ajeno la confirmó"

        canal_de = {k: (CANAL_2 if k.endswith("b2") else CANAL_1) for k in claves}
        dobles = await asyncio.gather(
            *(_ack(inst.base, m["claves"][duenos[n]], canal_de[duenos[n]], n) for n in duenos for _ in range(2))
        )
        assert {r.status_code for r in dobles} == {200}
        async with maker() as s:
            primeras = {n: (await s.get(PublicChatNotification, n)).delivered_at for n in duenos}
        assert all(primeras.values())
        await asyncio.sleep(1.1)
        tercera = await asyncio.gather(
            *(_ack(inst.base, m["claves"][duenos[n]], canal_de[duenos[n]], n) for n in duenos)
        )
        assert {r.status_code for r in tercera} == {200}
        async with maker() as s:
            for n, cuando in primeras.items():
                assert (await s.get(PublicChatNotification, n)).delivered_at == cuando, "un ack repetido la reescribió"
        for _ in range(2):
            for b in (b1, b2):
                for k in b.claves:
                    assert (await b.sondear(k)).json() == [], "un aviso confirmado volvió a entregarse"

        por_clave = await _ronda("B")
        crudo: list[tuple[str, str]] = []

        async def _consumidor(bridge, clave):
            for _ in range(3):
                r = await bridge.sondear(clave)
                assert r.status_code == 200
                for n in r.json():
                    crudo.append((n["id"], clave))
                    assert (await _ack(inst.base, m["claves"][clave], bridge.canal, n["id"])).status_code == 200

        inicio = time.monotonic()
        await asyncio.gather(*(_consumidor(b, k) for b in (b1, b2) for k in b.claves))
        _medicion(
            caso="duplicados", que="tres vueltas de ambos bridges en paralelo", s=round(time.monotonic() - inicio, 2)
        )
        esperados = {(nid, clave) for clave, nids in por_clave.items() for nid in nids}
        assert sorted(crudo) == sorted(esperados), "un aviso se entregó dos veces o por otra clave"


class TestArchivos:
    async def test_adjuntos_y_documentos_solo_para_la_cuenta_de_servicio_y_el_bot_correctos(self, dos_bots):  # noqa: F811
        maker, m = dos_bots["maker"], dos_bots["m"]
        a2, a3 = m["agentes"]["a2"], m["agentes"]["a3"]
        await _cuenta_de_servicio(maker, m, "bridge-teams", ("a1", "a2"), 1)
        await _cuenta_de_servicio(maker, m, "bridge-teams-2", ("a2", "a3", "a4"), 2)
        pila = await dos_bots["levantar"]("robustez-archivos")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        svc1, svc2 = await _entrar(inst.base, "bridge-teams"), await _entrar(inst.base, "bridge-teams-2")
        humano = {
            x: await _entrar(inst.base, await _usuario_de(maker, m["usuarios"][x]), "clave-solo-pruebas")
            for x in ("u1", "u2", "u3")
        }

        subidos = {}
        for agente in ("a3", "a2"):
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{inst.base}/api/agents/{m['agentes'][agente]}/attachments",
                    headers=svc2,
                    files={"file": ("foto.png", f"imagen de U del bot 2 en {agente}".encode(), "image/png")},
                )
            assert r.status_code == 200, r.text
            subidos[agente] = r.json()["file_id"]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{inst.base}/api/agents/{a3}/attachments", headers=svc1, files={"file": ("x.png", b"x", "image/png")}
            )
        assert r.status_code == 403, "la cuenta del bot 1 subió a un agente que no es suyo"

        matriz_subidos = [
            ("a3", svc2, 200),
            ("a3", svc1, 403),
            ("a3", humano["u3"], 404),
            ("a3", humano["u1"], 403),
            ("a2", svc2, 200),
            ("a2", svc1, 404),
            ("a2", humano["u2"], 404),
        ]
        for agente, quien, estado in matriz_subidos:
            r = await _descargar(inst.base, quien, m["agentes"][agente], subidos[agente])
            assert r.status_code == estado, (agente, estado, r.status_code)

        s3a, t3a = await b2.sesion("u3-a3-b2", EXTERNO[("u3", 2)], AAD["u3"])
        s22, t22 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        s21, t21 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        for sesion, token, marca in (
            (s3a, t3a, "MARCA-DOCA3"),
            (s22, t22, "MARCA-DOCA2B2"),
            (s21, t21, "MARCA-DOCA2B1"),
        ):
            assert (await _chat(inst.base, sesion, token, f"Genera el informe {marca} DEMORA=0.2"))["type"] == "done"
        doc = {
            marca: await _adjuntar_documento(maker, m, (await _tarea_con(maker, marca)).id, agente, marca.encode())
            for marca, agente in (("MARCA-DOCA3", "a3"), ("MARCA-DOCA2B2", "a2"), ("MARCA-DOCA2B1", "a2"))
        }
        matriz_docs = [
            ("MARCA-DOCA3", a3, svc2, 200),
            ("MARCA-DOCA3", a3, svc1, 403),
            ("MARCA-DOCA3", a3, humano["u3"], 200),
            ("MARCA-DOCA3", a3, humano["u1"], 403),
            ("MARCA-DOCA2B2", a2, svc2, 200),
            ("MARCA-DOCA2B2", a2, svc1, 404),
            ("MARCA-DOCA2B2", a2, humano["u2"], 200),
            ("MARCA-DOCA2B1", a2, svc1, 200),
            ("MARCA-DOCA2B1", a2, svc2, 404),
        ]
        for marca, agente_id, quien, estado in matriz_docs:
            r = await _descargar(inst.base, quien, agente_id, doc[marca])
            assert r.status_code == estado, (marca, estado, r.status_code)
            if estado == 200:
                assert r.content == marca.encode()

        cron = await _cron(maker, m, "a3", "u3", "doc-u3", "Informe DEMORA=0.2 MARCA-DOCCRON")
        listas = await _esperar(lambda: _crons_terminados(maker, {"c": cron}, {}), 60, "cron con documento")
        corrida = listas["c"][0]
        fid = await _adjuntar_documento(maker, m, corrida.id, "a3", b"documento del cron de U3")
        (aviso,) = await _filas_de(maker, schedule_id=cron)
        ruta = f"{inst.base}/api/public/chat/notifications/{aviso.id}/attachments/{fid}"
        async with httpx.AsyncClient(timeout=15) as c:
            propio = await c.get(ruta, headers=b2.cabeceras(m["claves"]["u3-a3-b2"]))
            otra_clave_mismo_usuario = await c.get(ruta, headers=b2.cabeceras(m["claves"]["u3-a4-b2"]))
            otro_usuario = await c.get(ruta, headers=b2.cabeceras(m["claves"]["u2-a2-b2"]))
            otro_bot = await c.get(ruta, headers=b1.cabeceras(m["claves"]["u2-a2-b1"]))
        assert propio.status_code == 200 and propio.content == b"documento del cron de U3"
        assert otra_clave_mismo_usuario.status_code == otro_usuario.status_code == otro_bot.status_code == 404


class TestCredenciales:
    async def test_claves_canales_y_tokens_de_un_bot_no_sirven_en_el_otro(self, dos_bots):  # noqa: F811
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        pila = await dos_bots["levantar"]("robustez-credenciales")
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        k1, k2 = m["claves"]["u2-a2-b1"], m["claves"]["u2-a2-b2"]
        ka3, ka4 = m["claves"]["u3-a3-b2"], m["claves"]["u3-a4-b2"]
        sesion_b1 = await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"])
        sesion_b2 = await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"])
        sesion_a3 = await b2.sesion("u3-a3-b2", EXTERNO[("u3", 2)], AAD["u3"])
        sesion_a4 = await b2.sesion("u3-a4-b2", EXTERNO[("u3", 2)], AAD["u3"])
        async with maker() as s:
            for (sid, _), agente in ((sesion_a3, "a3"), (sesion_a4, "a4"), (sesion_b1, "a2"), (sesion_b2, "a2")):
                assert (await s.get(PublicChatSession, sid)).agent_id == m["agentes"][agente]
        cron_b2 = await _cron(maker, m, "a2", "u2", "cred-b2", "Informe DEMORA=0.2 MARCA-CREDB2", clave="u2-a2-b2")
        cron_a4 = await _cron(maker, m, "a4", "u3", "cred-a4", "Informe DEMORA=0.2 MARCA-CREDA4")
        aviso = {}
        for nombre, sid in (("b2", cron_b2), ("a4", cron_a4)):
            filas = await _esperar(lambda sid=sid: _filas_de(maker, schedule_id=sid), 60, f"aviso {nombre}")
            aviso[nombre] = filas[0]
        aprob_b2 = await _aprobacion(maker, m, "a2", "u2", sesion_b2[0])
        aprob_a4 = await _aprobacion(maker, m, "a4", "u3", sesion_a4[0])

        canal_cruzado = {
            "sondeo": await b1.sondear("u2-a2-b1", canal=CANAL_2),
            "ack": await _ack(inst.base, k1, CANAL_2, aviso["b2"].id),
            "pendientes": await _pendientes(inst.base, k1, CANAL_2, sesion_b1),
            "decision": await _decidir(inst, k1, CANAL_2, aprob_b2, sesion_b1, AAD["u2"]),
            "sesion": await _sesion_cruda(inst.base, k1, CANAL_2, EXTERNO[("u2", 1)], AAD["u2"]),
        }
        for nombre, r in canal_cruzado.items():
            codigo = _codigo(r) if r.status_code >= 400 else None
            assert (r.status_code, codigo) == (403, "canal_incorrecto"), (nombre, r.text)

        otro_agente = {
            "pendientes": (await _pendientes(inst.base, ka3, CANAL_2, sesion_a4), 403, None),
            "decision": (await _decidir(inst, ka3, CANAL_2, aprob_a4, sesion_a4, AAD["u3"]), 403, "otra_conversacion"),
            "ack": (await _ack(inst.base, ka3, CANAL_2, aviso["a4"].id), 404, None),
        }
        for nombre, (r, estado, codigo) in otro_agente.items():
            assert r.status_code == estado and (codigo is None or _codigo(r) == codigo), (nombre, r.text)
        async with httpx.AsyncClient(timeout=15) as c:
            colado = await c.post(
                f"{inst.base}/api/public/chat/sessions/{sesion_a4[0]}/messages",
                headers={"X-Session-Token": sesion_a3[1]},
                json={"content": "Hola MARCA-COLADOA4 DEMORA=0.2"},
            )
            cruzado = await c.post(
                f"{inst.base}/api/public/chat/sessions/{sesion_b2[0]}/messages",
                headers={"X-Session-Token": sesion_b1[1]},
                json={"content": "Hola MARCA-COLADOB2 DEMORA=0.2"},
            )
        assert colado.status_code == cruzado.status_code == 400
        assert not _llamadas(handler, "MARCA-COLADOA4") and not _llamadas(handler, "MARCA-COLADOB2")

        token_ajeno = {
            "pendientes": (await _pendientes(inst.base, k2, CANAL_2, sesion_b1), 403, None),
            "decision": (await _decidir(inst, k2, CANAL_2, aprob_b2, sesion_b1, AAD["u2"]), 403, "otra_conversacion"),
            "decision inversa": (
                await _decidir(inst, k1, CANAL_1, aprob_b2, sesion_b2, AAD["u2"]),
                403,
                "otra_conversacion",
            ),
        }
        for nombre, (r, estado, codigo) in token_ajeno.items():
            assert r.status_code == estado and (codigo is None or _codigo(r) == codigo), (nombre, r.text)

        async with maker() as s:
            for aprob in (aprob_b2, aprob_a4):
                assert (await ac._cargar(s, aprob.id)).estado == "pendiente"
            for fila in aviso.values():
                assert (await s.get(PublicChatNotification, fila.id)).status == "pending"
        assert [a["id"] for a in (await _pendientes(inst.base, k2, CANAL_2, sesion_b2)).json()["aprobaciones"]] == [
            aprob_b2.id
        ], "control: con su clave, su canal y su sesión sí la ve"
        assert (await _ack(inst.base, ka4, CANAL_2, aviso["a4"].id)).status_code == 200, "control: su clave confirma"
        valida = await _decidir(inst, ka4, CANAL_2, aprob_a4, sesion_a4, AAD["u3"])
        assert valida.status_code == 200 and valida.json()["estado"] == "rechazada", "control: su sesión decide"


async def _escenario_un_solo_bot(maker, m, pila, etiqueta: str) -> dict:
    inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
    u1, k1, id_k1 = m["usuarios"]["u1"], m["claves"]["u1-a1-b1"], m["id_clave"]["u1-a1-b1"]
    b1.arrancar()
    b2.arrancar()
    await _esperar_entregas(None, lambda: len(b1.respuestas) >= 2 and len(b2.respuestas) >= 3, 20, "sondeos")
    sesion = await b1.sesion("u1-a1-b1", "29:u1-b1", AAD["u1"], canal=None, instancia=None)
    r = await _chat(inst.base, *sesion, f"Hola MARCA-UNO{etiqueta} DEMORA=0.2")
    assert r["type"] == "done" and f"MARCA-UNO{etiqueta}" in r["content"]
    tarea = await _tarea_con(maker, f"MARCA-UNO{etiqueta}")
    volatiles = {"public_session_id", "conversation_epoch", "usage", "desglose_tiempos", "tiempos", "execution"}
    cron = await _cron(maker, m, "a1", "u1", f"uno-{etiqueta}", f"Informe DEMORA=0.2 MARCA-UNOCRON{etiqueta}")
    chat = asyncio.create_task(_chat(inst.base, *sesion, f"Informe pesado MARCA-UNOTARDIA{etiqueta} DEMORA=6"))
    await _esperar(lambda: _llamada(pila["handler"], f"MARCA-UNOTARDIA{etiqueta}"), 30, "chat tardío en curso")
    tardia = await _tarea_con(maker, f"MARCA-UNOTARDIA{etiqueta}")
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE tasks SET metadata = (metadata::jsonb || '{\"respuesta_tardia\": true}'::jsonb)::json "
                "WHERE id = :i"
            ),
            {"i": tardia.id},
        )
        await s.commit()
    chat.cancel()
    await _esperar_entregas(None, lambda: b1.de(schedule_id=cron) and b1.de(task_id=tardia.id), 60, "avisos de U1")
    await asyncio.sleep(3)

    def _ficha(n: dict) -> dict:
        campos = ("origin", "bot_channel_id", "recipient_external_id", "recipient_aad_object_id", "sender_id", "clave")
        return {c: n.get(c) for c in campos}

    avisos = {
        "cron": b1.de(schedule_id=cron) + b2.de(schedule_id=cron),
        "tardia": b1.de(task_id=tardia.id) + b2.de(task_id=tardia.id),
    }
    filas = {"cron": await _filas_de(maker, schedule_id=cron), "tardia": await _filas_de(maker, task_id=tardia.id)}
    aprob_a = await _aprobacion(maker, m, "a1", "u1", sesion[0], tarea.id)
    vistas = await _pendientes(inst.base, k1, None, sesion)
    decision = await _decidir(inst, k1, None, aprob_a, sesion, AAD["u1"])
    repetida = await _decidir(inst, k1, None, aprob_a, sesion, AAD["u1"])
    aprob_b = await _aprobacion(maker, m, "a1", "u1", sesion[0], tarea.id)
    decision_otro_aad = await _decidir(inst, k1, None, aprob_b, sesion, AAD["u3"])
    return {
        "chat_metadata": sorted(set(tarea.metadata_json) - volatiles),
        "chat_hilo": (
            tarea.metadata_json["thread_user_id"] == u1,
            tarea.metadata_json["public_chat_api_key_id"] == id_k1,
        ),
        "avisos": {k: [_ficha(n) for n in v] for k, v in avisos.items()},
        "filas": {
            k: [(f.api_key_id == id_k1, f.recipient_user_id == u1, f.status) for f in v] for k, v in filas.items()
        },
        "pendientes": (
            vistas.status_code,
            [a["id"] == aprob_a.id for a in vistas.json()["aprobaciones"]],
        ),
        "decision": (decision.status_code, decision.json().get("estado")),
        "decision_repetida": (repetida.status_code, _codigo(repetida)),
        "decision_otro_aad": (decision_otro_aad.status_code, decision_otro_aad.json().get("estado")),
        "corridas": len(await _corridas(maker, cron)),
    }


class TestUnSoloBot:
    async def test_usuario_de_un_solo_bot_sin_datos_nuevos_igual_con_y_sin_canales(self, dos_bots):  # noqa: F811
        maker, m = dos_bots["maker"], dos_bots["m"]
        uno = await dos_bots["levantar"]("robustez-un-bot-con-canales")
        con_canales = await _escenario_un_solo_bot(maker, m, {**uno, "handler": dos_bots["handler"]}, "C")
        await uno["b1"].detener()
        await uno["b2"].detener()
        uno["inst"].kill_group()
        async with maker() as s:
            await s.execute(text("UPDATE public_chat_api_keys SET bot_channel_id = NULL"))
            await s.execute(text("DELETE FROM user_channel_identities"))
            await s.execute(text("DELETE FROM bot_channels"))
            await s.commit()
        dos = await dos_bots["levantar"]("robustez-un-bot-sin-canales")
        sin_canales = await _escenario_un_solo_bot(maker, m, {**dos, "handler": dos_bots["handler"]}, "S")
        print("MEDICION", json.dumps({"caso": "un solo bot", "con": con_canales, "sin": sin_canales}, default=str))
        assert con_canales == sin_canales, "U1 se comporta distinto según existan o no canales e identidades"
        legado = {
            "origin": None,
            "bot_channel_id": None,
            "recipient_external_id": None,
            "recipient_aad_object_id": None,
            "sender_id": None,
            "clave": "u1-a1-b1",
        }
        assert con_canales["avisos"] == {
            "cron": [{**legado, "origin": "cron"}],
            "tardia": [{**legado, "origin": "respuesta_tardia"}],
        }
        assert con_canales["filas"] == {"cron": [(True, True, "delivered")], "tardia": [(True, True, "delivered")]}
        assert "bot_channel_id" not in con_canales["chat_metadata"] and con_canales["chat_hilo"] == (True, True)
        assert con_canales["pendientes"] == (200, [True])
        assert con_canales["decision"] == (200, "rechazada") and con_canales["corridas"] == 1
        assert con_canales["decision_repetida"][0] != 200, "la misma aprobación se decidió dos veces"
        assert con_canales["decision_otro_aad"] == (200, "rechazada"), (
            "sin identidad de canal registrada una clave fija acepta el AAD que declare el bridge (comportamiento previo)"
        )


class TestConcurrencia:
    async def test_crons_de_tres_usuarios_y_chats_en_ambos_bots_a_la_vez_sin_cruces(self, dos_bots):  # noqa: F811
        maker, handler, m = dos_bots["maker"], dos_bots["handler"], dos_bots["m"]
        u1, u2, u3 = (m["usuarios"][x] for x in ("u1", "u2", "u3"))
        pila = await dos_bots["levantar"]("robustez-concurrencia", LATIDO_HOLGADO)
        inst, b1, b2 = pila["inst"], pila["b1"], pila["b2"]
        b1.arrancar()
        b2.arrancar()
        await _esperar_entregas(None, lambda: len(b1.respuestas) >= 2 and len(b2.respuestas) >= 3, 20, "sondeos")
        sesiones = {
            "u1-a1-b1": await b1.sesion("u1-a1-b1", "29:u1-b1", AAD["u1"], canal=None, instancia=None),
            "u2-a2-b1": await b1.sesion("u2-a2-b1", EXTERNO[("u2", 1)], AAD["u2"]),
            "u2-a2-b2": await b2.sesion("u2-a2-b2", EXTERNO[("u2", 2)], AAD["u2"]),
            "u3-a3-b2": await b2.sesion("u3-a3-b2", EXTERNO[("u3", 2)], AAD["u3"]),
            "u3-a4-b2": await b2.sesion("u3-a4-b2", EXTERNO[("u3", 2)], AAD["u3"]),
        }
        esperado = {
            "u1": [("u1-a1-b1", u1, None)],
            "u2_b2": [("u2-a2-b2", u2, EXTERNO[("u2", 2)])],
            "u2_b1": [("u2-a2-b1", u2, EXTERNO[("u2", 1)])],
            "u3_a3": [("u3-a3-b2", u3, EXTERNO[("u3", 2)])],
            "u3_a4": [("u3-a4-b2", u3, EXTERNO[("u3", 2)])],
        }
        definicion = {
            "u1": ("a1", "u1", "u1-a1-b1"),
            "u2_b2": ("a2", "u2", "u2-a2-b2"),
            "u2_b1": ("a2", "u2", "u2-a2-b1"),
            "u3_a3": ("a3", "u3", "u3-a3-b2"),
            "u3_a4": ("a4", "u3", "u3-a4-b2"),
        }
        ids = {}
        for k, (agente, usuario, clave) in definicion.items():
            ids[k] = await _cron(
                maker,
                m,
                agente,
                usuario,
                f"conc-{k}",
                f"Informe DEMORA=3 MARCA-CC{k.upper()}",
                vencido=False,
                clave=None if k == "u1" else clave,
            )
        disparo = time.time()
        async with maker() as s:
            await s.execute(
                text("UPDATE scheduled_tasks SET next_run = now() - interval '2 seconds' WHERE id = ANY(:i)"),
                {"i": list(ids.values())},
            )
            await s.commit()
        chats = await asyncio.gather(
            *(
                _chat(inst.base, *sesiones[c], f"Mi dato es SECRETO-{c}. MARCA-CH{c.replace('-', '')} DEMORA=2")
                for c in sesiones
            )
        )
        aprobaciones = await asyncio.gather(
            *(
                _aprobacion(maker, m, definicion[k][0], definicion[k][1], sesiones[definicion[k][2]][0])
                for k in ("u2_b1", "u2_b2", "u3_a3")
            )
        )
        await _esperar(lambda: _crons_terminados(maker, ids, {}), 120, "cinco crons a la vez")
        await _esperar_entregas(
            None,
            lambda: all(b1.de(schedule_id=v) or b2.de(schedule_id=v) for v in ids.values()),
            40,
            "entregas de los crons",
        )
        await asyncio.sleep(4)

        for c, r in zip(sesiones, chats, strict=True):
            marca = f"MARCA-CH{c.replace('-', '')}"
            assert r["type"] == "done" and marca in r["content"], (c, r)
            assert not [o for o in sesiones if o != c and f"MARCA-CH{o.replace('-', '')}" in r["content"]]
            tarea = await _tarea_con(maker, marca)
            agente, usuario = {
                "u1-a1-b1": ("a1", u1),
                "u2-a2-b1": ("a2", u2),
                "u2-a2-b2": ("a2", u2),
                "u3-a3-b2": ("a3", u3),
                "u3-a4-b2": ("a4", u3),
            }[c]
            assert (tarea.agent_id, tarea.metadata_json["thread_user_id"]) == (m["agentes"][agente], usuario), c
            assert tarea.metadata_json["public_chat_api_key_id"] == m["id_clave"][c], c
            _medicion(caso="concurrencia", chat=c, cliente_s=r["duracion_cliente_s"])
        for k, filas_esperadas in esperado.items():
            vistas = [n for n in _todas(b1, b2) if n.get("schedule_id") == ids[k]]
            assert [(n["clave"], n["recipient_external_id"]) for n in vistas] == [
                (c, e) for c, _, e in filas_esperadas
            ], (k, vistas)
            filas = await _filas_de(maker, schedule_id=ids[k])
            assert [(f.api_key_id, f.recipient_user_id, f.recipient_external_id) for f in filas] == [
                (m["id_clave"][c], u, e) for c, u, e in filas_esperadas
            ], k
            corrida = (await _corridas(maker, ids[k]))[0]
            _medicion(
                caso="concurrencia",
                cron=k,
                disparo_s=round(corrida.queued_at.timestamp() - disparo, 2),
                total_s=round(corrida.completed_at.timestamp() - corrida.queued_at.timestamp(), 2),
                entrega_s=[await _demora_entrega(maker, n) for n in vistas],
            )
        assert not [n for n in _todas(b1, b2) if n.get("task_id") and n.get("origin") != "cron"], (
            "un chat síncrono notificó"
        )
        _medicion(caso="concurrencia", llm_simultaneos=_maximo_simultaneo(handler, ("MARCA-CC", "MARCA-CH")))
        assert _maximo_simultaneo(handler, ("MARCA-CC", "MARCA-CH")) >= 3

        for k, aprob in zip(("u2_b1", "u2_b2", "u3_a3"), aprobaciones, strict=True):
            clave = definicion[k][2]
            for otra in sesiones:
                if otra.startswith("u1"):
                    continue
                otro_canal = CANAL_2 if otra.endswith("b2") else CANAL_1
                r = await _pendientes(inst.base, m["claves"][otra], otro_canal, sesiones[otra])
                ids_vistos = [a["id"] for a in r.json()["aprobaciones"]]
                assert (aprob.id in ids_vistos) == (otra == clave), (k, otra, ids_vistos)

        for c in sesiones:
            r = await _chat(inst.base, *sesiones[c], f"¿Qué dato te di? MARCA-PREG{c.replace('-', '')}")
            assert r["type"] == "done"
            contexto = _contexto(handler, f"MARCA-PREG{c.replace('-', '')}")
            assert f"SECRETO-{c}" in contexto, c
            assert not [o for o in sesiones if o != c and f"SECRETO-{o}." in contexto], (c, "historial cruzado")


class TestBridgeRealConcurrencia:
    async def test_bridge_real_publica_cada_cron_una_vez_en_su_conversacion_y_no_repite_al_reiniciar(self, dos_bots):  # noqa: F811
        ruta = _ruta_bridge()
        if ruta is None:
            pytest.skip("sin copia del bridge (HQ_BRIDGE_ARCHIVO o /bridge)")
        import aiohttp

        maker, m = dos_bots["maker"], dos_bots["m"]
        pila = await dos_bots["levantar"]("robustez-bridge-real")
        inst = pila["inst"]
        conv = {
            "u1-b1": {"conversation_id": "conv-u1-b1", "user_id": "29:u1-b1", "aad_object_id": AAD["u1"]},
            "u2-b1": {"conversation_id": "conv-u2-b1", "user_id": EXTERNO[("u2", 1)], "aad_object_id": AAD["u2"]},
            "x-b1": {"conversation_id": "conv-ajeno-b1", "user_id": "29:ajeno-b1", "aad_object_id": "aad-ajeno"},
            "u2-b2": {"conversation_id": "conv-u2-b2", "user_id": EXTERNO[("u2", 2)], "aad_object_id": AAD["u2"]},
            "u3-b2": {"conversation_id": "conv-u3-b2", "user_id": EXTERNO[("u3", 2)], "aad_object_id": AAD["u3"]},
            "x-b2": {"conversation_id": "conv-ajeno-b2", "user_id": "29:ajeno-b2", "aad_object_id": "aad-ajeno"},
        }
        bot1 = _BotFalso({"t-b1-a1": [conv["u1-b1"]], "t-b1-a2": [conv["x-b1"], conv["u2-b1"]]})
        bot2 = _BotFalso(
            {
                "t-b2-a2": [conv["x-b2"], conv["u2-b2"], conv["u3-b2"]],
                "t-b2-a3": [conv["x-b2"], conv["u3-b2"], conv["u2-b2"]],
                "t-b2-a4": [conv["u2-b2"], conv["x-b2"], conv["u3-b2"]],
            }
        )
        await bot1.arrancar()
        await bot2.arrancar()
        cfg1 = {
            "hermeshq_url": inst.base,
            "bot_url": bot1.url,
            "adjuntos": False,
            "bot_channel": CANAL_1,
            "instancia_bridge": "bridge-real-1",
        }
        cfg2 = {
            "hermeshq_url": inst.base,
            "bot_url": bot2.url,
            "adjuntos": False,
            "bot_channel": CANAL_2,
            "instancia_bridge": "bridge-real-2",
        }
        agentes1 = [
            {"hermes_id": "a1-b1", "public_chat_key": m["claves"]["u1-a1-b1"], "bot_token": "t-b1-a1"},
            {"hermes_id": "a2-b1", "public_chat_key": m["claves"]["u2-a2-b1"], "bot_token": "t-b1-a2"},
        ]
        agentes2 = [
            {"hermes_id": "a2-b2", "public_chat_key": m["claves"]["u2-a2-b2"], "bot_token": "t-b2-a2"},
            {"hermes_id": "a3-b2", "public_chat_key": m["claves"]["u3-a3-b2"], "bot_token": "t-b2-a3"},
            {"hermes_id": "a4-b2", "public_chat_key": m["claves"]["u3-a4-b2"], "bot_token": "t-b2-a4"},
        ]

        def _lanzar(sesion):
            puente1, puente2 = _cargar_bridge(ruta), _cargar_bridge(ruta)
            return [
                asyncio.create_task(p.bucle_notificaciones(a, cfg, sesion))
                for p, cfg, lista in ((puente1, cfg1, agentes1), (puente2, cfg2, agentes2))
                for a in lista
            ], puente2

        esperado = {
            "u1": ("a1", "u1", None, bot1, "conv-u1-b1"),
            "u2_b1": ("a2", "u2", "u2-a2-b1", bot1, "conv-u2-b1"),
            "u2_b2": ("a2", "u2", "u2-a2-b2", bot2, "conv-u2-b2"),
            "u3_a3": ("a3", "u3", None, bot2, "conv-u3-b2"),
            "u3_a4": ("a4", "u3", None, bot2, "conv-u3-b2"),
        }
        async with aiohttp.ClientSession() as sesion:
            tareas, _ = _lanzar(sesion)
            try:
                await asyncio.sleep(3)
                ids = {}
                for k, (agente, usuario, clave, _, _) in esperado.items():
                    ids[k] = await _cron(
                        maker,
                        m,
                        agente,
                        usuario,
                        f"real-{k}",
                        f"Informe DEMORA=1 MARCA-BR{k.upper()}",
                        vencido=False,
                        clave=clave,
                    )
                async with maker() as s:
                    await s.execute(
                        text("UPDATE scheduled_tasks SET next_run = now() - interval '2 seconds' WHERE id = ANY(:i)"),
                        {"i": list(ids.values())},
                    )
                    await s.commit()

                def _listo():
                    return all(bot.en(c, f"MARCA-BR{k.upper()}") for k, (_, _, _, bot, c) in esperado.items())

                await _esperar_entregas(None, _listo, 90, "publicación del bridge real")
                await asyncio.sleep(3)
            finally:
                for t in tareas:
                    t.cancel()
                await asyncio.gather(*tareas, return_exceptions=True)
            publicadas_antes = len(bot1.publicadas) + len(bot2.publicadas)
            tareas, _ = _lanzar(sesion)
            try:
                await asyncio.sleep(5)
            finally:
                for t in tareas:
                    t.cancel()
                await asyncio.gather(*tareas, return_exceptions=True)
                await bot1.cerrar()
                await bot2.cerrar()
        assert len(bot1.publicadas) + len(bot2.publicadas) == publicadas_antes, "el bridge reiniciado repitió avisos"
        for k, (_, _, _, bot, conversacion) in esperado.items():
            marca = f"MARCA-BR{k.upper()}"
            todas = [p for p in bot1.publicadas + bot2.publicadas if marca in json.dumps(p)]
            assert len(todas) == 1 and todas[0]["conversation_id"] == conversacion, (k, todas)
            assert bot.en(conversacion, marca) == todas
            corrida = (await _corridas(maker, ids[k]))[0]
            _medicion(
                caso="bridge real concurrente",
                cron=k,
                entrega_teams_s=round(todas[0]["t"] - corrida.completed_at.timestamp(), 2),
            )
        assert not [p for p in bot1.publicadas + bot2.publicadas if p["conversation_id"].startswith("conv-ajeno")]
        for k in ids:
            for n in await _filas_de(maker, schedule_id=ids[k]):
                assert n.status == "delivered", k
