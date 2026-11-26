from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import requires_database
from fastapi import HTTPException
from test_crons_propietario import _escenario, _nueva_tarea

from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.task import Task
from hermeshq.routers import internal_agents
from hermeshq.services import referencias_correo as rc

ELEMENTOS = [
    {
        "posicion": 1,
        "display_id": "#851",
        "asunto": "Caída del servidor",
        "remitente": "Juan Pérez",
        "remitente_correo": "juan.perez@cliente.example",
        "message_id": "AAMk-851",
    },
    {
        "posicion": 2,
        "display_id": "#849",
        "asunto": "Factura octubre",
        "remitente": "Proveedor Uno",
        "remitente_correo": "facturas@proveedor.example",
        "message_id": "AAMk-849",
    },
    {
        "posicion": 3,
        "display_id": "#02",
        "asunto": "Reclamo despacho",
        "remitente": "Juan Soto",
        "remitente_correo": "jsoto@cliente.example",
        "message_id": "AAMk-02",
    },
]


def _r(texto):
    return rc.resolver(ELEMENTOS, rc.interpretar(texto))


class TestInterpretarYResolver:
    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [
            ("quiero trabajar con el segundo correo", "#849"),
            ("el #849", "#849"),
            ("respóndele al 849", "#849"),
            ("el de juan soto", "#02"),
            ("el segundo de Juan", "#02"),
            ("el último", "#02"),
            ("el 3º", "#02"),
            ("posición 1", "#851"),
            ("el correo de la factura", "#849"),
        ],
    )
    def test_referencias_que_identifican_un_solo_correo(self, texto, esperado):
        resultado = _r(texto)
        assert resultado["estado"] == "resuelto", resultado
        assert resultado["correo"]["display_id"] == esperado

    def test_dos_remitentes_con_el_mismo_nombre_piden_aclaracion(self):
        resultado = _r("quiero responder el de Juan")
        assert resultado["estado"] == "ambiguo"
        assert {o["display_id"] for o in resultado["opciones"]} == {"#851", "#02"}

    def test_numero_suelto_que_es_id_y_posicion_distintos_pide_aclaracion(self):
        resultado = _r("el 2")
        assert resultado["estado"] == "ambiguo", resultado
        assert {o["display_id"] for o in resultado["opciones"]} == {"#02", "#849"}

    @pytest.mark.parametrize("texto", ["el décimo", "el #777", "el tercero de Pedro", "el de Pedro"])
    def test_lo_que_no_existe_no_se_adivina(self, texto):
        resultado = _r(texto)
        assert resultado["estado"] == "sin_coincidencia", resultado
        assert "correo" not in resultado

    def test_sin_referencia(self):
        assert _r("hola, ¿cómo estás?")["estado"] == "sin_referencia"

    def test_la_hora_no_se_confunde_con_una_posicion(self):
        assert rc.interpretar("el resumen de las 10:30") == {}
        assert rc.hora_indicada("el de las 9:05") == "09:05"


def _digest(filas, orden=None):
    return {
        "success": True,
        "tablas_md": {"CALIENTE": "..."},
        "filas_nuevas": filas,
        "filas": [],
        **({"orden_visible": orden} if orden else {}),
    }


def _fila(did, asunto, remitente, categoria="CALIENTE"):
    return {"id": did, "asunto": asunto, "remitente": remitente, "categoria": categoria}


def _mensajes(digest):
    return [
        {"role": "user", "content": "cron"},
        {"role": "tool", "name": "ms365_correo_sync", "content": json.dumps(digest, ensure_ascii=False)},
    ]


def _estado(tmp_path, items):
    ruta = tmp_path / "work" / "correo_unificado_estado.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps({"pending": [], "resolved": items}, ensure_ascii=False), encoding="utf-8")


class TestExtraerResumen:
    def test_orden_de_las_tablas_entregadas_y_ids_verificados(self, tmp_path):
        _estado(
            tmp_path,
            [
                {
                    "display_id": "#849",
                    "message_id": "AAMk-849",
                    "internet_message_id": "<849@x>",
                    "subject": "Factura octubre",
                    "sender_address": "f@p.example",
                },
                {"display_id": "#851", "message_id": "AAMk-851", "subject": "Caída del servidor"},
            ],
        )
        digest = _digest([_fila("#849", "Factura octubre", "Proveedor"), _fila("#851", "Caída del servidor", "Juan")])
        respuesta = (
            "## INMEDIATO\n\n| ID | Asunto |\n|---|---|\n| #851 | Caída |\n\n## CALIENTE\n\n| #849 | Factura |\n"
        )
        resumen = rc.extraer_resumen(respuesta, _mensajes(digest), tmp_path)
        assert [(e["posicion"], e["display_id"], e["message_id"]) for e in resumen["elementos"]] == [
            (1, "#851", "AAMk-851"),
            (2, "#849", "AAMk-849"),
        ]
        assert resumen["elementos"][1]["internet_message_id"] == "<849@x>"
        assert resumen["elementos"][1]["remitente_correo"] == "f@p.example"

    def test_asunto_distinto_en_el_estado_no_entrega_un_id_ajeno(self, tmp_path):
        _estado(tmp_path, [{"display_id": "#849", "message_id": "AAMk-otro", "subject": "Otro asunto"}])
        digest = _digest([_fila("#849", "Factura octubre", "Proveedor")], orden=["#849"])
        [elemento] = rc.extraer_resumen("sin tablas", _mensajes(digest), tmp_path)["elementos"]
        assert elemento["message_id"] == "" and elemento["verificado"] is False

    def test_sin_commit_del_informe_no_hay_resumen(self, tmp_path):
        mensajes = [{"role": "tool", "name": "ms365_correo_sync", "content": json.dumps({"success": True})}]
        assert rc.extraer_resumen("| #1 | x |", mensajes, tmp_path) is None
        assert rc.extraer_resumen("| #1 | x |", [], tmp_path) is None

    def test_aviso_recortado_solo_ofrece_lo_que_se_vio(self):
        visibles = rc.visibles_en(ELEMENTOS, "| #851 | Caída |\n| #849 | Factura |")
        assert [e["display_id"] for e in visibles] == ["#851", "#849"]
        assert rc.resolver(visibles, rc.interpretar("el tercero"))["estado"] == "sin_coincidencia"
        assert rc.visibles_en(ELEMENTOS, "Tienes tres correos pendientes.") == ELEMENTOS


