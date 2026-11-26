import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _plugin():
    spec = importlib.util.spec_from_file_location("hq_pptx_plugin_editar_test", PLUGIN / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _sha(ruta: Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _crear_presentacion(ruta: Path) -> None:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    portada = prs.slides.add_slide(prs.slide_layouts[0])
    portada.shapes.title.text = "Informe Orion 2026"
    portada.placeholders[1].text = "Gerencia de Operaciones"

    contenido = prs.slides.add_slide(prs.slide_layouts[5])
    contenido.shapes.title.text = "Resultados del trimestre"
    caja = contenido.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(4), Inches(1))
    parrafo = caja.text_frame.paragraphs[0]
    run = parrafo.add_run()
    run.text = "Ventas en Orion crecieron 12%"
    run.font.bold = True
    tabla = contenido.shapes.add_table(2, 2, Inches(0.5), Inches(3), Inches(4), Inches(1)).table
    tabla.cell(0, 0).text = "Zona"
    tabla.cell(0, 1).text = "Valor"
    tabla.cell(1, 0).text = "Norte Orion"
    tabla.cell(1, 1).text = "120"
    imagen = ruta.with_name("logo.png")
    imagen.write_bytes(PNG)
    contenido.shapes.add_picture(str(imagen), Inches(6), Inches(1.5), width=Inches(1))

    graficos = prs.slides.add_slide(prs.slide_layouts[5])
    graficos.shapes.title.text = "Tendencia"
    datos = CategoryChartData()
    datos.categories = ["Ene", "Feb"]
    datos.add_series("Serie", (1, 2))
    graficos.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5), Inches(4), Inches(3), datos)
    grupo = graficos.shapes.add_group_shape()
    grupo.shapes.add_textbox(Inches(6), Inches(1), Inches(2), Inches(1)).text_frame.text = "Nota del grupo"
    graficos.notes_slide.notes_text_frame.text = "Notas originales"
    prs.save(str(ruta))


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    (tmp_path / "uploads").mkdir()
    original = tmp_path / "uploads" / "informe.pptx"
    _crear_presentacion(original)
    plugin = _plugin()

    def editar(cambios, ruta="uploads/informe.pptx"):
        return json.loads(plugin._handle_edit_pptx({"path": ruta, "cambios": cambios}))

    return {"tmp": tmp_path, "original": original, "editar": editar, "sha": _sha(original)}


def _abrir(ruta: Path):
    from pptx import Presentation

    return Presentation(str(ruta))


def _textos(prs) -> list[str]:
    editar = importlib.util.spec_from_file_location("hq_pptx_editar_lectura", PLUGIN / "editar.py")
    modulo = importlib.util.module_from_spec(editar)
    editar.loader.exec_module(modulo)
    return [" | ".join(t for t in lamina["textos"].values() if t) for lamina in modulo.foto(prs)]


class TestEdicionPositiva:
    def test_reemplazo_conserva_todo_lo_demas_y_el_formato(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"texto": "Orion", "por": "Atlas", "lamina": 2}}])
        assert r["success"] is True, r
        assert r["path"] == "uploads/informe_v2.pptx" and r["original_intacto"] is True
        assert _sha(entorno["original"]) == entorno["sha"]
        nueva = _abrir(entorno["tmp"] / r["path"])
        assert len(nueva.slides) == 3
        assert [s.shapes.title.text for s in nueva.slides] == [
            "Informe Orion 2026",
            "Resultados del trimestre",
            "Tendencia",
        ]
        textos = _textos(nueva)
        assert "Ventas en Atlas crecieron 12%" in textos[1] and "Norte Atlas" in textos[1]
        assert "Orion" in textos[0], "la portada no se pidió editar"
        caja = [f for f in nueva.slides[1].shapes if f.has_text_frame and "Atlas" in f.text_frame.text][0]
        assert caja.text_frame.paragraphs[0].runs[0].font.bold is True
        conservado = r["comparacion"]["conservado"]["elementos_originales"]
        assert conservado.get("imagen") == 1 and conservado.get("tabla") == 1 and conservado.get("grafico") == 1
        assert r["comparacion"]["cambios"][0]["reemplazos"] == 2

    def test_titulo_notas_y_eliminar_lamina(self, entorno):
        r = entorno["editar"](
            [
                {"titulo": {"lamina": 3, "nuevo": "Tendencia anual"}},
                {"notas": {"lamina": 3, "texto": "Notas nuevas"}},
                {"eliminar_lamina": 2},
            ]
        )
        assert r["success"] is True, r
        nueva = _abrir(entorno["tmp"] / r["path"])
        assert [s.shapes.title.text for s in nueva.slides] == ["Informe Orion 2026", "Tendencia anual"]
        assert nueva.slides[1].notes_slide.notes_text_frame.text == "Notas nuevas"
        assert r["comparacion"]["laminas_antes"] == 3 and r["comparacion"]["laminas_despues"] == 2

    def test_una_segunda_edicion_crea_v3_sin_pisar_v2(self, entorno):
        entorno["editar"]([{"reemplazar": {"texto": "Orion", "por": "Atlas"}}])
        segunda = entorno["editar"]([{"reemplazar": {"texto": "Tendencia", "por": "Evolución"}}])
        assert segunda["path"] == "uploads/informe_v3.pptx"
        assert "Atlas" in " ".join(_textos(_abrir(entorno["tmp"] / "uploads" / "informe_v2.pptx")))


