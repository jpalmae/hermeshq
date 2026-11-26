from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree
from test_editar_docx import SCRIPT, _doc_tree, _ejecutar, _q

DOCUMENTO = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Prueba OD-1</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:jc w:val="both"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Primer párrafo</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Segundo párrafo</w:t></w:r></w:p>'
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Celda única</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
    "</w:body></w:document>"
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


def _textos(archivo: Path) -> list[str]:
    cuerpo = _doc_tree(archivo).find(_q("body"))
    return ["".join(t.text or "" for t in p.iter(_q("t"))) for p in cuerpo.findall(_q("p"))]


class TestAgregarParrafo:
    def test_al_final_antes_de_sectpr_y_con_el_formato_del_ultimo_parrafo(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        datos = _ejecutar(src, [{"op": "agregar_parrafo", "texto": "Segundo párrafo de prueba"}], out, tmp_path)
        assert datos["resumen"]["agregar_parrafo"][0] == {"parrafos_agregados": 1}
        assert datos["sin_cambios"] is False
        cuerpo = _doc_tree(out).find(_q("body"))
        assert _textos(out)[-1] == "Segundo párrafo de prueba"
        assert cuerpo[-1].tag == _q("sectPr")
        nuevo = cuerpo.findall(_q("p"))[-1]
        assert nuevo.find(_q("pPr")).find(_q("numPr")) is None

    def test_despues_de_un_parrafo_existente_hereda_su_formato(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        _ejecutar(src, [{"op": "agregar_parrafo", "texto": "Insertado", "despues_de": "primer párrafo"}], out, tmp_path)
        assert _textos(out)[:3] == ["Prueba OD-1", "Primer párrafo", "Insertado"]
        nuevo = _doc_tree(out).find(_q("body")).findall(_q("p"))[2]
        assert nuevo.find(_q("pPr")).find(_q("jc")).get(_q("val")) == "both"
        assert nuevo.find(_q("r")).find(_q("rPr")).find(_q("b")) is not None

    def test_tras_un_titulo_no_hereda_el_estilo_de_titulo(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        _ejecutar(
            src, [{"op": "agregar_parrafo", "texto": "Bajo el título", "despues_de": "Prueba OD-1"}], out, tmp_path
        )
        nuevo = _doc_tree(out).find(_q("body")).findall(_q("p"))[1]
        assert nuevo.find(_q("pPr")) is None or nuevo.find(_q("pPr")).find(_q("pStyle")) is None

    def test_una_seccion_con_estilo_de_titulo(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        _ejecutar(
            src,
            [
                {"op": "agregar_parrafo", "texto": "Anexo A", "estilo": "Heading1"},
                {"op": "agregar_parrafo", "texto": "Contenido del anexo"},
            ],
            out,
            tmp_path,
        )
        parrafos = _doc_tree(out).find(_q("body")).findall(_q("p"))
        assert [_textos(out)[-2], _textos(out)[-1]] == ["Anexo A", "Contenido del anexo"]
        assert parrafos[-2].find(_q("pPr")).find(_q("pStyle")).get(_q("val")) == "Heading1"
        assert parrafos[-1].find(_q("pPr")) is None or parrafos[-1].find(_q("pPr")).find(_q("pStyle")) is None

    def test_el_original_no_se_toca_y_el_resultado_abre_como_zip_valido(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        antes = src.read_bytes()
        _ejecutar(src, [{"op": "agregar_parrafo", "texto": "Nuevo"}], out, tmp_path)
        assert src.read_bytes() == antes
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None
            etree.fromstring(z.read("word/document.xml"))

    def test_control_negativo_texto_vacio_y_referencia_inexistente_se_rechazan(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        for op in (
            {"op": "agregar_parrafo", "texto": "  "},
            {"op": "agregar_parrafo", "texto": "x", "despues_de": "no existe"},
        ):
            entrada = tmp_path / "e.json"
            entrada.write_text(
                json.dumps({"archivo": str(src), "operaciones": [op], "salida": str(out)}), encoding="utf-8"
            )
            res = subprocess.run(
                [sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60
            )
            assert res.returncode == 1 and json.loads(res.stdout.strip().splitlines()[-1])["ok"] is False
            assert not out.exists()


class TestEliminarParrafo:
    def test_elimina_el_primero_que_contiene_el_texto(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        datos = _ejecutar(src, [{"op": "eliminar_parrafo", "contiene": "segundo"}], out, tmp_path)
        assert datos["resumen"]["eliminar_parrafo"][0] == {"parrafos_eliminados": 1, "eliminado": ["Segundo párrafo"]}
        assert "Segundo párrafo" not in _textos(out)
        assert len(_textos(out)) == len(_textos(src)) - 1

    def test_no_deja_una_celda_sin_parrafos(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        datos = _ejecutar(src, [{"op": "eliminar_parrafo", "contiene": "Celda única"}], out, tmp_path)
        assert datos["sin_cambios"] is True
        assert "Celda única" in "".join(t.text or "" for t in _doc_tree(out).iter(_q("t")))

    def test_sin_coincidencias_avisa_y_no_cambia_nada(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        datos = _ejecutar(src, [{"op": "eliminar_parrafo", "contiene": "no existe"}], out, tmp_path)
        assert datos["sin_cambios"] is True and datos["avisos"]
        assert _textos(out) == _textos(src)

    def test_info_lista_las_dos_operaciones(self):
        res = subprocess.run([sys.executable, str(SCRIPT), "--info"], capture_output=True, text=True, timeout=60)
        ops = {o["op"] for o in json.loads(res.stdout.strip().splitlines()[-1])["receta"]["operaciones"]}
        assert {"agregar_parrafo", "eliminar_parrafo"} <= ops


class TestCamposDesconocidos:
    def _falla(self, tmp_path, op):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        entrada = tmp_path / "e.json"
        entrada.write_text(json.dumps({"archivo": str(src), "operaciones": [op], "salida": str(out)}), encoding="utf-8")
        res = subprocess.run([sys.executable, str(SCRIPT), str(entrada)], capture_output=True, text=True, timeout=60)
        datos = json.loads(res.stdout.strip().splitlines()[-1])
        assert res.returncode == 1 and datos["ok"] is False and not out.exists()
        return datos["error"]

    def test_caso_real_titulo_ignorado_en_silencio(self, tmp_path):
        error = self._falla(
            tmp_path,
            {
                "op": "agregar_parrafo",
                "texto": "Contenido de ejemplo del anexo.",
                "estilo": "Heading1",
                "título": "Anexo A",
            },
        )
        assert "campos_desconocidos" in error and "título" in error and "dos operaciones" in error

    def test_eliminar_con_campo_desconocido(self, tmp_path):
        assert "campos_desconocidos" in self._falla(tmp_path, {"op": "eliminar_parrafo", "contiene": "x", "texto": "y"})

    def test_control_negativo_los_campos_validos_no_se_rechazan(self, tmp_path):
        src, out = _docx(tmp_path / "in.docx"), tmp_path / "out.docx"
        _ejecutar(
            src,
            [
                {"op": "agregar_parrafo", "texto": "Anexo A", "estilo": "Heading1", "despues_de": None},
                {"op": "agregar_parrafo", "texto": "Cuerpo", "despues_de": "Anexo A"},
            ],
            out,
            tmp_path,
        )
        assert _textos(out)[-2:] == ["Anexo A", "Cuerpo"]
