import importlib.util
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"


def _editor():
    spec = importlib.util.spec_from_file_location("editar_punto_prueba", PLUGIN / "editar.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def editor():
    pytest.importorskip("pptx")
    return _editor()


def _deck(ruta: Path):
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Portada"
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Riesgos principales"
    caja = s.shapes.add_textbox(Emu(900000), Emu(1800000), Emu(6000000), Emu(3000000))
    caja.name = "Cuerpo"
    caja.text_frame.text = "▪  Adopción: baja adopción"
    caja.text_frame.add_paragraph().text = "▪  Recursos: equipo reducido"
    caja.text_frame.add_paragraph().text = "▪  Tecnología: integraciones pendientes"
    pie = s.shapes.add_textbox(Emu(900000), Emu(6000000), Emu(2000000), Emu(300000))
    pie.text_frame.text = "contoso.cl"
    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "Próximos pasos"
    prs.save(ruta)


def _parrafos(ruta: Path, lamina: int, nombre: str):
    from pptx import Presentation

    s = Presentation(str(ruta)).slides[lamina - 1]
    caja = next(sh for sh in s.shapes if sh.name == nombre)
    return [p.text for p in caja.text_frame.paragraphs]


def test_agrega_una_vineta_al_final_con_el_mismo_prefijo(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    r = editor.editar(
        origen, salida, [{"agregar_punto": {"lamina": 2, "texto": "Calidad de datos de origen: registros incompletos"}}]
    )
    assert r["cambios"][0]["operacion"] == "agregar_punto"
    ps = _parrafos(salida, 2, "Cuerpo")
    assert ps[:3] == [
        "▪  Adopción: baja adopción",
        "▪  Recursos: equipo reducido",
        "▪  Tecnología: integraciones pendientes",
    ]
    assert ps[3] == "▪  Calidad de datos de origen: registros incompletos" and len(ps) == 4


def test_por_titulo_de_lamina_y_sin_tocar_el_pie_ni_otras_laminas(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    editor.editar(origen, salida, [{"agregar_punto": {"lamina_titulo": "Riesgos", "texto": "Nuevo"}}])
    from pptx import Presentation

    prs = Presentation(str(salida))
    pie = next(sh for sh in prs.slides[1].shapes if sh.has_text_frame and sh.text_frame.text == "contoso.cl")
    assert pie is not None
    assert [p.text for p in prs.slides[2].shapes.title.text_frame.paragraphs] == ["Próximos pasos"]


def test_sin_texto_o_en_lamina_sin_lista_se_rechaza_sin_escribir(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    with pytest.raises(editor.EdicionRechazada, match="necesita"):
        editor.editar(origen, salida, [{"agregar_punto": {"lamina": 2, "texto": ""}}])
    with pytest.raises(editor.EdicionRechazada, match="no tiene una lista"):
        editor.editar(origen, salida, [{"agregar_punto": {"lamina": 1, "texto": "x"}}])
    assert not salida.exists()


def test_agregar_tarjeta_en_una_lista_orienta_a_agregar_punto(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    with pytest.raises(editor.EdicionRechazada, match="agregar_punto"):
        editor.editar(origen, salida, [{"agregar_tarjeta": {"lamina": 2, "titulo": "T", "puntos": ["a"]}}])


def test_con_vineta_en_corrida_propia_conserva_el_formato_del_texto_y_no_hereda_el_de_la_vineta(editor, tmp_path):
    from pptx import Presentation
    from pptx.dml.color import RGBColor

    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    prs = Presentation(str(origen))
    caja = next(sh for sh in prs.slides[1].shapes if sh.name == "Cuerpo")
    for p in caja.text_frame.paragraphs:
        texto = p.text.replace("▪  ", "")
        for r in list(p.runs):
            r._r.getparent().remove(r._r)  # noqa: SLF001
        puntito = p.add_run()
        puntito.text = "▪  "
        puntito.font.bold = True
        puntito.font.color.rgb = RGBColor(0, 218, 255)
        cuerpo = p.add_run()
        cuerpo.text = texto
        cuerpo.font.bold = False
    prs.save(origen)
    editor.editar(origen, salida, [{"agregar_punto": {"lamina": 2, "texto": "Nuevo riesgo"}}])
    ultimo = next(sh for sh in Presentation(str(salida)).slides[1].shapes if sh.name == "Cuerpo").text_frame.paragraphs[
        -1
    ]
    assert [r.text for r in ultimo.runs] == ["▪  ", "Nuevo riesgo"]
    assert ultimo.runs[0].font.bold is True and ultimo.runs[1].font.bold is False


def test_el_resultado_dice_en_que_lamina_se_agrego_para_detectar_un_numero_equivocado(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    r = editor.editar(origen, salida, [{"agregar_punto": {"lamina": 2, "texto": "Calidad de datos"}}])
    cambio = r["cambios"][0]
    assert cambio["titulo_de_la_lamina"] == "Riesgos principales"
    assert "«Riesgos principales»" in cambio["nota"] and "lamina_titulo" in cambio["nota"]


def test_agregar_tarjeta_en_la_lamina_equivocada_nombra_su_titulo_y_orienta_a_lamina_titulo(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    with pytest.raises(editor.EdicionRechazada) as info:
        editor.editar(origen, salida, [{"agregar_tarjeta": {"lamina": 2, "titulo": "X", "puntos": ["y"]}}])
    mensaje = str(info.value)
    assert "«Riesgos principales»" in mensaje and "lamina_titulo" in mensaje
    assert not salida.exists()


def test_formato_op_con_max_se_traduce_a_ocurrencias_y_no_reemplaza_de_mas(editor, tmp_path):
    from pptx import Presentation

    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    prs = Presentation(str(origen))
    prs.slides[0].shapes.title.text = "Portada Riesgos"
    prs.save(origen)
    with pytest.raises(editor.EdicionRechazada, match="2 veces"):
        editor.editar(
            origen, salida, [{"op": "buscar_reemplazar", "buscar": "Riesgos", "reemplazar": "Otro", "max": 1}]
        )
    assert not salida.exists()


def test_formato_op_rechaza_campos_desconocidos_en_vez_de_ignorarlos(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    with pytest.raises(editor.EdicionRechazada, match="campos que no entiendo"):
        editor.editar(
            origen,
            salida,
            [{"op": "buscar_reemplazar", "buscar": "Portada", "reemplazar": "Otro", "solo_titulo": True}],
        )


def _deck_con_tarjetas_vacias(ruta: Path):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Riesgos del proyecto"
    for i, nombre in enumerate(["Adopción", "Recursos", "Tecnología"]):
        x = 600000 + i * 3300000
        fondo = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Emu(x), Emu(1500000), Emu(3000000), Emu(3500000))
        fondo.name = f"Fondo {i}"
        etiqueta = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Emu(x), Emu(1500000), Emu(3000000), Emu(800000))
        etiqueta.text_frame.text = nombre
        cuerpo = s.shapes.add_textbox(Emu(x + 200000), Emu(2500000), Emu(2600000), Emu(2200000))
        cuerpo.name = f"Cuerpo {i}"
    prs.save(ruta)


def test_agregar_tarjeta_reconoce_tarjetas_con_el_cuerpo_vacio(editor, tmp_path):
    from pptx import Presentation

    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck_con_tarjetas_vacias(origen)
    r = editor.editar(
        origen,
        salida,
        [
            {
                "agregar_tarjeta": {
                    "lamina_titulo": "Riesgos",
                    "titulo": "Dependencia de proveedor",
                    "puntos": ["Un solo proveedor"],
                }
            }
        ],
    )
    assert r["cambios"][0]["tarjetas_despues"] == 4
    textos = [sh.text_frame.text for sh in Presentation(str(salida)).slides[0].shapes if sh.has_text_frame]
    assert any("Dependencia de proveedor" in t for t in textos)
    assert any("Un solo proveedor" in t for t in textos)


def _plugin_pptx():
    import importlib.util as iu

    spec = iu.spec_from_file_location("pptx_plugin_borrado", PLUGIN / "__init__.py")
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _con_mensaje(mensaje):
    import json as j
    import os
    from unittest.mock import patch

    return patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": j.dumps({"metadata": {"mensaje_usuario": mensaje}})})


def test_eliminar_una_lamina_sin_que_el_usuario_lo_pida_se_rechaza():
    m = _plugin_pptx()
    args = {"path": "x.pptx", "operaciones": '[{"op":"eliminar_lamina","n":2}]'}
    with _con_mensaje("En la presentación agrega una lámina de cierre. No cambies nada más."):
        assert m._borra_laminas_sin_que_se_pida(args) is True
        salida = m._handle_edit_pptx(args)
    assert "no pidió eliminar" in salida and "agregar_lamina" in salida


def test_control_negativo_si_el_usuario_pide_eliminar_se_permite():
    m = _plugin_pptx()
    args = {"path": "x.pptx", "cambios": [{"eliminar_lamina": 2}]}
    for texto in ("elimina la lámina 2", "quita la última lámina", "borra la segunda diapositiva"):
        with _con_mensaje(texto):
            assert m._borra_laminas_sin_que_se_pida(args) is False


def test_otras_operaciones_no_se_bloquean():
    m = _plugin_pptx()
    with _con_mensaje("cambia el título"):
        assert (
            m._borra_laminas_sin_que_se_pida({"path": "x.pptx", "cambios": [{"titulo": {"lamina": 1, "nuevo": "a"}}]})
            is False
        )
