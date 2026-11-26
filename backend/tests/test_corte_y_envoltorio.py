import unittest

from hermeshq.services.respuesta_corta import pedido_original
from hermeshq.services.schedule_notifications import TEXTO_CORTE_DE_STREAM, _resumen_de_background


class TestPedidoOriginal(unittest.TestCase):
    def test_quita_el_envoltorio_de_respuesta_corta(self):
        envuelto = (
            "[Respuesta corta a tu último mensaje: «Tienes razón (no hay respaldo)». "
            "Interpreta «hazlo» como respuesta a eso, no a temas anteriores.]\n"
            "hazlo en segundo plano"
        )
        self.assertEqual(pedido_original(envuelto), "hazlo en segundo plano")

    def test_un_pedido_sin_envoltorio_no_cambia(self):
        self.assertEqual(pedido_original("Corrige el rol Lubricador"), "Corrige el rol Lubricador")

    def test_texto_vacio(self):
        self.assertEqual(pedido_original(""), "")
        self.assertEqual(pedido_original(None), "")


class TestCorteDeStreamEnBackground(unittest.TestCase):
    def test_un_corte_se_marca_como_fallo_con_texto_en_espanol(self):
        resumen = "Now I have all the data I need.\n\n⚠ Stream stalled mid tool-call (hq_documento_modificar); the action was not executed."
        ok, texto = _resumen_de_background(True, resumen)
        self.assertFalse(ok)
        self.assertEqual(texto, TEXTO_CORTE_DE_STREAM)
        self.assertNotIn("Now I have", texto)

    def test_un_resumen_normal_no_cambia(self):
        self.assertEqual(_resumen_de_background(True, "Listo, informe generado."), (True, "Listo, informe generado."))

    def test_sin_resumen(self):
        self.assertEqual(_resumen_de_background(False, None), (False, None))


class TestRachaNoCuentaLoQuePasoASegundoPlano(unittest.TestCase):
    def test_un_timeout_que_ya_paso_a_segundo_plano_no_cuenta(self):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from hermeshq.services.auto_background import AUTO_MARKER
        from hermeshq.services.public_chat_service import PublicChatService

        ahora = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)

        def tarea(minutos, **metadata):
            inicio = ahora - timedelta(minutes=minutos)
            return SimpleNamespace(
                status="failed",
                error_message="Hermes runtime exceeded the task timeout of 300s",
                started_at=inicio,
                completed_at=inicio + timedelta(seconds=300),
                queued_at=inicio,
                iterations=0,
                metadata_json={"conversation": True, **metadata},
            )

        tareas = [
            tarea(10, **{AUTO_MARKER: "job-1"}),
            tarea(30, **{AUTO_MARKER: "job-2"}),
            tarea(50, **{AUTO_MARKER: "job-3"}),
        ]
        racha = PublicChatService.calcular_racha(tareas, ahora)
        self.assertEqual(racha["ventana"], 0)
        self.assertFalse(PublicChatService.debe_reiniciar(racha))

    def test_un_timeout_sin_pase_a_segundo_plano_sigue_contando(self):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from hermeshq.services.public_chat_service import PublicChatService

        ahora = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
        inicio = ahora - timedelta(minutes=10)
        tarea = SimpleNamespace(
            status="failed",
            error_message="Hermes runtime exceeded the task timeout of 300s",
            started_at=inicio,
            completed_at=inicio + timedelta(seconds=300),
            queued_at=inicio,
            iterations=0,
            metadata_json={"conversation": True},
        )
        self.assertEqual(PublicChatService.calcular_racha([tarea], ahora)["ventana"], 1)


class TestSinTextoEntreHerramientas(unittest.TestCase):
    def test_el_prompt_de_segundo_plano_lo_pide(self):
        from hermeshq.services.background_style import SIN_TEXTO_ENTRE_HERRAMIENTAS

        self.assertIn("No escribas texto entre herramientas", SIN_TEXTO_ENTRE_HERRAMIENTAS)
        self.assertIn("en español", SIN_TEXTO_ENTRE_HERRAMIENTAS)


class TestContinuacionDeSegundoPlano(unittest.TestCase):
    def test_un_trabajo_que_agota_el_tiempo_se_continua_hasta_dos_veces(self):
        from types import SimpleNamespace

        from hermeshq.services.auto_background import is_background_continuable

        error = "Hermes runtime exceeded the task timeout of 3600s"
        base = {"background_job": True, "thread_user_id": "u1"}
        tarea = SimpleNamespace(prompt="informe largo", metadata_json=dict(base))
        self.assertTrue(is_background_continuable(tarea, error))
        segunda = SimpleNamespace(prompt="informe largo", metadata_json={**base, "continuaciones": 1})
        self.assertTrue(is_background_continuable(segunda, error))
        tercera = SimpleNamespace(prompt="informe largo", metadata_json={**base, "continuaciones": 2})
        self.assertFalse(is_background_continuable(tercera, error))

    def test_un_error_que_no_es_de_tiempo_no_se_continua(self):
        from types import SimpleNamespace

        from hermeshq.services.auto_background import is_background_continuable

        tarea = SimpleNamespace(prompt="informe", metadata_json={"background_job": True, "thread_user_id": "u1"})
        self.assertFalse(is_background_continuable(tarea, "Connection dropped"))

    def test_el_prompt_de_continuacion_retoma_sin_repetir_el_envoltorio(self):
        from hermeshq.services.auto_background import prompt_continuacion_background

        original = (
            "[Respuesta corta a tu último mensaje: «x». Interpreta «y» como respuesta a eso.]\n"
            "Genera el informe por rol"
        )
        texto = prompt_continuacion_background(original, 2, 60)
        self.assertIn("parte 2", texto)
        self.assertIn("No escribas texto entre herramientas", texto)
        self.assertTrue(texto.endswith("Genera el informe por rol"))
        self.assertNotIn("Respuesta corta", texto)
