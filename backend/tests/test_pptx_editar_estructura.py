import base64
import copy
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_OTRO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _cargar(nombre: str, archivo: str):
    spec = importlib.util.spec_from_file_location(nombre, PLUGIN / archivo)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _crear(ruta: Path) -> None:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    prs = Presentation()
    portada = prs.slides.add_slide(prs.slide_layouts[0])
    portada.shapes.title.text = "Informe Orion 2026"
    portada.placeholders[1].text = "Gerencia de Operaciones"

    resultados = prs.slides.add_slide(prs.slide_layouts[5])
    resultados.shapes.title.text = "Resultados del trimestre"
    caja = resultados.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(4), Inches(1))
    parrafo = caja.text_frame.paragraphs[0]
    destacado = parrafo.add_run()
    destacado.text = "Ventas en Orion crecieron 12%"
    destacado.font.bold = True
    destacado.font.size = Pt(28)
    destacado.font.color.rgb = RGBColor(0xC0, 0x10, 0x20)
    aclaracion = parrafo.add_run()
    aclaracion.text = " (cifras preliminares)"
    aclaracion.font.italic = True
    tabla = resultados.shapes.add_table(2, 2, Inches(0.5), Inches(3), Inches(4), Inches(1)).table
    for (fila, col), texto in {(0, 0): "Zona", (0, 1): "Valor", (1, 0): "Norte Orion", (1, 1): "120"}.items():
        tabla.cell(fila, col).text = texto
    tabla.cell(1, 0).text_frame.paragraphs[0].runs[0].font.bold = True
    tabla.cell(1, 1).fill.solid()
    tabla.cell(1, 1).fill.fore_color.rgb = RGBColor(0xEE, 0xEE, 0x00)
    imagen = ruta.with_name("logo.png")
    imagen.write_bytes(PNG)
    resultados.shapes.add_picture(str(imagen), Inches(6), Inches(1.5), width=Inches(1))
    resultados.notes_slide.notes_text_frame.text = "Notas de resultados"

    tendencia = prs.slides.add_slide(prs.slide_layouts[5])
    tendencia.shapes.title.text = "Tendencia"
    datos = CategoryChartData()
    datos.categories = ["Ene", "Feb"]
    datos.add_series("Serie", (1, 2))
    tendencia.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5), Inches(4), Inches(3), datos)
    tendencia.notes_slide.notes_text_frame.text = "Notas originales"

    vinculo = portada.shapes.add_textbox(Inches(1), Inches(6), Inches(3), Inches(0.5))
    vinculo.text_frame.text = "Ir a la tendencia"
    vinculo.click_action.target_slide = tendencia
    prs.save(str(ruta))


def _partes(ruta: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(ruta) as z:
        return {n: z.read(n) for n in z.namelist()}


def _sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    (tmp_path / "uploads").mkdir()
    original = tmp_path / "uploads" / "informe.pptx"
    _crear(original)
    plugin = _cargar("hq_pptx_plugin_estructura", "__init__.py")

    def editar(cambios):
        return json.loads(plugin._handle_edit_pptx({"path": "uploads/informe.pptx", "cambios": cambios}))

    return {"tmp": tmp_path, "original": original, "editar": editar, "sha": _sha(original.read_bytes())}


def _abrir(ruta: Path):
    from pptx import Presentation

    return Presentation(str(ruta))


class TestConservaLoNoPedido:
    def test_reemplazo_no_altera_ninguna_otra_parte_del_paquete(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"texto": "Orion", "por": "Atlas", "lamina": 2}}])
        assert r["success"] is True, r
        antes, despues = _partes(entorno["original"]), _partes(entorno["tmp"] / r["path"])
        assert sorted(antes) == sorted(despues), "no debe aparecer ni desaparecer ninguna parte"
        cambiadas = {n for n in antes if antes[n] != despues[n]}
        assert cambiadas == {"ppt/slides/slide2.xml"}, cambiadas
        medios = [n for n in antes if n.startswith(("ppt/media/", "ppt/embeddings/", "ppt/charts/"))]
        assert medios and all(_sha(antes[n]) == _sha(despues[n]) for n in medios)
        assert antes["ppt/slides/_rels/slide2.xml.rels"] == despues["ppt/slides/_rels/slide2.xml.rels"]

    def test_formato_de_los_runs_y_de_la_tabla_se_conserva(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"texto": "Orion", "por": "Atlas", "lamina": 2}}])
        lamina = _abrir(entorno["tmp"] / r["path"]).slides[1]
        caja = next(f for f in lamina.shapes if f.has_text_frame and "Atlas" in f.text_frame.text)
        destacado, aclaracion = caja.text_frame.paragraphs[0].runs
        assert (destacado.text, destacado.font.bold, destacado.font.size.pt) == (
            "Ventas en Atlas crecieron 12%",
            True,
            28,
        )
        assert str(destacado.font.color.rgb) == "C01020"
        assert (aclaracion.text, aclaracion.font.italic) == (" (cifras preliminares)", True)
        tabla = next(f for f in lamina.shapes if f.has_table).table
        assert (len(tabla.rows), len(tabla.columns)) == (2, 2)
        assert tabla.cell(1, 0).text_frame.paragraphs[0].runs[0].font.bold is True
        assert str(tabla.cell(1, 1).fill.fore_color.rgb) == "EEEE00"

    def test_eliminar_una_lamina_conserva_grafico_notas_y_vinculos_de_las_demas(self, entorno):
        r = entorno["editar"]([{"eliminar_lamina": 2}])
        assert r["success"] is True, r
        nueva = _abrir(entorno["tmp"] / r["path"])
        assert [s.shapes.title.text for s in nueva.slides] == ["Informe Orion 2026", "Tendencia"]
        tendencia = nueva.slides[1]
        grafico = next(f for f in tendencia.shapes if f.has_chart).chart
        assert list(grafico.plots[0].categories) == ["Ene", "Feb"] and list(grafico.series[0].values) == [1.0, 2.0]
        assert tendencia.notes_slide.notes_text_frame.text == "Notas originales"
        vinculo = next(
            f for f in nueva.slides[0].shapes if f.has_text_frame and f.text_frame.text == "Ir a la tendencia"
        )
        assert vinculo.click_action.target_slide.slide_id == tendencia.slide_id
        partes = _partes(entorno["tmp"] / r["path"])
        assert not any("media/" in n for n in partes), "la imagen solo estaba en la lámina eliminada"
        assert len([n for n in partes if n.startswith("ppt/slides/slide")]) == 2


