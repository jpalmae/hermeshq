import unittest
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

Completions = pytest.importorskip("openai.resources.chat.completions").Completions


def _asst(name, args):
    return {"role": "assistant", "tool_calls": [{"type": "function", "function": {"name": name, "arguments": args}}]}


def _tool():
    return {"role": "tool", "content": "ok"}


def _secuencia(n, name="execute_code", args='{"code":"print(1)"}'):
    mensajes = [{"role": "user", "content": "haz el documento"}]
    for _ in range(n):
        mensajes.append(_asst(name, args))
        mensajes.append(_tool())
    return mensajes


class TestConteoRepeticiones(unittest.TestCase):
    def test_counts_identical_trailing_tool_calls_skipping_results(self):
        self.assertEqual(runner._repeticiones_identicas(_secuencia(4)), 4)
        self.assertEqual(runner._repeticiones_identicas(_secuencia(1)), 1)

    def test_a_different_call_breaks_the_streak(self):
        mensajes = _secuencia(2) + [_asst("sharepoint_read_file", '{"x":1}'), _tool()]
        self.assertEqual(runner._repeticiones_identicas(mensajes), 1)

    def test_different_arguments_are_not_the_same_call(self):
        mensajes = [_asst("execute_code", '{"code":"a"}'), _tool(), _asst("execute_code", '{"code":"b"}'), _tool()]
        self.assertEqual(runner._repeticiones_identicas(mensajes), 1)

    def test_a_real_user_message_in_between_resets(self):
        mensajes = _secuencia(3) + [{"role": "user", "content": "otra cosa"}]
        self.assertEqual(runner._repeticiones_identicas(mensajes), 0)

    def test_it_reads_object_style_messages_too(self):
        tc = SimpleNamespace(function=SimpleNamespace(name="execute_code", arguments='{"code":"x"}'))
        asst = SimpleNamespace(role="assistant", tool_calls=[tc])
        tool = SimpleNamespace(role="tool", content="ok")
        self.assertEqual(runner._repeticiones_identicas([asst, tool, asst, tool, asst, tool, asst, tool]), 4)


class TestCreatePatch(unittest.TestCase):
    def setUp(self):
        self._original = Completions.create

        def espia(self, *args, **kwargs):
            TestCreatePatch._ultimo = kwargs
            return "ok"

        Completions.create = espia
        runner._aplicar_overrides_resumen({}, False)

    def tearDown(self):
        Completions.create = self._original

    def test_four_identical_calls_force_a_tool_less_wrap_up(self):
        Completions.create(None, tools=[{"type": "function"}], messages=_secuencia(4))
        enviado = TestCreatePatch._ultimo
        self.assertNotIn("tools", enviado)
        self.assertNotIn("tool_choice", enviado)
        self.assertEqual(enviado["messages"][-1]["role"], "user")
        self.assertIn("vuelvas a llamar", enviado["messages"][-1]["content"])

    def test_three_identical_calls_do_not_trigger(self):
        Completions.create(None, tools=[{"type": "function"}], messages=_secuencia(3))
        enviado = TestCreatePatch._ultimo
        self.assertIn("tools", enviado)
        self.assertEqual(len(enviado["messages"]), len(_secuencia(3)))

    def test_threshold_is_four(self):
        self.assertEqual(runner.REPETICIONES_MAXIMAS, 4)


if __name__ == "__main__":
    unittest.main()
