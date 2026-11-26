import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hermeshq.services.public_chat_service import (
    PublicChatMessage,
    PublicChatService,
    SimpleRateLimiter,
    _prompt_con_referencia,
)

AHORA = datetime.now(UTC)


def _tarea(orden, status="completed", response="Respuesta anterior", **metadata):
    base = {"conversation": True, "source": "public_chat", "public_session_id": "s1", "thread_user_id": "u1"}
    base.update(metadata)
    return SimpleNamespace(
        id=f"t{orden}",
        status=status,
        prompt=f"pedido {orden}",
        response=response,
        queued_at=AHORA - timedelta(minutes=orden),
        metadata_json=base,
    )


def _reset(orden, session_id="s1"):
    return _tarea(
        orden,
        response="Conversación reiniciada",
        reset=True,
        public_session_id=session_id,
    )


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _db(items):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(items))
    return db


class TestReferenciaRespuestaCorta(unittest.IsolatedAsyncioTestCase):
    async def test_no_cruza_el_reset_hacia_la_respuesta_de_antes(self):
        tareas = [_reset(1), _tarea(2, response="Vieja respuesta de antes del reset")]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "sí", "s1")
        self.assertNotIn("Vieja respuesta", texto)
        self.assertEqual(texto, "sí")

    async def test_usa_la_respuesta_posterior_al_reset(self):
        tareas = [_tarea(1, response="Respuesta nueva"), _reset(2), _tarea(3, response="Vieja")]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "sí", "s1")
        self.assertIn("Respuesta nueva", texto)
        self.assertNotIn("Vieja", texto)

    async def test_no_cruza_a_otra_sesion_del_chat(self):
        tareas = [_tarea(1, response="De otra sesión", public_session_id="s-vieja")]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "sí", "s1")
        self.assertEqual(texto, "sí")

    async def test_mensaje_largo_no_se_modifica(self):
        texto = "Necesito un análisis completo de la hoja de reprocesos del equipo"
        self.assertEqual(await _prompt_con_referencia(_db([_tarea(1)]), "a1", "u1", texto, "s1"), texto)


class TestReferenciaSinFalsosSeguimientos(unittest.IsolatedAsyncioTestCase):
    async def test_un_pedido_con_verbo_no_se_toma_como_respuesta(self):
        tareas = [_tarea(1, response="Listo, el análisis está en OneDrive")]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "genera una presentación", "s1")
        self.assertEqual(texto, "genera una presentación")

    async def test_envialo_si_sigue_apuntando_a_lo_anterior(self):
        tareas = [_tarea(1, response="Aquí va el resumen del correo")]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "envíalo", "s1")
        self.assertIn("Aquí va el resumen del correo", texto)

    async def test_el_aviso_de_informe_largo_no_se_cita(self):
        tareas = [
            _tarea(1, response="Este informe es largo: lo preparo en segundo plano y te aviso por aquí"),
            _tarea(2, response="Resultado de los reprocesos de mayo"),
        ]
        texto = await _prompt_con_referencia(_db(tareas), "a1", "u1", "sí", "s1")
        self.assertIn("Resultado de los reprocesos de mayo", texto)
        self.assertNotIn("Este informe es largo", texto)


class TestResetConTexto(unittest.IsolatedAsyncioTestCase):
    def _service(self, tareas):
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
        db.execute = AsyncMock(return_value=_Result(tareas))
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        return service, db

    async def test_reset_con_texto_guarda_el_reset_en_el_historial(self):
        service, db = self._service([])
        await service.send_message("s1", "tok", "/reset quiero otro informe", db)
        mensajes = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], PublicChatMessage)]
        self.assertIn("/reset", [m.content for m in mensajes if m.role == "user"])


