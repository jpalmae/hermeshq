import importlib
import re
import unittest
from pathlib import Path

CONTROL_PATH = Path(__file__).resolve().parents[1] / "hermeshq" / "routers" / "internal_control.py"


class TestInternalControlReferences(unittest.TestCase):
    def test_every_router_function_called_by_control_plane_exists(self):
        source = CONTROL_PATH.read_text()
        module_by_alias = dict(
            (alias, module) for module, alias in re.findall(r"from hermeshq\.routers import (\w+) as (\w+)", source)
        )
        module_by_alias["agents_shared"] = "agents_shared"
        references = sorted(set(re.findall(r"\b(\w+_router|agents_shared)\.(\w+)\(", source)))
        self.assertGreater(len(references), 20)
        for alias, function_name in references:
            with self.subTest(reference=f"{alias}.{function_name}"):
                module = importlib.import_module(f"hermeshq.routers.{module_by_alias[alias]}")
                self.assertTrue(hasattr(module, function_name))

    def test_update_agent_signature_still_takes_background_tasks(self):
        import inspect

        from hermeshq.routers import agents_crud

        parameters = list(inspect.signature(agents_crud.update_agent).parameters)
        self.assertEqual(parameters[:4], ["agent_id", "payload", "request", "background_tasks"])
