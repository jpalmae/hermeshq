import importlib.util
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.routers import internal_agents
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.public_chat_service import PublicChatService
from hermeshq.services.schedule_notifications import enqueue_schedule_notification

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
)


def _plugin():
    spec = importlib.util.spec_from_file_location("hermeshq_schedule_plugin_background", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin()

    def _llamar(self, metadata, args=None):
        payload = {"task_id": "t1", "metadata": metadata}
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(self.plugin, "_api_request", return_value=json.dumps({"success": True})) as api,
        ):
            resultado = json.loads(
                self.plugin.hq_trabajo_largo_tool(
                    args or {"titulo": "Análisis HSE", "instrucciones": "Cruza las hojas"}
                )
            )
        return resultado, api

    def test_it_sends_the_job_with_the_live_task(self):
        resultado, api = self._llamar({"thread_user_id": "u1"})
        self.assertTrue(resultado["success"])
        api.assert_called_once_with(
            "POST",
            "/agents/self/background-jobs",
            {"task_id": "t1", "title": "Análisis HSE", "instructions": "Cruza las hojas"},
        )

    def test_it_is_refused_inside_scheduled_or_background_runs(self):
        for metadata in ({"scheduled": True}, {"background_job": True}):
            resultado, api = self._llamar(metadata)
            self.assertFalse(resultado["success"])
            api.assert_not_called()

    def test_title_and_instructions_are_required(self):
        resultado, api = self._llamar({}, {"titulo": "", "instrucciones": "x"})
        self.assertFalse(resultado["success"])
        api.assert_not_called()


def _origin(**metadata):
    base = {"source": "public_chat", "thread_user_id": "u1"}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", status="running", metadata_json=base)


def _db(origin, user, activos):
    db = MagicMock()
    db.get = AsyncMock(side_effect=lambda model, key: origin if model.__name__ == "Task" else user)
    bloqueo = MagicMock()
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = activos
    db.execute = AsyncMock(side_effect=[bloqueo, resultado])
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=lambda job: setattr(job, "id", "job1"))
    return db


