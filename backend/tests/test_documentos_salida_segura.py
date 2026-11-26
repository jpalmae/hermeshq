from __future__ import annotations

import importlib.util
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


def _cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(f"{nombre}_salida_segura", RAIZ / nombre / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


docx_p = _cargar("hermeshq_docx")
xlsx_p = _cargar("hermeshq_xlsx")
pptx_p = _cargar("hermeshq_pptx")
pdf_p = _cargar("hermeshq_pdf")


@pytest.fixture
def espacio(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    from PIL import Image

    Image.new("RGB", (60, 40), (10, 120, 200)).save(ws / "foto.png")
    Image.new("RGB", (60, 40), (200, 10, 10)).save(tmp_path / "ajena.png")
    with patch.dict(os.environ, {"HERMES_SESSION_CWD": str(ws)}):
        yield ws


def _docx(**args):
    return json.loads(
        docx_p._handle_generate_docx({"title": "Informe", "sections": [{"type": "paragraph", "text": "hola"}], **args})
    )


def _xlsx(**args):
    return json.loads(
        xlsx_p._handle_generate_xlsx(
            {"title": "Datos", "sheets": [{"name": "Hoja", "headers": ["a"], "rows": [[1]]}], **args}
        )
    )


def _pptx(**args):
    return json.loads(
        pptx_p._handle_generate_pptx({"title": "Deck", "slides": [{"title": "Uno", "bullets": ["x"]}], **args})
    )


def _pdf(**args):
    return json.loads(pdf_p._handle_generate_pdf({"title": "Acta", "html_content": "<p>Texto</p>", **args}))


GENERADORES = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "pdf": _pdf}


@pytest.mark.parametrize("ext", sorted(GENERADORES))
class TestSalidaSegura:
    def test_nombre_con_saltos_de_directorio_queda_en_output(self, espacio, tmp_path, ext):
        r = GENERADORES[ext](filename="../../fuera")
        assert r["success"] is True, r
        ruta = Path(r["path"]).resolve()
        assert ruta.parent == (espacio / "output").resolve()
        assert ruta.name == f"fuera.{ext}"
        assert not list(tmp_path.glob(f"fuera*.{ext}"))

    def test_nombre_absoluto_no_escribe_fuera(self, espacio, tmp_path, ext):
        destino = tmp_path / f"pisado.{ext}"
        r = GENERADORES[ext](filename=str(destino))
        assert r["success"] is True, r
        assert not destino.exists()
        assert Path(r["path"]).resolve().parent == (espacio / "output").resolve()

    def test_mismo_nombre_no_sobrescribe(self, espacio, ext):
        primero = GENERADORES[ext](filename="informe")
        contenido = Path(primero["path"]).read_bytes()
        segundo = GENERADORES[ext](filename="informe")
        assert primero["path"] != segundo["path"]
        assert Path(segundo["path"]).name == f"informe-2.{ext}"
        assert Path(primero["path"]).read_bytes() == contenido

    def test_concurrentes_con_el_mismo_nombre_no_se_pisan(self, espacio, ext):
        resultados = []
        hilos = [
            threading.Thread(target=lambda: resultados.append(GENERADORES[ext](filename="mismo"))) for _ in range(3)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        assert all(r["success"] for r in resultados), resultados
        assert len({r["path"] for r in resultados}) == 3
        assert not [p for p in (espacio / "output").iterdir() if p.name.startswith(".tmp-")]

    def test_fallo_al_generar_no_deja_archivos(self, espacio, ext):
        objetivo = {
            "docx": (docx_p, "_verificar_ooxml"),
            "xlsx": (xlsx_p, "_verificar_xlsx"),
            "pptx": (pptx_p, "_verificar_pptx"),
            "pdf": (pdf_p, "_verificar_pdf"),
        }[ext]

        def roto(_ruta):
            raise ValueError("el archivo generado está dañado")

        with patch.object(objetivo[0], objetivo[1], roto):
            r = GENERADORES[ext](filename="roto")
        assert r["success"] is False
        assert not list((espacio / "output").glob("*")) if (espacio / "output").exists() else True


class TestEntradasConfinadas:
    def test_docx_imagen_fuera_del_espacio(self, espacio, tmp_path):
        r = _docx(sections=[{"type": "image", "path": str(tmp_path / "ajena.png")}])
        assert r["success"] is False and "fuera del espacio" in r["error"]
        ok = _docx(sections=[{"type": "image", "path": "foto.png"}])
        assert ok["success"] is True, ok

    def test_docx_imagen_por_enlace_simbolico_hacia_fuera(self, espacio, tmp_path):
        (espacio / "atajo.png").symlink_to(tmp_path / "ajena.png")
        r = _docx(sections=[{"type": "image", "path": "atajo.png"}])
        assert r["success"] is False

    def test_xlsx_imagen_fuera_del_espacio(self, espacio, tmp_path):
        r = _xlsx(sheets=[{"name": "H", "headers": ["a"], "rows": [[1]], "images": [{"path": "../ajena.png"}]}])
        assert r["success"] is False

    def test_pptx_imagen_y_plantilla_fuera_del_espacio(self, espacio, tmp_path):
        r = _pptx(slides=[{"title": "x", "image_path": str(tmp_path / "ajena.png")}])
        assert r["success"] is False
        from pptx import Presentation

        Presentation().save(str(tmp_path / "plantilla-ajena.pptx"))
        r = json.loads(pptx_p._handle_from_template({"template": str(tmp_path / "plantilla-ajena.pptx")}))
        assert r["success"] is False and "fuera del espacio" in r["error"]

    def test_pptx_plantilla_propia_con_imagen_ajena(self, espacio, tmp_path):
        from pptx import Presentation

        Presentation().save(str(espacio / "plantilla.pptx"))
        r = json.loads(
            pptx_p._handle_from_template(
                {"template": "plantilla.pptx", "slides": [{"title": "x", "image_path": str(tmp_path / "ajena.png")}]}
            )
        )
        assert r["success"] is False and "fuera del espacio" in r["error"]
        assert not list((espacio / "output").glob("*.pptx")) if (espacio / "output").exists() else True


class TestPdf:
    def test_titulo_se_escapa(self, espacio):
        pedidos = []
        with patch.object(
            pdf_p,
            "_safe_url_fetcher",
            side_effect=lambda url, **k: pedidos.append(url) or {"string": b"", "mime_type": "text/plain"},
        ):
            r = _pdf(title='<img src="https://atacante.example/x.png">Acta')
        assert r["success"] is True, r
        assert pedidos == []

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://example.com/x.png",
            "https://127.0.0.1/x",
            "https://[::1]/x",
            "https://0.0.0.0/x",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.5/x",
            "https://localhost/x",
        ],
    )
    def test_recursos_internos_o_inseguros_no_se_descargan(self, url):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no debe salir a la red")):
            assert pdf_p._safe_url_fetcher(url) == {"string": b"", "mime_type": "text/plain"}

    def test_no_sigue_redirecciones(self):
        assert pdf_p._SinRedirecciones().redirect_request(None, None, 302, "Found", {}, "https://10.0.0.1/") is None

    def test_css_con_recursos_locales_no_los_lee(self, espacio, tmp_path):
        secreto = tmp_path / "secreto.css"
        secreto.write_text("body{background:red}")
        r = _pdf(css_content=f'@import url("file://{secreto}"); body {{ color: black }}')
        assert r["success"] is True, r


documentos_p = _cargar("hermeshq_documentos")
