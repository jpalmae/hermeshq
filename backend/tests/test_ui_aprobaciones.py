from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import test_e2e_correo_central as _correo_central
from conftest import requires_database
from sqlalchemy import text
from test_e2e_correo_central import _login, _login_como, _pedido_y_aprobado

mundo = _correo_central.mundo

from hermeshq.models.public_chat import PublicChatApiKey, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.services.execution_lease import ExecutionLeaseStore
from hermeshq.services.public_chat_service import _hash_key

pytestmark = [pytest.mark.integration, requires_database]


async def _pendiente(m):
    task_id, lease = await m.tarea()
    plugin = m.plugin_para(task_id, lease)
    pedido = await asyncio.to_thread(m.enviar, plugin)
    return task_id, lease, plugin, pedido


async def _post(m, ruta: str, usuario: str = "admin", **kw):
    headers = await _login_como(m.backend.base, usuario)
    async with httpx.AsyncClient() as c:
        return await c.post(f"{m.backend.base}/api/aprobaciones{ruta}", headers=headers, **kw)


class TestPanelWeb:
    async def test_panel_servido_con_csp_y_sin_insertar_html(self, mundo):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{mundo.backend.base}/api/aprobaciones/panel")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "default-src 'none'" in r.headers["content-security-policy"]
        html = r.text
        assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html
        assert "textContent" in html and 'localStorage.getItem("hermeshq.token")' in html
        assert "x.disabled = true" in html

    async def test_muestra_el_contenido_exacto_que_se_enviara(self, mundo):
        _, _, _, pedido = await _pendiente(mundo)
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            lista = (await c.get(f"{mundo.backend.base}/api/aprobaciones?estado=pendiente", headers=headers)).json()
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        item = next(a for a in lista if a["id"] == pedido["aprobacion_id"])
        assert item["vista_previa"] == fila["vista_previa"] == pedido["borrador"]
        assert item["huella"] == fila["huella"] and item["envio_estado"] == "sin_envio"

    async def test_doble_clic_y_concurrencia_aprobar_rechazar(self, mundo):
        _, _, _, pedido = await _pendiente(mundo)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        cuerpo = {"huella": fila["huella"], "conversacion_id": fila["conversacion_id"]}
        rutas = [f"/{pedido['aprobacion_id']}/aprobar"] * 3 + [f"/{pedido['aprobacion_id']}/rechazar"] * 2
        respuestas = await asyncio.gather(*(_post(mundo, ruta, json=cuerpo) for ruta in rutas))
        codigos = sorted(r.status_code for r in respuestas)
        assert codigos.count(200) == 1 and all(c == 409 for c in codigos if c != 200), codigos
        final = await mundo.aprobacion(pedido["aprobacion_id"])
        assert final["estado"] in ("aprobada", "rechazada")
        async with mundo.maker() as s:
            decisiones = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM approval_events WHERE approval_id = :i AND evento IN ('aprobada', 'rechazada')"
                    ),
                    {"i": pedido["aprobacion_id"]},
                )
            ).scalar()
        assert decisiones == 1

    async def test_expiracion_y_huella_vieja(self, mundo):
        _, _, _, pedido = await _pendiente(mundo)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        vieja = await _post(
            mundo,
            f"/{pedido['aprobacion_id']}/aprobar",
            json={"huella": "f" * 64, "conversacion_id": fila["conversacion_id"]},
        )
        assert vieja.status_code == 409 and vieja.json()["detail"]["codigo"] == "huella_distinta"
        async with mundo.maker() as s:
            await s.execute(
                text("UPDATE approvals SET expira_en = now() - interval '1 minute' WHERE id = :i"),
                {"i": pedido["aprobacion_id"]},
            )
            await s.commit()
        r = await _post(
            mundo,
            f"/{pedido['aprobacion_id']}/aprobar",
            json={"huella": fila["huella"], "conversacion_id": fila["conversacion_id"]},
        )
        assert r.status_code == 410

    async def test_acceso_no_autorizado(self, mundo):
        _, _, _, pedido = await _pendiente(mundo)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        async with httpx.AsyncClient() as c:
            sin_sesion = await c.post(
                f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}/aprobar",
                json={"huella": fila["huella"], "conversacion_id": fila["conversacion_id"]},
            )
        assert sin_sesion.status_code == 401
        ajena = await _post(
            mundo,
            f"/{pedido['aprobacion_id']}/aprobar",
            usuario="otra-usuaria",
            json={"huella": fila["huella"], "conversacion_id": fila["conversacion_id"]},
        )
        assert ajena.status_code == 404
        assert (await mundo.aprobacion(pedido["aprobacion_id"]))["estado"] == "pendiente"

    async def test_estado_incierto_visible_y_conciliable(self, mundo):
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        mundo.graph.modo = "timeout_tras_aceptar"
        await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=pedido["aprobacion_id"])
        headers = await _login(mundo.backend.base)
        async with httpx.AsyncClient() as c:
            item = (
                await c.get(f"{mundo.backend.base}/api/aprobaciones/{pedido['aprobacion_id']}", headers=headers)
            ).json()
        assert item["requiere_conciliacion"] is True and item["envio_estado"] == "desconocida"
        r = await _post(mundo, f"/{pedido['aprobacion_id']}/conciliar")
        assert r.status_code == 200 and r.json()["envio_estado"] == "confirmada"


