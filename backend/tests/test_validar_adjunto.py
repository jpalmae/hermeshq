import base64
import io
import zipfile
from pathlib import Path

import pytest

from hermeshq.services import validar_adjunto as va

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _docx(tmp: Path) -> Path:
    docx = pytest.importorskip("docx")
    ruta = tmp / "informe.docx"
    documento = docx.Document()
    documento.add_paragraph("Hola")
    documento.save(str(ruta))
    return ruta


def _pptx(tmp: Path, vacias: int = 0) -> Path:
    pptx = pytest.importorskip("pptx")
    ruta = tmp / "deck.pptx"
    prs = pptx.Presentation()
    lamina = prs.slides.add_slide(prs.slide_layouts[0])
    lamina.shapes.title.text = "Portada"
    for _ in range(vacias):
        prs.slides.add_slide(prs.slide_layouts[6])
    prs.save(str(ruta))
    return ruta


class TestValidos:
    def test_pdf_docx_pptx_png_validos(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
        (tmp_path / "logo.png").write_bytes(PNG)
        for ruta in (tmp_path / "a.pdf", _docx(tmp_path), _pptx(tmp_path), tmp_path / "logo.png"):
            r = va.validar(ruta)
            assert r.valido, (ruta.name, r.motivo)

    def test_texto_y_csv_no_se_validan_por_firma(self, tmp_path):
        (tmp_path / "datos.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        assert va.validar(tmp_path / "datos.csv").valido


class TestInvalidos:
    @pytest.mark.parametrize(
        ("nombre", "contenido", "motivo"),
        [
            ("vacio.pdf", b"", "vacío"),
            ("html.pdf", b"<html><body>no soy pdf</body></html>", "no es un PDF"),
            ("truncado.pdf", b"%PDF-1.4\n1 0 obj<<>>endobj\n" + b"x" * 5000, "incompleto"),
            ("html.docx", b"<html>tabla</html>", "no es un archivo de Office"),
            ("roto.xlsx", b"PK\x03\x04" + b"\x00" * 64, "dañado"),
            ("falso.png", b"GIF89a....", "no es una imagen PNG"),
        ],
    )
    def test_contenido_que_no_corresponde(self, tmp_path, nombre, contenido, motivo):
        (tmp_path / nombre).write_bytes(contenido)
        r = va.validar(tmp_path / nombre)
        assert not r.valido and motivo in r.motivo, r

    def test_docx_que_en_realidad_es_un_excel(self, tmp_path):
        ruta = tmp_path / "planilla.docx"
        with zipfile.ZipFile(ruta, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("xl/workbook.xml", "<workbook/>")
        r = va.validar(ruta)
        assert not r.valido and "Excel" in r.motivo


class TestExtensionYAvisos:
    def test_sin_extension_se_reconoce_por_la_firma(self, tmp_path):
        (tmp_path / "informe_final").write_bytes(b"%PDF-1.7\n%%EOF\n")
        r = va.validar(tmp_path / "informe_final")
        assert r.valido and r.extension == ".pdf"
        docx = _docx(tmp_path)
        sin = tmp_path / "carta"
        sin.write_bytes(docx.read_bytes())
        assert va.validar(sin).extension == ".docx"

    def test_laminas_vacias_se_avisan(self, tmp_path):
        r = va.validar(_pptx(tmp_path, vacias=2))
        assert r.valido and any("láminas 2 y 3 están vacías" in a for a in r.avisos), r.avisos

    def test_office_protegido_se_entrega_con_aviso(self, tmp_path):
        ruta = tmp_path / "protegido.xlsx"
        ruta.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
        r = va.validar(ruta)
        assert r.valido and any("contraseña" in a for a in r.avisos)


def test_zip_valido(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hola")
    (tmp_path / "paquete.zip").write_bytes(buf.getvalue())
    assert va.validar(tmp_path / "paquete.zip").valido


class TestCriterioDeLaminaVacia:
    def _deck(self, tmp_path, armar):
        pptx = pytest.importorskip("pptx")
        from pptx.util import Emu

        prs = pptx.Presentation()
        ancho, alto = prs.slide_width, prs.slide_height
        for _ in range(3):
            lamina = prs.slides.add_slide(prs.slide_layouts[6])
            pie = lamina.shapes.add_textbox(Emu(ancho - 2000000), Emu(alto - 400000), Emu(1800000), Emu(300000))
            pie.text_frame.text = "Confidencial · Área TI"
        armar(prs, ancho, alto, Emu)
        ruta = tmp_path / "criterio.pptx"
        prs.save(str(ruta))
        return ruta

    def test_pie_repetido_logo_chico_y_fondo_no_cuentan(self, tmp_path):
        imagen = tmp_path / "logo.png"
        imagen.write_bytes(PNG)

        def armar(prs, ancho, alto, Emu):
            for lamina in prs.slides:
                lamina.shapes.add_picture(str(imagen), Emu(100000), Emu(100000), Emu(400000), Emu(400000))
            prs.slides[1].shapes.add_picture(str(imagen), 0, 0, ancho, alto)
            prs.slides[0].shapes.add_textbox(
                Emu(500000), Emu(1500000), Emu(5000000), Emu(800000)
            ).text_frame.text = "Resultados del trimestre"
            prs.slides[2].shapes.add_picture(
                str(imagen), Emu(ancho // 4), Emu(alto // 4), Emu(ancho // 2), Emu(alto // 2)
            )

        assert va._laminas_vacias(self._deck(tmp_path, armar)) == [2]

    def test_tabla_y_numero_de_pagina(self, tmp_path):
        def armar(prs, ancho, alto, Emu):
            prs.slides[0].shapes.add_table(2, 2, Emu(500000), Emu(1500000), Emu(4000000), Emu(1000000))
            prs.slides[1].shapes.add_textbox(Emu(500000), Emu(1500000), Emu(800000), Emu(400000)).text_frame.text = "2"
            prs.slides[2].shapes.add_textbox(
                Emu(500000), Emu(1500000), Emu(800000), Emu(400000)
            ).text_frame.text = "3,5%"

        assert va._laminas_vacias(self._deck(tmp_path, armar)) == [2]


_PAGINA_DETALLE = """<script>
function abrirDetalle(rep) {
  const html = `<!DOCTYPE html><html><body><h1>${rep}</h1>
<script>
function toggleTheme(){document.body.classList.toggle('dark');}
CIERRE
</body></html>`;
  window.open('').document.write(html);
}

function expandAll() {
  document.querySelectorAll('.node-header').forEach(h => { h.classList.add('expanded'); });
}
</script>"""


def _html(tmp: Path, cuerpo: str, nombre: str = "explorador.html") -> Path:
    ruta = tmp / nombre
    ruta.write_text(
        f"<!DOCTYPE html><html><head><title>X</title></head><body><div id='app'></div>{cuerpo}</body></html>",
        encoding="utf-8",
    )
    return ruta


class TestHtml:
    def test_script_interno_sin_escapar_corta_el_bloque_y_se_rechaza(self, tmp_path):
        r = va.validar(_html(tmp_path, _PAGINA_DETALLE.replace("CIERRE", "</script>")))
        assert not r.valido
        assert "JavaScript como texto" in r.motivo
        assert "<\\/script>" in r.motivo

    def test_script_interno_escapado_es_valido(self, tmp_path):
        r = va.validar(_html(tmp_path, _PAGINA_DETALLE.replace("CIERRE", "<\\/script>")))
        assert r.valido, r.motivo

    def test_codigo_mostrado_en_pre_o_code_no_es_falso_positivo(self, tmp_path):
        cuerpo = "<pre><code>function demo(a) { const x = 1; return x; }</code></pre><p>Ejemplo: <code>() => { }</code></p><script>const a = 1;</script>"
        assert va.validar(_html(tmp_path, cuerpo)).valido

    def test_html_sin_scripts_es_valido(self, tmp_path):
        assert va.validar(_html(tmp_path, "<h1>Informe</h1><p>Total: 2794 accesos; 253 reportes.</p>")).valido

    def test_datos_json_en_script_no_ejecutable_son_validos(self, tmp_path):
        cuerpo = '<script type="application/json" id="datos">{"a": "</p>"}</script><script>const d = JSON.parse(document.getElementById("datos").textContent);</script>'
        assert va.validar(_html(tmp_path, cuerpo)).valido

    def test_script_sin_cierre_se_rechaza(self, tmp_path):
        ruta = tmp_path / "corto.html"
        ruta.write_text("<html><body><script>const a = 1;\nfunction f() {", encoding="utf-8")
        r = va.validar(ruta)
        assert not r.valido and "no se cierra" in r.motivo

    def test_htm_tambien_se_revisa(self, tmp_path):
        r = va.validar(_html(tmp_path, _PAGINA_DETALLE.replace("CIERRE", "</script>"), nombre="x.htm"))
        assert not r.valido


class TestExcelYWordVacios:
    def _xlsx(self, tmp_path, hojas):
        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)
        for nombre, filas in hojas.items():
            ws = wb.create_sheet(nombre)
            for f in filas:
                ws.append(f)
        ruta = tmp_path / "libro.xlsx"
        wb.save(ruta)
        return ruta

    def test_un_excel_sin_ningun_dato_se_rechaza(self, tmp_path):
        from hermeshq.services import validar_adjunto as va

        r = va.validar(self._xlsx(tmp_path, {"Guía": [], "Cálculo": [[None, None]]}))
        assert r.valido is False and "no tiene datos" in r.motivo

    def test_un_excel_con_una_hoja_vacia_se_entrega_con_aviso(self, tmp_path):
        from hermeshq.services import validar_adjunto as va

        r = va.validar(self._xlsx(tmp_path, {"Datos": [["a", 1]], "Notas": []}))
        assert r.valido is True and any("Notas" in a for a in r.avisos)

    def test_control_negativo_un_excel_con_datos_pasa_sin_avisos(self, tmp_path):
        from hermeshq.services import validar_adjunto as va

        r = va.validar(self._xlsx(tmp_path, {"Datos": [["a", 1], ["b", 2]]}))
        assert r.valido is True and not r.avisos

    def test_un_word_sin_texto_se_rechaza_y_uno_con_texto_pasa(self, tmp_path):
        from docx import Document

        from hermeshq.services import validar_adjunto as va

        vacio, lleno = tmp_path / "v.docx", tmp_path / "l.docx"
        Document().save(vacio)
        d = Document()
        d.add_paragraph("Hola")
        d.save(lleno)
        assert va.validar(vacio).valido is False and "vacío" in va.validar(vacio).motivo
        assert va.validar(lleno).valido is True
