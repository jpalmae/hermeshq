import importlib.util
import json
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


def _cargar(slug):
    spec = importlib.util.spec_from_file_location(f"{slug}_contenido", RAIZ / slug / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(autouse=True)
def espacio(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    monkeypatch.delenv("HERMESHQ_TASK_PAYLOAD", raising=False)
    return tmp_path


class TestDocx:
    def test_el_archivo_trae_lo_pedido_en_orden(self):
        docx = pytest.importorskip("docx")
        plugin = _cargar("hermeshq_docx")
        r = json.loads(
            plugin._handle_generate_docx(
                {
                    "title": "Informe de prueba",
                    "sections": [
                        {"type": "heading1", "text": "Resumen"},
                        {"type": "paragraph", "text": "Texto del resumen."},
                        {"type": "bullet_list", "items": ["uno", "dos", "tres"]},
                        {"type": "table", "headers": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]},
                    ],
                }
            )
        )
        assert r["success"] is True and "bloques_omitidos" not in r
        doc = docx.Document(r["path"])
        textos = [p.text for p in doc.paragraphs]
        assert textos[0] == "Informe de prueba"
        assert textos.index("Resumen") < textos.index("Texto del resumen.") < textos.index("uno")
        assert [p.text for p in doc.paragraphs if p.style.name == "List Bullet"] == ["uno", "dos", "tres"]
        tabla = doc.tables[0]
        assert [c.text for c in tabla.rows[0].cells] == ["A", "B"]
        assert [c.text for c in tabla.rows[-1].cells] == ["3", "4"]

    def test_un_bloque_que_no_se_dibuja_se_avisa(self):
        docx = pytest.importorskip("docx")
        plugin = _cargar("hermeshq_docx")
        r = json.loads(
            plugin._handle_generate_docx(
                {
                    "title": "T",
                    "sections": [{"type": "paragraph", "text": "ok"}, "basura", {"type": "grafico", "text": "x"}],
                }
            )
        )
        assert r["success"] is True
        assert len(r["bloques_omitidos"]) == 2 and "Se omitieron 2" in r["aviso"]
        assert [p.text for p in docx.Document(r["path"]).paragraphs] == ["T", "ok"]

    def test_sin_secciones_falla(self):
        pytest.importorskip("docx")
        r = json.loads(_cargar("hermeshq_docx")._handle_generate_docx({"title": "T", "sections": []}))
        assert r["success"] is False


class TestPdf:
    def test_el_pdf_tiene_el_texto_y_las_paginas(self):
        pytest.importorskip("weasyprint")
        pymupdf = pytest.importorskip("pymupdf")
        plugin = _cargar("hermeshq_pdf")
        html = "<h2>Capítulo uno</h2><p>Contenido del capítulo.</p><table><tr><th>X</th></tr><tr><td>valor-9</td></tr></table>"
        r = json.loads(plugin._handle_generate_pdf({"title": "Reporte PDF", "html_content": html}))
        assert r["success"] is True
        doc = pymupdf.open(r["path"])
        texto = " ".join(p.get_text() for p in doc)
        assert doc.page_count >= 1
        for esperado in ("Reporte PDF", "Capítulo uno", "Contenido del capítulo.", "valor-9"):
            assert esperado in texto

    def test_imagen_inexistente_no_entrega_pdf_sin_ella(self, espacio):
        pytest.importorskip("weasyprint")
        plugin = _cargar("hermeshq_pdf")
        r = json.loads(plugin._handle_generate_pdf({"title": "T", "html_content": '<img src="uploads/no-existe.png">'}))
        assert r["success"] is False


class TestXlsx:
    def test_las_celdas_y_las_hojas_son_las_pedidas(self):
        openpyxl = pytest.importorskip("openpyxl")
        plugin = _cargar("hermeshq_xlsx")
        r = json.loads(
            plugin._handle_generate_xlsx(
                {
                    "title": "Ventas",
                    "sheets": [
                        {"name": "Ventas", "headers": ["Mes", "Total"], "rows": [["Enero", 100], ["Febrero", 250]]},
                        {"name": "Notas", "headers": ["Nota"], "rows": [["ok"]]},
                    ],
                }
            )
        )
        assert r["success"] is True and r["sheet_count"] == 2 and r["filas_por_hoja"] == {"Ventas": 2, "Notas": 1}
        libro = openpyxl.load_workbook(r["path"])
        assert libro.sheetnames == ["Ventas", "Notas"]
        hoja = libro["Ventas"]
        assert [[c.value for c in fila] for fila in hoja.iter_rows()] == [
            ["Mes", "Total"],
            ["Enero", 100],
            ["Febrero", 250],
        ]

    def test_hoja_sin_filas_se_avisa(self):
        pytest.importorskip("openpyxl")
        r = json.loads(
            _cargar("hermeshq_xlsx")._handle_generate_xlsx({"title": "T", "sheets": [{"name": "A", "headers": ["x"]}]})
        )
        assert r["success"] is True and "sin filas" in r["aviso"]


class TestPptx:
    def test_portada_y_laminas_con_su_texto(self):
        pptx = pytest.importorskip("pptx")
        plugin = _cargar("hermeshq_pptx")
        r = json.loads(
            plugin._handle_generate_pptx(
                {
                    "title": "Clima",
                    "subtitle": "Valparaíso",
                    "slides": [
                        {"title": "Temperatura", "bullets": ["18 grados", "viento sur"], "notes": "nota 1"},
                        {"title": "Lluvia", "bullets": ["poca"]},
                    ],
                }
            )
        )
        assert r["success"] is True and r["slide_count"] == 3
        prs = pptx.Presentation(r["path"])
        assert len(prs.slides) == r["slide_count"]
        titulos = [s.shapes.title.text for s in prs.slides]
        assert titulos == ["Clima", "Temperatura", "Lluvia"]
        textos = " ".join(f.text_frame.text for s in prs.slides for f in s.shapes if f.has_text_frame)
        assert "18 grados" in textos and "viento sur" in textos and "Valparaíso" in textos
        assert prs.slides[1].notes_slide.notes_text_frame.text == "nota 1"
