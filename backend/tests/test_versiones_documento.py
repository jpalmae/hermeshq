import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_docx" / "__init__.py"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    pytest.importorskip("docx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_docx_versiones_prueba", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _docx(ruta: Path, parrafos, hace_s):
    from docx import Document

    ruta.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    for p in parrafos:
        d.add_paragraph(p)
    d.save(ruta)
    t = time.time() - hace_s
    os.utime(ruta, (t, t))


@pytest.fixture
def cadena(tmp_path):
    out = tmp_path / "output"
    _docx(out / "prueba-od-1.docx", ["Prueba OD-1", "Texto base"], 600)
    _docx(out / "prueba-od-1_v2.docx", ["Prueba OD-1", "Texto base", "Segundo párrafo"], 500)
    _docx(out / "prueba-od-1_editado.docx", ["Prueba OD-1 revisada", "Texto base"], 400)
    _docx(out / "prueba-od-1_editado-3.docx", ["Prueba OD-1", "Texto base", "Anexo A"], 300)
    _docx(
        out / "prueba-od-1_editado-3_editado.docx", ["Prueba OD-1", "Texto base", "Anexo A", "Contenido del anexo"], 200
    )
    _docx(out / "otro-documento.docx", ["Otra cosa"], 100)
    return tmp_path


def _llamar(plugin, **args):
    return json.loads(plugin._handle_versiones(args))


class TestListar:
    def test_ordena_por_fecha_y_solo_las_versiones_del_mismo_documento(self, plugin, cadena):
        r = _llamar(plugin, documento="prueba-od-1_editado-3.docx")
        assert r["success"] is True
        assert [v["archivo"].split("/")[-1] for v in r["versiones"]] == [
            "prueba-od-1.docx",
            "prueba-od-1_v2.docx",
            "prueba-od-1_editado.docx",
            "prueba-od-1_editado-3.docx",
            "prueba-od-1_editado-3_editado.docx",
        ]
        assert [v["version"] for v in r["versiones"]] == [1, 2, 3, 4, 5]

    def test_dice_que_agrego_y_que_quito_cada_version(self, plugin, cadena):
        v = _llamar(plugin, documento="prueba-od-1.docx")["versiones"]
        assert v[1]["agrega"] == ["Segundo párrafo"] and v[1]["quita"] == []
        assert (
            "Prueba OD-1 revisada" in v[2]["agrega"]
            and "Prueba OD-1" in v[2]["quita"]
            and "Segundo párrafo" in v[2]["quita"]
        )
        assert "Anexo A" in v[3]["agrega"]
        assert v[4]["agrega"] == ["Contenido del anexo"]

    def test_control_negativo_documento_sin_versiones(self, plugin, cadena):
        r = _llamar(plugin, documento="no-existe.docx")
        assert r["success"] is False and "No encontré" in r["error"]

    def test_falta_el_documento(self, plugin):
        assert _llamar(plugin)["success"] is False


class TestRestaurar:
    def test_restaura_la_ultima_version_sin_anexo_como_archivo_nuevo(self, plugin, cadena):
        antes = {p.name for p in (cadena / "output").iterdir()}
        r = _llamar(plugin, documento="prueba-od-1.docx", restaurar_version=3)
        assert r["success"] is True and r["path"].endswith("prueba-od-1_restaurada.docx")
        from docx import Document

        textos = [p.text for p in Document(str(cadena / r["path"])).paragraphs]
        assert textos == ["Prueba OD-1 revisada", "Texto base"]
        assert antes <= {p.name for p in (cadena / "output").iterdir()}

    def test_una_segunda_restauracion_no_pisa_la_primera(self, plugin, cadena):
        a = _llamar(plugin, documento="prueba-od-1.docx", restaurar_version=1)
        b = _llamar(plugin, documento="prueba-od-1.docx", restaurar_version=2)
        assert a["path"] != b["path"] and b["path"].endswith("_restaurada-2.docx")

    @pytest.mark.parametrize("numero", [0, 9])
    def test_version_inexistente(self, plugin, cadena, numero):
        r = _llamar(plugin, documento="prueba-od-1.docx", restaurar_version=numero)
        assert r["success"] is False and "No existe la versión" in r["error"]
