import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.models.base import utcnow
from hermeshq.services.agent_supervisor import (
    HISTORY_FAILED_MARKER,
    HISTORY_MAX_TURNS,
    HISTORY_MAX_TURNS_CADENA,
    HISTORY_USER_MARKER,
    AgentSupervisor,
    aviso_de_corte_del_historial,
    history_assistant_text,
)


def _task(status="completed", error=None):
    return SimpleNamespace(status=status, error_message=error)


class TestHistoryAssistantText(unittest.TestCase):
    def test_failed_turn_never_carries_partial_text(self):
        text = history_assistant_text(
            _task("failed", "Hermes runtime exceeded the task timeout of 540s"), "perdí el hilo… medio texto", 4000
        )
        self.assertNotIn("perdí el hilo", text)
        self.assertIn("sin completar", text)
        self.assertIn("540s", text)

    def test_cancelled_turn_is_marked(self):
        self.assertIn("sin completar", history_assistant_text(_task("cancelled"), "", 4000))

    def test_long_answer_is_trimmed(self):
        text = history_assistant_text(_task(), "x" * 9000, 4000)
        self.assertLess(len(text), 4100)
        self.assertIn("recortada", text)

    def test_short_answer_is_untouched(self):
        self.assertEqual(history_assistant_text(_task(), "hola", 4000), "hola")

    def test_zero_limit_disables_trimming(self):
        self.assertEqual(len(history_assistant_text(_task(), "x" * 9000, 0)), 9000)


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)


class _Session:
    def __init__(self, items):
        self.items = items

    async def execute(self, _query):
        return _Result(self.items)


def _turn(idx, hours_ago, status="completed", response="ok"):
    return SimpleNamespace(
        id=f"t{idx}",
        prompt=f"pregunta {idx}",
        status=status,
        response=response,
        error_message="timeout" if status == "failed" else None,
        messages_json=[],
        queued_at=utcnow() - timedelta(hours=hours_ago),
        metadata_json={"conversation": True, "thread_id": "conv-1"},
    )


class TestBuildConversationHistory(unittest.IsolatedAsyncioTestCase):
    async def _history(self, items, max_age=3):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        current = SimpleNamespace(id="now", metadata_json={"conversation": True, "thread_id": "conv-1"}, agent_id="a")
        settings = SimpleNamespace(conversation_history_max_age_hours=max_age, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            return await supervisor._build_conversation_history(_Session(list(reversed(items))), current)

    async def test_old_turns_are_dropped(self):
        history = await self._history([_turn(1, 10), _turn(2, 5), _turn(3, 1)])
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], [HISTORY_USER_MARKER + "pregunta 3"])

    async def test_max_age_zero_keeps_everything(self):
        history = await self._history([_turn(1, 100), _turn(2, 1)], max_age=0)
        self.assertEqual(len([m for m in history if m["role"] == "user"]), 2)

    async def test_failed_turn_keeps_question_but_not_partial_answer(self):
        history = await self._history([_turn(1, 1, status="failed", response="texto a medias")])
        self.assertEqual(history[0], {"role": "user", "content": HISTORY_FAILED_MARKER + "pregunta 1"})
        self.assertIn("sin completar", history[1]["content"])
        self.assertNotIn("a medias", history[1]["content"])

    async def test_every_prior_user_message_is_marked_as_already_answered(self):
        history = await self._history([_turn(1, 1), _turn(2, 1)])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(len(users), 2)
        self.assertTrue(all(u.startswith("[Mensaje anterior, ya respondido") for u in users))

    async def test_older_failed_turns_are_dropped_once_the_user_moved_on(self):
        history = await self._history(
            [_turn(1, 2, status="failed"), _turn(2, 1, status="failed"), _turn(3, 1), _turn(4, 1)]
        )
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(users, [HISTORY_USER_MARKER + "pregunta 3", HISTORY_USER_MARKER + "pregunta 4"])
        self.assertFalse(any("sin completar" in m["content"] for m in history if m["role"] == "assistant"))

    async def test_the_last_failed_turn_stays_so_that_retrying_works(self):
        history = await self._history([_turn(1, 2), _turn(2, 1, status="failed")])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(users, [HISTORY_USER_MARKER + "pregunta 1", HISTORY_FAILED_MARKER + "pregunta 2"])

    async def test_only_the_last_completed_turns_are_kept(self):
        history = await self._history([_turn(i, 1) for i in range(1, 12)])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(users, [HISTORY_USER_MARKER + f"pregunta {i}" for i in range(12 - HISTORY_MAX_TURNS, 12)])

    async def test_cx3_el_dato_del_primer_turno_sigue_visible_tras_cuatro_turnos(self):
        history = await self._history([_turn(i, 1, response="Listo.") for i in range(1, 6)])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertIn(HISTORY_USER_MARKER + "pregunta 1", users)
        self.assertEqual(len(users), 5)

    async def test_a_chain_of_pending_questions_keeps_the_request_that_started_it(self):
        preguntas = [
            "Esquema del documento. ¿Procedo?",
            "Necesito los firmantes. ¿Me los indicas?",
            "¿Confirmas la distribución?",
        ]
        total = HISTORY_MAX_TURNS + 3
        turnos = [_turn(1, 1, response="Listo.")] + [
            _turn(i, 1, response=preguntas[i % 3]) for i in range(2, total + 1)
        ]
        history = await self._history(turnos)
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(users, [HISTORY_USER_MARKER + f"pregunta {i}" for i in range(2, total + 1)])

    async def test_without_pending_questions_the_window_stays_at_the_cap(self):
        total = HISTORY_MAX_TURNS + 3
        turnos = [_turn(i, 1, response="Listo.") for i in range(1, total + 1)]
        history = await self._history(turnos)
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(
            users, [HISTORY_USER_MARKER + f"pregunta {i}" for i in range(total - HISTORY_MAX_TURNS + 1, total + 1)]
        )

    async def test_a_chain_of_questions_never_grows_past_the_cap(self):
        turnos = [_turn(i, 1, response="¿Seguimos?") for i in range(1, 20)]
        history = await self._history(turnos)
        users = [m for m in history if m["role"] == "user"]
        self.assertEqual(len(users), HISTORY_MAX_TURNS + HISTORY_MAX_TURNS_CADENA)

    def test_the_candidate_window_is_wide_enough_for_busy_agents(self):
        from hermeshq.services.agent_supervisor import HISTORY_CANDIDATE_LIMIT

        self.assertGreaterEqual(HISTORY_CANDIDATE_LIMIT, 100)