def _resumen(elementos=None):
    return {"version": 1, "elementos": [dict(e) for e in (elementos or ELEMENTOS)], "total": 3}


async def _ejecucion_entregada(db, e, usuario, hace: timedelta, elementos=None, estado="delivered", cuerpo=None):
    dueno = e[usuario]
    cron = e["crons"]["ana_aprobado" if usuario == "ana" else "beto_aprobado"]
    clave = PublicChatApiKey(
        key_hash=f"hash-{usuario}-{hace.total_seconds()}-{datetime.now(UTC).timestamp()}",
        key_prefix="hq_",
        label="teams",
        agent_id=e["agente"].id,
        bound_user_id=dueno.id,
    )
    tarea = Task(
        agent_id=e["agente"].id,
        title=cron.name,
        prompt=cron.prompt,
        status="completed",
        created_by_user_id=dueno.id,
        queued_at=datetime.now(UTC) - hace - timedelta(minutes=2),
        metadata_json={
            "scheduled": True,
            "scheduled_task_id": cron.id,
            "thread_user_id": dueno.id,
            "resumen_correo": _resumen(elementos),
        },
    )
    db.add_all([clave, tarea])
    await db.flush()
    db.add(
        PublicChatNotification(
            api_key_id=clave.id,
            agent_id=e["agente"].id,
            recipient_user_id=dueno.id,
            schedule_id=cron.id,
            task_id=tarea.id,
            title=cron.name,
            body=cuerpo if cuerpo is not None else "| #851 | a |\n| #849 | b |\n| #02 | c |",
            status=estado,
            delivered_at=datetime.now(UTC) - hace if estado == "delivered" else None,
        )
    )
    await db.commit()
    return tarea


async def _pedir(db, e, tarea, **campos):
    payload = internal_agents.InternalReferenciaCorreo(task_id=tarea.id, **campos)
    return await internal_agents.resolver_referencia_correo(payload, e["agente"], db)


async def _turno(db, e, usuario, texto, hace_segundos=0):
    tarea = await _nueva_tarea(db, e, usuario, texto, conversation=True, mensaje_usuario=texto)
    tarea.queued_at = datetime.now(UTC) - timedelta(seconds=hace_segundos)
    await db.commit()
    return tarea


