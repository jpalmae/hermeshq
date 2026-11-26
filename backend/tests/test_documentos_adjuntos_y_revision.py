import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos"
_spec = importlib.util.spec_from_file_location("hermeshq_documentos_adjuntos", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def _payload(mensaje: str = "", metadata: dict | None = None) -> str:
    return json.dumps({"task_id": "t", "metadata": metadata or {"source": "public_chat"}, "mensaje": mensaje})


@pytest.fixture
def espacio(tmp_path, monkeypatch):
    raiz = tmp_path / "espacio"
    raiz.mkdir()
    monkeypatch.setenv("HERMES_SESSION_CWD", str(raiz))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    return raiz


class TestSkillsSinUsuarioPresente:
    @pytest.fixture
    def entorno(self, espacio, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        fab = MagicMock()
        fab.skills_de_usuario.return_value = ["mi-skill"]
        monkeypatch.setattr(plugin, "_fabrica", lambda: fab)
        return fab

    def test_los_scripts_que_la_skill_pide_correr_siguen_permitidos(self, espacio, monkeypatch):
        monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", _payload("", {"source": "public_chat"}))
        monkeypatch.delenv("HERMESHQ_GUARDA", raising=False)
        imagen = "python .hermes/skills/hermeshq-managed/mi-skill/scripts/generar.py 'ciudad'"
        assert plugin._guarda_pre_tool_call(tool_name="terminal", args={"command": imagen}) is None
        assert (
            plugin._guarda_pre_tool_call(tool_name="execute_code", args={"code": "import openpyxl\nprint(1)"}) is None
        )
