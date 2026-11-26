import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos"
_spec = importlib.util.spec_from_file_location("hermeshq_documentos_adjuntos", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def _csv(ruta: Path, filas: int = 2) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("Zona;Monto\n" + "".join(f"Z{i};{10 + i}\n" for i in range(filas)), encoding="utf-8")
    return ruta


def _payload(mensaje: str = "", metadata: dict | None = None) -> str:
    return json.dumps({"task_id": "t", "metadata": metadata or {"source": "public_chat"}, "mensaje": mensaje})


@pytest.fixture
def espacio(tmp_path, monkeypatch):
    raiz = tmp_path / "espacio"
    raiz.mkdir()
    monkeypatch.setenv("HERMES_SESSION_CWD", str(raiz))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    return raiz


def _extraer(**args):
    return json.loads(plugin._extraer_datos(args))


def _informe_build(pptx: Path, revision: dict | None):
    informe = {"ok": True, "pptx": str(pptx), "advertencias": []}
    if revision is not None:
        informe["revision"] = revision
    return informe


@pytest.fixture
def construir(espacio, tmp_path, monkeypatch):
    script = tmp_path / "build_deck.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(plugin, "_script", lambda par: script)
    leidos = []

    def correr(spec, informe):
        def ejecutar(_script, argumentos, **_kw):
            leidos.append(json.loads(Path(argumentos[0]).read_text(encoding="utf-8")))
            return SimpleNamespace(stdout=json.dumps(informe), stderr="")

        with patch.object(plugin, "_ejecutar", ejecutar):
            return json.loads(
                plugin._crear(
                    plugin.PRESENTACION, plugin.TIPOS_PRESENTACION, {"tipo": "proyecto", "spec": spec}, "pptx"
                )
            )

    return SimpleNamespace(correr=correr, leidos=leidos, espacio=espacio)


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
