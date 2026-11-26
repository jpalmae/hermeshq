import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermeshq.services import adjunto_pendiente as ap
from hermeshq.services.public_chat_service import PublicChatService, SimpleRateLimiter

PEDIDO_ANA = (
    "Hace un informe interactivo de 1 sola pagina sin resumen solo 1 pagina de contenido explicando lo que sale en la "
    "imagen con titulo Estructura Demo. Además, agregar dos árbol jerárquico uno de Empresa (Unidad A) "
    "y otro Filial Sur (Unidad B). Guardalo en C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana y el "
    "archivo que se llame Estructura Demo"
)
SOLO_IMAGEN = (
    "[Archivos adjuntos, ya disponibles en tu workspace:\n- imagen_pegada.png (702 KB) -> "
    "uploads/34997c04-e89d-4ed0-8863-c8fa983dfc60.png]"
)


class TestDeteccion:
    def test_caso_real_pedido_que_menciona_la_imagen_sin_adjunto(self):
        assert ap.menciona_adjunto_ausente(PEDIDO_ANA) == "la imagen"

    def test_con_la_imagen_en_el_mismo_mensaje_no_se_retiene(self):
        assert ap.menciona_adjunto_ausente(SOLO_IMAGEN + "\n" + PEDIDO_ANA) is None

    @pytest.mark.parametrize(
        "texto",
        [
            "hazme un informe interactivo de ventas con una imagen de portada",
            "mejora la imagen corporativa del informe interactivo",
            "usa la imagen que está en mi OneDrive para el informe interactivo",
            "toma la captura de la carpeta Proyectos y haz un informe interactivo",
            "hazme un informe interactivo y adjunta el resultado",
        ],
    )
    def test_negativos(self, texto):
        assert ap.menciona_adjunto_ausente(texto) is None

    def test_adjunto(self):
        assert ap.menciona_adjunto_ausente("resume el adjunto en un informe interactivo") == "el archivo adjunto"

    def test_solo_adjunto_y_union(self):
        assert ap.es_solo_adjunto(SOLO_IMAGEN)
        assert ap.es_solo_adjunto(SOLO_IMAGEN + "\naquí va")
        assert not ap.es_solo_adjunto(SOLO_IMAGEN + "\n" + PEDIDO_ANA)
        assert not ap.es_solo_adjunto("hola")
        unido = ap.unir_con_retenido(SOLO_IMAGEN, PEDIDO_ANA)
        assert unido.startswith("[Archivos adjuntos") and unido.endswith("Estructura Demo")


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _servicio():
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
    db.execute = AsyncMock(return_value=_Result([]))
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return service, db


def _trabajos(db):
    return [
        c.args[0]
        for c in db.add.call_args_list
        if getattr(c.args[0], "metadata_json", None) and c.args[0].metadata_json.get("background_job")
    ]


class TestFlujoDelChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parche = patch("hermeshq.services.ejecucion_aprobada.avisos_pendientes", AsyncMock(return_value=None))
        parche.start()
        self.addCleanup(parche.stop)

    async def test_caso_real_sin_imagen_no_lanza_el_trabajo_y_la_pide(self):
        service, db = _servicio()
        with patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")):
            task_id, reply = await service.send_message("s1", "tok", PEDIDO_ANA, db)
        self.assertIsNone(task_id)
        self.assertIn("necesito la imagen", reply)
        self.assertEqual(_trabajos(db), [])
        service.supervisor.submit_task.assert_not_called()

    async def test_caso_real_la_imagen_sola_se_une_al_pedido_y_parte_un_solo_trabajo(self):
        service, db = _servicio()
        historial = [
            SimpleNamespace(role="assistant", content=ap.aviso_falta_adjunto("la imagen")),
            SimpleNamespace(role="user", content=PEDIDO_ANA),
        ]
        with (
            patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")),
            patch("hermeshq.services.auto_background._load_recent_messages", AsyncMock(return_value=historial)),
            patch(
                "hermeshq.services.public_chat_service.con_archivos_generados",
                AsyncMock(side_effect=lambda *a: a[-1]),
            ),
        ):
            await service.send_message("s1", "tok", SOLO_IMAGEN, db)
        trabajos = _trabajos(db)
        self.assertEqual(len(trabajos), 1)
        self.assertIn("uploads/34997c04", trabajos[0].prompt)
        self.assertIn("Estructura Demo", trabajos[0].prompt)
        self.assertTrue(trabajos[0].metadata_json["background_title"].startswith("Hace un informe interactivo"))

    async def test_imagen_sola_sin_pedido_retenido_sigue_el_flujo_normal(self):
        service, db = _servicio()
        historial = [SimpleNamespace(role="assistant", content="Listo, ¿algo más?")]
        with (
            patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")),
            patch("hermeshq.services.auto_background._load_recent_messages", AsyncMock(return_value=historial)),
        ):
            await service.send_message("s1", "tok", SOLO_IMAGEN, db)
        self.assertEqual(_trabajos(db), [])