SABOTAJES = {
    "imagen_reemplazada": lambda p: _sustituir_imagen(p),
    "fila_de_tabla_agregada": lambda p: _agregar_fila(p),
    "datos_del_grafico": lambda p: _cambiar_grafico(p),
    "formato_de_otra_forma": lambda p: setattr(
        p.slides[0].placeholders[1].text_frame.paragraphs[0].runs[0].font, "bold", True
    ),
    "formato_de_la_forma_editada": lambda p: setattr(_caja(p).text_frame.paragraphs[0].runs[0].font, "bold", False),
    "forma_desplazada": lambda p: setattr(_imagen(p), "left", _imagen(p).left + 914400),
    "notas_de_otra_lamina": lambda p: setattr(p.slides[2].notes_slide.notes_text_frame, "text", "otra cosa"),
    "vinculo_redirigido": lambda p: setattr(_vinculo(p).click_action, "target_slide", p.slides[1]),
    "fondo_de_lamina": lambda p: p.slides[1].background.fill.solid(),
    "diseno_de_lamina": lambda p: setattr(p.slide_layouts[5], "name", "Otro diseño"),
    "orden_de_formas": lambda p: _invertir_formas(p),
    "forma_nueva": lambda p: p.slides[2].shapes.add_textbox(0, 0, 914400, 914400),
}


def _caja(p):
    return next(f for f in p.slides[1].shapes if f.has_text_frame and "crecieron" in f.text_frame.text)


def _imagen(p):
    return next(f for f in p.slides[1].shapes if f.shape_type == 13)


def _vinculo(p):
    return next(f for f in p.slides[0].shapes if f.has_text_frame and f.text_frame.text == "Ir a la tendencia")


def _sustituir_imagen(p):
    imagen = _imagen(p)
    parte = p.slides[1].part.related_part(imagen._element.blipFill.blip.rEmbed)  # noqa: SLF001
    parte._blob = PNG_OTRO  # noqa: SLF001


def _agregar_fila(p):
    tbl = next(f for f in p.slides[1].shapes if f.has_table).table._tbl  # noqa: SLF001
    tbl.append(copy.deepcopy(tbl.tr_lst[-1]))


def _cambiar_grafico(p):
    from pptx.chart.data import CategoryChartData

    datos = CategoryChartData()
    datos.categories = ["Ene", "Feb"]
    datos.add_series("Serie", (1, 9))
    next(f for f in p.slides[2].shapes if f.has_chart).chart.replace_data(datos)


def _invertir_formas(p):
    arbol = p.slides[1].shapes._spTree  # noqa: SLF001
    formas = [e for e in arbol if e.tag.endswith(("}sp", "}pic", "}graphicFrame"))]
    for elemento in formas:
        arbol.remove(elemento)
    for elemento in reversed(formas):
        arbol.append(elemento)


class TestDetectaModificacionesIndebidas:
    @pytest.mark.parametrize("sabotaje", sorted(SABOTAJES))
    def test_una_modificacion_no_pedida_impide_entregar(self, entorno, monkeypatch, sabotaje):
        editor = _cargar(f"hq_pptx_editar_sabotaje_{sabotaje}", "editar.py")
        original = editor.aplicar

        def aplicar_y_sabotear(presentacion, cambios):
            resultado = original(presentacion, cambios)
            SABOTAJES[sabotaje](presentacion)
            return resultado

        monkeypatch.setattr(editor, "aplicar", aplicar_y_sabotear)
        destino = entorno["tmp"] / "uploads" / "salida.pptx"
        with pytest.raises(editor.EdicionRechazada):
            editor.editar(
                entorno["original"], destino, [{"reemplazar": {"texto": "Orion", "por": "Atlas", "lamina": 2}}]
            )
        assert _sha(entorno["original"].read_bytes()) == entorno["sha"]

    def test_eliminar_una_lamina_a_la_que_apunta_un_vinculo_se_rechaza(self, entorno):
        r = entorno["editar"]([{"eliminar_lamina": 3}])
        assert r["success"] is False and "vínculo" in r["error"], r
        assert not list((entorno["tmp"] / "uploads").glob("informe_v*.pptx"))
