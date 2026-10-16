import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.public_chat_service import PublicChatService

AHORA = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


def _t(
    minutos_atras,
    estado="failed",
    segundos=870,
    error="Hermes runtime exceeded the task timeout of 870s",
    iteraciones=0,
    **metadata,
):
    inicio = AHORA - timedelta(minutes=minutos_atras)
    return SimpleNamespace(
        status=estado,
        error_message=error if estado == "failed" else None,
        started_at=inicio,
        completed_at=inicio + timedelta(seconds=segundos),
        queued_at=inicio,
        iterations=iteraciones,
        metadata_json={"conversation": True, "source": "public_chat", **metadata},
    )


class TestRacha(unittest.TestCase):
    def _racha(self, tareas):
        return PublicChatService.calcular_racha(tareas, AHORA)

    def test_three_timeouts_in_a_row_trigger_the_reset(self):
        racha = self._racha([_t(10), _t(30), _t(50), _t(70, "completed", 200, iteraciones=9)])
        self.assertEqual((racha["consecutivos"], racha["ventana"], racha["minutos"]), (3, 3, 44))
        self.assertTrue(PublicChatService.debe_reiniciar(racha))

    def test_two_are_not_enough_and_real_progress_cuts_the_streak(self):
        self.assertFalse(PublicChatService.debe_reiniciar(self._racha([_t(10), _t(30)])))
        racha = self._racha([_t(10), _t(20, "completed", 300, iteraciones=12), _t(40), _t(60)])
        self.assertEqual((racha["consecutivos"], racha["ventana"]), (1, 3))
        self.assertFalse(PublicChatService.debe_reiniciar(racha))

    def test_four_timeouts_between_steps_that_worked_trigger_a_reset(self):
        tareas = [
            _t(5),
            _t(20, "completed", 25, iteraciones=2),
            _t(40),
            _t(60, "completed", 197, iteraciones=10),
            _t(70),
            _t(90, "completed", 60, iteraciones=3),
            _t(100),
            _t(120),
        ]
        racha = self._racha(tareas)
        self.assertEqual(racha["ventana"], 5)
        self.assertTrue(PublicChatService.debe_reiniciar(racha))

    def test_cheap_replies_and_other_errors_do_not_count_or_cut(self):
        tareas = [
            _t(5),
            _t(10, "completed", 20, iteraciones=2),
            _t(20, error="Task waited 240s in the queue"),
            _t(30),
            _t(50),
        ]
        racha = self._racha(tareas)
        self.assertEqual((racha["consecutivos"], racha["ventana"]), (3, 3))

    def test_a_reset_or_old_failures_end_the_count(self):
        reinicio = _t(25, "completed", 0, reset=True)
        self.assertEqual(self._racha([_t(5), _t(15), reinicio, _t(40), _t(50), _t(60)])["ventana"], 2)
        self.assertEqual(self._racha([_t(5), _t(15), _t(5 * 60), _t(6 * 60)])["ventana"], 2)

    def test_the_other_failure_types_that_mean_it_is_stuck_count_too(self):
        tareas = [
            _t(5, error="Hermes runtime produced no output for 600s and was stopped"),
            _t(10, error="Hermes runtime returned no result payload"),
            _t(15, error="Hermes runtime stopped early: x"),
        ]
        self.assertEqual(self._racha(tareas)["ventana"], 3)