class TestResetCancelaTareas(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parche = patch("hermeshq.services.ejecucion_aprobada.avisos_pendientes", AsyncMock(return_value=None))
        parche.start()
        self.addCleanup(parche.stop)

    def _service(self, tareas, activas=None):
        service = PublicChatService.__new__(PublicChatService)
        service.rate_limiter = SimpleRateLimiter(max_requests=100)
        service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
        service.supervisor = SimpleNamespace(
            submit_task=AsyncMock(), active_tasks=activas or {}, cancel_task=AsyncMock()
        )
        session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None, sender_id=None)
        service.validate_session = AsyncMock(return_value=session)
        service._check_monthly_quota = AsyncMock()
        service._resolve_identity = AsyncMock(return_value=("u1", None))
        service._racha_del_chat = AsyncMock(return_value={})
        service.debe_reiniciar = MagicMock(return_value=False)
        db = MagicMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(
                id="k1", bound_user_id=None, requests_per_month=0, tokens_per_month=0, status="running"
            )
        )
        db.execute = AsyncMock(return_value=_Result(tareas))
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        return service, db

    async def test_reset_solo_cancela_la_tarea_en_curso_y_lo_dice(self):
        en_curso = _tarea(1, status="running", response=None, public_session_id="s1")
        en_curso.id = "t-run"
        service, db = self._service([en_curso], activas={"t-run": object()})
        reply = await service.handle_cancel_request("s1", "tok", "/reset", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t-run")
        self.assertIn("Cancelé 1 tarea", reply)

    async def test_reset_con_texto_cancela_y_procesa_el_texto_nuevo(self):
        en_curso = _tarea(1, status="running", response=None, public_session_id="s1")
        en_curso.id = "t-run"
        service, db = self._service([en_curso], activas={"t-run": object()})
        service._running_interactive_task = AsyncMock(return_value=en_curso)
        self.assertIsNone(await service.handle_cancel_request("s1", "tok", "/reset necesito el informe de ventas", db))
        await service.send_message("s1", "tok", "/reset necesito el informe de ventas", db)
        service.supervisor.cancel_task.assert_awaited_once_with("t-run")
        service.supervisor.submit_task.assert_awaited_once()

    async def test_reset_con_texto_no_se_queda_esperando_a_la_tarea_cancelada(self):
        en_curso = _tarea(1, status="running", response=None, public_session_id="s1")
        en_curso.id = "t-run"
        service, db = self._service([en_curso], activas={"t-run": object()})
        service._running_interactive_task = AsyncMock(return_value=en_curso)
        self.assertIsNone(await service.handle_cancel_request("s1", "tok", "/reset otra cosa", db))
        await service.send_message("s1", "tok", "/reset otra cosa", db)
        service.supervisor.submit_task.assert_awaited_once()


class TestPedidoDuplicado(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parche = patch("hermeshq.services.ejecucion_aprobada.avisos_pendientes", AsyncMock(return_value=None))
        parche.start()
        self.addCleanup(parche.stop)

    async def test_no_encola_dos_veces_el_mismo_trabajo_en_segundo_plano(self):
        pedido = "necesito el análisis de reprocesos del archivo Datos.xlsx"
        activa = SimpleNamespace(
            id="bg1",
            status="running",
            title=f"Trabajo en segundo plano: {pedido}"[:120],
            prompt="x",
            response=None,
            queued_at=AHORA,
            metadata_json={"background_job": True, "thread_user_id": "u1"},
        )
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
        db.execute = AsyncMock(return_value=_Result([activa]))
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        with patch("hermeshq.services.auto_background._recent_history", AsyncMock(return_value="")):
            task_id, reply = await service.send_message("s1", "tok", pedido, db)
        self.assertIsNone(task_id)
        self.assertIn("Ya estoy trabajando", reply)
        service.supervisor.submit_task.assert_not_called()


class TestBusquedaPreviaEnSharePoint(unittest.IsolatedAsyncioTestCase):
    def _service(self, vault=object()):
        service = PublicChatService.__new__(PublicChatService)
        service.secret_vault = vault
        return service

    async def test_un_archivo_nombrado_sin_adjunto_se_busca_antes(self):
        from unittest.mock import patch

        hits = [{"name": "Datos.xlsx", "url": "https://sp/x"}]
        with patch(
            "hermeshq.services.public_chat_service.buscar_en_sharepoint", AsyncMock(return_value=hits)
        ) as buscar:
            texto = await self._service()._con_busqueda_previa(
                MagicMock(), "u1", "analiza Datos.xlsx", "analiza Datos.xlsx"
            )
        buscar.assert_awaited_once()
        self.assertIn("Datos.xlsx (https://sp/x)", texto)

    async def test_con_adjunto_no_busca(self):
        from unittest.mock import patch

        with patch("hermeshq.services.public_chat_service.buscar_en_sharepoint", AsyncMock(return_value=[])) as buscar:
            texto = await self._service()._con_busqueda_previa(
                MagicMock(), "u1", "[Archivos adjuntos, ya disponibles] Datos.xlsx", "x"
            )
        buscar.assert_not_called()
        self.assertEqual(texto, "x")

    async def test_sin_vault_no_cambia_nada(self):
        texto = await self._service(vault=None)._con_busqueda_previa(MagicMock(), "u1", "analiza Datos.xlsx", "p")
        self.assertEqual(texto, "p")

    async def test_si_la_busqueda_falla_el_chat_sigue(self):
        from unittest.mock import patch

        with patch(
            "hermeshq.services.public_chat_service.buscar_en_sharepoint", AsyncMock(side_effect=RuntimeError("x"))
        ):
            texto = await self._service()._con_busqueda_previa(MagicMock(), "u1", "analiza Datos.xlsx", "p")
        self.assertIn("no disponible", texto)


class TestPedidoLargoSinDuplicar(unittest.IsolatedAsyncioTestCase):
    async def test_el_pedido_en_segundo_plano_no_se_repite_en_el_historial(self):
        pedido = "Analiza el archivo Datos.xlsx y detecta los reprocesos del equipo, hazlo directo"
        service = PublicChatService.__new__(PublicChatService)
        service.rate_limiter = SimpleRateLimiter(max_requests=100)
        service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
        service.supervisor = SimpleNamespace(submit_task=AsyncMock(), active_tasks={}, cancel_task=AsyncMock())
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
        mensaje_actual = SimpleNamespace(role="user", content=pedido)
        with patch(
            "hermeshq.services.auto_background._load_recent_messages",
            AsyncMock(return_value=[mensaje_actual]),
        ):
            await service.handle_cancel_request("s1", "tok", pedido, db)
            await service.send_message("s1", "tok", pedido, db)
        trabajos = [
            c.args[0]
            for c in db.add.call_args_list
            if getattr(c.args[0], "title", "").startswith("Trabajo en segundo plano")
        ]
        self.assertEqual(len(trabajos), 1)
        self.assertNotIn("Conversación reciente", trabajos[0].prompt)
        self.assertEqual(trabajos[0].prompt.count(pedido), 1)


if __name__ == "__main__":
    unittest.main()
