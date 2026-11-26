from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree

SCRIPT = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "editar_docx.py"
)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
    "</Relationships>"
)
DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>'
    '<w:p><w:r><w:rPr><w:color w:val="EE0000"/></w:rPr><w:t>rojo1</w:t></w:r>'
    "<w:r><w:t>negro</w:t></w:r></w:p>"
    '<w:p><w:r><w:rPr><w:color w:val="FF0000"/></w:rPr><w:t>rojoSolo</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    "<w:r><w:t>item lista</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>texto viejo aqui</w:t></w:r></w:p>"
    '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>comentado</w:t></w:r>'
    '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>'
    "</w:body></w:document>"
)
COMMENTS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
    '<w:comment w:id="0" w:author="x" w:date="2026-01-01T00:00:00Z">'
    '<w:p w14:paraId="PARA0001"><w:r><w:t>un comentario</w:t></w:r></w:p></w:comment>'
    "</w:comments>"
)


def _comments_ext(done: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
        f'<w15:commentEx w15:paraId="PARA0001" w15:done="{done}"/>'
        "</w15:commentsEx>"
    )


def _construir_docx(destino: Path, done: str = "1") -> None:
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/document.xml", DOCUMENT)
        z.writestr("word/comments.xml", COMMENTS)
        z.writestr("word/commentsExtended.xml", _comments_ext(done))


def _ejecutar(archivo: Path, operaciones: list[dict], salida: Path, tmp: Path) -> dict:
    entrada = tmp / "entrada.json"
    entrada.write_text(
        json.dumps({"archivo": str(archivo), "operaciones": operaciones, "salida": str(salida)}),
        encoding="utf-8",
    )
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(entrada)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _doc_tree(archivo: Path):
    with zipfile.ZipFile(archivo) as z:
        return etree.fromstring(z.read("word/document.xml"))


def _comments_tree(archivo: Path):
    with zipfile.ZipFile(archivo) as z:
        return etree.fromstring(z.read("word/comments.xml"))


class TestEditarDocx:
    def test_info_devuelve_receta(self):
        res = subprocess.run([sys.executable, str(SCRIPT), "--info"], capture_output=True, text=True, timeout=60)
        datos = json.loads(res.stdout.strip().splitlines()[-1])
        assert datos["ok"] is True
        ops = {o["op"] for o in datos["receta"]["operaciones"]}
        assert {"eliminar_color", "quitar_comentarios", "listas_a_parrafos", "buscar_reemplazar"} <= ops

    def test_eliminar_color(self, tmp_path):
        src = tmp_path / "in.docx"
        out = tmp_path / "out.docx"
        _construir_docx(src)
        datos = _ejecutar(src, [{"op": "eliminar_color"}], out, tmp_path)
        resumen = datos["resumen"]["eliminar_color"][0]
        assert resumen["runs_en_color_quitados"] == 2
        assert resumen["parrafos_vacios_borrados"] == 1
        doc = _doc_tree(out)
        rojos = [c for c in doc.iter(_q("color")) if (c.get(_q("val")) or "").upper() in ("EE0000", "FF0000")]
        assert rojos == []
        textos = [t.text for t in doc.iter(_q("t"))]
        assert "negro" in textos
        assert "rojo1" not in textos
        assert "rojoSolo" not in textos

    def test_listas_a_parrafos(self, tmp_path):
        src = tmp_path / "in.docx"
        out = tmp_path / "out.docx"
        _construir_docx(src)
        datos = _ejecutar(src, [{"op": "listas_a_parrafos"}], out, tmp_path)
        assert datos["resumen"]["listas_a_parrafos"][0]["items_de_lista_convertidos"] == 1
        doc = _doc_tree(out)
        assert sum(1 for _ in doc.iter(_q("numPr"))) == 0
        assert "item lista" in [t.text for t in doc.iter(_q("t"))]

    def test_buscar_reemplazar(self, tmp_path):
        src = tmp_path / "in.docx"
        out = tmp_path / "out.docx"
        _construir_docx(src)
        datos = _ejecutar(src, [{"op": "buscar_reemplazar", "buscar": "viejo", "reemplazar": "nuevo"}], out, tmp_path)
        assert datos["resumen"]["buscar_reemplazar"][0]["reemplazos"] == 1
        textos = "".join(t.text or "" for t in _doc_tree(out).iter(_q("t")))
        assert "texto nuevo aqui" in textos
        assert "viejo" not in textos

    def test_quitar_comentarios_todos(self, tmp_path):
        src = tmp_path / "in.docx"
        out = tmp_path / "out.docx"
        _construir_docx(src)
        datos = _ejecutar(src, [{"op": "quitar_comentarios", "cuales": "todos"}], out, tmp_path)
        assert datos["resumen"]["quitar_comentarios"][0]["comentarios_quitados"] == 1
        doc = _doc_tree(out)
        assert sum(1 for _ in doc.iter(_q("commentReference"))) == 0
        assert sum(1 for _ in doc.iter(_q("commentRangeStart"))) == 0
        assert sum(1 for _ in _comments_tree(out).iter(_q("comment"))) == 0
        assert "comentado" in [t.text for t in doc.iter(_q("t"))]

    def test_quitar_comentarios_solo_resueltos(self, tmp_path):
        src_done = tmp_path / "done.docx"
        out_done = tmp_path / "done_out.docx"
        _construir_docx(src_done, done="1")
        d1 = _ejecutar(src_done, [{"op": "quitar_comentarios", "cuales": "resueltos"}], out_done, tmp_path)
        assert d1["resumen"]["quitar_comentarios"][0]["comentarios_quitados"] == 1

        src_open = tmp_path / "open.docx"
        out_open = tmp_path / "open_out.docx"
        _construir_docx(src_open, done="0")
        d2 = _ejecutar(src_open, [{"op": "quitar_comentarios", "cuales": "resueltos"}], out_open, tmp_path)
        assert d2["resumen"]["quitar_comentarios"][0]["comentarios_quitados"] == 0
        assert sum(1 for _ in _comments_tree(out_open).iter(_q("comment"))) == 1

    def test_original_intacto_y_varias_ops(self, tmp_path):
        src = tmp_path / "in.docx"
        out = tmp_path / "out.docx"
        _construir_docx(src)
        antes = src.read_bytes()
        datos = _ejecutar(
            src,
            [
                {"op": "eliminar_color"},
                {"op": "quitar_comentarios", "cuales": "todos"},
                {"op": "listas_a_parrafos"},
            ],
            out,
            tmp_path,
        )
        assert datos["ok"] is True
        assert src.read_bytes() == antes
        assert out.exists()

    def test_archivo_inexistente(self, tmp_path):
        entrada = tmp_path / "e.json"
        entrada.write_text(
            json.dumps({"archivo": str(tmp_path / "noexiste.docx"), "operaciones": [{"op": "listas_a_parrafos"}]})
        )
        res = subprocess.run([sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60)
        salida = json.loads(res.stdout.strip().splitlines()[-1])
        assert salida["ok"] is False
