import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
CITA_GRAPH = (
    '<p>Hola</p><hr style="display:inline-block;width:98%" tabindex="-1">'
    '<div id="divRplyFwdMsg" dir="ltr"><font face="Calibri, sans-serif" style="font-size:11pt" color="#000000">'
    "<b>From:</b> Equipo Datos (Sede Central) &lt;equipo-datos@contoso.cl&gt;<br><b>Sent:</b> Thursday, 12 November 2026 "
    "16:02:32<br><b>To:</b> Ana Soto (Sede Central) &lt;asoto@contoso.cl&gt;<br><b>Subject:</b> "
    "Informe trimestral</font><div>&nbsp;</div></div><div>Estimada Ana. Sent: no tocar. From: tampoco.</div>"
)
ENVIADO = "2026-11-12T16:02:32Z"


def _cargar():
    spec = importlib.util.spec_from_file_location("ms365_mail_cita", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestCitaEnEspanol(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()

    def test_caso_real_encabezado_traducido_y_en_hora_de_chile(self):
        salida = self.mod._cita_en_espanol(CITA_GRAPH, ENVIADO)
        for etiqueta in ("<b>De:</b>", "<b>Enviado:</b> jueves, 12 de noviembre de 2026 13:02", "<b>Para:</b>"):
            self.assertIn(etiqueta, salida)
        self.assertIn("<b>Asunto:</b> Informe trimestral", salida)
        for ingles in ("<b>From:</b>", "<b>Sent:</b>", "16:02:32", "Thursday"):
            self.assertNotIn(ingles, salida)

    def test_el_resto_del_cuerpo_no_se_toca(self):
        salida = self.mod._cita_en_espanol(CITA_GRAPH, ENVIADO)
        self.assertTrue(salida.startswith("<p>Hola</p>"))
        self.assertIn("<div>Estimada Ana. Sent: no tocar. From: tampoco.</div>", salida)

    def test_sin_bloque_de_cita_o_sin_fecha(self):
        self.assertEqual(self.mod._cita_en_espanol("<p>sin cita</p>", ENVIADO), "<p>sin cita</p>")
        sin_fecha = self.mod._cita_en_espanol(CITA_GRAPH, None)
        self.assertIn("<b>Enviado:</b> Thursday, 12 November 2026 16:02:32", sin_fecha)

    def test_asunto_del_reenvio(self):
        self.assertEqual(self.mod._asunto_reenvio("Informe trimestral"), "RV: Informe trimestral")
        self.assertEqual(self.mod._asunto_reenvio("FW: Informe trimestral"), "RV: Informe trimestral")
        self.assertEqual(self.mod._asunto_reenvio("RV: fw: Informe trimestral"), "RV: Informe trimestral")


class TestReenvioPorBorrador(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.llamadas = []

    def _reenviar(self, error_borrador=None, cc=None, to=None):
        def graph(method, path, token, payload=None):
            self.llamadas.append((method, path, payload))
            if path.startswith("/me?"):
                return {"mail": "usuario1@contoso.cl"}
            if path.endswith("/createForward"):
                return {"error": error_borrador} if error_borrador else {"id": "D1"}
            if method == "GET" and path.startswith("/me/messages/D1"):
                return {"body": {"content": CITA_GRAPH}}
            if method == "GET":
                return {
                    "id": "M1",
                    "subject": "Informe trimestral",
                    "sentDateTime": ENVIADO,
                    "from": {"emailAddress": {"address": "equipo-datos@contoso.cl"}},
                }
            return {}

        datos = {"message_id": "M1", "to": to or ["persona.externa@example.com"], "aprobado_por_el_usuario": True}
        if cc:
            datos["cc"] = cc
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_token_herramienta", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_list_attachments", return_value=([], None)),
            patch.object(self.mod, "_approval_gate", return_value=None),
            patch.object(self.mod, "_aprobacion_central_activa", return_value=False),
            patch.object(self.mod, "_id_rechazable", return_value=None),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t2"})}),
        ):
            return json.loads(self.mod._forward_email_tool(datos))

    def test_caso_real_reenvio_con_rv_y_cita_en_espanol(self):
        salida = self._reenviar()
        self.assertTrue(salida["success"], salida)
        parche = [p for m, ruta, p in self.llamadas if m == "PATCH"][0]
        self.assertEqual(parche["subject"], "RV: Informe trimestral")
        self.assertIn("<b>Enviado:</b> jueves, 12 de noviembre de 2026 13:02", parche["body"]["content"])
        self.assertIn(("POST", "/me/messages/D1/send", None), self.llamadas)
        self.assertFalse([r for m, r, _ in self.llamadas if r.endswith("/M1/forward")])

    def test_caso_real_94afa468_reenviar_a_mi_propio_buzon(self):
        for destino in (["yo"], "mi propio buzón", ["a mí mismo"]):
            self.llamadas = []
            salida = self._reenviar(to=destino)
            self.assertTrue(salida["success"], (destino, salida))
            borrador = [p for m, ruta, p in self.llamadas if ruta.endswith("/createForward")][0]
            self.assertEqual(borrador["toRecipients"][0]["emailAddress"]["address"], "usuario1@contoso.cl")

    def test_caso_real_383dc18d_la_vista_previa_muestra_el_asunto_que_se_enviara(self):
        vistas = []
        with patch.object(
            self.mod, "_approval_gate", side_effect=lambda a, f, preview, *r: vistas.append(preview) or "{}"
        ):
            self.llamadas = []

            def graph(method, path, token, payload=None):
                return {
                    "id": "M1",
                    "subject": "Batería L16 C4",
                    "sentDateTime": ENVIADO,
                    "from": {"emailAddress": {"address": "a@b.cl"}},
                }

            with (
                patch.object(self.mod, "_task_user_id", return_value="u1"),
                patch.object(self.mod, "_token_herramienta", return_value=("tok", "")),
                patch.object(self.mod, "_graph", side_effect=graph),
                patch.object(self.mod, "_list_attachments", return_value=([], None)),
                patch.object(self.mod, "_resolver_directorio", return_value={}),
                patch.object(self.mod, "_aprobacion_central_activa", return_value=False),
                patch.object(self.mod, "_id_rechazable", return_value=None),
            ):
                self.mod._forward_email_tool({"message_id": "M1", "to": ["x@contoso.cl"]})
        self.assertEqual(vistas[0]["asunto"], "RV: Batería L16 C4")

    def test_sin_permiso_de_borrador_y_sin_cc_usa_el_reenvio_directo(self):
        salida = self._reenviar(error_borrador="403 Forbidden")
        self.assertTrue(salida["success"], salida)
        self.assertIn("/me/messages/M1/forward", [r for _, r, _ in self.llamadas])

    def test_sin_permiso_y_con_cc_explica(self):
        salida = self._reenviar(error_borrador="403 Forbidden", cc=["jefe@contoso.cl"])
        self.assertFalse(salida["success"])
        self.assertIn("Mail.ReadWrite", salida["error"])