@pytest.mark.integration
@requires_database
class TestResolucionContraElUltimoResumen:
    async def test_segundo_correo_del_ultimo_resumen_entregado(self, db_session):
        e = await _escenario(db_session)
        cron = await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=30))
        chat = await _turno(db_session, e, "ana", "quiero trabajar con el segundo correo")
        res = await _pedir(db_session, e, chat)
        assert res["estado"] == "resuelto", res
        assert res["correo"]["display_id"] == "#849" and res["correo"]["message_id"] == "AAMk-849"
        assert res["ejecucion"]["task_id"] == cron.id

    async def test_otro_usuario_no_resuelve_referencias_ajenas(self, db_session):
        e = await _escenario(db_session)
        cron_ana = await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=30))
        chat_beto = await _turno(db_session, e, "beto", "quiero trabajar con el segundo correo")
        assert (await _pedir(db_session, e, chat_beto))["estado"] == "sin_resumen"
        explicito = await _pedir(db_session, e, chat_beto, ejecucion=cron_ana.id)
        assert explicito["estado"] == "sin_resumen" and "AAMk" not in json.dumps(explicito)

    async def test_resumen_viejo_o_anterior_al_reset_no_se_usa_implicitamente(self, db_session):
        e = await _escenario(db_session)
        viejo = await _ejecucion_entregada(db_session, e, "ana", timedelta(hours=13))
        chat = await _turno(db_session, e, "ana", "el segundo")
        assert (await _pedir(db_session, e, chat))["estado"] == "sin_resumen"
        explicito = await _pedir(db_session, e, chat, ejecucion=viejo.id)
        assert explicito["estado"] == "resuelto" and explicito["correo"]["display_id"] == "#849"

        reciente = await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=20))
        assert (await _pedir(db_session, e, chat))["ejecucion"]["task_id"] == reciente.id
        await _nueva_tarea(db_session, e, "ana", "/reset", conversation=True, reset=True)
        tras_reset = await _turno(db_session, e, "ana", "el segundo")
        assert (await _pedir(db_session, e, tras_reset))["estado"] == "sin_resumen"

    async def test_dos_resumenes_sin_leer_piden_cual_y_la_hora_lo_resuelve(self, db_session):
        e = await _escenario(db_session)
        otros = [dict(x, display_id=f"#9{i}", message_id=f"AAMk-9{i}") for i, x in enumerate(ELEMENTOS)]
        primero = await _ejecucion_entregada(
            db_session, e, "ana", timedelta(hours=2), cuerpo="| #90 |\n| #91 |\n| #92 |", elementos=otros
        )
        segundo = await _ejecucion_entregada(db_session, e, "ana", timedelta(hours=1))
        chat = await _turno(db_session, e, "ana", "el segundo")
        res = await _pedir(db_session, e, chat)
        assert res["estado"] == "ambiguo_resumenes", res
        assert {o["task_id"] for o in res["opciones"]} == {primero.id, segundo.id}
        hora = next(o["hora_local"] for o in res["opciones"] if o["task_id"] == primero.id)
        elegido = await _pedir(db_session, e, chat, ejecucion=hora)
        assert elegido["estado"] == "resuelto" and elegido["correo"]["display_id"] == "#91"
        por_numero = await _pedir(db_session, e, chat, referencia="el #849")
        assert por_numero["estado"] == "resuelto" and por_numero["ejecucion"]["task_id"] == segundo.id

    async def test_tras_hablar_del_primero_el_nuevo_resumen_manda(self, db_session):
        e = await _escenario(db_session)
        otros = [dict(x, display_id=f"#9{i}", message_id=f"AAMk-9{i}") for i, x in enumerate(ELEMENTOS)]
        await _ejecucion_entregada(
            db_session, e, "ana", timedelta(hours=2), cuerpo="| #90 |\n| #91 |\n| #92 |", elementos=otros
        )
        await _turno(db_session, e, "ana", "gracias", hace_segundos=90 * 60)
        nuevo = await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=30))
        chat = await _turno(db_session, e, "ana", "el segundo")
        res = await _pedir(db_session, e, chat)
        assert res["estado"] == "resuelto" and res["ejecucion"]["task_id"] == nuevo.id, res

    async def test_aviso_no_entregado_o_recortado(self, db_session):
        e = await _escenario(db_session)
        await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=10), estado="pending")
        chat = await _turno(db_session, e, "ana", "el segundo")
        assert (await _pedir(db_session, e, chat))["estado"] == "sin_resumen"
        await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=5), cuerpo="| #851 | a |\n| #849 | b |")
        assert (await _pedir(db_session, e, chat, referencia="el tercero"))["estado"] == "sin_coincidencia"

    async def test_ambiguo_y_posicion_inexistente_no_entregan_id(self, db_session):
        e = await _escenario(db_session)
        await _ejecucion_entregada(db_session, e, "ana", timedelta(minutes=30))
        chat = await _turno(db_session, e, "ana", "el de Juan")
        ambiguo = await _pedir(db_session, e, chat)
        assert ambiguo["estado"] == "ambiguo" and len(ambiguo["opciones"]) == 2
        inexistente = await _pedir(db_session, e, chat, referencia="el décimo")
        assert inexistente["estado"] == "sin_coincidencia"
        assert "AAMk" not in json.dumps(ambiguo) + json.dumps(inexistente)

    async def test_desde_una_corrida_programada_no_se_resuelve(self, db_session):
        e = await _escenario(db_session)
        programada = await _nueva_tarea(db_session, e, "ana", "corre", scheduled=True)
        with pytest.raises(HTTPException) as exc:
            await _pedir(db_session, e, programada, referencia="el segundo")
        assert exc.value.status_code == 403


class _Historial(list):
    herramientas: list[str] = []


@pytest.mark.integration
@requires_database
class TestResumenEnElSegundoTurno:
    async def test_el_resumen_sigue_en_el_turno_de_aclaracion(self, db_session):
        from test_crons_conversacion import TestResumenProgramadoEnElHistorial

        h = TestResumenProgramadoEnElHistorial()
        e = await _escenario(db_session)
        corrida = await h._resumen_entregado(db_session, e, minutos=30)
        await h._turno(db_session, e, "ana", "respóndele al segundo", minutos=5, sesion="S1", status="completed")
        actual = await h._turno(db_session, e, "ana", "el de las 9", sesion="S1")
        historial = await h._historial(db_session, actual)
        contenidos = [m["content"] for m in historial]
        assert any(c.startswith("[Resumen programado") and corrida.id in c for c in contenidos), contenidos
        assert contenidos.index(next(c for c in contenidos if c.startswith("[Resumen"))) == 0
