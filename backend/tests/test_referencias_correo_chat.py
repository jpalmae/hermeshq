from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import requires_database, run_queued
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_referencias_correo_plugin import _plugin_correo

from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services import referencias_correo as rc
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.hermes_runtime import RuntimeExecutionResult
from hermeshq.services.public_chat_service import PublicChatService, _hash_key

BANDEJA = [
    ("Reunión semanal de seguimiento del proyecto Demo", "Ana Pérez", "ana.perez@cliente.example"),
    ("RE: Factura octubre", "Proveedor Uno", "facturas@proveedor.example"),
    ("Solicitud de acceso a SharePoint", "Carlos Díaz", "cdiaz@hq.example"),
    ("Recordatorio: evaluación de desempeño", "Recursos Humanos", "rrhh@hq.example"),
    ("Tienes 3 mensajes nuevos en el canal General", "Microsoft Teams", "noreply@teams.example"),
    ("Cotización licencias Power BI", "Proveedor Dos", "ventas@pbi.example"),
    ("Caída del servidor de correo", "Soporte TI", "soporte@hq.example"),
    ("Invitación: comité de seguridad", "Ana Rojas", "drojas@hq.example"),
    ("Informe mensual de ventas", "Bruno Ruiz", "bruiz@hq.example"),
    ("Confirmación de pedido 4471", "Tienda Online", "pedidos@tienda.example"),
    ("Newsletter noviembre", "Comunicaciones", "comunicaciones@hq.example"),
]


def _mid(indice: int) -> str:
    return f"AAMkAGI2chat{indice:02d}" + "Q" * 70


def _graph(indice: int, asunto: str, nombre: str, direccion: str) -> dict:
    return {
        "id": _mid(indice),
        "internetMessageId": f"<m{indice}@cliente.example>",
        "subject": asunto,
        "from": {"emailAddress": {"name": nombre, "address": direccion}},
        "receivedDateTime": (datetime(2026, 11, 11, 12, 0, tzinfo=UTC) - timedelta(hours=indice)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "bodyPreview": f"Texto de {asunto}",
    }


def _mensajes(correos=None, herramienta="ms365_mail_list", con_nombre=True) -> list[dict]:
    correos = correos if correos is not None else [_graph(i, *c) for i, c in enumerate(BANDEJA, start=1)]
    resultado = {"success": True, "count": len(correos), "messages": correos}
    herramienta_msg = {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(resultado, ensure_ascii=False)}
    if con_nombre:
        herramienta_msg["name"] = herramienta
    return [
        {"role": "user", "content": "tráeme mis correos en la bandeja de entrada"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": herramienta, "arguments": json.dumps({"count": 50, "folder": "inbox"})},
                }
            ],
        },
        herramienta_msg,
    ]


def _respuesta_numerada() -> str:
    filas = []
    for i, (asunto, nombre, _) in enumerate(BANDEJA, start=1):
        corto = asunto if len(asunto) <= 34 else asunto[:30].rstrip() + "…"
        filas.append(f"{i}. **{corto}** — {nombre} · {12 - i:02d}:00")
    return (
        "Estos son tus últimos correos en la bandeja de entrada:\n\n" + "\n".join(filas) + "\n\n¿Quieres abrir alguno?"
    )


def _respuesta_tabla() -> str:
    filas = ["| # | Asunto | Remitente | Fecha |", "|---|---|---|---|"]
    for i, (asunto, nombre, _) in enumerate(BANDEJA, start=1):
        filas.append(f"| {i} | {asunto} | {nombre} | 11/11 |")
    return "Tu bandeja:\n\n" + "\n".join(filas)


