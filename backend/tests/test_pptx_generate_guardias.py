import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


def _cargar(slug, nombre):
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / slug / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture
def pptx(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    return _cargar("hermeshq_pptx", "hq_pptx_generate_guardias")


def _pedir(monkeypatch, mensaje):
    monkeypatch.setenv("HERMESHQ_TASK_PAYLOAD", json.dumps({"metadata": {"mensaje_usuario": mensaje}}))


def _lamina(n):
    return {"title": f"Tema {n}", "bullets": [f"punto {n}"]}


def _llamar(plugin, slides):
    return json.loads(plugin._handle_generate_pptx({"title": "Clima", "slides": slides}))


PEDIDO = (
    "Crea una presentación sencilla de exactamente 5 láminas sobre el clima de Valparaíso, sin plantilla corporativa."
)


class TestElementosQueNoSonObjeto:
    def test_se_rechazan_y_dicen_cuales(self, pptx):
        r = _llamar(pptx, [_lamina(1), "basura", _lamina(2), 7])
        assert r["success"] is False
        assert "2, 4" in r["error"]

    def test_no_deja_archivo_a_medias(self, pptx, tmp_path):
        _llamar(pptx, [_lamina(1), "basura"])
        assert not list((tmp_path / "output").glob("*.pptx")) if (tmp_path / "output").exists() else True

    def test_lista_valida_sigue_funcionando(self, pptx):
        r = _llamar(pptx, [_lamina(1), _lamina(2)])
        assert r["success"] is True and r["slide_count"] == 3


class TestLaminasPedidas:
    def test_caso_real_pidio_5_y_trae_1_de_contenido(self, pptx, monkeypatch):
        _pedir(monkeypatch, PEDIDO)
        r = _llamar(pptx, [_lamina(1)])
        assert r["success"] is False
        assert "5 diapositivas" in r["error"] and "1 de contenido" in r["error"]

    def test_5_de_contenido_pasa(self, pptx, monkeypatch):
        _pedir(monkeypatch, PEDIDO)
        r = _llamar(pptx, [_lamina(n) for n in range(1, 6)])
        assert r["success"] is True and r["slide_count"] == 6

    def test_5_en_total_con_la_portada_tambien_pasa(self, pptx, monkeypatch):
        _pedir(monkeypatch, PEDIDO)
        r = _llamar(pptx, [_lamina(n) for n in range(1, 5)])
        assert r["success"] is True and r["slide_count"] == 5

    def test_de_mas_se_rechaza(self, pptx, monkeypatch):
        _pedir(monkeypatch, PEDIDO)
        r = _llamar(pptx, [_lamina(n) for n in range(1, 9)])
        assert r["success"] is False

    def test_maximo_permite_menos(self, pptx, monkeypatch):
        _pedir(monkeypatch, "Hazme una presentación de máximo 6 láminas sobre el clima")
        assert _llamar(pptx, [_lamina(1), _lamina(2)])["success"] is True

    def test_sin_numero_pedido_no_se_exige_nada(self, pptx, monkeypatch):
        _pedir(monkeypatch, "Hazme una presentación sobre el clima")
        assert _llamar(pptx, [_lamina(1)])["success"] is True

    def test_el_resultado_trae_el_conteo_para_el_mensaje(self, pptx, monkeypatch):
        r = _llamar(pptx, [_lamina(1), _lamina(2)])
        assert "3 diapositivas" in r["resumen"] and r["slide_count"] == 3
