import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
GUARDADO = "f6f876ec-3131-46eb-b306-2cbed708731f.xlsx"
MENSAJE_REAL = (
    "[Archivos adjuntos, ya disponibles en tu workspace:\n"
    f"- Pruebas.xlsx (6 KB) -> uploads/{GUARDADO}]\n\n"
    "NECESITO QUE GUARDES ESTE ARCHIVO EN LAS SIGUIENTES RUTAS 1.- C:\\Users\\usuario1\\OneDrive - Empresa\\"
    "Escritorio\\Ana\\Proyectos\\Demo\\Pruebas"
)


def _cargar():
    spec = importlib.util.spec_from_file_location("sharepoint_nombre_adjunto", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestNombreOriginal(unittest.TestCase):
    def setUp(self):
        self.sp = _cargar()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.uploads = Path(self.tmp.name) / "uploads"
        self.uploads.mkdir()
        (self.uploads / GUARDADO).write_bytes(b"PK\x03\x04 excel")

    def _con(self, mensaje, anterior=""):
        payload = {"mensaje": mensaje, "metadata": {"mensaje_usuario": mensaje}, "pedido_anterior": anterior}
        return patch.dict(
            os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload), "HERMES_SESSION_CWD": self.tmp.name}
        )

    def test_caso_real_sin_registro_el_nombre_sale_del_aviso_de_adjuntos(self):
        with self._con(MENSAJE_REAL):
            self.assertEqual(self.sp._nombre_original(self.uploads / GUARDADO), "Pruebas.xlsx")

    def test_en_un_seguimiento_sale_del_pedido_anterior(self):
        with self._con("sí, súbelo también a la otra carpeta", anterior=MENSAJE_REAL):
            self.assertEqual(self.sp._nombre_original(self.uploads / GUARDADO), "Pruebas.xlsx")

    def test_el_registro_manda_y_sin_datos_queda_el_guardado(self):
        (self.uploads / ".nombres.json").write_text(json.dumps({GUARDADO: "Pruebas v2.xlsx"}), encoding="utf-8")
        with self._con(MENSAJE_REAL):
            self.assertEqual(self.sp._nombre_original(self.uploads / GUARDADO), "Pruebas v2.xlsx")
        (self.uploads / ".nombres.json").unlink()
        with self._con("guarda el archivo"):
            self.assertEqual(self.sp._nombre_original(self.uploads / GUARDADO), GUARDADO)

    def test_caso_real_la_subida_usa_el_nombre_original(self):
        subidas = []

        def fake_put(url, data, token=None, content_range=None):
            subidas.append(url)
            return {"id": "1", "name": "Pruebas.xlsx", "webUrl": "https://od/x"}

        with (
            self._con(MENSAJE_REAL),
            patch.object(self.sp, "_task_user_id", return_value="u1"),
            patch.object(self.sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.sp, "_put_binary", side_effect=fake_put),
        ):
            salida = json.loads(
                self.sp._upload_file_tool(
                    {
                        "file_path": f"uploads/{GUARDADO}",
                        "folder": "Escritorio/Ana/Proyectos/Demo/Pruebas",
                    }
                )
            )
        self.assertTrue(salida["success"], salida)
        self.assertIn("/Demo/Pruebas/Pruebas.xlsx:/content", subidas[0])
        self.assertNotIn("f6f876ec", subidas[0])