class TestAlineacionDelListado:
    def test_lista_numerada_con_asuntos_recortados_y_markdown(self):
        listado = rc.extraer_listado_chat(_respuesta_numerada(), _mensajes())
        assert listado["origen"] == "chat" and listado["total"] == 11
        assert [e["message_id"] for e in listado["elementos"]] == [_mid(i) for i in range(1, 12)]
        tercero = listado["elementos"][2]
        assert tercero["posicion"] == 3 and tercero["display_id"] == "#3"
        assert tercero["asunto"] == "Solicitud de acceso a SharePoint" and tercero["remitente"] == "Carlos Díaz"
        assert tercero["internet_message_id"] == "<m3@cliente.example>" and tercero["verificado"] is True

    def test_tabla_numerada_y_nombre_de_la_herramienta_por_tool_call_id(self):
        listado = rc.extraer_listado_chat(_respuesta_tabla(), _mensajes(con_nombre=False))
        assert [e["message_id"] for e in listado["elementos"]] == [_mid(i) for i in range(1, 12)]

    def test_el_orden_visible_manda_aunque_difiera_del_de_la_herramienta(self):
        correos = [_graph(i, *c) for i, c in enumerate(BANDEJA[:3], start=1)]
        respuesta = (
            "1. Solicitud de acceso a SharePoint — Carlos\n2. Reunión semanal de seguimiento — Ana\n"
            "3. RE: Factura octubre — Proveedor Uno"
        )
        listado = rc.extraer_listado_chat(respuesta, _mensajes(correos))
        assert [e["message_id"] for e in listado["elementos"]] == [_mid(3), _mid(1), _mid(2)]

    def test_tabla_sin_numeros_usa_el_orden_de_las_filas(self):
        correos = [_graph(i, *c) for i, c in enumerate(BANDEJA[:3], start=1)]
        respuesta = "| Asunto | De |\n|---|---|\n| Factura octubre | Proveedor Uno |\n| Solicitud de acceso a SharePoint | Carlos |"
        listado = rc.extraer_listado_chat(respuesta, _mensajes(correos))
        assert [(e["posicion"], e["message_id"]) for e in listado["elementos"]] == [(1, _mid(2)), (2, _mid(3))]

    def test_asuntos_repetidos_se_distinguen_por_remitente(self):
        correos = [_graph(1, "Aviso", "Banco Uno", "a@b1.example"), _graph(2, "Aviso", "Banco Dos", "a@b2.example")]
        listado = rc.extraer_listado_chat("1. Aviso — Banco Dos\n2. Aviso — Banco Uno", _mensajes(correos))
        assert [e["message_id"] for e in listado["elementos"]] == [_mid(2), _mid(1)]

    @pytest.mark.parametrize(
        "respuesta",
        [
            "Tienes 11 correos nuevos; el más importante es la factura de octubre.",
            "1. Factura octubre — Proveedor Uno\n2. Correo que no salió de la herramienta — Nadie",
            "",
        ],
    )
    def test_sin_numeracion_o_sin_alineacion_confiable_no_se_guarda(self, respuesta):
        assert rc.extraer_listado_chat(respuesta, _mensajes()) is None

    def test_sin_resultado_de_listado_no_se_guarda(self):
        otra = _mensajes(herramienta="ms365_mail_send")
        assert rc.extraer_listado_chat(_respuesta_numerada(), otra) is None
        fallida = [{"role": "tool", "name": "ms365_mail_list", "content": json.dumps({"success": False})}]
        assert rc.extraer_listado_chat(_respuesta_numerada(), fallida) is None
        assert rc.extraer_listado_chat(_respuesta_numerada(), None) is None

    def test_ms365_mail_search_tambien_cuenta(self):
        listado = rc.extraer_listado_chat(_respuesta_numerada(), _mensajes(herramienta="ms365_mail_search"))
        assert len(listado["elementos"]) == 11


def _elementos():
    return rc.extraer_listado_chat(_respuesta_numerada(), _mensajes())["elementos"]


class TestReferenciaEnElListado:
    @pytest.mark.parametrize(
        ("texto", "posicion"),
        [
            ("muéstrame el #3", 3),
            ("el tercero", 3),
            ("ábreme el 3", 3),
            ("el de Microsoft Teams", 5),
            ("el de Carlos Díaz", 3),
            ("el último", 11),
            ("el 3º", 3),
        ],
    )
    def test_resuelve_un_solo_correo_con_su_message_id(self, texto, posicion):
        referencia = rc.referencia_en_listado(_elementos(), texto)
        assert referencia["correos"] == [
            {"posicion": posicion, "message_id": _mid(posicion), "asunto": BANDEJA[posicion - 1][0]}
        ]
        assert f'message_id="#{posicion}"' in referencia["linea"]
        assert _mid(posicion) not in referencia["linea"]
        assert "ms365_mail_get" in referencia["linea"] and "no preguntes si debes abrirlo" in referencia["linea"]
        assert referencia["linea"].startswith(f"[Referencia: el #{posicion} de la lista")

    def test_dos_posiciones_dan_las_dos_referencias(self):
        referencia = rc.referencia_en_listado(_elementos(), "el primero y el tercero")
        assert [c["posicion"] for c in referencia["correos"]] == [1, 3]
        assert '"#1"' in referencia["linea"] and '"#3"' in referencia["linea"]
        assert _mid(1) not in referencia["linea"] and _mid(3) not in referencia["linea"]

    def test_ambiguo_da_opciones_sin_ids(self):
        correos = [
            _graph(1, "Aviso uno", "Juan Pérez", "jp@x.example"),
            _graph(2, "Aviso dos", "Juan Soto", "js@x.example"),
        ]
        elementos = rc.extraer_listado_chat("1. Aviso uno — Juan Pérez\n2. Aviso dos — Juan Soto", _mensajes(correos))[
            "elementos"
        ]
        referencia = rc.referencia_en_listado(elementos, "el de Juan")
        assert referencia["correos"] == [] and "Pregúntale cuál" in referencia["linea"]
        assert "#1" in referencia["linea"] and "#2" in referencia["linea"] and "AAMk" not in referencia["linea"]

    def test_numero_fuera_de_la_lista_no_se_inventa(self):
        referencia = rc.referencia_en_listado(_elementos(), "muéstrame el #15")
        assert referencia["correos"] == [] and "no corresponde a ninguno" in referencia["linea"]
        assert "AAMk" not in referencia["linea"]

    @pytest.mark.parametrize(
        "texto",
        [
            "hola, ¿cómo estás?",
            "dame 3 ideas para la reunión",
            "tráeme los correos de Ana",
            "busca el de Pedro",
            "el de Pedro",
            "/reset",
            "redacta un correo de felicitación para Pedro",
        ],
    )
    def test_mensajes_que_no_son_referencia_no_reciben_nada(self, texto):
        assert rc.referencia_en_listado(_elementos(), texto) is None