class TestAvisoParaElAgente(unittest.TestCase):
    def test_the_notice_tells_the_agent_to_say_it_and_check_the_catalog(self):
        nota = HermesRuntime._nota_reinicio_automatico(
            {"auto_reset_aviso": {"fallos": 4, "minutos": 58}}, Path("/no/existe")
        )
        for esperado in (
            "4 solicitudes",
            "58 min",
            "contexto limpio",
            "catalogo-de-capacidades",
            "NO retomes",
        ):
            self.assertIn(esperado, nota)
        self.assertEqual(HermesRuntime._nota_reinicio_automatico({}, Path("/no/existe")), "")
        self.assertEqual(HermesRuntime._nota_reinicio_automatico(None, Path("/no/existe")), "")

    def test_the_notice_is_appended_to_the_prompt_only_when_flagged(self):
        con = HermesRuntime._prompt_with_notices(
            "sigue", {"auto_reset_aviso": {"fallos": 3, "minutos": 40}}, Path("/no/existe")
        )
        self.assertTrue(con.startswith("sigue\n\n[AVISO DEL SISTEMA"))
        self.assertEqual(HermesRuntime._prompt_with_notices("sigue", {}, Path("/no/existe")), "sigue")

    def test_every_chat_message_carries_the_real_chile_time_so_the_agent_never_guesses_it(self):
        con_hora = HermesRuntime._prompt_with_notices("¿qué hora es?", {"conversation": True}, Path("/no/existe"))
        self.assertTrue(con_hora.startswith("¿qué hora es?\n\n[Hora actual:"))
        self.assertIn("America/Santiago", con_hora)
        self.assertIn("UTC-0", con_hora)
        programada = HermesRuntime._prompt_with_notices(
            "x", {"conversation": True, "scheduled": True}, Path("/no/existe")
        )
        self.assertEqual(programada, "x")


class TestMarcador(unittest.TestCase):
    def test_the_automatic_marker_is_a_reset_flagged_as_automatic(self):
        sesion = SimpleNamespace(agent_id="a1")
        manual = PublicChatService._reset_marker(sesion, "s1", "u1")
        automatico = PublicChatService._reset_marker(sesion, "s1", "u1", automatico=True)
        self.assertTrue(manual.metadata_json["reset"])
        self.assertNotIn("auto_reset", manual.metadata_json)
        self.assertTrue(automatico.metadata_json["reset"] and automatico.metadata_json["auto_reset"])
        self.assertIn("automático", automatico.title)


class TestEnvioDeMensaje(unittest.IsolatedAsyncioTestCase):
    async def test_a_stuck_chat_gets_a_marker_and_the_notice_in_the_new_task(self):
        servicio = PublicChatService.__new__(PublicChatService)
        servicio.rate_limiter = SimpleNamespace(check=lambda _: True)
        servicio.ip_rate_limiter = SimpleNamespace(check=lambda _: True)
        sesion = SimpleNamespace(id="s1", agent_id="a1", api_key_id="k1", last_activity=None, sender_id="x")
        servicio.validate_session = AsyncMock(return_value=sesion)
        servicio._check_monthly_quota = AsyncMock()
        servicio._resolve_identity = AsyncMock(return_value=("u1", None))
        servicio.supervisor = SimpleNamespace(submit_task=AsyncMock())
        fallidas = [
            _t(5, public_session_id="s1", thread_user_id="u1"),
            _t(15, public_session_id="s1", thread_user_id="u1"),
            _t(25, public_session_id="s1", thread_user_id="u1"),
        ]
        for tarea in fallidas:
            tarea.queued_at = datetime.now(UTC) - (AHORA - tarea.queued_at)
        resultado = MagicMock()
        resultado.scalars.return_value.all.return_value = fallidas
        db = MagicMock()
        agente = SimpleNamespace(status="running")
        db.get = AsyncMock(
            side_effect=lambda modelo, _id: (
                agente
                if modelo.__name__ == "Agent"
                else SimpleNamespace(bound_user_id=None, requests_per_month=0, tokens_per_month=0, id="k1")
            )
        )
        db.execute = AsyncMock(return_value=resultado)
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock(side_effect=lambda t: setattr(t, "id", "nueva"))
        await servicio.send_message("s1", "tok", "sigue desde la ultima parte", db)
        agregados = [c.args[0] for c in db.add.call_args_list]
        marcadores = [
            a for a in agregados if a.__class__.__name__ == "Task" and (a.metadata_json or {}).get("auto_reset")
        ]
        nuevas = [a for a in agregados if a.__class__.__name__ == "Task" and not (a.metadata_json or {}).get("reset")]
        self.assertEqual(len(marcadores), 1)
        self.assertEqual(nuevas[0].metadata_json["auto_reset_aviso"]["fallos"], 3)
        self.assertEqual(nuevas[0].prompt, "sigue desde la ultima parte")
