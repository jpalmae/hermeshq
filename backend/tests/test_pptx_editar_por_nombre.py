import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx" / "__init__.py"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_pptx_por_nombre_prueba", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _deck(ruta: Path, titulo="Gobernanza"):
    from pptx import Presentation

    ruta.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    diapo = prs.slides.add_slide(prs.slide_layouts[5])
    diapo.shapes.title.text = titulo
    prs.save(ruta)


def _editar(plugin, nombre):
    cambios = [{"titulo": {"lamina": 1, "nuevo": "Nuevo título"}}]
    return json.loads(plugin._handle_edit_pptx({"path": nombre, "cambios": cambios}))


def test_un_nombre_sin_carpeta_se_resuelve_en_outputs(plugin, tmp_path):
    _deck(tmp_path / "outputs" / "gobernanza-datos_3.pptx")
    r = _editar(plugin, "gobernanza-datos_3.pptx")
    assert r["success"] is True, r
    assert r["path"].startswith("outputs/") and r["original_intacto"] is True


def test_dos_candidatos_se_listan_y_no_se_elige(plugin, tmp_path):
    _deck(tmp_path / "outputs" / "x.pptx")
    _deck(tmp_path / "uploads" / "x.pptx")
    r = _editar(plugin, "x.pptx")
    assert r["success"] is False and "varios archivos" in r["error"] and "outputs/x.pptx" in r["error"]


def test_un_nombre_que_no_existe_sigue_fallando(plugin):
    assert _editar(plugin, "no-existe.pptx")["success"] is False


def test_una_ruta_con_carpeta_no_se_reinterpreta(plugin, tmp_path):
    _deck(tmp_path / "outputs" / "y.pptx")
    r = _editar(plugin, "otra/y.pptx")
    assert r["success"] is False


def test_los_archivos_de_hermes_no_cuentan(plugin, tmp_path):
    _deck(tmp_path / "work" / ".hermes" / "skills" / "z.pptx")
    assert _editar(plugin, "z.pptx")["success"] is False


def test_operaciones_como_alias_y_como_texto_json_se_aceptan(plugin, tmp_path):
    _deck(tmp_path / "outputs" / "z.pptx")
    texto = json.dumps([{"titulo": {"lamina": 1, "nuevo": "Otro"}}])
    for args in ({"path": "z.pptx", "operaciones": texto}, {"path": "z.pptx", "cambios": texto}):
        r = json.loads(plugin._handle_edit_pptx(args))
        assert r["success"] is True, r


def test_sin_cambios_el_error_nombra_el_parametro(plugin, tmp_path):
    _deck(tmp_path / "outputs" / "w.pptx")
    r = json.loads(plugin._handle_edit_pptx({"path": "w.pptx"}))
    assert r["success"] is False and "«cambios»" in r["error"]
