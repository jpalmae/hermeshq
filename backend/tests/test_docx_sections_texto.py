import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    spec = importlib.util.spec_from_file_location("hq_docx_sections", RAIZ / "hermeshq_docx" / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _generar(plugin, sections):
    return json.loads(plugin._handle_generate_docx({"title": "T", "sections": sections}))


class TestSectionsComoTexto:
    def test_texto_json_invalido_se_rechaza_con_el_motivo_y_sin_crear_archivo(self, plugin, tmp_path):
        r = _generar(plugin, '[{"type": "paragraph", "text": "dice "hola" mal"}]')
        assert r["success"] is False and "no es JSON válido" in r["error"]
        assert not list((tmp_path / "output").glob("*.docx")) if (tmp_path / "output").exists() else True

    def test_lista_con_un_solo_texto_invalido_tambien(self, plugin):
        r = _generar(plugin, ['[{"type": "paragraph", "text": "x" "y"}]'])
        assert r["success"] is False and "no es JSON válido" in r["error"]

    def test_texto_json_valido_se_interpreta_y_dibuja(self, plugin):
        docx = pytest.importorskip("docx")
        r = _generar(plugin, '[{"type": "heading1", "text": "Resumen"}, {"type": "paragraph", "text": "Hola"}]')
        assert r["success"] is True and "bloques_omitidos" not in r
        assert "Hola" in [p.text for p in docx.Document(r["path"]).paragraphs]

    def test_si_ningun_bloque_se_dibuja_no_se_entrega_un_documento_vacio(self, plugin):
        r = _generar(plugin, [1, "texto suelto", {"type": "inventado"}])
        assert r["success"] is False and "ningún bloque" in r["error"]

    def test_con_algun_bloque_valido_sigue_avisando_de_los_omitidos(self, plugin):
        r = _generar(plugin, [{"type": "paragraph", "text": "ok"}, 5])
        assert r["success"] is True and r["bloques_omitidos"] == ["bloque 2: no es un objeto"]
