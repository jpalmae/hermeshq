import importlib.util
import json
import os
import tempfile
import unicodedata
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from hermeshq.services import graph_proxy as gp

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages"


def _cargar(paquete: str, nombre: str):
    spec = importlib.util.spec_from_file_location(nombre, _RAIZ / paquete / "plugin" / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


SP = _cargar("sharepoint", "sharepoint_nombres_onedrive")
MAIL = _cargar("ms365-mail", "mail_nombres_onedrive")

NOMBRE = "Ventas Ñuñoa 2026.xlsx"
CODIFICADO = "Ventas%20%C3%91u%C3%B1oa%202026.xlsx"


class TestNombreSeguro(unittest.TestCase):
    def test_tildes_y_enie_se_conservan(self):
        for modulo in (SP, MAIL):
            self.assertEqual(modulo._safe_drive_name(NOMBRE), NOMBRE)
            self.assertEqual(modulo._safe_drive_name("Análisis de migración.pdf"), "Análisis de migración.pdf")

    def test_forma_descompuesta_se_normaliza(self):
        descompuesto = unicodedata.normalize("NFD", NOMBRE)
        self.assertNotEqual(descompuesto, NOMBRE)
        self.assertEqual(SP._safe_drive_name(descompuesto), NOMBRE)

    def test_caracteres_prohibidos_por_onedrive_se_reemplazan(self):
        for modulo in (SP, MAIL):
            self.assertEqual(
                modulo._safe_drive_name('Informe: Q1/2026 | "v2"*?<>.pdf'), "Informe_ Q1_2026 _ _v2_____.pdf"
            )
            self.assertEqual(modulo._safe_drive_name("a#b%c.pdf"), "a_b_c.pdf")
            self.assertEqual(modulo._safe_drive_name("linea\nnueva\t.pdf"), "linea_nueva_.pdf")

    def test_bordes_y_casos_vacios(self):
        self.assertEqual(SP._safe_drive_name("  . informe.pdf .  "), "informe.pdf")
        self.assertEqual(SP._safe_drive_name("~$borrador.docx"), "_borrador.docx")
        self.assertEqual(SP._safe_drive_name(" . "), "archivo")
        self.assertEqual(SP._safe_drive_name(None), "archivo")
        self.assertEqual(len(SP._safe_drive_name("x" * 300 + ".pdf")), 120)

    def test_sin_recorrido_de_rutas(self):
        nombre = SP._safe_drive_name("../../secreto.txt")
        self.assertNotIn("/", nombre)
        self.assertNotIn("\\", SP._safe_drive_name("..\\..\\secreto.txt"))


class TestSubidaConTildes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        (Path(self.tmp.name) / "output").mkdir()
        (Path(self.tmp.name) / "output" / NOMBRE).write_bytes(b"xlsx")

    def test_subida_directa_codifica_el_nombre_real(self):
        urls = []

        def put(url, data, token=None, content_range=None):
            urls.append(url)
            return {"id": "1", "name": NOMBRE, "webUrl": "https://od/x"}

        with (
            patch.object(SP, "_task_user_id", return_value="u1"),
            patch.object(SP, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(SP, "_put_binary", side_effect=put),
        ):
            out = json.loads(SP._upload_file_tool({"file_path": f"output/{NOMBRE}", "folder": "Escritorio"}))
        self.assertTrue(out["success"], out)
        self.assertIn(f"/me/drive/root:/Escritorio/{CODIFICADO}:/content", urls[0])
        self.assertNotIn("__u_oa", urls[0])

    def test_por_el_broker_la_ruta_llega_codificada_una_sola_vez_y_se_acepta(self):
        rutas = []

        def broker(metodo, ruta, cuerpo_json=None, cuerpo_bytes=None, confirmacion_compartir=None):
            rutas.append((metodo, ruta))
            return {"json": {"id": "1", "name": NOMBRE, "webUrl": "https://od/x"}}

        with (
            patch.object(SP, "_task_user_id", return_value="u1"),
            patch.object(SP, "_get_m365_token", return_value=(SP.BROKER_TOKEN, None, "")),
            patch.object(SP, "_broker_request", side_effect=broker),
        ):
            out = json.loads(SP._upload_file_tool({"file_path": f"output/{NOMBRE}", "folder": "Escritorio"}))
        self.assertTrue(out["success"], out)
        consultas = [r for m, r in rutas if m == "GET"]
        self.assertTrue(consultas and all(gp.clase_de("GET", r) != "denegada" for r in consultas), rutas)
        metodo, ruta = next((m, r) for m, r in rutas if m != "GET")
        self.assertEqual(metodo, "PUT")
        self.assertIn(f"/me/drive/root:/Escritorio/{CODIFICADO}:/content", ruta)
        url = gp.GRAPH_BASE + ruta
        self.assertEqual(urllib.parse.quote(url, safe=":/?=&$,'()*+;@!~[]#%"), url)
        self.assertNotEqual(gp.clase_de("PUT", ruta), "denegada")
        self.assertEqual(urllib.parse.unquote(ruta.split("/Escritorio/")[1].split(":/content")[0]), NOMBRE)


class TestCopiaDelChatConservaSuNombre(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.uploads = Path(self.tmp.name) / "uploads"
        self.uploads.mkdir()
        (self.uploads / "b4b786c6-0ac2-492c-bde8-06214dee0f5a.xlsx").write_bytes(b"xlsx")

    def _subir(self):
        urls = []

        def put(url, data, token=None, content_range=None):
            urls.append(url)
            return {"id": "1", "name": "x", "webUrl": "https://od/x"}

        with (
            patch.object(SP, "_task_user_id", return_value="u1"),
            patch.object(SP, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(SP, "_put_binary", side_effect=put),
        ):
            out = json.loads(
                SP._upload_file_tool(
                    {"file_path": "uploads/b4b786c6-0ac2-492c-bde8-06214dee0f5a.xlsx", "folder": "Pruebas HermesHQ"}
                )
            )
        self.assertTrue(out["success"], out)
        return urls[0]

    def test_caso_real_la_copia_uploads_sube_con_su_nombre_original(self):
        from hermeshq.services.hermes_runtime import registrar_nombre_original

        registrar_nombre_original(
            self.uploads, "b4b786c6-0ac2-492c-bde8-06214dee0f5a.xlsx", "Ventas_Ñuñoa_Peñalolén_2026.xlsx"
        )
        url = self._subir()
        self.assertIn(urllib.parse.quote("Ventas_Ñuñoa_Peñalolén_2026.xlsx"), url)
        self.assertNotIn("b4b786c6", url)

    def test_sin_registro_se_mantiene_el_nombre_de_la_copia(self):
        self.assertIn("b4b786c6-0ac2-492c-bde8-06214dee0f5a.xlsx", self._subir())

    def test_el_registro_acumula_y_tolera_un_archivo_danado(self):
        from hermeshq.services.hermes_runtime import registrar_nombre_original

        (self.uploads / ".nombres.json").write_text("{dañado", encoding="utf-8")
        registrar_nombre_original(self.uploads, "a.xlsx", "Uno.xlsx")
        registrar_nombre_original(self.uploads, "b.pdf", "Dos.pdf")
        nombres = json.loads((self.uploads / ".nombres.json").read_text(encoding="utf-8"))
        self.assertEqual(nombres, {"a.xlsx": "Uno.xlsx", "b.pdf": "Dos.pdf"})


if __name__ == "__main__":
    unittest.main()


class TestNombreConExtension(unittest.TestCase):
    def test_caso_real_nombre_sin_extension_recibe_la_del_archivo(self):
        ruta = Path("/ws/outputs/Estructura_Report_Server.html")
        self.assertEqual(SP._nombre_con_extension("Estructura Demo", ruta), "Estructura Demo.html")

    def test_nombre_con_la_misma_extension_no_cambia(self):
        ruta = Path("/ws/outputs/informe.html")
        self.assertEqual(SP._nombre_con_extension("Estructura Demo.HTML", ruta), "Estructura Demo.HTML")
        self.assertEqual(SP._nombre_con_extension(NOMBRE, Path("/ws/output/v.xlsx")), NOMBRE)

    def test_punto_dentro_del_nombre_no_es_extension(self):
        ruta = Path("/ws/output/plan.xlsx")
        self.assertEqual(SP._nombre_con_extension("Plan v1.2", ruta), "Plan v1.2.xlsx")

    def test_archivo_local_sin_extension_no_inventa_una(self):
        self.assertEqual(SP._nombre_con_extension("Notas", Path("/ws/work/notas")), "Notas")