class TestEdicionNegativa:
    def _sin_versiones(self, entorno):
        return not list((entorno["tmp"] / "uploads").glob("informe_v*.pptx"))

    def test_todo_o_nada_si_un_texto_no_existe(self, entorno):
        r = entorno["editar"](
            [{"reemplazar": {"texto": "Orion", "por": "Atlas"}}, {"reemplazar": {"texto": "Inexistente", "por": "x"}}]
        )
        assert r["success"] is False and "Inexistente" in r["error"]
        assert self._sin_versiones(entorno) and _sha(entorno["original"]) == entorno["sha"]

    def test_texto_en_otra_lamina_dice_en_cual_esta(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"lamina": 1, "texto": "Tendencia", "por": "Otra"}}])
        assert r["success"] is False
        assert "en la lámina 1" in r["error"] and "está en la lámina 3" in r["error"]
        assert self._sin_versiones(entorno)

    def test_texto_inexistente_lista_las_laminas(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"texto": "Próximos pasos", "por": "x"}}])
        assert r["success"] is False
        assert "No aparece en ninguna lámina" in r["error"] and "2 «Resultados del trimestre»" in r["error"]
        assert "agregar_tarjeta" in r["error"]

    def test_ocurrencias_distintas_de_las_esperadas(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"texto": "Orion", "por": "Atlas", "ocurrencias": 1}}])
        assert r["success"] is False and "aparece 3 veces" in r["error"]
        assert self._sin_versiones(entorno)

    def test_archivo_protegido_con_contrasena(self, entorno):
        protegido = entorno["tmp"] / "uploads" / "protegido.pptx"
        protegido.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
        r = entorno["editar"]([{"titulo": {"lamina": 1, "nuevo": "x"}}], ruta="uploads/protegido.pptx")
        assert r["success"] is False and "contraseña" in r["error"]

    def test_archivo_danado(self, entorno):
        danado = entorno["tmp"] / "uploads" / "danado.pptx"
        danado.write_bytes(b"esto no es un pptx")
        r = entorno["editar"]([{"titulo": {"lamina": 1, "nuevo": "x"}}], ruta="uploads/danado.pptx")
        assert r["success"] is False and "no es un .pptx válido" in r["error"]

    def test_lamina_inexistente_y_operacion_desconocida(self, entorno):
        assert "no existe la 9" in entorno["editar"]([{"titulo": {"lamina": 9, "nuevo": "x"}}])["error"]
        assert "una sola operación" in entorno["editar"]([{"rehacer_todo": True}])["error"]
        assert self._sin_versiones(entorno)

    def test_ruta_fuera_del_espacio_de_trabajo(self, entorno):
        r = entorno["editar"]([{"titulo": {"lamina": 1, "nuevo": "x"}}], ruta="/etc/passwd.pptx")
        assert r["success"] is False and "fuera del espacio" in r["error"]


class TestDeteccionDeCambiosNoPedidos:
    def test_un_cambio_ajeno_a_lo_pedido_se_detecta(self, entorno):
        editar = importlib.util.spec_from_file_location("hq_pptx_editar_det", PLUGIN / "editar.py")
        modulo = importlib.util.module_from_spec(editar)
        editar.loader.exec_module(modulo)
        antes = modulo.foto(_abrir(entorno["original"]))
        despues = json.loads(json.dumps(antes))
        primera_forma = next(iter(despues[0]["textos"]))
        despues[0]["textos"] = {int(k): v for k, v in despues[0]["textos"].items()}
        despues[0]["textos"][int(primera_forma)] = "portada reemplazada"
        despues[1]["conteo"]["imagen"] = 0
        for lamina in antes + despues:
            lamina["textos"] = {int(k): v for k, v in lamina["textos"].items()}
        problemas = modulo.diferencias_no_pedidas(antes, despues, set(), set())
        assert any("Lámina 1" in p for p in problemas) and any("Lámina 2" in p for p in problemas)
