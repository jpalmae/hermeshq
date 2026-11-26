import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    spec = importlib.util.spec_from_file_location("hq_docx_imagen", RAIZ / "hermeshq_docx" / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _png_vertical(ruta: Path):
    pil = pytest.importorskip("PIL.Image")
    pil.new("RGB", (600, 900), (240, 240, 240)).save(ruta)


def _generar(plugin, tmp_path, caption="Pie de foto"):
    _png_vertical(tmp_path / "v.png")
    bloque = {"type": "image", "path": "v.png"}
    if caption:
        bloque["caption"] = caption
    r = json.loads(plugin._handle_generate_docx({"title": "T", "sections": [bloque]}))
    assert r["success"], r
    docx = pytest.importorskip("docx")
    ruta = next(Path(tmp_path).rglob("*.docx"))
    return docx.Document(str(ruta))


def test_la_imagen_vertical_no_supera_el_alto_maximo(plugin, tmp_path):
    doc = _generar(plugin, tmp_path)
    forma = doc.inline_shapes[0]
    assert forma.height <= int(4.5 * 914400) + 1
    assert abs(forma.width / forma.height - 600 / 900) < 0.01


def test_la_imagen_se_mantiene_con_su_pie_y_tiene_aire_arriba(plugin, tmp_path):
    doc = _generar(plugin, tmp_path)
    imagen = next(p for p in doc.paragraphs if p._p.xpath(".//w:drawing"))
    assert imagen.paragraph_format.keep_with_next is True
    assert imagen.paragraph_format.space_before is not None and imagen.paragraph_format.space_before.pt >= 12


def test_sin_pie_no_se_ata_al_siguiente_parrafo(plugin, tmp_path):
    doc = _generar(plugin, tmp_path, caption="")
    imagen = next(p for p in doc.paragraphs if p._p.xpath(".//w:drawing"))
    assert not imagen.paragraph_format.keep_with_next
