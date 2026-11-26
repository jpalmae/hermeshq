import importlib.util
import os
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermeshq.services import runner_reserva as rr
from hermeshq.services.runner_heartbeat import HeartbeatChannel

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"


def _runner():
    spec = importlib.util.spec_from_file_location("hermes_task_runner_reserva_test", RUNNER)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class ClaveYEntorno(unittest.TestCase):
    def test_el_entorno_base_no_lleva_variables_del_intento(self):
        base = rr.entorno_base({"HOME": "/w", "HERMESHQ_ATTEMPT_ID": "a1", "HERMESHQ_EXECUTION_ID": "e1"})
        self.assertEqual(base, {"HOME": "/w"})

    def test_la_clave_separa_agentes_interpretes_y_entornos(self):
        base = rr.clave("agente-1", "/py", {"HOME": "/w"})
        self.assertNotEqual(base, rr.clave("agente-2", "/py", {"HOME": "/w"}))
        self.assertNotEqual(base, rr.clave("agente-1", "/otro", {"HOME": "/w"}))
        self.assertNotEqual(base, rr.clave("agente-1", "/py", {"HOME": "/w", "API_KEY": "rotada"}))
        self.assertEqual(base, rr.clave("agente-1", "/py", {"HOME": "/w"}))


class Caducidad(unittest.TestCase):
    def _entrada(self, vivo=True, edad=0.0):
        r, w = os.pipe()
        proceso = SimpleNamespace(returncode=None if vivo else 1, kill=lambda: None)
        return rr.RunnerEnReserva(proceso, r, w, time.monotonic() - edad)

    def test_una_reserva_viva_y_reciente_se_entrega_una_sola_vez(self):
        reserva = rr.ReservaDeRunners(ttl_segundos=60)
        entrada = self._entrada()
        reserva._reservas["k"] = entrada
        self.assertIs(reserva.tomar("k"), entrada)
        self.assertIsNone(reserva.tomar("k"))
        entrada.descartar()

    def test_una_reserva_caducada_o_muerta_no_se_usa(self):
        reserva = rr.ReservaDeRunners(ttl_segundos=5)
        reserva._reservas["vieja"] = self._entrada(edad=10)
        reserva._reservas["muerta"] = self._entrada(vivo=False)
        self.assertIsNone(reserva.tomar("vieja"))
        self.assertIsNone(reserva.tomar("muerta"))

    def test_otra_clave_no_recibe_la_reserva(self):
        reserva = rr.ReservaDeRunners(ttl_segundos=60)
        entrada = self._entrada()
        reserva._reservas["agente-1"] = entrada
        self.assertIsNone(reserva.tomar("agente-2"))
        entrada.descartar()


class AdopcionDelLatido(unittest.TestCase):
    def test_el_canal_usa_la_tuberia_de_la_reserva_y_cierra_la_propia(self):
        canal = HeartbeatChannel("e1", "a1", 1, max_age_seconds=60)
        propio = canal.open()
        r, w = os.pipe()
        self.assertEqual(canal.adoptar(r, w), w)
        self.assertEqual(canal.runner_config(10)["heartbeat_fd"], w)
        with self.assertRaises(OSError):
            os.fstat(propio)
        os.close(r)
        os.close(w)


class Precalentamiento(unittest.TestCase):
    def test_calienta_con_hogar_temporal_y_restaura_el_entorno(self):
        vistos = []

        class _Agente:
            def __init__(self, **kwargs):
                vistos.append(
                    (os.environ.get("HERMES_HOME"), kwargs.get("enabled_toolsets"), kwargs.get("skip_memory"))
                )

        falso = types.ModuleType("run_agent")
        falso.AIAgent = _Agente
        previo_modulo = sys.modules.get("run_agent")
        sys.modules["run_agent"] = falso
        previo_hogar = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = "/hogar/real"
        try:
            _runner()._precalentar()
            hogar_temporal, toolsets, sin_memoria = vistos[0]
            self.assertNotEqual(hogar_temporal, "/hogar/real")
            self.assertFalse(Path(hogar_temporal).exists())
            self.assertEqual((toolsets, sin_memoria), ([], True))
            self.assertEqual(os.environ.get("HERMES_HOME"), "/hogar/real")
        finally:
            if previo_modulo is None:
                sys.modules.pop("run_agent", None)
            else:
                sys.modules["run_agent"] = previo_modulo
            if previo_hogar is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previo_hogar


if __name__ == "__main__":
    unittest.main()