async def _sesion_teams(m, trust_sender: bool = False):
    clave, token = f"hqk-{uuid.uuid4().hex}", f"tok-{uuid.uuid4().hex}"
    async with m.maker() as s:
        key = PublicChatApiKey(
            key_hash=_hash_key(clave),
            key_prefix=clave[:8],
            label="bridge-pruebas",
            agent_id=m.agent_id,
            allowed_domains=["*"],
            is_active=True,
            bound_user_id=None if trust_sender else m.admin_id,
            trust_sender_id=trust_sender,
        )
        s.add(key)
        await s.flush()
        sesion = PublicChatSession(
            api_key_id=key.id,
            agent_id=m.agent_id,
            session_token=_hash_key(token),
            status="active",
            last_activity=datetime.now(UTC),
            ttl_minutes=60,
            sender_id="aad-dueno",
        )
        s.add(sesion)
        await s.flush()
        tarea = Task(
            agent_id=m.agent_id,
            prompt="envía",
            status="running",
            created_by_user_id=m.admin_id,
            metadata_json={
                "conversation": True,
                "source": "public_chat",
                "public_session_id": sesion.id,
                "thread_user_id": m.admin_id,
            },
        )
        s.add(tarea)
        await s.commit()
        ids = {"clave": clave, "token": token, "sesion": sesion.id, "tarea": tarea.id}
    lease = await ExecutionLeaseStore(m.maker, owner="runner-teams").begin(ids["tarea"], str(uuid.uuid4()), 1, 600)
    return ids, lease


