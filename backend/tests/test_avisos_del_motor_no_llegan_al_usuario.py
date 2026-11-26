import re

"""Avisos internos del motor en el chat del usuario. Caso real 12-11 12:24 (Ana, abcbbfb4, «busca la reunión
Prueba HermesHQ y dime quienes confirmaron asistencia»): en Teams aparecieron «⟳ compacting context…», «Session
compressed 8 times — accuracy may degrade. Consider /new to start fresh.» y «⚠️ API call failed (attempt 1/3):
BadRequestError [HTTP 400] 🔌 Provider: … Model: qwen3.8-27b 🌐 Endpoint: https://servicio-imagenes.proveedor.io/v1 …». El motor
los imprime con print por fd 1 y el runtime entregaba al usuario toda línea que no fuera JSON."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from hermeshq.services.hermes_runtime import HermesRuntime

BACKEND = Path(__file__).resolve().parents[1]
AVISOS = (
    "  ⟳ compacting context…",
    "⚠️  Session compressed 8 times — accuracy may degrade. Consider /new to start fresh.",
    "⚠️  API call failed (attempt 1/3): BadRequestError [HTTP 400]",
    "   🌐 Endpoint: https://servicio-imagenes.proveedor.io/v1",
)


class TestRunnerDesviaLaSalidaDelMotor(unittest.TestCase):
    def test_los_print_del_motor_no_salen_por_el_canal_del_protocolo(self):
        codigo = (
            "import hermeshq.scripts.hermes_task_runner as r, subprocess, sys\n"
            "r._abrir_salida()\n"
            "r._desviar_salida_del_motor()\n"
            f"for linea in {AVISOS!r}:\n"
            "    print(linea, flush=True)\n"
            "subprocess.run([sys.executable, '-c', 'print(\"salida de un proceso hijo\")'])\n"
            "r._emit({'event': 'delta', 'data': 'hola'})\n"
            "import time; time.sleep(0.3)\n"
        )
        proceso = subprocess.run(
            [sys.executable, "-c", codigo], cwd=BACKEND, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        lineas = [linea for linea in proceso.stdout.splitlines() if linea.strip()]
        self.assertEqual([json.loads(linea) for linea in lineas], [{"event": "delta", "data": "hola"}])
        for aviso in ("compacting context", "servicio-imagenes.proveedor.io", "salida de un proceso hijo"):
            self.assertNotIn(aviso, proceso.stdout)
            self.assertIn(aviso, proceso.stderr, "queda en el diagnóstico")

    def test_el_diagnostico_tiene_tope(self):
        codigo = (
            "import hermeshq.scripts.hermes_task_runner as r\n"
            "r._abrir_salida()\n"
            "r._desviar_salida_del_motor()\n"
            "for i in range(2000):\n"
            "    print('x' * 200, flush=True)\n"
            "import time; time.sleep(0.5)\n"
        )
        proceso = subprocess.run(
            [sys.executable, "-c", codigo], cwd=BACKEND, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proceso.returncode, 0)
        self.assertLess(len(proceso.stderr), 12000)
        self.assertEqual(proceso.stdout.strip(), "")


class TestRuntimeNoEntregaLineasFueraDelProtocolo(unittest.TestCase):
    def test_se_registra_y_no_se_entrega(self):
        traza = {"no_json": 0, "ultima_no_json": ""}
        HermesRuntime._registrar_no_json(traza, AVISOS[3], "t1")
        self.assertEqual(traza["no_json"], 1)
        self.assertIn("servicio-imagenes", traza["ultima_no_json"])

    def test_el_bucle_del_runtime_no_entrega_el_texto(self):
        fuente = (BACKEND / "hermeshq" / "services" / "hermes_runtime.py").read_text(encoding="utf-8")
        bloque = re.split(
            r"\n\s*(?:continue|return)\n", fuente.split("except json.JSONDecodeError:", 1)[1], maxsplit=1
        )[0]
        self.assertIn("_registrar_no_json", bloque)
        self.assertNotIn("_entregar", bloque)