def _pub_turn(idx, session_id, response, hours_ago=1, status="completed"):
    return SimpleNamespace(
        id=f"t{idx}",
        prompt=f"pregunta {idx}",
        status=status,
        response=response,
        error_message=None,
        messages_json=[],
        queued_at=utcnow() - timedelta(hours=hours_ago),
        metadata_json={
            "conversation": True,
            "source": "public_chat",
            "public_session_id": session_id,
            "thread_user_id": "__public_chat__",
        },
    )


class TestConversationKeyIsolation(unittest.IsolatedAsyncioTestCase):
    async def _history(self, items, current):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        settings = SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            return await supervisor._build_conversation_history(_Session(list(reversed(items))), current)

    async def test_only_turns_of_the_same_public_session_are_kept(self):
        t1 = _pub_turn(1, "S2", "ANSWER-S2", hours_ago=2)
        t2 = _pub_turn(2, "S1", "ANSWER-S1", hours_ago=1)
        t3 = SimpleNamespace(
            id="t3",
            agent_id="a",
            metadata_json={
                "conversation": True,
                "source": "public_chat",
                "public_session_id": "S1",
                "thread_user_id": "__public_chat__",
            },
        )
        t1.agent_id = t2.agent_id = "a"
        history = await self._history([t1, t2], t3)
        blob = "\n".join(m["content"] for m in history)
        self.assertIn("ANSWER-S1", blob)
        self.assertIn("pregunta 2", blob)
        self.assertNotIn("ANSWER-S2", blob)
        self.assertNotIn("pregunta 1", blob)


class TestPendingRequestAndReset(unittest.IsolatedAsyncioTestCase):
    async def _history(self, items):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        current = SimpleNamespace(id="now", metadata_json={"conversation": True, "thread_id": "conv-1"}, agent_id="a")
        settings = SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            return await supervisor._build_conversation_history(_Session(list(reversed(items))), current)

    async def test_the_real_request_survives_vague_follow_ups_that_also_failed(self):
        larga = _turn(2, 2, status="failed")
        larga.prompt = "Crea un reporte html con un gráfico de barras con los incidentes y accidentes de los últimos 5 años de Norte"
        history = await self._history([_turn(1, 2.5), larga, _turn(3, 1, status="failed")])
        users = [m["content"] for m in history if m["role"] == "user"]
        self.assertEqual(len(users), 3)
        self.assertIn("gráfico de barras", users[1])
        self.assertTrue(users[1].startswith(HISTORY_FAILED_MARKER))

    async def test_a_reset_drops_everything_before_it(self):
        reset = _turn(3, 1)
        reset.prompt = "/reset"
        reset.metadata_json = {"conversation": True, "thread_id": "conv-1", "reset": True}
        history = await self._history([_turn(1, 2), _turn(2, 2), reset])
        self.assertEqual(history, [])
        history = await self._history([_turn(1, 2), reset, _turn(4, 1)])
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], [HISTORY_USER_MARKER + "pregunta 4"])

    def test_reset_commands_are_recognised(self):
        from hermeshq.services.cancel_intent import is_reset_request

        for texto in ("/reset", " /Nuevo ", "reset", "/limpiar."):
            self.assertTrue(is_reset_request(texto), texto)
        for texto in ("resetea el informe de ventas", "hola", "", None):
            self.assertFalse(is_reset_request(texto), texto)

    def test_a_reset_prefix_keeps_the_rest_of_the_message(self):
        from hermeshq.services.cancel_intent import split_reset_prefix

        self.assertEqual(
            split_reset_prefix("/new  Tomando como base el informe V2, crea V3"),
            "Tomando como base el informe V2, crea V3",
        )
        self.assertEqual(split_reset_prefix("/nuevo: haz un resumen"), "haz un resumen")
        self.assertIsNone(split_reset_prefix("/reset"))
        self.assertIsNone(split_reset_prefix("hola /new cosa"))
        self.assertIsNone(split_reset_prefix("/newsletter semanal"))
        self.assertIsNone(split_reset_prefix(None))


