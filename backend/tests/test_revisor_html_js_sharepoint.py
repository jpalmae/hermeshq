from pathlib import Path

import pytest

import hermeshq.scripts.hermes_task_runner as runner
from tests.test_revisor_html_js import (
    _RAIZ,
    CLIC,
    CON_CDN,
    ELEMENTO_INEXISTENTE,
    FALLA_AL_CLIC,
    PAGINA_SANA,
    ROTO,
    TERNARIO_ROTO,
    _cargar,
    _revisar,
    requiere_jsdom,
)

SP = _cargar("sharepoint_revisor_js", _RAIZ / "integration_packages" / "sharepoint" / "plugin" / "__init__.py")


@requiere_jsdom
class TestRevisorNavegador:
    def test_elemento_inexistente_al_cargar(self, tmp_path):
        revision = _revisar(tmp_path, ELEMENTO_INEXISTENTE)
        assert revision["modo"] == "jsdom"
        error = revision["errores_carga"][0]
        assert "reading 'addEventListener'" in error["mensaje"]
        assert error["linea"] == 4
        assert error["codigo"] == "sb.addEventListener('transitionend', function(){});"

    def test_error_al_hacer_clic_nombra_el_boton(self, tmp_path):
        revision = _revisar(tmp_path, FALLA_AL_CLIC)
        assert revision["errores_carga"] == []
        error = revision["errores_clic"][0]
        assert "reading 'forEach'" in error["mensaje"]
        assert error["elemento"] == "button#reset2 «⟲ Restablecer»"

    def test_pagina_sana_con_canvas_svg_y_buscador_no_da_errores(self, tmp_path):
        revision = _revisar(tmp_path, PAGINA_SANA)
        assert revision["errores_carga"] == []
        assert revision["errores_clic"] == []
        assert revision["clics"] >= 2

    def test_bibliotecas_de_cdn_no_se_marcan_como_error(self, tmp_path):
        revision = _revisar(tmp_path, CON_CDN)
        assert revision["scripts_remotos"] is True
        assert revision["errores_carga"] == []

    def test_ruta_relativa_al_workspace_del_caso_en_vivo(self, tmp_path, monkeypatch):
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "prueba_js.html").write_text(TERNARIO_ROTO, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        pendientes = runner.verificar_afirmaciones(
            "Archivo creado en `outputs/prueba_js.html`.\n\nMEDIA:outputs/prueba_js.html", [], 0, cwd=str(tmp_path)
        )
        assert [p["tipo"] for p in pendientes] == ["html_roto"]
        assert "línea 4: SyntaxError" in pendientes[0]["detalle"]
        assert "línea 4: SyntaxError" in SP._html_con_errores_js(Path("outputs/prueba_js.html"))

    def test_resultado_se_reutiliza_si_el_archivo_no_cambia(self, tmp_path, monkeypatch):
        ruta = tmp_path / "p.html"
        ruta.write_text(PAGINA_SANA, encoding="utf-8")
        primera = runner.revisar_html_js(ruta)
        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("revisó dos veces"))
        assert runner.revisar_html_js(ruta) is primera


class TestSubidaOneDrive:
    def test_html_roto_no_se_sube(self, tmp_path, monkeypatch):
        ruta = tmp_path / "Estructura_Demo_GRUPO.html"
        ruta.write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(SP, "_revisar_html_js", lambda _p: ROTO)
        error = SP._html_con_errores_js(ruta)
        assert error.startswith("No subí 'Estructura_Demo_GRUPO.html'")
        assert "línea 852: SyntaxError" in error
        assert "No digas que quedó subido" in error

    def test_error_solo_al_clic_no_impide_la_subida(self, tmp_path, monkeypatch):
        ruta = tmp_path / "p.html"
        ruta.write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(SP, "_revisar_html_js", lambda _p: CLIC)
        assert SP._html_con_errores_js(ruta) is None

    def test_descarga_del_usuario_y_otros_formatos_no_se_revisan(self, tmp_path, monkeypatch):
        (tmp_path / "sharepoint_downloads").mkdir()
        descargado = tmp_path / "sharepoint_downloads" / "suyo.html"
        descargado.write_text("<html></html>", encoding="utf-8")
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF")
        monkeypatch.setattr(SP, "_revisar_html_js", lambda _p: pytest.fail("revisó"))
        assert SP._html_con_errores_js(descargado) is None
        assert SP._html_con_errores_js(pdf) is None

    @requiere_jsdom
    def test_revision_real_del_caso(self, tmp_path):
        ruta = tmp_path / "Explorador.html"
        ruta.write_text(TERNARIO_ROTO, encoding="utf-8")
        assert "línea 4: SyntaxError: Unexpected token ':'" in SP._html_con_errores_js(ruta)

    def test_la_subida_usa_la_revision(self):
        fuente = (_RAIZ / "integration_packages" / "sharepoint" / "plugin" / "__init__.py").read_text(encoding="utf-8")
        assert "or _html_con_errores_js(path)" in fuente
