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
PEDIDO_REAL = (
    "Crea un dashboard interactivo en HTML con estos datos de tickets de soporte de enero a junio 2026: Hardware 12, "
    "15, 9, 14, 11, 8; Software 20, 18, 25, 22, 19, 24; Accesos 7, 9, 6, 10, 12, 9. Quiero un gráfico por mes, el "
    "total por categoría y un filtro por categoría. Adjúntalo aquí, no lo subas a OneDrive."
)


class TestSubidaProhibida(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("sharepoint_prohibida", PLUGIN)
        self.sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sp)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "outputs").mkdir()
        (Path(self.tmp.name) / "outputs" / "a.docx").write_bytes(b"PK docx")
        self.subidas = []

    def _subir(self, metadata, prompt="", pedido_anterior=""):
        def fake_put(url, data, token=None, content_range=None):
            self.subidas.append(url)
            return {"id": "1", "name": "a.docx", "webUrl": "https://od/x"}

        payload = {"metadata": metadata, "prompt": prompt, "pedido_anterior": pedido_anterior}
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload), "HERMES_SESSION_CWD": self.tmp.name}),
            patch.object(self.sp, "_task_user_id", return_value="u1"),
            patch.object(self.sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(self.sp, "_graph", return_value={"id": "d", "name": "Pruebas", "folder": {}}),
            patch.object(self.sp, "_put_binary", side_effect=fake_put),
        ):
            return json.loads(self.sp._upload_file_tool({"file_path": "outputs/a.docx", "folder": "Pruebas"}))

    def test_caso_real_en_el_trabajo_en_segundo_plano(self):
        salida = self._subir(
            {"background_job": True, "pedido_del_usuario": PEDIDO_REAL},
            prompt="Crea un archivo HTML autocontenido y guárdalo en OneDrive del usuario.",
        )
        self.assertFalse(salida["success"])
        self.assertIn("no lo subas a onedrive", salida["error"])
        self.assertIn("MEDIA:", salida["error"])
        self.assertEqual(self.subidas, [])

    def test_caso_real_en_el_chat(self):
        self.assertFalse(self._subir({"mensaje_usuario": PEDIDO_REAL})["success"])
        self.assertEqual(self.subidas, [])

    def test_frases_que_prohiben_subir(self):
        for frase in (
            "hazme el informe pero no lo subas.",
            "no lo subas todavía",
            "no subir a sharepoint",
            "créalo y no la guardes en tu OneDrive",
            "solo adjúntalo aquí",
        ):
            with self.subTest(frase=frase):
                self.assertFalse(self._subir({"mensaje_usuario": frase})["success"])
        self.assertEqual(self.subidas, [])

    def test_frases_que_no_prohiben_subir(self):
        for frase in (
            "súbelo a OneDrive en la carpeta Pruebas",
            "no lo guardes en Escritorio, guárdalo en la carpeta Pruebas",
            "no termines sin subirlo a la carpeta Pruebas",
            "si no lo puedo abrir te aviso; guárdalo en Pruebas",
            "adjúntalo aquí y guárdalo en Pruebas",
        ):
            with self.subTest(frase=frase):
                self.assertTrue(self._subir({"mensaje_usuario": frase})["success"])

    def test_la_prohibicion_sigue_en_los_turnos_siguientes_de_la_conversacion(self):
        sesion = {"public_session_id": "s1", "conversation_epoch": 1}
        self.assertFalse(self._subir({**sesion, "mensaje_usuario": PEDIDO_REAL})["success"])
        segundo = self._subir({**sesion, "mensaje_usuario": "agrégale un filtro por mes al dashboard"})
        self.assertFalse(segundo["success"])
        self.assertIn("no lo subas a onedrive", segundo["error"])
        self.assertEqual(self.subidas, [])

    def test_si_despues_pide_subirlo_se_sube_y_se_levanta_la_prohibicion(self):
        sesion = {"public_session_id": "s1", "conversation_epoch": 1}
        self._subir({**sesion, "mensaje_usuario": PEDIDO_REAL})
        self.assertTrue(self._subir({**sesion, "mensaje_usuario": "ahora sí, súbelo a OneDrive"})["success"])
        self.assertTrue(self._subir({**sesion, "mensaje_usuario": "y esta otra versión también"})["success"])

    def test_otra_conversacion_o_tras_reset_no_hereda_la_prohibicion(self):
        self._subir({"public_session_id": "s1", "conversation_epoch": 1, "mensaje_usuario": PEDIDO_REAL})
        otra = {"public_session_id": "s2", "conversation_epoch": 1, "mensaje_usuario": "guarda el informe en Pruebas"}
        self.assertTrue(self._subir(otra)["success"])
        reset = {"public_session_id": "s1", "conversation_epoch": 2, "mensaje_usuario": "guarda el informe en Pruebas"}
        self.assertTrue(self._subir(reset)["success"])

    def test_el_mensaje_anterior_que_lo_prohibio_vale_sin_marca(self):
        salida = self._subir({"mensaje_usuario": "agrégale un filtro"}, pedido_anterior=PEDIDO_REAL)
        self.assertFalse(salida["success"])

    def test_las_instrucciones_del_modelo_no_cuentan_como_pedido(self):
        salida = self._subir(
            {"background_job": True},
            prompt="Crea el informe en work/. No lo subas a SharePoint todavía.",
        )
        self.assertTrue(salida["success"], salida)


class TestAdjuntarAquiEsNoSubir(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("sharepoint_adjuntar_aqui", PLUGIN)
        self.sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sp)

    def test_adjuntalo_aqui_sin_pedir_subirlo_cuenta_como_no_subir(self):
        self.assertTrue(self.sp._prohibicion_en("Hazme un Excel con las ventas. Adjúntalo aquí."))
        self.assertTrue(self.sp._prohibicion_en("genera el informe y adjúntalo en el chat"))

    def test_si_pide_subirlo_o_nombra_onedrive_no_se_bloquea(self):
        self.assertFalse(self.sp._prohibicion_en("Hazme un Excel, súbelo a mi OneDrive y adjúntalo aquí."))
        self.assertFalse(self.sp._prohibicion_en("Guárdalo en OneDrive y adjúntalo aquí"))
        self.assertFalse(self.sp._prohibicion_en("Hazme un Excel con las ventas"))


if __name__ == "__main__":
    unittest.main()
