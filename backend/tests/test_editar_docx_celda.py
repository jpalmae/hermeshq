from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import test_editar_docx_tabla as base
from test_editar_docx import SCRIPT, _doc_tree, _ejecutar, _q

TABLA = (
    "<w:tbl>"
    "<w:tr><w:tc><w:p><w:r><w:t>Tarea</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Responsable</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>Estado</w:t></w:r></w:p></w:tc></w:tr>"
    "<w:tr><w:tc><w:p><w:r><w:t>Diseño</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Ana</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>En curso</w:t></w:r></w:p></w:tc></w:tr>"
    "<w:tr><w:tc><w:p><w:r><w:t>Pruebas</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Luis</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>Pendiente</w:t></w:r></w:p></w:tc></w:tr>"
    "</w:tbl>"
)
CUERPO = (
    "<w:p><w:r><w:t>Plan</w:t></w:r></w:p>"
    + TABLA
    + "<w:p><w:r><w:t>Las tareas de Pruebas y Entrega están pendientes</w:t></w:r></w:p>"
    + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
)


def _docx(destino: Path) -> Path:
    documento = base.DOCUMENTO.replace(base.CUERPO, CUERPO)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", base.CONTENT_TYPES)
        z.writestr("_rels/.rels", base.RELS)
        z.writestr("word/document.xml", documento)
    return destino


def _celdas(archivo: Path) -> list[list[str]]:
    tabla = _doc_tree(archivo).find(_q("body")).find(_q("tbl"))
    return [
        ["".join(t.text or "" for t in c.iter(_q("t"))) for c in fila.findall(_q("tc"))]
        for fila in tabla.findall(_q("tr"))
    ]


def _parrafos(archivo: Path) -> list[str]:
    cuerpo = _doc_tree(archivo).find(_q("body"))
    return ["".join(t.text or "" for t in p.iter(_q("t"))) for p in cuerpo.findall(_q("p"))]


def test_cambia_solo_la_celda_pedida_y_no_el_resto_del_documento(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    datos = _ejecutar(
        src, [{"op": "cambiar_celda", "fila": "Pruebas", "columna": "Estado", "valor": "En curso"}], out, tmp_path
    )
    assert datos["resumen"]["cambiar_celda"][0]["celdas_cambiadas"] == 1
    assert datos["resumen"]["cambiar_celda"][0]["antes"] == "Pendiente"
    assert _celdas(out)[2] == ["Pruebas", "Luis", "En curso"]
    assert _celdas(out)[1] == ["Diseño", "Ana", "En curso"]
    assert "Las tareas de Pruebas y Entrega están pendientes" in _parrafos(out)


def test_columna_por_numero(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    _ejecutar(src, [{"op": "cambiar_celda", "fila": "diseño", "columna": 2, "valor": "Ana María"}], out, tmp_path)
    assert _celdas(out)[1] == ["Diseño", "Ana María", "En curso"]


def _rechaza(src, out, tmp, op):
    entrada = tmp / "entrada.json"
    entrada.write_text(json.dumps({"archivo": str(src), "operaciones": [op], "salida": str(out)}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


def test_fila_o_columna_inexistente_se_rechaza_sin_escribir(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, salida = _rechaza(
        src, out, tmp_path, {"op": "cambiar_celda", "fila": "Entrega", "columna": "Estado", "valor": "x"}
    )
    assert codigo != 0 and "la_fila_coincide_con_0_filas" in salida and not out.exists()
    codigo, salida = _rechaza(
        src, out, tmp_path, {"op": "cambiar_celda", "fila": "Pruebas", "columna": "Prioridad", "valor": "x"}
    )
    assert codigo != 0 and "columna_no_encontrada" in salida and "estado" in salida and not out.exists()


def test_campos_desconocidos_se_rechazan(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, salida = _rechaza(
        src,
        out,
        tmp_path,
        {"op": "cambiar_celda", "fila": "Pruebas", "columna": "Estado", "valor": "x", "color": "rojo"},
    )
    assert codigo != 0 and "campos_desconocidos" in salida
