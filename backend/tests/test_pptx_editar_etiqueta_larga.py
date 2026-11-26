import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"
FRASE = "Calidad de datos de origen: registros incompletos en sistemas legados"


def _plugin():
    spec = importlib.util.spec_from_file_location("hq_pptx_plugin_etiqueta_test", PLUGIN / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _crear_presentacion(ruta: Path) -> None:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    prs = Presentation()
    lamina = prs.slides.add_slide(prs.slide_layouts[5])
    lamina.shapes.title.text = "Riesgos principales"
    for indice, (etiqueta, puntos) in enumerate(
        (
            ("Recursos", "Carga operativa vs. proyecto"),
            ("Adopción", "Cambio de cultura de datos"),
            ("Tecnología", "Integración con sistemas legacy"),
        )
    ):
        tarjeta = lamina.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.5 + 3 * indice), Inches(1.5), Inches(2.5), Inches(0.5)
        )
        tarjeta.text_frame.text = etiqueta
        caja = lamina.shapes.add_textbox(Inches(0.5 + 3 * indice), Inches(2.2), Inches(2.5), Inches(1))
        caja.text_frame.text = puntos
    proximos = prs.slides.add_slide(prs.slide_layouts[5])
    proximos.shapes.title.text = "Próximos pasos"
    prs.save(str(ruta))


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    (tmp_path / "outputs").mkdir()
    original = tmp_path / "outputs" / "gobernanza.pptx"
    _crear_presentacion(original)
    plugin = _plugin()

    def editar(cambios):
        return json.loads(plugin._handle_edit_pptx({"path": "outputs/gobernanza.pptx", "cambios": cambios}))

    return {
        "tmp": tmp_path,
        "editar": editar,
        "sha": hashlib.sha256(original.read_bytes()).hexdigest(),
        "original": original,
    }


class TestEtiquetaLarga:
    def test_el_caso_real_se_rechaza_y_no_se_escribe_nada(self, entorno):
        r = entorno["editar"](
            [
                {"reemplazar": {"lamina": 2, "texto": "Próximos pasos", "por": "Hoja de ruta Q1 2027"}},
                {"reemplazar": {"lamina": 1, "texto": "Tecnología", "por": FRASE}},
            ]
        )
        assert r["success"] is False, r
        assert "Tecnología" in r["error"]
        assert "no agrega elementos" in r["error"]
        assert "No se cambió nada" in r["error"]
        assert not (entorno["tmp"] / "outputs" / "gobernanza_v2.pptx").exists()
        assert hashlib.sha256(entorno["original"].read_bytes()).hexdigest() == entorno["sha"]

    def test_el_titulo_se_puede_cambiar_solo(self, entorno):
        r = entorno["editar"]([{"reemplazar": {"lamina": 2, "texto": "Próximos pasos", "por": "Hoja de ruta Q1 2027"}}])
        assert r["success"] is True, r

    def test_reemplazar_una_etiqueta_por_otra_de_largo_parecido_sigue_funcionando(self, entorno):
        r = entorno["editar"](
            [{"reemplazar": {"lamina": 1, "texto": "Tecnología", "por": "Tecnología e infraestructura"}}]
        )
        assert r["success"] is True, r

    def test_un_titulo_largo_en_un_marcador_de_titulo_no_se_bloquea(self, entorno):
        largo = "Riesgos principales del proyecto de gobernanza de datos para el primer trimestre de 2027"
        r = entorno["editar"]([{"reemplazar": {"lamina": 1, "texto": "Riesgos principales", "por": largo}}])
        assert r["success"] is True, r

    def test_un_texto_largo_en_un_cuadro_de_texto_libre_no_se_bloquea(self, entorno):
        r = entorno["editar"](
            [
                {
                    "reemplazar": {
                        "lamina": 1,
                        "texto": "Carga operativa vs. proyecto",
                        "por": "Carga operativa vs. proyecto: " + FRASE,
                    }
                }
            ]
        )
        assert r["success"] is True, r

    def test_la_descripcion_de_la_herramienta_dice_que_solo_agrega_tarjetas(self):
        modulo = _plugin()

        class Ctx:
            def __init__(self):
                self.tools = {}

            def register_tool(self, **kwargs):
                self.tools[kwargs["name"]] = kwargs

        ctx = Ctx()
        modulo.register(ctx)
        descripcion = ctx.tools["hq_pptx_editar"]["schema"]["description"]
        assert "NO agrega columnas" in descripcion
        assert "agregar_tarjeta" in descripcion
