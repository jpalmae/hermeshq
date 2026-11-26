from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree
from test_editar_docx import SCRIPT, _doc_tree, _ejecutar, _q

CUERPO = (
    "<w:p><w:r><w:t>Informe</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Ventas por zona</w:t></w:r></w:p>"
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Zona Ventas</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    "<w:p><w:r><w:t>Costos por zona</w:t></w:r></w:p>"
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Zona Costos</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
)
DOCUMENTO = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    + CUERPO
    + "</w:body></w:document>"
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _docx(destino: Path) -> Path:
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", DOCUMENTO)
    return destino


def _tablas(archivo: Path) -> list[str]:
    cuerpo = _doc_tree(archivo).find(_q("body"))
    return ["".join(t.text or "" for t in tbl.iter(_q("t"))) for tbl in cuerpo.findall(_q("tbl"))]


def test_elimina_la_segunda_tabla_por_numero_y_deja_la_primera(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    datos = _ejecutar(src, [{"op": "eliminar_tabla", "numero": 2}], out, tmp_path)
    assert datos["resumen"]["eliminar_tabla"][0] == {"tablas_eliminadas": 1}
    assert _tablas(out) == ["Zona Ventas"]


def test_elimina_por_texto_y_el_original_no_se_toca(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    antes = src.read_bytes()
    _ejecutar(src, [{"op": "eliminar_tabla", "contiene": "zona costos"}], out, tmp_path)
    assert _tablas(out) == ["Zona Ventas"] and src.read_bytes() == antes
    with zipfile.ZipFile(out) as z:
        etree.fromstring(z.read("word/document.xml"))


def test_eliminar_parrafo_no_quita_tablas(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    _ejecutar(src, [{"op": "eliminar_parrafo", "contiene": "Costos por zona"}], out, tmp_path)
    assert _tablas(out) == ["Zona Ventas", "Zona Costos"]


def _rechaza(src, out, tmp, op):
    import json

    entrada = tmp / "e.json"
    entrada.write_text(json.dumps({"archivo": str(src), "operaciones": [op], "salida": str(out)}), encoding="utf-8")
    res = subprocess.run([sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60)
    return res.returncode, res.stdout + res.stderr


def test_numero_fuera_de_rango_y_argumentos_invalidos_se_rechazan_sin_escribir(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    for op in (
        {"op": "eliminar_tabla", "numero": 3},
        {"op": "eliminar_tabla", "numero": 0},
        {"op": "eliminar_tabla"},
        {"op": "eliminar_tabla", "numero": 1, "contiene": "x"},
        {"op": "eliminar_tabla", "contiene": "zona"},
    ):
        codigo, salida = _rechaza(src, out, tmp_path, op)
        assert codigo != 0 or '"ok": false' in salida.lower(), (op, salida)
        assert not out.exists(), op


def test_eliminar_parrafo_con_texto_ambiguo_se_rechaza_y_el_exacto_gana(tmp_path):
    import json

    cuerpo = (
        "<w:p><w:r><w:t>Este informe resume las ventas y los costos por zona.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Costos por zona</w:t></w:r></w:p>"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    )
    src = tmp_path / "amb.docx"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(
            "word/document.xml",
            DOCUMENTO.replace(CUERPO, cuerpo),
        )
    out = tmp_path / "o.docx"
    codigo, salida = _rechaza(src, out, tmp_path, {"op": "eliminar_parrafo", "contiene": "costos"})
    assert codigo != 0 and "aparece en 2 párrafos" in salida and not out.exists()
    datos = _ejecutar(src, [{"op": "eliminar_parrafo", "contiene": "Costos por zona"}], out, tmp_path)
    assert datos["resumen"]["eliminar_parrafo"][0]["eliminado"] == ["Costos por zona"]
    textos = ["".join(t.text or "" for t in p.iter(_q("t"))) for p in _doc_tree(out).find(_q("body")).findall(_q("p"))]
    assert textos == ["Este informe resume las ventas y los costos por zona."]
    _ = json


def test_buscar_reemplazar_muestra_el_texto_resultante_y_avisa_si_reemplazar_contiene_a_buscar(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    datos = _ejecutar(
        src, [{"op": "buscar_reemplazar", "buscar": "Informe", "reemplazar": "Informe de ventas 2026"}], out, tmp_path
    )
    r = datos["resumen"]["buscar_reemplazar"][0]
    assert r["textos_resultantes"] == ["Informe de ventas 2026"] and "ojo" in r
