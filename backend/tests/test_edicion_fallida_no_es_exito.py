import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermeshq.scripts.hermes_task_runner as runner

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
RESPUESTA_REAL = (
    "Listo. Dashboard actualizado:\n\n1. **Título** cambiado a «Tickets de Soporte primer semestre 2026».\n2. **Nuevo "
    "KPI** «Promedio mensual» con el promedio de tickets por mes (250 tickets ÷ 6 meses ≈ 42).\n3. No toqué nada más: "
    "gráficos, tabla y resto del diseño quedan igual.\n\nMEDIA: uploads/22c84cab.html"
)


def _herramienta(i, nombre, contenido):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{i}", "function": {"name": nombre, "arguments": "{}"}}],
        },
        {"role": "tool", "name": nombre, "tool_call_id": f"c{i}", "content": json.dumps(contenido, ensure_ascii=False)},
    ]


MENSAJES = (
    [{"role": "user", "content": "cambia el título del dashboard y agrega un KPI"}]
    + _herramienta(1, "sharepoint_download_file", {"success": True, "local_path": "sharepoint_downloads/b773_x.html"})
    + _herramienta(2, "patch", {"error": "Bloqueado: no parchees a mano un HTML"})
    + _herramienta(3, "hq_dashboard_crear", {"success": False, "error": "formato 'entero' no válido"})
    + _herramienta(4, "sharepoint_upload_file", {"success": True, "url": "https://od.sharepoint.com/x 1.html"})
)


class TestVerificador(unittest.TestCase):
    def test_caso_real_subir_el_original_no_respalda_la_edicion(self):
        pendientes = runner.verificar_afirmaciones(
            RESPUESTA_REAL, MENSAJES, 0, mensaje="cambia el título del dashboard"
        )
        self.assertIn("archivo", [p["tipo"] for p in pendientes], pendientes)
        final = runner.respuesta_verificada(RESPUESTA_REAL, pendientes)
        self.assertIn("No se generó ni modificó ningún archivo", final)
        self.assertNotIn("Título", final)
        self.assertNotIn("MEDIA:", final)

    def test_con_la_herramienta_de_edicion_exitosa_no_se_marca(self):
        mensajes = MENSAJES[:-2] + _herramienta(
            5, "hq_dashboard_crear", {"success": True, "archivos": ["outputs/d.html"]}
        )
        pendientes = runner.verificar_afirmaciones(
            RESPUESTA_REAL, mensajes, 0, mensaje="cambia el título del dashboard"
        )
        self.assertNotIn("archivo", [p["tipo"] for p in pendientes])


class TestSubidaSinCambios(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "sharepoint_sin_cambios", _RAIZ / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
        )
        self.sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sp)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)

    def subir(self, ruta):
        with (
            patch.dict(
                os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {}}), "HERMES_SESSION_CWD": self.tmp.name}
            ),
            patch.object(self.sp, "_task_user_id", return_value="u1"),
            patch.object(self.sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.sp, "_graph", return_value={"id": "d", "name": "UltraBot-archivos", "folder": {}}),
            patch.object(self.sp, "_put_binary", return_value={"id": "1", "name": "x.html", "webUrl": "https://od/x"}),
        ):
            return json.loads(self.sp._upload_file_tool({"file_path": ruta, "folder": "UltraBot-archivos"}))

    def test_caso_real_el_original_descargado_sin_cambios_no_se_sube(self):
        ruta = self.sp._save_to_workspace("dashboard_tickets_sporte.html", b"<html>original</html>")
        salida = self.subir(ruta)
        self.assertFalse(salida["success"])
        self.assertIn("copia idéntica", salida["error"])

    def test_si_se_modifico_se_sube(self):
        ruta = self.sp._save_to_workspace("dashboard_tickets_sporte.html", b"<html>original</html>")
        (Path(self.tmp.name) / ruta).write_bytes(b"<html>con cambios</html>")
        self.assertTrue(self.subir(ruta)["success"])