def _request():
    supervisor = MagicMock()
    supervisor.submit_task = AsyncMock()
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor=supervisor))), supervisor


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def _payload(self):
        return internal_agents.InternalBackgroundJob(
            task_id="t1", title="Análisis HSE", instructions="Cruza las hojas y entrega el resumen."
        )

    async def test_it_creates_the_job_and_submits_it(self):
        user = SimpleNamespace(id="u1", is_active=True)
        db = _db(_origin(), user, [])
        request, supervisor = _request()
        with patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=True)):
            resultado = await internal_agents.start_background_job(
                self._payload(), request, agent=SimpleNamespace(id="a1"), db=db
            )
        self.assertTrue(resultado["success"])
        self.assertEqual(resultado["job_id"], "job1")
        job = next(c.args[0] for c in db.add.call_args_list if c.args[0].__class__.__name__ == "Task")
        self.assertTrue(job.metadata_json["background_job"])
        self.assertEqual(job.metadata_json["thread_user_id"], "u1")
        self.assertEqual(job.metadata_json["background_title"], "Análisis HSE")
        self.assertIn("TRABAJO EN SEGUNDO PLANO", job.prompt)
        self.assertNotIn("conversation", job.metadata_json)
        supervisor.submit_task.assert_awaited_once_with("job1")

    async def test_the_job_carries_the_users_own_words_and_respects_a_no_upload(self):
        pedido = "Crea un dashboard con estos datos. Adjúntalo aquí, no lo subas a OneDrive."
        db = _db(_origin(mensaje_usuario=pedido), SimpleNamespace(id="u1", is_active=True), [])
        request, _ = _request()
        payload = internal_agents.InternalBackgroundJob(
            task_id="t1", title="Dashboard", instructions="Crea el HTML y guárdalo en OneDrive del usuario."
        )
        with patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=True)):
            await internal_agents.start_background_job(payload, request, agent=SimpleNamespace(id="a1"), db=db)
        job = next(c.args[0] for c in db.add.call_args_list if c.args[0].__class__.__name__ == "Task")
        self.assertIn(f"«{pedido}»", job.prompt)
        self.assertIn("manda este pedido", job.prompt)
        self.assertIn("salvo que el usuario haya pedido no subirlo", job.prompt)
        self.assertEqual(job.metadata_json["pedido_del_usuario"], pedido)

    async def test_only_one_background_job_per_user_at_a_time(self):
        user = SimpleNamespace(id="u1", is_active=True)
        activo = SimpleNamespace(
            metadata_json={"background_job": True, "thread_user_id": "u1", "background_title": "otro"}
        )
        db = _db(_origin(), user, [activo])
        request, _ = _request()
        with (
            patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=True)),
            self.assertRaises(HTTPException) as contexto,
        ):
            await internal_agents.start_background_job(self._payload(), request, agent=SimpleNamespace(id="a1"), db=db)
        self.assertEqual(contexto.exception.status_code, 409)

    async def test_it_is_refused_from_scheduled_background_or_non_chat_origins(self):
        user = SimpleNamespace(id="u1", is_active=True)
        for origen in (_origin(scheduled=True), _origin(background_job=True), _origin(source="web")):
            db = _db(origen, user, [])
            request, _ = _request()
            with (
                patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=True)),
                self.assertRaises(HTTPException) as contexto,
            ):
                await internal_agents.start_background_job(
                    self._payload(), request, agent=SimpleNamespace(id="a1"), db=db
                )
            self.assertEqual(contexto.exception.status_code, 403)

    async def test_it_needs_an_identified_active_user(self):
        db = _db(_origin(thread_user_id=""), None, [])
        request, _ = _request()
        with self.assertRaises(HTTPException) as contexto:
            await internal_agents.start_background_job(self._payload(), request, agent=SimpleNamespace(id="a1"), db=db)
        self.assertEqual(contexto.exception.status_code, 403)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def _sesion(self, user, keys):
        session = MagicMock()
        session.get = AsyncMock(return_value=user)
        resultado = MagicMock()
        resultado.scalars.return_value.all.return_value = keys
        session.execute = AsyncMock(return_value=resultado)
        session.add = MagicMock()
        return session

    def _tarea(self, **extra):
        metadata = {"background_job": True, "thread_user_id": "u1", "background_title": "Análisis HSE"}
        metadata.update(extra)
        return SimpleNamespace(id="job1", agent_id="a1", title="x", metadata_json=metadata)

    def _llave(self):
        return SimpleNamespace(
            id="k1",
            trust_sender_id=False,
            bound_user_id="u1",
            last_notification_poll_at=datetime.now(UTC) - timedelta(seconds=30),
        )

    async def test_the_result_is_delivered_to_the_chat(self):
        sesion = self._sesion(SimpleNamespace(id="u1", is_active=True, teams_id="29:abc"), [self._llave()])
        cantidad = await enqueue_schedule_notification(sesion, self._tarea(), success=True, summary="846 casos únicos")
        self.assertEqual(cantidad, 1)
        notificacion = sesion.add.call_args.args[0]
        self.assertIn("Análisis HSE", notificacion.title)
        self.assertIn("846 casos únicos", notificacion.body)
        self.assertTrue(notificacion.body.startswith("✅"))
        self.assertIsNone(notificacion.schedule_id)

    async def test_a_result_with_only_attachments_still_says_something(self):
        sesion = self._sesion(SimpleNamespace(id="u1", is_active=True, teams_id="29:abc"), [self._llave()])
        await enqueue_schedule_notification(
            sesion, self._tarea(), success=True, summary="MEDIA: uploads/30801855-b795-4d3e-826b-dd7e723ad7af.html"
        )
        cuerpo = sesion.add.call_args.args[0].body
        self.assertNotIn("MEDIA:", cuerpo)
        self.assertTrue(cuerpo.rstrip().endswith("Te dejo el archivo adjunto."), cuerpo)

    async def test_a_result_with_text_and_attachments_is_not_padded(self):
        sesion = self._sesion(SimpleNamespace(id="u1", is_active=True, teams_id="29:abc"), [self._llave()])
        await enqueue_schedule_notification(
            sesion, self._tarea(), success=True, summary="Informe listo.\n\nMEDIA: uploads/a.html\nMEDIA: uploads/a.pdf"
        )
        cuerpo = sesion.add.call_args.args[0].body
        self.assertIn("Informe listo.", cuerpo)
        self.assertNotIn("Te dejo", cuerpo)

    async def test_a_failure_is_reported_too(self):
        sesion = self._sesion(SimpleNamespace(id="u1", is_active=True, teams_id=None), [self._llave()])
        await enqueue_schedule_notification(
            sesion, self._tarea(), success=False, summary="Hermes runtime exceeded the task timeout of 1800s"
        )
        cuerpo = sesion.add.call_args.args[0].body
        self.assertTrue(cuerpo.startswith("❌"))
        self.assertIn("tardó demasiado", cuerpo)
        self.assertNotIn("exceeded the task timeout", cuerpo)

    async def test_without_an_identified_user_nothing_is_queued(self):
        sesion = self._sesion(SimpleNamespace(id="u1", is_active=True, teams_id=None), [self._llave()])
        self.assertEqual(
            await enqueue_schedule_notification(sesion, self._tarea(thread_user_id=""), success=True, summary="x"), 0
        )


class StatusAndRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_status_reply_for_a_background_job(self):
        ahora = datetime.now(UTC)
        trabajo = SimpleNamespace(
            status="running",
            started_at=ahora - timedelta(seconds=300),
            queued_at=ahora,
            prompt="instrucciones largas",
            metadata_json={"background_job": True, "background_title": "Análisis HSE"},
        )
        texto = PublicChatService._status_reply([trabajo])
        self.assertIn("Análisis HSE", texto)
        self.assertIn("5 min", texto)
        self.assertIn("Te aviso por aquí", texto)

    def test_background_tasks_are_matched_to_the_chat_by_user(self):
        tarea = SimpleNamespace(metadata_json={"background_job": True, "thread_user_id": "u1"})
        self.assertTrue(PublicChatService._task_belongs_to_chat(tarea, "s1", "u1"))
        self.assertFalse(PublicChatService._task_belongs_to_chat(tarea, "s1", "u2"))

    def test_background_jobs_get_their_own_longer_timeout(self):
        tarea = SimpleNamespace(metadata_json={"background_job": True, "source": "background_job"})
        ajustes = SimpleNamespace(
            task_timeout_seconds=900, public_chat_task_timeout_seconds=870, background_job_timeout_seconds=1800
        )
        with patch("hermeshq.config.get_settings", return_value=ajustes):
            self.assertEqual(HermesRuntime._task_timeout_for(tarea), 1800)
            chat = SimpleNamespace(metadata_json={"source": "public_chat"})
            self.assertEqual(HermesRuntime._task_timeout_for(chat), 870)

    def test_background_jobs_can_run_up_to_an_hour_and_no_longer(self):
        tarea = SimpleNamespace(metadata_json={"background_job": True, "source": "background_job"})
        for configurado, esperado in ((3600, 3600), (7200, 3600), (30, 60)):
            ajustes = SimpleNamespace(
                task_timeout_seconds=900,
                public_chat_task_timeout_seconds=870,
                background_job_timeout_seconds=configurado,
            )
            with patch("hermeshq.config.get_settings", return_value=ajustes):
                self.assertEqual(HermesRuntime._task_timeout_for(tarea), esperado, configurado)

    def test_the_default_limit_for_background_jobs_is_one_hour(self):
        from hermeshq.config import Settings

        self.assertEqual(Settings.model_fields["background_job_timeout_seconds"].default, 3600)