class TestStatusRequests(unittest.TestCase):
    def test_short_status_questions_are_recognised(self):
        from hermeshq.services.cancel_intent import is_status_request

        for texto in (
            "¿Cómo va?",
            "que paso? por qué demoras tanto? necesito el entregable",
            "Y el informe?",
            "y la pptx?",
            "no obtuve respuesta",
            "estado de mi solicitud",
        ):
            self.assertTrue(is_status_request(texto), texto)
        for texto in (
            "cómo va el proyecto de licencias de power bi en todas las sucursales y qué pasó con los costos del año",
            "genera el informe de Norte",
            "estado",
            "hola",
            "",
            None,
        ):
            self.assertFalse(is_status_request(texto), texto)

    def test_the_reply_says_how_long_it_has_been_running_and_what_is_running(self):
        from hermeshq.services.public_chat_service import PublicChatService

        ahora = utcnow()
        corriendo = SimpleNamespace(
            status="running",
            started_at=ahora - timedelta(seconds=185),
            queued_at=ahora,
            prompt="genera el informe de Norte " * 10,
        )
        espera = SimpleNamespace(status="queued", started_at=None, queued_at=ahora, prompt="y el informe?")
        texto = PublicChatService._status_reply([corriendo, espera])
        self.assertIn("3 min", texto)
        self.assertIn("genera el informe de Norte", texto)
        self.assertIn("1 mensaje(s) más", texto)
        self.assertIn("cancela", texto)
        solo_espera = PublicChatService._status_reply([espera])
        self.assertIn("en cola", solo_espera)


class TestConversationWithoutKey(unittest.IsolatedAsyncioTestCase):
    async def test_a_keyless_conversation_never_sees_other_users_turns(self):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        otro_usuario = _turn(1, 1)
        otro_usuario.metadata_json = {"conversation": True, "thread_id": "conv-de-otro", "thread_user_id": "u-otro"}
        sin_clave = _turn(2, 1)
        sin_clave.metadata_json = {"conversation": True}
        sin_clave.created_by_user_id = "u-otro"
        current = SimpleNamespace(
            id="now", metadata_json={"conversation": True}, agent_id="a", created_by_user_id="u-yo"
        )
        settings = SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            history = await supervisor._build_conversation_history(_Session([sin_clave, otro_usuario]), current)
        self.assertEqual(history, [])

    async def test_keyless_prior_turns_never_leak_into_a_keyed_conversation(self):
        supervisor = AgentSupervisor.__new__(AgentSupervisor)
        sin_clave = _turn(1, 1)
        sin_clave.metadata_json = {"conversation": True}
        propio = _turn(2, 1)
        current = SimpleNamespace(id="now", metadata_json={"conversation": True, "thread_id": "conv-1"}, agent_id="a")
        settings = SimpleNamespace(conversation_history_max_age_hours=3, conversation_history_turn_max_chars=4000)
        with patch("hermeshq.services.agent_supervisor.get_settings", return_value=settings):
            history = await supervisor._build_conversation_history(_Session([propio, sin_clave]), current)
        self.assertEqual([m["content"] for m in history if m["role"] == "user"], [HISTORY_USER_MARKER + "pregunta 2"])


class TestAvisoDeCorteDelContexto(TestBuildConversationHistory):
    async def test_cuenta_los_turnos_que_quedaron_fuera_de_la_ventana(self):
        total = HISTORY_MAX_TURNS + 5
        history = await self._history([_turn(i, 1) for i in range(1, total + 1)])
        self.assertEqual(history.omitidos, 5)
        self.assertIn("5 turno(s)", aviso_de_corte_del_historial(history))

    async def test_cuenta_los_turnos_demasiado_antiguos(self):
        history = await self._history([_turn(1, 10), _turn(2, 5), _turn(3, 1)])
        self.assertEqual(history.por_antiguedad, 2)
        self.assertIn("hace demasiado tiempo", aviso_de_corte_del_historial(history))

    async def test_control_negativo_sin_corte_no_hay_aviso(self):
        history = await self._history([_turn(1, 1), _turn(2, 1)])
        self.assertEqual((history.omitidos, history.por_antiguedad), (0, 0))
        self.assertIsNone(aviso_de_corte_del_historial(history))

    async def test_el_aviso_dice_que_no_invente(self):
        history = await self._history([_turn(i, 1) for i in range(1, HISTORY_MAX_TURNS + 4)])
        texto = aviso_de_corte_del_historial(history)
        self.assertIn("pídele que lo repita", texto)
        self.assertIn("no lo inventes", texto)
