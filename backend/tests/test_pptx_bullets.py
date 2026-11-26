import importlib.util
import json
from pathlib import Path

import pytest

RUTA_PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx" / "__init__.py"


def _cargar_plugin():
    spec = importlib.util.spec_from_file_location("hq_pptx_plugin_test", RUTA_PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def generar(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    plugin = _cargar_plugin()

    def _generar(slides, nombre="prueba"):
        resultado = json.loads(plugin._handle_generate_pptx({"title": "T", "slides": slides, "filename": nombre}))
        return resultado

    return _generar


def _textos_cuerpo(resultado):
    from pptx import Presentation

    presentacion = Presentation(resultado["path"])
    cuerpos = []
    for diapositiva in list(presentacion.slides)[1:]:
        for marcador in diapositiva.placeholders:
            if marcador.placeholder_format.idx == 1 and marcador.has_text_frame:
                cuerpos.append(marcador.text_frame.text)
    return cuerpos


class TestVinetasPptx:
    def test_vinetas_en_texto_no_se_separan_por_caracter(self, generar):
        resultado = generar([{"title": "B", "bullets": "uno dos"}])
        assert resultado["success"] is True
        assert _textos_cuerpo(resultado) == ["uno dos"]

    def test_lista_de_vinetas_se_conserva(self, generar):
        resultado = generar([{"title": "A", "bullets": ["uno", "dos"]}])
        assert _textos_cuerpo(resultado) == ["uno\ndos"]

    def test_entradas_invalidas_se_rechazan_en_vez_de_descartarse(self, generar):
        resultado = generar([{"title": "C", "bullets": ["x"]}, "basura", {"title": "D", "bullets": ["y"]}])
        assert resultado["success"] is False
        assert "2" in resultado["error"]

    def test_slide_count_cuenta_la_portada_y_las_laminas(self, generar):
        resultado = generar([{"title": "C", "bullets": ["x"]}, {"title": "D", "bullets": ["y"]}])
        assert resultado["success"] is True
        assert resultado["slide_count"] == 3

    def test_diapositiva_sin_vinetas_no_deja_cuadro_vacio(self, generar):
        resultado = generar([{"title": "C"}])
        assert resultado["success"] is True
        assert _textos_cuerpo(resultado) == []
