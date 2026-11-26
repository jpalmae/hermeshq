import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.routers import internal_agents as ia
from hermeshq.schemas.message import MessageCreate
from hermeshq.services import delegacion_entre_usuarios as d
from hermeshq.services.comms_router import CommsRouter
from hermeshq.services.metadata_delegacion import metadata_delegacion_segura
from hermeshq.services.schedule_notifications import _agent_owner

CLAVES_PROHIBIDAS = (
    "thread_user_id",
    "created_by_user_id",
    "conversation",
    "thread_id",
    "public_session_id",
    "source",
    "background_job",
    "background_title",
    "scheduled",
    "scheduled_task_id",
    "respuesta_tardia",
    "attachments",
    "response_attachments",
    "public_chat_bound_user_id",
    "public_chat_sender_id",
    "public_chat_binding_denied",
    "auto_reset",
    "auto_reset_aviso",
    "reset",
    "reply_to",
    "hermeshq_user_id",
    "delegation_owner_user_id",
    "identity_approval_code",
    "identity_approval_expires",
    "identity_granted",
    "delegation_result",
    "manual_trigger",
)


def _metadata_maliciosa():
    datos = {clave: "x" for clave in CLAVES_PROHIBIDAS}
    datos.update(
        {
            "thread_user_id": "u-bruno",
            "conversation": True,
            "scheduled": True,
            "delegated": False,
            "title": "Revisa el correo",
            "parent_task_id": "P1",
            "callback_delivery": {"platform": "telegram", "chat_id": 123, "thread_id": None, "extra": "x"},
        }
    )
    return datos


class TestListaBlancaDeMetadata(unittest.TestCase):
    def test_solo_quedan_las_claves_permitidas(self):
        seguro = metadata_delegacion_segura(_metadata_maliciosa())
        self.assertEqual(set(seguro), {"title", "parent_task_id", "callback_delivery"})
        self.assertEqual(seguro["callback_delivery"], {"platform": "telegram", "chat_id": "123", "thread_id": None})

    def test_valores_invalidos_se_descartan(self):
        self.assertEqual(metadata_delegacion_segura({"title": 5, "parent_task_id": " ", "callback_delivery": "x"}), {})
        self.assertEqual(metadata_delegacion_segura(None), {})


class _Sesion:
    def __init__(self):
        self.agregados = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def add(self, obj):
        self.agregados.append(obj)

    async def flush(self):
        for obj in self.agregados:
            if getattr(obj, "id", None) is None:
                obj.id = "nuevo"

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class TestCaminoComms(unittest.IsolatedAsyncioTestCase):
    async def test_delegar_por_comms_no_hereda_identidad(self):
        sesion = _Sesion()
        router = CommsRouter(lambda: sesion, SimpleNamespace(publish=AsyncMock()))
        await router.send_message(
            MessageCreate(
                from_agent_id="a1",
                to_agent_id="a2",
                message_type="delegate",
                content="haz algo",
                metadata=_metadata_maliciosa(),
            )
        )
        tarea = next(obj for obj in sesion.agregados if obj.__class__.__name__ == "Task")
        mensaje = next(obj for obj in sesion.agregados if obj.__class__.__name__ == "AgentMessage")
        for clave in CLAVES_PROHIBIDAS:
            self.assertNotIn(clave, tarea.metadata_json, clave)
            self.assertNotIn(clave, mensaje.metadata_json, clave)
        self.assertIs(tarea.metadata_json["delegated"], True)
        self.assertEqual(tarea.title, "Revisa el correo")
        self.assertEqual(tarea.parent_task_id, "P1")


def _agente(agent_id, system=False):
    return SimpleNamespace(
        id=agent_id,
        slug=agent_id,
        name=agent_id,
        friendly_name=None,
        is_system_agent=system,
        status="running",
    )


AGENTES = {
    "den": _agente("den"),
    "den2": _agente("den2"),
    "bruno": _agente("bruno"),
    "hq-operator": _agente("hq-operator", system=True),
    "sin-dueno": _agente("sin-dueno"),
}
DUENOS_HUMANOS = {"den": {"u-den"}, "den2": {"u-den"}, "bruno": {"u-bruno"}}


def _request():
    comms = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id="m1", task_id="t-nueva")))
    supervisor = SimpleNamespace(start_agent=AsyncMock(), submit_task=AsyncMock())
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(comms_router=comms, supervisor=supervisor)))


