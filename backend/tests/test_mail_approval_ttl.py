import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_mail_ttl_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

FINGERPRINT = "fp-envio-marcela"
PREVIEW = {"accion": "enviar correo", "para": ["fsilva@contoso.cl"]}


class TestAprobacionEnvioVigencia(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        entorno = patch.dict(
            os.environ,
            {
                "HERMES_SESSION_CWD": self._tmp.name,
                "HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "T-nueva", "metadata": {}, "mensaje": "sí"}),
                "HERMESHQ_MODO_AUTOMATICO": "",
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        patch.object(plugin, "_modo_automatico", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _guardar_vista_previa(self, hace_segundos):
        plugin._save_previews({FINGERPRINT: {"task": "T-previa", "ts": time.time() - hace_segundos}})

    def test_aprobacion_de_una_vista_previa_de_hace_una_hora_se_acepta(self):
        self._guardar_vista_previa(60 * 60)
        gate = plugin._approval_gate({"aprobado_por_el_usuario": True}, FINGERPRINT, PREVIEW)
        self.assertIsNone(gate)

    def test_aprobacion_de_una_vista_previa_caducada_pide_otra_vez(self):
        self._guardar_vista_previa(5 * 60 * 60)
        gate = json.loads(plugin._approval_gate({"aprobado_por_el_usuario": True}, FINGERPRINT, PREVIEW))
        self.assertTrue(gate["requiere_aprobacion"])


class TestAprobacionPorTexto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        patch.object(plugin, "_modo_automatico", return_value=False).start()
        self.addCleanup(patch.stopall)
        plugin._save_previews({FINGERPRINT: {"task": "T-previa", "ts": time.time() - 600}})

    def _gate_con_mensaje(self, mensaje):
        entorno = patch.dict(
            os.environ,
            {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "T-nueva", "metadata": {}, "mensaje": mensaje})},
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        return plugin._approval_gate({}, FINGERPRINT, PREVIEW)

    def test_envia_en_un_mensaje_nuevo_aprueba_la_vista_previa(self):
        for mensaje in ("envia", "envialo", "si, envía", "Enviar", "envialo ya te lo había dicho"):
            with self.subTest(mensaje=mensaje):
                plugin._save_previews({FINGERPRINT: {"task": "T-previa", "ts": time.time() - 600}})
                self.assertIsNone(self._gate_con_mensaje(mensaje))

    def test_una_pregunta_o_un_si_suelto_no_aprueba(self):
        for mensaje in ("si", "envías?", "¿lo envío?", "cambia el asunto y después envía", "[Hora actual: 10:00] si"):
            with self.subTest(mensaje=mensaje):
                plugin._save_previews({FINGERPRINT: {"task": "T-previa", "ts": time.time() - 600}})
                gate = self._gate_con_mensaje(mensaje)
                self.assertIsNotNone(gate)
                self.assertTrue(json.loads(gate)["requiere_aprobacion"])


class TestFirmaNoSeArmaAMano(unittest.TestCase):
    def setUp(self):
        carpeta = tempfile.TemporaryDirectory()
        self.addCleanup(carpeta.cleanup)
        firma = Path(carpeta.name) / "work" / "firma"
        firma.mkdir(parents=True)
        (firma / "manifest.json").write_text(
            json.dumps({"personal": {"imagenes": [{"path": "work/firma/logo.png", "content_id": "logo_firma"}]}}),
            encoding="utf-8",
        )
        entorno = patch.dict(
            os.environ,
            {
                "HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "T1", "metadata": {}}),
                "HERMES_SESSION_CWD": carpeta.name,
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)
        for nombre, valor in (("_task_user_id", "u1"), ("_get_m365_token", ("tok", None))):
            parche = patch.object(plugin, nombre, return_value=valor)
            parche.start()
            self.addCleanup(parche.stop)

    def test_rechaza_firma_armada_a_mano_sin_parametro_firma(self):
        cuerpo = '<p>Hola</p><img src="cid:logo_firma"><img src="cid:otra">'
        res = json.loads(plugin._send_email_tool({"to": "ana@ejemplo.cl", "subject": "x", "body": cuerpo}))
        self.assertFalse(res["success"])
        self.assertIn('firma="personal"', res["error"])

    def test_con_firma_personal_no_se_rechaza_por_firma(self):
        cuerpo = '<p>Hola</p><img src="cid:logo_firma">'
        res = json.loads(
            plugin._send_email_tool({"to": "ana@ejemplo.cl", "subject": "x", "body": cuerpo, "firma": "personal"})
        )
        self.assertNotIn("armada a mano", res.get("error", ""))
        self.assertNotIn("La firma la agrega el servidor", res.get("error", ""))


if __name__ == "__main__":
    unittest.main()


class TestRegistroDeVistasPreviasNoPierdeEntradas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def test_guardar_una_vista_previa_no_borra_la_de_otra_escritura(self):
        ahora = time.time()
        plugin._save_previews({"A": {"task": "T1", "ts": ahora}})
        plugin._save_previews({"B": {"task": "T2", "ts": ahora}})
        self.assertEqual(set(plugin._load_previews()), {"A", "B"})

    def test_archivo_corrupto_no_impide_guardar(self):
        ruta = plugin._previews_path()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("{no es json", encoding="utf-8")
        plugin._save_previews({"C": {"task": "T3", "ts": time.time()}})
        self.assertEqual(set(plugin._load_previews()), {"C"})

    def test_no_queda_archivo_temporal(self):
        plugin._save_previews({"D": {"task": "T4", "ts": time.time()}})
        self.assertFalse(plugin._previews_path().with_name(plugin._previews_path().name + ".tmp").exists())
