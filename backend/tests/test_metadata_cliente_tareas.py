import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.routers import comms
from hermeshq.schemas.message import MessageCreate
from hermeshq.services import delegation_approval
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.metadata_cliente import metadata_cliente_segura
from hermeshq.services.schedule_notifications import enqueue_delegation_notice, enqueue_schedule_notification

CLAVES_INTERNAS = {
    "scheduled": True,
    "scheduled_task_id": "cron-de-a",
    "scheduled_fire_key": "cron-de-a@x",
    "scheduled_definition_version": 1,
    "manual_trigger": True,
    "agent_created_schedule": True,
    "delegated": True,
    "delegation_result": True,
    "delegation_owner_user_id": "u-a",
    "identity_approval_code": "ABC123",
    "identity_approval_expires": "2099-01-01T00:00:00+00:00",
    "identity_granted": True,
    "callback_delivery": {"platform": "telegram", "chat_id": "999"},
    "background_job": True,
    "background_title": "x",
    "aprobacion_id": "ap-1",
    "aprobacion_aviso": "x",
    "public_session_id": "ps-1",
    "public_chat_bound_user_id": "u-a",
    "public_chat_api_key_id": "k-1",
    "bot_channel_id": "c-1",
    "thread_user_id": "u-a",
    "created_by_user_id": "u-a",
    "response_attachments": [{"path": "x"}],
    "conversation_epoch": 7,
    "reset": True,
    "respuesta_tardia": True,
    "retry_of": "t0",
    "parent_task_id": "p1",
}


class TestMetadataClienteSegura(unittest.TestCase):
    def test_descarta_todas_las_claves_internas(self):
        seguro = metadata_cliente_segura({**CLAVES_INTERNAS, "conversation": True, "source": "agent_conversation"})
        self.assertEqual(seguro, {"conversation": True, "source": "agent_conversation"})

    def test_conserva_lo_que_usa_el_frontend(self):
        self.assertEqual(
            metadata_cliente_segura({"conversation": True, "thread_id": "console_ag1"}),
            {"conversation": True, "thread_id": "console_ag1"},
        )
        self.assertEqual(
            metadata_cliente_segura({"reply_to": "mobile_app", "source": "mobile_app", "title": " Hola "}),
            {"reply_to": "mobile_app", "source": "mobile_app", "title": "Hola"},
        )

    def test_fuentes_reservadas_del_servidor_se_descartan(self):
        for fuente in ("public_chat", "background_job", "mcp", "kapso_whatsapp", "agents_bulk_message"):
            self.assertNotIn("source", metadata_cliente_segura({"source": fuente}))
        self.assertNotIn("reply_to", metadata_cliente_segura({"reply_to": "telegram"}))

    def test_adjuntos_solo_de_uploads(self):
        bueno = {"path": "uploads/0f8fad5b-d9cb-469f-a165-70867728950e.pdf", "media_type": "document", "x": "y"}
        seguro = metadata_cliente_segura(
            {"attachments": [bueno, {"path": "../.hermes/config.yaml"}, {"path": "/etc/passwd"}, "x"]}
        )
        self.assertEqual(
            seguro["attachments"],
            [{"path": "uploads/0f8fad5b-d9cb-469f-a165-70867728950e.pdf", "media_type": "document"}],
        )
        self.assertEqual(metadata_cliente_segura({"attachments": [{"path": "sharepoint_downloads/a.xlsx"}]}), {})

    def test_valores_invalidos(self):
        self.assertEqual(metadata_cliente_segura(None), {})
        self.assertEqual(metadata_cliente_segura({"conversation": "true", "title": 5}), {})


def _cron():
    return SimpleNamespace(id="s1", agent_id="ag", name="Informe", notify_public_chat=True, owner_user_id="u-a")


