import unittest
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

Completions = pytest.importorskip("openai.resources.chat.completions").Completions


def _asst(name, args):
    return {"role": "assistant", "tool_calls": [{"type": "function", "function": {"name": name, "arguments": args}}]}


def _tool():
    return {"role": "tool", "content": "ok"}


def _bucle(n):
    mensajes = [{"role": "user", "content": "haz el documento"}]
    for _ in range(n):
        mensajes.append(_asst("vision_analyze", '{"image":"a.png"}'))
        mensajes.append(_tool())
    return mensajes


def _respuesta_con_herramienta():
    llamada = SimpleNamespace(
        id="call_1", type="function", function=SimpleNamespace(name="vision_analyze", arguments='{"image":"a.png"}')
    )
    mensaje = SimpleNamespace(role="assistant", content=None, tool_calls=[llamada])
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=mensaje)])


class CierreForzadoEsEfectivo(unittest.TestCase):
    def setUp(self):
        self._original = Completions.create
        self.llamadas_al_modelo = 0

        def modelo_insistente(obj, *args, **kwargs):
            self.llamadas_al_modelo += 1
            return _respuesta_con_herramienta()

        Completions.create = modelo_insistente
        runner._aplicar_overrides_resumen({}, False)

    def tearDown(self):
        Completions.create = self._original

    def test_la_respuesta_forzada_no_trae_llamadas_a_herramientas(self):
        respuesta = Completions.create(None, tools=[{"type": "function"}], messages=_bucle(4))
        mensaje = respuesta.choices[0].message
        self.assertFalse(getattr(mensaje, "tool_calls", None))
        self.assertNotEqual(respuesta.choices[0].finish_reason, "tool_calls")
        self.assertTrue((mensaje.content or "").strip(), "el cierre debe traer texto útil, no vacío")

    def test_una_respuesta_normal_sin_bucle_conserva_sus_llamadas(self):
        respuesta = Completions.create(None, tools=[{"type": "function"}], messages=_bucle(2))
        self.assertTrue(respuesta.choices[0].message.tool_calls)

    def test_sin_aviso_de_bucle_no_se_modifica_la_respuesta(self):
        respuesta = Completions.create(None, tools=[{"type": "function"}], messages=_bucle(3))
        self.assertEqual(respuesta.choices[0].finish_reason, "tool_calls")


if __name__ == "__main__":
    unittest.main()
