import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_lote" / "__init__.py"
_spec = importlib.util.spec_from_file_location("lote_html_alias", PLUGIN)
lote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lote)


def test_acepta_los_nombres_habituales_de_la_ruta():
    for clave in ("path", "archivo", "file_path", "file", "ruta", "html"):
        assert lote._ruta_pedida({clave: " outputs/x.html "}) == "outputs/x.html"


def test_path_tiene_prioridad_y_lo_vacio_se_ignora():
    assert lote._ruta_pedida({"path": "a.html", "archivo": "b.html"}) == "a.html"
    assert lote._ruta_pedida({"path": "  ", "archivo": "b.html"}) == "b.html"


def test_sin_ruta_devuelve_vacio():
    assert lote._ruta_pedida({}) == ""
    assert lote._ruta_pedida({"pages": [1]}) == ""
