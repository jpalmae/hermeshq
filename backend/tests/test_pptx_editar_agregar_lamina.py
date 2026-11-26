import importlib.util
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_pptx"


def _editor():
    spec = importlib.util.spec_from_file_location("editar_lamina_prueba", PLUGIN / "editar.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def editor():
    pytest.importorskip("pptx")
    return _editor()


def _deck(ruta: Path):
    from pptx import Presentation

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Portada"
    for titulo, puntos in (("Objetivos", ["Uno", "Dos"]), ("Riesgos", ["Alto", "Medio", "Bajo"])):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = titulo
        marco = s.placeholders[1].text_frame
        marco.text = puntos[0]
        for p in puntos[1:]:
            marco.add_paragraph().text = p
    prs.save(ruta)


def _textos(ruta: Path):
    from pptx import Presentation

    return [
        [sh.text_frame.text for sh in s.shapes if getattr(sh, "has_text_frame", False) and sh.has_text_frame]
        for s in Presentation(str(ruta)).slides
    ]


def test_agrega_una_lamina_al_final_y_las_demas_quedan_iguales(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    antes = _textos(origen)
    r = editor.editar(
        origen, salida, [{"agregar_lamina": {"titulo": "Cierre", "puntos": ["Gracias", "Próximos pasos acordados"]}}]
    )
    assert r["laminas_antes"] == 3 and r["laminas_despues"] == 4
    despues = _textos(salida)
    assert despues[:3] == antes
    assert despues[3][0] == "Cierre"
    assert "Gracias\nPróximos pasos acordados" in despues[3]


def test_la_lamina_nueva_no_lleva_marca_de_viñeta_en_el_texto(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    editor.editar(origen, salida, [{"agregar_lamina": {"titulo": "Cierre", "puntos": ["• Gracias", "- Fin"]}}])
    assert "Gracias\nFin" in _textos(salida)[3]


def test_acepta_el_formato_op(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    r = editor.editar(origen, salida, [{"op": "agregar_lamina", "titulo": "Cierre", "puntos": ["Gracias"]}])
    assert r["laminas_despues"] == 4


def test_despues_ubica_la_lamina(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    editor.editar(origen, salida, [{"agregar_lamina": {"titulo": "Nueva", "puntos": ["x"], "despues": 1}}])
    assert _textos(salida)[1][0] == "Nueva"
    assert _textos(salida)[2][0] == "Objetivos"


def test_sin_titulo_se_rechaza(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    with pytest.raises(editor.EdicionRechazada):
        editor.editar(origen, salida, [{"agregar_lamina": {"puntos": ["x"]}}])


def test_agregar_lamina_y_cambiar_otra_cosa_a_la_vez(editor, tmp_path):
    origen, salida = tmp_path / "o.pptx", tmp_path / "s.pptx"
    _deck(origen)
    r = editor.editar(
        origen,
        salida,
        [{"agregar_lamina": {"titulo": "Cierre", "puntos": ["Gracias"]}}, {"titulo": {"lamina": 2, "nuevo": "Metas"}}],
    )
    assert r["laminas_despues"] == 4
    assert _textos(salida)[1][0] == "Metas"