class TestTeams:
    async def test_tarjeta_exacta_decision_del_bridge_y_doble_pulsacion(self, mundo):
        ids, lease = await _sesion_teams(mundo)
        plugin = mundo.plugin_para(ids["tarea"], lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["conversacion_id"] == ids["sesion"]
        async with httpx.AsyncClient() as c:
            pend = await c.get(
                f"{mundo.backend.base}/api/public/chat/aprobaciones/pendientes",
                params={"session_id": ids["sesion"], "session_token": ids["token"]},
                headers={"X-Api-Key": ids["clave"]},
            )
            assert pend.status_code == 200, pend.text
            tarjetas = pend.json()["aprobaciones"]
            assert [t["id"] for t in tarjetas] == [pedido["aprobacion_id"]]
            tarjeta = tarjetas[0]["tarjeta"]
            assert fila["vista_previa"] in [b.get("text") for b in tarjeta["body"]]
            datos = [a["data"] for a in tarjeta["actions"]]
            assert all(d["huella"] == fila["huella"] and d["aprobacion_id"] == fila["id"] for d in datos)
            cuerpo = {
                "session_id": ids["sesion"],
                "session_token": ids["token"],
                "decision": "aprobar",
                "huella": fila["huella"],
                "aad_object_id": "aad-dueno",
            }
            dobles = await asyncio.gather(
                *(
                    c.post(
                        f"{mundo.backend.base}/api/public/chat/aprobaciones/{fila['id']}/decision",
                        json=cuerpo,
                        headers={"X-Api-Key": ids["clave"]},
                    )
                    for _ in range(3)
                )
            )
        codigos = sorted(r.status_code for r in dobles)
        assert codigos == [200, 409, 409], codigos
        ok = next(r for r in dobles if r.status_code == 200).json()
        assert ok["estado"] == "aprobada" and "actions" not in ok["tarjeta"]
        enviado = await asyncio.to_thread(mundo.enviar, plugin, aprobacion_id=fila["id"])
        assert enviado["success"] is True and mundo.envios_graph() == 1

    async def test_quien_pulsa_debe_ser_el_dueno_de_la_sesion(self, mundo):
        ids, lease = await _sesion_teams(mundo, trust_sender=True)
        async with mundo.maker() as s:
            await s.execute(
                text("UPDATE tasks SET metadata = :m WHERE id = :t"),
                {
                    "m": json.dumps(
                        {
                            "conversation": True,
                            "source": "public_chat",
                            "public_session_id": ids["sesion"],
                            "thread_user_id": mundo.admin_id,
                        }
                    ),
                    "t": ids["tarea"],
                },
            )
            await s.commit()
        plugin = mundo.plugin_para(ids["tarea"], lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{mundo.backend.base}/api/public/chat/aprobaciones/{fila['id']}/decision",
                headers={"X-Api-Key": ids["clave"]},
                json={
                    "session_id": ids["sesion"],
                    "session_token": ids["token"],
                    "decision": "aprobar",
                    "huella": fila["huella"],
                    "aad_object_id": "aad-otra-persona",
                },
            )
        assert r.status_code == 403
        assert (await mundo.aprobacion(fila["id"]))["estado"] == "pendiente"

    async def test_sesion_ajena_no_ve_ni_decide(self, mundo):
        ids, lease = await _sesion_teams(mundo)
        otra, _ = await _sesion_teams(mundo)
        plugin = mundo.plugin_para(ids["tarea"], lease)
        pedido = await asyncio.to_thread(mundo.enviar, plugin)
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        async with httpx.AsyncClient() as c:
            pend = await c.get(
                f"{mundo.backend.base}/api/public/chat/aprobaciones/pendientes",
                params={"session_id": otra["sesion"], "session_token": otra["token"]},
                headers={"X-Api-Key": otra["clave"]},
            )
            assert pend.json()["aprobaciones"] == []
            r = await c.post(
                f"{mundo.backend.base}/api/public/chat/aprobaciones/{fila['id']}/decision",
                headers={"X-Api-Key": otra["clave"]},
                json={
                    "session_id": otra["sesion"],
                    "session_token": otra["token"],
                    "decision": "aprobar",
                    "huella": fila["huella"],
                    "aad_object_id": "aad-dueno",
                },
            )
            mezcla = await c.post(
                f"{mundo.backend.base}/api/public/chat/aprobaciones/{fila['id']}/decision",
                headers={"X-Api-Key": otra["clave"]},
                json={
                    "session_id": ids["sesion"],
                    "session_token": ids["token"],
                    "decision": "aprobar",
                    "huella": fila["huella"],
                    "aad_object_id": "aad-dueno",
                },
            )
        assert r.status_code == 409 and mezcla.status_code == 403
        assert (await mundo.aprobacion(fila["id"]))["estado"] == "pendiente"
