import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"
ANCHO, ALTO = 20104100, 11309350
TARJETAS = [
    ("Adopción", ["Falta de cultura de datos", "Dependencia de procesos manuales"]),
    ("Recursos", ["Carga completa asignada", "Brecha de habilidades"]),
    ("Tecnología", ["Infraestructura por consolidar", "Integraciones pendientes"]),
]


def _modulo(nombre, archivo):
    spec = importlib.util.spec_from_file_location(nombre, PLUGIN / archivo)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def editor():
    pytest.importorskip("pptx")
    return _modulo("editar_tarjeta_prueba", "editar.py")


def _deck(ruta: Path, tarjetas=TARJETAS):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE

    prs = Presentation()
    prs.slide_width, prs.slide_height = ANCHO, ALTO
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Portada"
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Riesgos principales"
    ancho, hueco, izq0 = 5669280, 456240, 1060704
    for k, (titulo, puntos) in enumerate(tarjetas):
        izq = izq0 + k * (ancho + hueco)
        fondo = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, izq, 2377440, ancho, 4414520)
        fondo.name = f"Fondo {k}"
        etiqueta = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, izq, 2377440, ancho, 1188720)
        etiqueta.text_frame.text = titulo
        caja = s.shapes.add_textbox(izq + 365760, 3886200, ancho - 731520, 2677160)
        caja.text_frame.text = "▪  " + puntos[0]
        for punto in puntos[1:]:
            caja.text_frame.add_paragraph().text = "▪  " + punto
    pie = s.shapes.add_textbox(1000000, 10000000, 3000000, 400000)
    pie.text_frame.text = "contoso.cl"
    otra = prs.slides.add_slide(prs.slide_layouts[5])
    otra.shapes.title.text = "Próximos pasos"
    prs.save(ruta)


def _textos(ruta: Path, lamina: int):
    from pptx import Presentation

    s = Presentation(str(ruta)).slides[lamina - 1]
    return [sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]


def _editar(editor, tmp_path, cambios, tarjetas=TARJETAS):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen, tarjetas)
    return editor.editar(origen, salida, cambios), salida


NUEVA = {
    "agregar_tarjeta": {
        "lamina": 2,
        "titulo": "Calidad de datos de origen",
        "puntos": ["Registros incompletos en sistemas legados"],
    }
}


class TestAgregarTarjeta:
    def test_agrega_la_cuarta_tarjeta_con_el_texto_exacto_y_conserva_las_tres(self, editor, tmp_path):
        r, salida = _editar(editor, tmp_path, [NUEVA])
        t = _textos(salida, 2)
        assert (
            t[-1] == "▪  Registros incompletos en sistemas legados"
            or "▪  Registros incompletos en sistemas legados" in t
        )
        assert "Calidad de datos de origen" in t
        for titulo, puntos in TARJETAS:
            assert titulo in t and ("▪  " + puntos[0]) in "\n".join(t)
        assert r["cambios"][0]["tarjetas_antes"] == 3 and r["cambios"][0]["tarjetas_despues"] == 4

    def test_las_cuatro_tarjetas_caben_en_el_mismo_ancho_y_no_se_pisan(self, editor, tmp_path):
        _, salida = _editar(editor, tmp_path, [NUEVA])
        from pptx import Presentation

        s = Presentation(str(salida)).slides[1]
        fondos = sorted(
            (f for f in s.shapes if f.name.startswith("Fondo") or "tarjeta nueva" in f.name and f.height == 4414520),
            key=lambda f: f.left,
        )
        assert len(fondos) == 4
        assert fondos[0].left == 1060704
        assert fondos[-1].left + fondos[-1].width <= 1060704 + 3 * 5669280 + 2 * 456240
        for a, b in zip(fondos, fondos[1:], strict=False):
            assert a.left + a.width <= b.left
        assert len({f.width for f in fondos}) == 1

    def test_el_pie_y_las_otras_laminas_no_cambian(self, editor, tmp_path):
        _, salida = _editar(editor, tmp_path, [NUEVA])
        assert "contoso.cl" in _textos(salida, 2)
        assert _textos(salida, 3) == ["Próximos pasos"] and _textos(salida, 1) == ["Portada"]

    def test_por_titulo_de_lamina_y_con_otro_cambio_a_la_vez(self, editor, tmp_path):
        cambios = [
            {"agregar_tarjeta": {"lamina_titulo": "riesgos", "titulo": "Legal", "puntos": ["a", "b"]}},
            {"titulo": {"lamina": 3, "nuevo": "Hoja de ruta Q1 2027"}},
        ]
        _, salida = _editar(editor, tmp_path, cambios)
        assert "Legal" in _textos(salida, 2) and _textos(salida, 3) == ["Hoja de ruta Q1 2027"]

    def test_dos_tarjetas_seguidas(self, editor, tmp_path):
        otra = {"agregar_tarjeta": {"lamina": 2, "titulo": "Legal", "puntos": ["x"]}}
        r, salida = _editar(editor, tmp_path, [NUEVA, otra])
        assert r["cambios"][1]["tarjetas_despues"] == 5
        assert {"Calidad de datos de origen", "Legal"} <= set(_textos(salida, 2))

    def test_original_intacto_y_resultado_reabre(self, editor, tmp_path):
        origen = tmp_path / "o.pptx"
        _editar(editor, tmp_path, [NUEVA])
        antes = origen.read_bytes()
        from pptx import Presentation

        assert len(Presentation(str(tmp_path / "s.pptx")).slides) == 3
        assert origen.read_bytes() == antes


