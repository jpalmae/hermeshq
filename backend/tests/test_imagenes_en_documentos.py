import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"


def _cargar(nombre):
    spec = importlib.util.spec_from_file_location(f"{nombre}_imagenes", _RAIZ / nombre / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


pdf = _cargar("hermeshq_pdf")
pptx = _cargar("hermeshq_pptx")


class _ConEspacio(unittest.TestCase):
    def setUp(self):
        from PIL import Image

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "uploads").mkdir()
        Image.new("RGB", (320, 240), (30, 120, 200)).save(self.ws / "uploads" / "perro-superman-1a2b3c4d.png")
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": str(self.ws)})
        entorno.start()
        self.addCleanup(entorno.stop)


class TestPdfConImagenes(_ConEspacio):
    def generar(self, html):
        return json.loads(pdf._handle_generate_pdf({"title": "Prueba", "html_content": html}))

    def imagenes(self, ruta):
        import pymupdf

        return sum(len(pagina.get_images()) for pagina in pymupdf.open(ruta))

    def test_la_imagen_del_espacio_de_trabajo_queda_dentro_del_pdf(self):
        out = self.generar('<p>Imagen:</p><img src="uploads/perro-superman-1a2b3c4d.png" width="300">')
        self.assertTrue(out["success"], out)
        self.assertEqual(self.imagenes(out["path"]), 1)

    def test_una_imagen_que_no_existe_no_da_un_pdf_sin_ella(self):
        out = self.generar('<img src="uploads/no-existe.png">')
        self.assertFalse(out["success"])
        self.assertIn("uploads/no-existe.png", out["error"])
        self.assertFalse(any((self.ws / "output").glob("*.pdf")) if (self.ws / "output").exists() else False)

    def test_un_archivo_fuera_del_espacio_de_trabajo_no_se_incrusta(self):
        out = self.generar('<img src="/etc/hostname"><img src="../../etc/passwd">')
        self.assertFalse(out["success"])
        self.assertIn("/etc/hostname", out["error"])

    def test_un_archivo_que_no_es_imagen_no_se_incrusta(self):
        (self.ws / "uploads" / "notas.txt").write_text("secreto", encoding="utf-8")
        out = self.generar('<img src="uploads/notas.txt">')
        self.assertFalse(out["success"])

    def test_una_imagen_remota_que_no_se_pudo_descargar_no_da_un_pdf_sin_ella(self):
        vacio = {"string": b"", "mime_type": "text/plain"}
        with patch.object(pdf, "_safe_url_fetcher", lambda url, *a, **k: vacio):
            out = self.generar('<img src="https://ejemplo.invalid/foto.png">')
        self.assertFalse(out["success"])
        self.assertIn("https://ejemplo.invalid/foto.png", out["error"])
        self.assertEqual(list((self.ws / "output").glob("*.pdf")), [])

    def test_caso_real_b9d38deb_una_pagina_con_titulo_unico_e_imagen_proporcionada(self):
        from PIL import Image

        Image.new("RGB", (1024, 1024), (200, 60, 40)).save(self.ws / "uploads" / "perro.jpg")
        html = (
            '<h1 style="text-align:center;">Mascota del proyecto</h1><p>Presentamos a la mascota del equipo.</p>'
            '<img src="uploads/perro.jpg" style="max-width:480px; width:100%;" alt="Perro">'
        )
        out = json.loads(pdf._handle_generate_pdf({"title": "Mascota del proyecto", "html_content": html}))
        self.assertTrue(out["success"], out)
        import pymupdf

        doc = pymupdf.open(out["path"])
        self.assertEqual(len(doc), 1)
        self.assertEqual(doc[0].get_text().count("Mascota del proyecto"), 1)
        caja = doc[0].get_image_bbox(doc[0].get_images(full=True)[0])
        self.assertAlmostEqual(caja.width, caja.height, delta=2)
        self.assertEqual(out["adjuntar_en_la_respuesta"], [f"MEDIA:output/{Path(out['path']).name}"])

    def test_con_css_propio_la_imagen_sigue_acotada(self):
        from PIL import Image

        Image.new("RGB", (1024, 1024), (200, 60, 40)).save(self.ws / "uploads" / "perro.jpg")
        out = json.loads(
            pdf._handle_generate_pdf(
                {"title": "T", "html_content": '<img src="uploads/perro.jpg">', "css_content": "body { color: #222; }"}
            )
        )
        import pymupdf

        self.assertEqual(len(pymupdf.open(out["path"])), 1)

    def test_otro_titulo_al_inicio_no_quita_el_del_documento(self):
        out = json.loads(pdf._handle_generate_pdf({"title": "Informe", "html_content": "<h1>Resumen</h1><p>x</p>"}))
        import pymupdf

        texto = pymupdf.open(out["path"])[0].get_text()
        self.assertIn("Informe", texto)
        self.assertIn("Resumen", texto)

    def test_sin_imagenes_sigue_igual(self):
        out = self.generar("<p>Sin imágenes</p>")
        self.assertTrue(out["success"], out)


class TestPptxConImagen(_ConEspacio):
    def test_la_clave_image_tambien_pone_la_imagen(self):
        out = json.loads(
            pptx._handle_generate_pptx(
                {"title": "Prueba", "slides": [{"title": "Con imagen", "image": "uploads/perro-superman-1a2b3c4d.png"}]}
            )
        )
        self.assertTrue(out["success"], out)
        medios = [n for n in zipfile.ZipFile(out["path"]).namelist() if n.startswith("ppt/media/")]
        self.assertEqual(len(medios), 1)
        self.assertEqual(out["adjuntar_en_la_respuesta"], [f"MEDIA:output/{Path(out['path']).name}"])


if __name__ == "__main__":
    unittest.main()
