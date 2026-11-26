import pytest

from hermeshq.services import validar_adjunto as va

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f0005000100"
    "ffbe4ff40000000049454e44ae426082"
)


@pytest.fixture
def deck(tmp_path):
    pptx = pytest.importorskip("pptx")
    from pptx.util import Emu

    logo = tmp_path / "logo.png"
    logo.write_bytes(PNG)
    prs = pptx.Presentation()
    ancho, alto = prs.slide_width, prs.slide_height
    for _ in range(6):
        lamina = prs.slides.add_slide(prs.slide_layouts[6])
        lamina.shapes.add_textbox(
            Emu(ancho - 2200000), Emu(alto - 400000), Emu(2000000), Emu(300000)
        ).text_frame.text = "Confidencial · Área TI"
        lamina.shapes.add_picture(str(logo), Emu(100000), Emu(100000), Emu(350000), Emu(350000))
    laminas = list(prs.slides)
    laminas[0].shapes.add_textbox(Emu(500000), Emu(1500000), Emu(6000000), Emu(800000)).text_frame.text = "Portada"
    laminas[1].shapes.add_textbox(Emu(500000), Emu(500000), Emu(6000000), Emu(800000)).text_frame.text = "Pendiente"
    laminas[2].shapes.add_table(2, 2, Emu(500000), Emu(1500000), Emu(4000000), Emu(1000000))
    laminas[3].shapes.add_textbox(Emu(500000), Emu(300000), Emu(800000), Emu(300000)).text_frame.text = "4"
    ruta = tmp_path / "mixto.pptx"
    prs.save(str(ruta))
    return ruta


class TestDetectorDiscrimina:
    def test_solo_las_laminas_sin_nada_que_mostrar_se_marcan(self, deck):
        assert va._laminas_vacias(deck) == [4, 5, 6]

    def test_el_solo_titulo_pedido_no_se_marca_como_vacio(self, deck):
        assert 2 not in va._laminas_vacias(deck)

    def test_la_tabla_cuenta_como_contenido(self, deck):
        assert 3 not in va._laminas_vacias(deck)

    def test_el_numero_de_pagina_suelto_no_cuenta_como_contenido(self, deck):
        assert 4 in va._laminas_vacias(deck)

    def test_vacia_pedida_o_por_error_se_avisa_sin_rechazar_el_archivo(self, deck):
        resultado = va.validar(deck)
        assert resultado.valido
        assert any("láminas 4, 5 y 6 están vacías" in a for a in resultado.avisos), resultado.avisos
        assert not any("Pendiente" in a for a in resultado.avisos)


class TestLaPruebaDetectariaUnaRegresion:
    def test_si_el_pie_y_el_logo_contaran_como_contenido_no_se_veria_ninguna_vacia(self, deck, monkeypatch):
        monkeypatch.setattr(va, "_texto_decorativo", lambda *a, **k: False)
        monkeypatch.setattr(va, "AREA_DECORACION", 0.0)
        assert va._laminas_vacias(deck) != [4, 5, 6]

    def test_si_el_titulo_no_contara_como_contenido_el_solo_titulo_se_marcaria(self, deck, monkeypatch):
        original = va._tiene_contenido

        def sin_texto(forma, ancho, alto, pies):
            if getattr(forma, "has_text_frame", False) and forma.text_frame.text.strip():
                return False
            return original(forma, ancho, alto, pies)

        monkeypatch.setattr(va, "_tiene_contenido", sin_texto)
        assert 2 in va._laminas_vacias(deck)

    def test_si_la_tabla_no_contara_como_contenido_se_marcaria_vacia(self, deck, monkeypatch):
        original = va._tiene_contenido

        def sin_tabla(forma, ancho, alto, pies):
            if getattr(forma, "has_table", False):
                return False
            return original(forma, ancho, alto, pies)

        monkeypatch.setattr(va, "_tiene_contenido", sin_tabla)
        assert 3 in va._laminas_vacias(deck)

    def test_si_el_detector_no_hiciera_nada_las_vacias_no_se_avisarian(self, deck, monkeypatch):
        monkeypatch.setattr(va, "_laminas_vacias", lambda ruta: [])
        assert not any("vacía" in a for a in va.validar(deck).avisos)
