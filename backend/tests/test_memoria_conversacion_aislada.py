import importlib.util
import unittest
from pathlib import Path

from hermeshq.services.hermes_runtime import memoria_persistente_para

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("hermes_task_runner_memoria_test", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


class MemoriaPersistenteEnConversaciones(unittest.TestCase):
    def test_una_tarea_conversacional_no_carga_la_memoria_compartida(self):
        self.assertFalse(memoria_persistente_para({"conversation": True, "public_session_id": "s1"}))

    def test_una_tarea_sin_conversacion_conserva_la_memoria_actual(self):
        self.assertTrue(memoria_persistente_para({}))
        self.assertTrue(memoria_persistente_para({"scheduled": True}))
        self.assertTrue(memoria_persistente_para(None))

    def test_un_trabajo_en_segundo_plano_de_un_usuario_no_carga_la_memoria_compartida(self):
        for metadata in (
            {"background_job": True, "source": "background_job", "thread_user_id": "u1", "origin_task_id": "t0"},
            {"background_job": True, "source": "background_job", "thread_user_id": "u1", "auto_continuation": True},
            {"background_job": True, "source": "background_job", "thread_user_id": "u1", "pedido_largo": True},
            {"background_job": True},
            {"source": "background_job"},
        ):
            with self.subTest(metadata=metadata):
                self.assertFalse(memoria_persistente_para(metadata))

    def test_otras_tareas_a_nombre_de_un_usuario_no_cargan_la_memoria_compartida(self):
        self.assertFalse(memoria_persistente_para({"delegated": True, "thread_user_id": "u1"}))
        self.assertFalse(memoria_persistente_para({"source": "kapso_whatsapp", "thread_user_id": "u1"}))

    def test_el_cron_del_dueno_conserva_la_memoria(self):
        from types import SimpleNamespace

        from hermeshq.services.cron_schedule import scheduled_task_metadata

        cron = SimpleNamespace(id="s1", definition_version=1, owner_user_id="u1", created_by_agent_id=None)
        self.assertTrue(memoria_persistente_para(scheduled_task_metadata(cron)))
        self.assertTrue(memoria_persistente_para(scheduled_task_metadata(cron, manual=True)))
        sin_dueno = SimpleNamespace(id="s2", definition_version=1, owner_user_id=None, created_by_agent_id="a1")
        self.assertTrue(memoria_persistente_para(scheduled_task_metadata(sin_dueno)))

    def test_un_trabajo_en_segundo_plano_marcado_como_programado_tampoco_carga_la_memoria(self):
        self.assertFalse(memoria_persistente_para({"background_job": True, "scheduled": True, "thread_user_id": "u1"}))

    def test_el_runner_respeta_la_marca_del_payload(self):
        self.assertTrue(runner._usa_memoria_persistente({}))
        self.assertTrue(runner._usa_memoria_persistente({"memoria_persistente": True}))
        self.assertFalse(runner._usa_memoria_persistente({"memoria_persistente": False}))


if __name__ == "__main__":
    unittest.main()
