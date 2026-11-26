import pytest

"""Caso real cc6a28b4 (Ana, chat público): 62 vision_analyze sobre UNA imagen pegada, ~9 s cada una, hasta el
timeout de 600 s, sin traza del cortacircuito. vision_analyze exige image_url y question: el modelo repetía la
misma imagen con preguntas distintas, la firma exacta cambiaba y el cortacircuito (argumentos idénticos) no se
disparaba. Se reproduce con el esquema real de la herramienta, no con argumentos inventados."""

import json
import unittest
from types import SimpleNamespace

import hermeshq.scripts.hermes_task_runner as runner

Completions = pytest.importorskip("openai.resources.chat.completions").Completions


def _vision(imagen, pregunta):
    argumentos = json.dumps({"image_url": imagen, "question": pregunta})
    return {
        "role": "assistant",
        "tool_calls": [{"type": "function", "function": {"name": "vision_analyze", "arguments": argumentos}}],
    }


def _tool():
    return {"role": "tool", "content": "La imagen muestra una tabla con tres columnas."}


def _historial(llamadas):
    mensajes = [{"role": "user", "content": "quiero hacer otro cambio"}]
    for llamada in llamadas:
        mensajes += [llamada, _tool()]
    return mensajes


IMAGEN = "uploads/361ee709.png"


class MismoRecursoConArgumentosDistintos(unittest.TestCase):
    def test_misma_imagen_con_preguntas_distintas_cuenta_como_repeticion(self):
        mensajes = _historial([_vision(IMAGEN, f"¿Qué dice la fila {n}?") for n in range(4)])
        self.assertEqual(runner._repeticiones_identicas(mensajes), 4)

    def test_imagenes_distintas_no_son_repeticion(self):
        mensajes = _historial([_vision(f"uploads/pagina-{n}.png", "Describe la página") for n in range(6)])
        self.assertEqual(runner._repeticiones_identicas(mensajes), 1)

    def test_otras_herramientas_siguen_exigiendo_argumentos_identicos(self):
        llamadas = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "informe.pdf", "offset": n * 100}),
                        },
                    }
                ],
            }
            for n in range(6)
        ]
        self.assertEqual(runner._repeticiones_identicas(_historial(llamadas)), 1)

    def test_argumentos_ilegibles_no_rompen_la_firma(self):
        llamada = {
            "role": "assistant",
            "tool_calls": [{"type": "function", "function": {"name": "vision_analyze", "arguments": "{no-json"}}],
        }
        self.assertEqual(runner._repeticiones_identicas(_historial([llamada] * 4)), 4)


class CierreForzadoConElEsquemaReal(unittest.TestCase):
    def setUp(self):
        self._original = Completions.create
        self.peticiones = []

        def modelo(obj, *args, **kwargs):
            self.peticiones.append(kwargs)
            mensaje = SimpleNamespace(role="assistant", content="Ya tengo lo necesario.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=mensaje)])

        Completions.create = modelo
        runner._aplicar_overrides_resumen({}, False)

    def tearDown(self):
        Completions.create = self._original

    def test_a_la_cuarta_pregunta_sobre_la_misma_imagen_se_quitan_las_herramientas(self):
        mensajes = _historial([_vision(IMAGEN, f"pregunta {n}") for n in range(4)])
        Completions.create(None, tools=[{"type": "function"}], messages=mensajes)
        self.assertNotIn("tools", self.peticiones[-1])
        self.assertIn("NO vuelvas a llamar", self.peticiones[-1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
