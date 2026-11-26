from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from test_editar_docx import SCRIPT, _doc_tree, _q
from test_editar_docx_parrafos import CONTENT_TYPES, RELS

CUERPO = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    "<w:p><w:r><w:t>Ana Pérez – cargo por definir</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Luis Soto – cargo por definir</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>María Rojas – cargo por definir</w:t></w:r></w:p>"
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    "</w:body></w:document>"
)


def _docx(destino: Path) -> Path:
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", CUERPO)
    return destino


def _correr(src: Path, op: dict, out: Path, tmp: Path):
    entrada = tmp / "e.json"
    entrada.write_text(json.dumps({"archivo": str(src), "operaciones": [op], "salida": str(out)}), encoding="utf-8")
    res = subprocess.run([sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60)
    return res.returncode, json.loads(res.stdout.strip().splitlines()[-1])


def _textos(archivo: Path) -> list[str]:
    cuerpo = _doc_tree(archivo).find(_q("body"))
    return ["".join(t.text or "" for t in p.iter(_q("t"))) for p in cuerpo.findall(_q("p"))]


def test_varias_coincidencias_sin_max_se_rechazan_y_no_se_escribe_nada(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, datos = _correr(
        src, {"op": "buscar_reemplazar", "buscar": "cargo por definir", "reemplazar": "Gerente"}, out, tmp_path
    )
    assert codigo == 1 and datos["ok"] is False
    assert "aparece_3_veces" in json.dumps(datos)
    assert not out.exists()


def test_un_texto_mas_especifico_cambia_solo_a_esa_persona(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, _ = _correr(
        src,
        {"op": "buscar_reemplazar", "buscar": "María Rojas – cargo por definir", "reemplazar": "María Rojas – Gerente"},
        out,
        tmp_path,
    )
    assert codigo == 0
    assert _textos(out) == ["Ana Pérez – cargo por definir", "Luis Soto – cargo por definir", "María Rojas – Gerente"]


def test_max_no_resuelve_la_ambiguedad(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, datos = _correr(
        src, {"op": "buscar_reemplazar", "buscar": "cargo por definir", "reemplazar": "X", "max": 1}, out, tmp_path
    )
    assert codigo == 1 and datos["ok"] is False
    assert "max_no_elige_cual_cambiar" in json.dumps(datos)
    assert not out.exists()


def test_parrafo_contiene_cambia_solo_el_parrafo_de_esa_persona(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, _ = _correr(
        src,
        {
            "op": "buscar_reemplazar",
            "buscar": "cargo por definir",
            "reemplazar": "Gerente de Seguridad",
            "parrafo_contiene": "María Rojas",
        },
        out,
        tmp_path,
    )
    assert codigo == 0
    assert _textos(out) == [
        "Ana Pérez – cargo por definir",
        "Luis Soto – cargo por definir",
        "María Rojas – Gerente de Seguridad",
    ]


def test_parrafo_contiene_con_dos_parrafos_de_la_misma_persona_cambia_ambos_y_no_los_de_otros(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    cuerpo = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Ana Pérez – cargo por definir</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>María Rojas – cargo por definir</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Aprobador: María Rojas – cargo por definir</w:t></w:r></w:p>"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>'
    )
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", cuerpo)
    codigo, _ = _correr(
        src,
        {
            "op": "buscar_reemplazar",
            "buscar": "cargo por definir",
            "reemplazar": "Gerente",
            "parrafo_contiene": "María Rojas",
        },
        out,
        tmp_path,
    )
    assert codigo == 0
    assert _textos(out) == [
        "Ana Pérez – cargo por definir",
        "María Rojas – Gerente",
        "Aprobador: María Rojas – Gerente",
    ]


def test_parrafo_contiene_que_no_existe_no_cambia_nada(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, datos = _correr(
        src,
        {"op": "buscar_reemplazar", "buscar": "cargo por definir", "reemplazar": "X", "parrafo_contiene": "Pedro"},
        out,
        tmp_path,
    )
    assert datos.get("sin_cambios") is True or codigo != 0 or _textos(out) == _textos(src)


def test_todas_true_cuando_si_pidieron_cambiar_todas(tmp_path):
    src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
    codigo, _ = _correr(
        src, {"op": "buscar_reemplazar", "buscar": "cargo por definir", "reemplazar": "X", "todas": True}, out, tmp_path
    )
    assert codigo == 0
    assert all(t.endswith("X") for t in _textos(out))