def _sesion_avisos(cron):
    session = MagicMock()
    usuario = SimpleNamespace(id="u-a", is_active=True, teams_id=None, username="usuario-a")
    session.get = AsyncMock(side_effect=lambda model, _k: cron if model.__name__ == "ScheduledTask" else usuario)
    llave = SimpleNamespace(
        id="k1", trust_sender_id=False, bound_user_id="u-a", last_notification_poll_at=datetime.now(UTC)
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [llave]
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    return session


class TestAvisoDeCronExigeAgenteYDueno(unittest.IsolatedAsyncioTestCase):
    async def test_tarea_del_dueno_en_su_agente_avisa(self):
        tarea = SimpleNamespace(
            id="t1",
            agent_id="ag",
            created_by_user_id="u-a",
            metadata_json={"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": "u-a"},
        )
        cantidad = await enqueue_schedule_notification(_sesion_avisos(_cron()), tarea, success=True, summary="ok")
        self.assertEqual(cantidad, 1)

    async def test_tarea_de_otro_usuario_no_avisa_al_dueno(self):
        sesion = _sesion_avisos(_cron())
        tarea = SimpleNamespace(
            id="t2",
            agent_id="ag",
            created_by_user_id="u-b",
            metadata_json={"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": "u-b"},
        )
        self.assertEqual(await enqueue_schedule_notification(sesion, tarea, success=True, summary="secreto B"), 0)
        sesion.add.assert_not_called()

    async def test_usuario_del_hilo_distinto_no_avisa(self):
        sesion = _sesion_avisos(_cron())
        tarea = SimpleNamespace(
            id="t3",
            agent_id="ag",
            created_by_user_id="u-a",
            metadata_json={"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": "u-b"},
        )
        self.assertEqual(await enqueue_schedule_notification(sesion, tarea, success=True, summary="x"), 0)

    async def test_tarea_de_otro_agente_no_avisa(self):
        sesion = _sesion_avisos(_cron())
        tarea = SimpleNamespace(
            id="t4",
            agent_id="otro-agente",
            created_by_user_id="u-a",
            metadata_json={"scheduled": True, "scheduled_task_id": "s1", "thread_user_id": "u-a"},
        )
        self.assertEqual(await enqueue_schedule_notification(sesion, tarea, success=True, summary="x"), 0)


class TestAvisoDeDelegacionSoloDelServidor(unittest.IsolatedAsyncioTestCase):
    async def test_tarea_creada_por_usuario_no_genera_aviso_ni_oferta(self):
        sesion = _sesion_avisos(_cron())
        tarea = SimpleNamespace(
            id="t5",
            agent_id="ag",
            source_agent_id=None,
            created_by_user_id="u-b",
            title="x",
            metadata_json={"delegated": True, "thread_user_id": "u-b"},
        )
        self.assertEqual(await enqueue_delegation_notice(sesion, tarea, success=False, summary="sin identidad"), 0)
        sesion.add.assert_not_called()

    async def test_aprobacion_de_identidad_ignora_tareas_de_usuario(self):
        meta = {
            "delegated": True,
            "delegation_owner_user_id": "u-a",
            "identity_approval_code": "ABC123",
            "identity_approval_expires": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        tarea = SimpleNamespace(
            id="orig",
            agent_id="ag",
            source_agent_id="sal",
            created_by_user_id="u-b",
            parent_task_id=None,
            title="x",
            prompt="lee el correo de A",
            status="failed",
            queued_at=datetime.now(UTC),
            metadata_json=meta,
        )
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [tarea]
        db.execute = AsyncMock(return_value=result)
        db.add = MagicMock()
        db.flush = AsyncMock()
        self.assertEqual(await delegation_approval.approve_delegation_identity(db, "ag", "u-a", "ABC123"), (None, None))
        db.add.assert_not_called()


def _supervisor():
    supervisor = AgentSupervisor.__new__(AgentSupervisor)
    supervisor.secret_vault = MagicMock()
    supervisor.secret_vault.decrypt.return_value = "token-bot"
    return supervisor


def _sesion_telegram():
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id="ag"))
    canal = MagicMock()
    canal.scalar_one_or_none.return_value = SimpleNamespace(secret_ref="tg")
    secreto = MagicMock()
    secreto.scalar_one_or_none.return_value = SimpleNamespace(value_enc="cifrado")
    session.execute = AsyncMock(side_effect=[canal, secreto])
    return session


class TestCallbackDeliverySoloDelServidor(unittest.IsolatedAsyncioTestCase):
    async def _entregar(self, tarea):
        callbacks: list = []
        sesion = _sesion_telegram()
        await _supervisor()._queue_external_callback_delivery(
            sesion, task=tarea, agent=SimpleNamespace(id="ag"), success=True, summary="respuesta", callbacks=callbacks
        )
        return callbacks, sesion

    async def test_tarea_de_usuario_con_callback_no_sale_por_telegram(self):
        tarea = SimpleNamespace(
            id="t6",
            agent_id="ag",
            source_agent_id=None,
            created_by_user_id="u-b",
            metadata_json={"callback_delivery": {"platform": "telegram", "chat_id": "999"}, "delegated": True},
        )
        callbacks, sesion = await self._entregar(tarea)
        self.assertEqual(callbacks, [])
        sesion.execute.assert_not_called()

    async def test_delegacion_del_servidor_si_entrega(self):
        tarea = SimpleNamespace(
            id="t7",
            agent_id="ag",
            source_agent_id="orquestador",
            created_by_user_id=None,
            metadata_json={"callback_delivery": {"platform": "telegram", "chat_id": "123"}, "delegated": True},
        )
        callbacks, _ = await self._entregar(tarea)
        self.assertEqual(len(callbacks), 1)


class _SesionComms:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_a):
        return SimpleNamespace(id="destino", is_archived=False, status="running")


class TestDelegacionDeUsuarioSinCallback(unittest.IsolatedAsyncioTestCase):
    async def test_usuario_no_fija_callback_delivery_al_delegar_por_comms(self):
        enviado = SimpleNamespace(
            id="m1",
            task_id=None,
            from_agent_id="origen",
            to_agent_id="destino",
            message_type="delegate",
            content="x",
            metadata_json={},
            created_at=datetime.now(UTC),
            read_at=None,
        )
        router = MagicMock()
        router.send_message = AsyncMock(return_value=enviado)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    supervisor=SimpleNamespace(session_factory=lambda: _SesionComms()), comms_router=router
                )
            )
        )
        payload = MessageCreate(
            from_agent_id="origen",
            to_agent_id="destino",
            message_type="delegate",
            content="haz esto",
            metadata={"title": "t", "callback_delivery": {"platform": "telegram", "chat_id": "999"}},
        )
        with (
            patch.object(comms, "ensure_agent_access", AsyncMock(return_value=SimpleNamespace(id="origen"))),
            patch.object(comms, "can_access_agent", AsyncMock(return_value=True)),
            patch.object(comms, "duenos_humanos_por_agente", AsyncMock(return_value={})),
            patch.object(comms, "exigir_con_duenos", MagicMock()),
            patch.object(comms, "_load_agent_map", AsyncMock(return_value={})),
            patch.object(comms, "validate_delegate_hierarchy", MagicMock()),
            patch.object(comms.MessageRead, "model_validate", MagicMock(return_value="ok")),
        ):
            await comms.send_message(payload, request, SimpleNamespace(id="u-b", role="user"))
        enviado_payload = router.send_message.call_args.args[0]
        self.assertEqual(enviado_payload.metadata, {"title": "t"})