class TestEndpointDelegar(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        d._pendientes.clear()
        d._autorizados.clear()

    async def _delegar(self, origen, destino, modo="bloquear", metadata=None):
        request = _request()
        tarea_origen = SimpleNamespace(
            id="T1",
            agent_id=origen,
            prompt="revisa el correo de Bruno",
            queued_at=None,
            metadata_json={"source": "public_chat", "thread_user_id": "u-den", "public_session_id": "S1"},
        )
        db = SimpleNamespace(get=AsyncMock(return_value=tarea_origen))
        payload = ia.InternalDelegateRequest(
            target_agent=destino,
            instruction="revisa mi correo",
            title="Correo",
            metadata=metadata or {},
            origin_task_id="T1",
        )
        settings = SimpleNamespace(cross_user_delegation_mode=modo)
        with (
            patch.object(ia, "_load_agent_map", AsyncMock(return_value=AGENTES)),
            patch.object(ia, "validate_delegate_hierarchy", MagicMock()),
            patch.object(ia, "delegate_route", MagicMock(return_value=(True, "independent"))),
            patch.object(ia, "duenos_humanos_por_agente", AsyncMock(return_value=DUENOS_HUMANOS)),
            patch.object(ia, "duenos_por_agente", AsyncMock(return_value=DUENOS_HUMANOS)),
            patch.object(d, "get_settings", return_value=settings),
        ):
            try:
                resultado = await ia.delegate_task(payload, request, AGENTES[origen], db)
            except HTTPException as exc:
                return exc, request
        return resultado, request

    async def test_por_defecto_bloquea_hacia_el_agente_de_otro_usuario(self):
        resultado, request = await self._delegar("den", "bruno")
        self.assertIsInstance(resultado, HTTPException)
        self.assertEqual(resultado.status_code, 403)
        self.assertEqual(resultado.detail, d.MENSAJE_DELEGACION_BLOQUEADA)
        request.app.state.comms_router.send_message.assert_not_called()
        request.app.state.supervisor.submit_task.assert_not_called()

    async def test_agente_de_sistema_del_admin_tambien_queda_bloqueado(self):
        resultado, request = await self._delegar("hq-operator", "bruno")
        self.assertIsInstance(resultado, HTTPException)
        request.app.state.comms_router.send_message.assert_not_called()

    async def test_entre_agentes_del_mismo_dueno_sigue_igual(self):
        resultado, request = await self._delegar("den", "den2", metadata=_metadata_maliciosa())
        self.assertTrue(resultado["success"])
        enviado = request.app.state.comms_router.send_message.call_args.args[0]
        for clave in CLAVES_PROHIBIDAS:
            self.assertNotIn(clave, enviado.metadata, clave)
        self.assertEqual(enviado.metadata["title"], "Correo")
        request.app.state.supervisor.submit_task.assert_awaited_once_with("t-nueva")

    async def test_hacia_agente_sin_dueno_humano_se_permite(self):
        resultado, _ = await self._delegar("den", "sin-dueno")
        self.assertTrue(resultado["success"])

    async def test_modo_permitir_no_acepta_el_si_como_autorizacion(self):
        resultado, request = await self._delegar("den", "bruno", modo="permitir_con_aprobacion_solicitante")
        self.assertIsInstance(resultado, HTTPException)
        self.assertEqual(resultado.status_code, 403)
        request.app.state.comms_router.send_message.assert_not_called()

    def test_valor_desconocido_del_setting_cae_en_bloquear(self):
        with patch.object(d, "get_settings", return_value=SimpleNamespace(cross_user_delegation_mode="otro")):
            self.assertEqual(d.modo_delegacion_entre_usuarios(), d.MODO_BLOQUEAR)


class TestDuenosHumanos(unittest.IsolatedAsyncioTestCase):
    async def test_excluye_cuentas_de_servicio_pero_no_admins(self):
        filas = MagicMock()
        filas.all.return_value = [
            ("den", "u-den", "ana"),
            ("den", "u-bridge", "bridge-teams"),
            ("bruno", "u-bridge", "bridge-teams"),
            ("hq-operator", "u-admin", "admin"),
        ]
        db = SimpleNamespace(execute=AsyncMock(return_value=filas))
        duenos = await d.duenos_humanos_por_agente(db)
        self.assertEqual(duenos, {"den": {"u-den"}, "hq-operator": {"u-admin"}})
        self.assertFalse(d.duenos_distintos(duenos, AGENTES["den"], AGENTES["bruno"]))
        self.assertTrue(d.duenos_distintos(duenos, AGENTES["hq-operator"], AGENTES["den"]))

    def test_dueno_compartido_no_basta_si_los_conjuntos_difieren(self):
        duenos = {"a": {"u-admin", "u-den"}, "b": {"u-admin", "u-bruno"}}
        self.assertTrue(d.duenos_distintos(duenos, SimpleNamespace(id="a"), SimpleNamespace(id="b")))


def _sesion_duenos(claves, usuarios):
    sesion = MagicMock()
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = [SimpleNamespace(bound_user_id=u) for u in claves]
    sesion.execute = AsyncMock(return_value=resultado)
    sesion.get = AsyncMock(side_effect=lambda _modelo, uid: usuarios.get(uid))
    return sesion


class TestDuenoUnicoParaAvisos(unittest.IsolatedAsyncioTestCase):
    USUARIOS = {
        "u-den": SimpleNamespace(id="u-den", username="ana"),
        "u-bruno": SimpleNamespace(id="u-bruno", username="bruno"),
        "u-bridge": SimpleNamespace(id="u-bridge", username="bridge-teams"),
    }

    async def test_un_solo_dueno(self):
        dueno = await _agent_owner(_sesion_duenos(["u-den", "u-den", "u-bridge"], self.USUARIOS), "den")
        self.assertEqual(dueno.id, "u-den")

    async def test_varios_duenos_no_avisa(self):
        self.assertIsNone(await _agent_owner(_sesion_duenos(["u-bruno", "u-den"], self.USUARIOS), "den"))

    async def test_sin_dueno_humano_no_avisa(self):
        self.assertIsNone(await _agent_owner(_sesion_duenos(["u-bridge"], self.USUARIOS), "den"))
        self.assertIsNone(await _agent_owner(_sesion_duenos([], self.USUARIOS), "den"))
