import asyncio
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services.hermes_runtime import HermesRuntime, RuntimeExecutionError


class TestAvisoDeActividad(unittest.TestCase):
    def setUp(self):
        self.eventos = []
        parche = patch.object(runner, "_emit", lambda e: self.eventos.append(e))
        parche.start()
        self.addCleanup(parche.stop)
        runner._ULTIMA_ACTIVIDAD["t"] = 0.0

    def test_avisa_como_maximo_cada_intervalo(self):
        runner._avisar_actividad("modelo")
        runner._avisar_actividad("modelo")
        self.assertEqual(self.eventos, [{"event": "actividad", "origen": "modelo"}])
        runner._ULTIMA_ACTIVIDAD["t"] -= runner.INTERVALO_ACTIVIDAD_S
        runner._avisar_actividad("modelo")
        self.assertEqual(len(self.eventos), 2)

    def test_avisa_desde_el_hilo_de_un_subagente(self):
        hilo = threading.Thread(target=runner._avisar_actividad, args=("modelo",))
        hilo.start()
        hilo.join()
        self.assertEqual(len(self.eventos), 1)

    def test_cada_llamada_al_modelo_avisa_tambien_la_de_un_subagente(self):
        Completions = pytest.importorskip("openai.resources.chat.completions").Completions

        original = Completions.create
        self.addCleanup(setattr, Completions, "create", original)
        Completions.create = lambda self, *a, **k: SimpleNamespace(choices=[])
        runner._aplicar_overrides_resumen({}, False)
        Completions.create(object(), model="m", messages=[{"role": "user", "content": "subtarea"}])
        self.assertIn({"event": "actividad", "origen": "modelo"}, self.eventos)


async def _leer_hasta_el_final(codigo: str, limite: float) -> int:
    proceso = await asyncio.create_subprocess_exec(sys.executable, "-c", codigo, stdout=asyncio.subprocess.PIPE)
    lineas = 0
    try:
        while True:
            linea = await HermesRuntime._readline_or_stall(proceso.stdout, limite)
            if not linea:
                break
            lineas += 1
    finally:
        if proceso.returncode is None:
            proceso.kill()
        await proceso.wait()
    return lineas


class TestVigilanteDeInactividad(unittest.TestCase):
    def test_con_avisos_de_actividad_no_se_detiene(self):
        codigo = (
            "import json,sys,time\n"
            "for _ in range(5):\n"
            "    time.sleep(0.4); print(json.dumps({'event': 'actividad', 'origen': 'modelo'}), flush=True)\n"
        )
        self.assertEqual(asyncio.run(_leer_hasta_el_final(codigo, 1.0)), 5)

    def test_una_llamada_colgada_sin_avisos_se_sigue_deteniendo(self):
        with self.assertRaises(RuntimeExecutionError) as error:
            asyncio.run(_leer_hasta_el_final("import time\ntime.sleep(3)\n", 1.0))
        self.assertIn("produced no output", str(error.exception))


if __name__ == "__main__":
    unittest.main()
