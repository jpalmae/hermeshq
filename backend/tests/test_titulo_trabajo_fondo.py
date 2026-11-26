import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.services.auto_background import _title_from
from hermeshq.services.public_chat_service import PublicChatService, SimpleRateLimiter

PEDIDO = (
    "[Archivos adjuntos, ya disponibles en tu workspace:\n- Datos.xlsx (1360 KB) -> "
    "uploads/e0c232b5-d8b9-43b7-a94a-9f.xlsx]\nAnaliza el archivo Datos.xlsx, que contiene dos hojas: Hoja OT "
    "y Hoja Solicitudes. Objetivo: determinar la existencia de reprocesos. Hazlo directo."
)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _servicio(activas):
    service = PublicChatService.__new__(PublicChatService)
    service.rate_limiter = SimpleRateLimiter(max_requests=100)
    service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
    service.supervisor = SimpleNamespace(submit_task=AsyncMock())
    session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None, sender_id=None)
    service.validate_session = AsyncMock(return_value=session)
    service._check_monthly_quota = AsyncMock()
    service._resolve_identity = AsyncMock(return_value=("u1", None))
    service._running_interactive_task = AsyncMock(return_value=None)
    service._racha_del_chat = AsyncMock(return_value={})
    service.debe_reiniciar = MagicMock(return_value=False)
    db = MagicMock()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            id="k1", bound_user_id=None, requests_per_month=0, tokens_per_month=0, status="running"
        )
    )
    db.execute = AsyncMock(return_value=_Result(activas))
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return service, db


class TestTituloDelTrabajoEnSegundoPlano(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parche = patch("hermeshq.services.ejecucion_aprobada.avisos_pendientes", AsyncMock(return_value=None))
        parche.start()
        self.addCleanup(parche.stop)

    def test_caso_real_el_titulo_no_trae_la_nota_de_adjuntos(self):
        titulo = _title_from(PEDIDO)
        self.assertTrue(titulo.startswith("Analiza el archivo Datos.xlsx"), titulo)
        self.assertNotIn("uploads/", titulo)
        self.assertNotIn("[Archivos adjuntos", titulo)

    async def test_el_trabajo_creado_usa_el_titulo_limpio(self):
        service, db = _servicio([])
        with (
            patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")),
            patch("hermeshq.services.auto_background._load_recent_messages", AsyncMock(return_value=[])),
            patch(
                "hermeshq.services.public_chat_service.con_archivos_generados",
                AsyncMock(side_effect=lambda *a, **k: a[-1]),
            ),
        ):
            await service.send_message("s1", "tok", PEDIDO, db)
        trabajos = [
            c.args[0]
            for c in db.add.call_args_list
            if getattr(c.args[0], "metadata_json", None) and c.args[0].metadata_json.get("background_job")
        ]
        self.assertEqual(len(trabajos), 1)
        self.assertNotIn("uploads/", trabajos[0].title)
        self.assertTrue(trabajos[0].metadata_json["background_title"].startswith("Analiza el archivo"))
        self.assertEqual(trabajos[0].metadata_json["mensaje_usuario"], PEDIDO)

    async def test_el_mismo_pedido_con_adjunto_no_se_encola_dos_veces(self):
        activa = SimpleNamespace(
            id="bg1",
            status="running",
            title=f"Trabajo en segundo plano: {_title_from(PEDIDO)}",
            prompt="x",
            response=None,
            queued_at=datetime.now(UTC),
            metadata_json={"background_job": True, "thread_user_id": "u1"},
        )
        service, db = _servicio([activa])
        with patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")):
            task_id, reply = await service.send_message("s1", "tok", PEDIDO, db)
        self.assertIsNone(task_id)
        self.assertIn("Ya estoy trabajando", reply)