class TestPluginUsaLaReferenciaDelChat(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin_correo()
        self._tmp = tempfile.TemporaryDirectory()
        ruta = Path(self._tmp.name) / "work" / "correo_unificado_estado.json"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps({"pending": [{"display_id": "#1212", "message_id": "CRON-ID", "subject": "Cron"}]}),
            encoding="utf-8",
        )
        self._env = {"HERMES_SESSION_CWD": self._tmp.name}

    def tearDown(self):
        self._tmp.cleanup()

    def _get(self, message_id, referencia=None):
        payload = {"metadata": {"referencia_correo_chat": referencia} if referencia else {}}
        mensaje = {"id": "x", "subject": "s"}
        with (
            patch.dict(os.environ, {**self._env, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_broker_token_check", return_value=("tok", None)),
            patch.object(self.plugin, "_fetch_message", return_value=(mensaje, None)) as fetch,
        ):
            return json.loads(self.plugin._get_email_tool({"message_id": message_id})), fetch

    def test_el_numero_de_la_lista_del_chat_abre_ese_correo(self):
        referencia = {"listado_task_id": "t1", "correos": [{"posicion": 3, "message_id": _mid(3), "asunto": "x"}]}
        for entrada in ("#3", "3"):
            resultado, fetch = self._get(entrada, referencia)
            assert resultado["success"] is True, resultado
            assert fetch.call_args.args[1] == _mid(3)

    def test_numero_desconocido_no_culpa_al_cron_ni_llama_a_graph(self):
        resultado, fetch = self._get("#3")
        assert resultado["success"] is False
        assert "cron" not in resultado["error"].lower()
        assert "#3" in resultado["error"] and "message_id" in resultado["error"] and "No inventes" in resultado["error"]
        fetch.assert_not_called()

    def test_el_id_del_cron_sigue_funcionando(self):
        resultado, fetch = self._get("#1212")
        assert resultado["success"] is True and fetch.call_args.args[1] == "CRON-ID"

    def test_responder_reenviar_y_marcar_tambien_traducen_el_numero(self):
        referencia = {"listado_task_id": "t1", "correos": [{"posicion": 3, "message_id": _mid(3), "asunto": "x"}]}
        payload = {"metadata": {"referencia_correo_chat": referencia}}
        with patch.dict(os.environ, {**self._env, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}):
            assert self.plugin._resolve_message_id("#3") == _mid(3)
            assert self.plugin._resolve_message_id("3") == _mid(3)
            assert self.plugin._resolve_message_id("#4") == "#4"
        with patch.dict(os.environ, {**self._env, "HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {}})}):
            assert self.plugin._resolve_message_id("#3") == "#3"

    def test_id_inexistente_no_se_informa_como_falta_de_permisos(self):
        import io
        import urllib.error

        for motivo, esperado in (
            ("mensaje_no_autorizado", "No es un problema de permisos"),
            ("mensaje_fuera_de_carpeta_autorizada", "Acceso denegado por la política de correo"),
        ):
            error = urllib.error.HTTPError(
                "http://hq/control/m365/graph",
                403,
                "Forbidden",
                {},
                io.BytesIO(json.dumps({"detail": {"motivo": motivo}}).encode()),
            )
            with (
                patch.object(
                    self.plugin,
                    "_broker_headers",
                    return_value=("http://hq", {"X-HermesHQ-Agent-ID": "a", "X-HermesHQ-Agent-Token": "t"}, "t1"),
                ),
                patch.object(self.plugin.urllib.request, "urlopen", side_effect=error),
            ):
                resultado = self.plugin._broker_graph("GET", "/me/messages/X")
            assert esperado in resultado["error"], resultado

    def test_caso_real_el_modelo_ya_no_copia_el_id_largo(self):
        linea = rc.referencia_en_listado(_elementos(), "Dame el detalle del #3")["linea"]
        assert 'message_id="#3"' in linea and _mid(3) not in linea


def _maker(db_engine):
    engine, _ = db_engine
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _mundo(maker) -> dict:
    sufijo = uuid.uuid4().hex[:8]
    async with maker() as db:
        nodo = Node(name=f"nodo-{sufijo}", hostname="localhost")
        ana = User(username=f"ana-{sufijo}", display_name="Ana", password_hash="x", is_active=True)
        beto = User(username=f"beto-{sufijo}", display_name="Beto", password_hash="x", is_active=True)
        db.add_all([nodo, ana, beto])
        await db.flush()
        agente = Agent(
            node_id=nodo.id,
            name=f"op-{sufijo}",
            slug=f"op-{sufijo}",
            workspace_path=f"/tmp/ws/op-{sufijo}",
            status="running",
        )
        otro = Agent(
            node_id=nodo.id,
            name=f"otro-{sufijo}",
            slug=f"otro-{sufijo}",
            workspace_path=f"/tmp/ws/otro-{sufijo}",
            status="running",
        )
        db.add_all([agente, otro])
        await db.flush()
        db.add_all([AgentAssignment(agent_id=ag.id, user_id=u.id) for ag in (agente, otro) for u in (ana, beto)])
        e = {"agente": agente, "otro": otro, "ana": ana, "beto": beto}
        claves = {"k1": (agente, ana), "k2": (agente, ana), "kb": (agente, beto), "ko": (otro, ana)}
        for nombre, (ag, usuario) in claves.items():
            e[nombre] = PublicChatApiKey(
                key_hash=f"hash-{nombre}-{sufijo}",
                key_prefix="pk_",
                label=f"bridge-teams-{nombre}",
                agent_id=ag.id,
                allowed_domains=["*"],
                bound_user_id=usuario.id,
                last_notification_poll_at=datetime.now(UTC),
            )
            db.add(e[nombre])
        await db.flush()
        for nombre, clave in (("s1", "k1"), ("s1b", "k1"), ("s2", "k2"), ("sb", "kb"), ("so", "ko")):
            e[nombre] = PublicChatSession(
                api_key_id=e[clave].id,
                agent_id=e[clave].agent_id,
                session_token=_hash_key(f"token-{nombre}"),
                status="active",
                last_activity=datetime.now(UTC),
                ttl_minutes=60,
            )
            db.add(e[nombre])
        await db.commit()
        return e


def _supervisor(maker, resultado):
    runtime = MagicMock()
    runtime.execute = AsyncMock(return_value=resultado)
    supervisor = AgentSupervisor(maker, MagicMock(publish=AsyncMock()), runtime, secret_vault=None)
    supervisor.submit_task = AsyncMock()
    return supervisor


async def _listado_en_el_chat(maker, e) -> Task:
    async with maker() as db:
        tarea = Task(
            agent_id=e["agente"].id,
            title="Public chat message",
            prompt="tráeme mis correos en la bandeja de entrada",
            status="queued",
            queued_at=datetime.now(UTC) - timedelta(minutes=3),
            metadata_json={
                "conversation": True,
                "source": "public_chat",
                "public_session_id": e["s1"].id,
                "thread_user_id": e["ana"].id,
                "public_chat_api_key_id": e["k1"].id,
                "public_chat_bound_user_id": e["ana"].id,
            },
        )
        db.add(tarea)
        await db.commit()
    resultado = RuntimeExecutionResult(
        final_response=_respuesta_numerada(),
        messages=_mensajes(),
        tool_calls=[{"name": "ms365_mail_list"}],
        tokens_used=10,
        iterations=2,
        engine="stub",
        response_attachments=[],
        usage={"turn_exit_reason": "text_response(finish_reason=stop)", "api_calls": 2},
    )
    await run_queued(_supervisor(maker, resultado), tarea.id)
    async with maker() as db:
        return await db.get(Task, tarea.id)


async def _enviar(maker, sesion_id: str, token: str, texto: str) -> Task | None:
    servicio = PublicChatService(maker, SimpleNamespace(active_tasks={}, submit_task=AsyncMock()), None)
    async with maker() as db:
        task_id, _ = await servicio.send_message(sesion_id, token, texto, db)
    if task_id is None:
        return None
    async with maker() as db:
        tarea = await db.get(Task, task_id)
        tarea.status = "completed"
        tarea.response = "Aquí está el contenido del correo."
        tarea.completed_at = datetime.now(UTC)
        await db.commit()
        return tarea


@pytest.mark.integration
@requires_database
class TestSeguimientoDelListadoDelChat:
    async def test_numero_ordinal_y_nombre_reciben_el_message_id_exacto(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        listado = await _listado_en_el_chat(maker, e)
        guardado = listado.metadata_json[rc.CLAVE_LISTADO_CHAT]
        assert listado.status == "completed" and guardado["origen"] == "chat"
        assert [x["message_id"] for x in guardado["elementos"]] == [_mid(i) for i in range(1, 12)]

        por_numero = await _enviar(maker, e["s1"].id, "token-s1", "muéstrame el #3")
        assert 'message_id="#3"' in por_numero.prompt, por_numero.prompt
        assert por_numero.prompt.index("[Referencia: el #3") < por_numero.prompt.index("muéstrame el #3")
        assert por_numero.metadata_json["referencia_correo_chat"]["correos"] == [
            {"posicion": 3, "message_id": _mid(3), "asunto": "Solicitud de acceso a SharePoint"}
        ]
        assert por_numero.metadata_json["referencia_correo_chat"]["listado_task_id"] == listado.id

        por_ordinal = await _enviar(maker, e["s1"].id, "token-s1", "el tercero")
        assert 'message_id="#3"' in por_ordinal.prompt

        por_nombre = await _enviar(maker, e["s1"].id, "token-s1", "el de Microsoft Teams")
        assert 'message_id="#5"' in por_nombre.prompt
        assert '"#3"' not in por_nombre.prompt
        assert por_nombre.metadata_json["referencia_correo_chat"]["correos"][0]["message_id"] == _mid(5)

    async def test_otro_usuario_otra_sesion_otra_clave_u_otro_agente_no_lo_reciben(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        await _listado_en_el_chat(maker, e)
        for sesion in ("sb", "s2", "s1b", "so"):
            tarea = await _enviar(maker, e[sesion].id, f"token-{sesion}", "muéstrame el #3")
            assert tarea is not None, sesion
            assert "AAMkAGI2chat" not in tarea.prompt and "[Referencia" not in tarea.prompt, (sesion, tarea.prompt)
            assert "referencia_correo_chat" not in tarea.metadata_json, sesion
            assert tarea.metadata_json.get("public_chat_bound_user_id"), sesion

    async def test_tras_reset_no_se_resuelve(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        await _listado_en_el_chat(maker, e)
        con_reset = await _enviar(maker, e["s1"].id, "token-s1", "/reset muéstrame el #3")
        assert "AAMkAGI2chat" not in con_reset.prompt
        despues = await _enviar(maker, e["s1"].id, "token-s1", "muéstrame el #3")
        assert "AAMkAGI2chat" not in despues.prompt and "referencia_correo_chat" not in despues.metadata_json

    async def test_un_resumen_de_cron_posterior_tiene_prioridad(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        await _listado_en_el_chat(maker, e)
        async with maker() as db:
            db.add(
                PublicChatNotification(
                    api_key_id=e["k1"].id,
                    agent_id=e["agente"].id,
                    recipient_user_id=e["ana"].id,
                    schedule_id=str(uuid.uuid4()),
                    title="Resumen de correos",
                    body="| #1212 | x |",
                    status="delivered",
                    delivered_at=datetime.now(UTC),
                )
            )
            await db.commit()
        tarea = await _enviar(maker, e["s1"].id, "token-s1", "muéstrame el #3")
        assert "AAMkAGI2chat" not in tarea.prompt

    async def test_listado_viejo_fuera_de_la_ventana_no_se_usa(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        listado = await _listado_en_el_chat(maker, e)
        async with maker() as db:
            fila = await db.get(Task, listado.id)
            fila.queued_at = datetime.now(UTC) - rc.VENTANA_IMPLICITA - timedelta(minutes=5)
            await db.commit()
        tarea = await _enviar(maker, e["s1"].id, "token-s1", "muéstrame el #3")
        assert "AAMkAGI2chat" not in tarea.prompt