class TestRechazos:
    @pytest.mark.parametrize(
        "datos",
        [
            {"lamina": 2, "titulo": "", "puntos": ["x"]},
            {"lamina": 2, "titulo": "X", "puntos": []},
            {"lamina": 2, "titulo": "X", "puntos": ["  "]},
            {"titulo": "X", "puntos": ["x"]},
            {"lamina": 3, "titulo": "X", "puntos": ["x"]},
            {"lamina": 1, "titulo": "X", "puntos": ["x"]},
            {"lamina": 9, "titulo": "X", "puntos": ["x"]},
            {"lamina_titulo": "inexistente", "titulo": "X", "puntos": ["x"]},
        ],
    )
    def test_no_escribe_nada_si_no_se_puede(self, editor, tmp_path, datos):
        origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
        _deck(origen)
        with pytest.raises(editor.EdicionRechazada):
            editor.editar(origen, salida, [{"agregar_tarjeta": datos}])
        assert not salida.exists()

    def test_una_sola_tarjeta_no_es_una_fila(self, editor, tmp_path):
        origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
        _deck(origen, TARJETAS[:1])
        with pytest.raises(editor.EdicionRechazada):
            editor.editar(origen, salida, [{"agregar_tarjeta": NUEVA["agregar_tarjeta"]}])

    def test_reemplazar_una_etiqueta_por_una_frase_remite_a_agregar_tarjeta(self, editor, tmp_path):
        origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
        _deck(origen)
        frase = "Calidad de datos de origen: registros incompletos en sistemas legados"
        with pytest.raises(editor.EdicionRechazada, match="agregar_tarjeta"):
            editor.editar(origen, salida, [{"reemplazar": {"texto": "Tecnología", "por": frase}}])


def test_el_handler_acepta_la_operacion(tmp_path, monkeypatch):
    pytest.importorskip("pptx")
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    plugin = _modulo("hq_pptx_tarjeta_prueba", "__init__.py")
    (tmp_path / "outputs").mkdir()
    _deck(tmp_path / "outputs" / "g.pptx")
    r = json.loads(plugin._handle_edit_pptx({"path": "g.pptx", "cambios": [NUEVA]}))
    assert r["success"] is True, r
    assert r["comparacion"]["cambios"][0]["tarjetas_despues"] == 4


class TestFormatoDelEditorDeWord:
    def test_buscar_reemplazar_se_traduce_a_reemplazar(self, editor, tmp_path):
        cambios = [{"op": "buscar_reemplazar", "buscar": "Próximos pasos", "reemplazar": "Hoja de ruta Q1 2027"}]
        _, salida = _editar(editor, tmp_path, cambios)
        assert "Hoja de ruta Q1 2027" in _textos(salida, 3)

    def test_agregar_tarjeta_con_op_y_el_resto_a_la_vez(self, editor, tmp_path):
        cambios = [
            {"op": "buscar_reemplazar", "buscar": "Próximos pasos", "reemplazar": "Hoja de ruta Q1 2027"},
            {"op": "agregar_tarjeta", "lamina": 2, "titulo": "Legal", "puntos": ["a", "b"]},
        ]
        r, salida = _editar(editor, tmp_path, cambios)
        assert "Legal" in _textos(salida, 2) and r["cambios"][1]["tarjetas_despues"] == 4

    def test_titulo_y_eliminar_lamina_con_op(self, editor, tmp_path):
        _, salida = _editar(editor, tmp_path, [{"op": "titulo", "lamina": 3, "nuevo": "Nuevo"}])
        assert _textos(salida, 3) == ["Nuevo"]
        _, salida2 = _editar(editor, tmp_path, [{"op": "eliminar_lamina", "lamina": 3}])
        from pptx import Presentation

        assert len(Presentation(str(salida2)).slides) == 2

    def test_control_negativo_una_operacion_desconocida_sigue_rechazada_con_ejemplo(self, editor, tmp_path):
        origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
        _deck(origen)
        with pytest.raises(editor.EdicionRechazada, match="por ejemplo"):
            editor.editar(origen, salida, [{"op": "inventada", "x": 1}])
        assert not salida.exists()


class TestSuperposiciones:
    def test_sin_superposicion_no_hay_advertencias(self, editor, tmp_path):
        r, _ = _editar(editor, tmp_path, [NUEVA])
        assert "advertencias" not in r

    def test_un_reemplazo_que_no_mueve_nada_no_advierte(self, editor, tmp_path):
        r, _ = _editar(editor, tmp_path, [{"reemplazar": {"texto": "Próximos pasos", "por": "Hoja de ruta"}}])
        assert "advertencias" not in r

    def test_detecta_el_par_superpuesto(self, editor, tmp_path):
        from pptx import Presentation
        from pptx.util import Emu

        origen = tmp_path / "o.pptx"
        _deck(origen)
        prs = Presentation(str(origen))
        s = prs.slides[1]
        a = s.shapes.add_textbox(Emu(1000000), Emu(1000000), Emu(3000000), Emu(1000000))
        a.text_frame.text = "Uno"
        b = s.shapes.add_textbox(Emu(1500000), Emu(1200000), Emu(3000000), Emu(1000000))
        b.text_frame.text = "Dos"
        pares = editor.superposiciones(prs)
        assert any({x[3], x[4]} == {"Uno", "Dos"} for x in pares)

    def test_control_negativo_cajas_separadas_no_son_pares(self, editor, tmp_path):
        from pptx import Presentation

        origen = tmp_path / "o.pptx"
        _deck(origen)
        assert editor.superposiciones(Presentation(str(origen))) == set()
