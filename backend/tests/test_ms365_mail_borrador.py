import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load


class TestBorradorReal(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.dir.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.llamadas = []

    def _correr(self, herramienta, args, respuestas=None):
        def graph(method, path, token, payload=None, **kwargs):
            self.llamadas.append((method, path, payload))
            if respuestas and (method, path) in respuestas:
                return respuestas[(method, path)]
            if (method, path) == ("POST", "/me/messages"):
                return {"id": "borrador-1", "webLink": "https://outlook.office365.com/owa/?ItemID=borrador-1"}
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_current_task_id", return_value="tarea-1"),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "tarea-1", "mensaje": "x"})}),
        ):
            return json.loads(getattr(self.mod, herramienta)(args))

    ARGS = {"to": ["admin@proveedor.com"], "subject": "Borrador auditoría A-P3", "body": "Borrador de prueba."}

    def test_el_borrador_se_guarda_sin_enviar_ni_pedir_aprobacion(self):
        salida = self._correr("_create_draft_tool", {**self.ARGS, "firma": "ninguna"})
        self.assertTrue(salida["success"], salida)
        self.assertEqual(salida["estado"], "borrador_guardado")
        self.assertIn("NO se envió", salida["message"])
        self.assertEqual(salida["para"], ["admin@proveedor.com"])
        self.assertEqual(salida["asunto"], "Borrador auditoría A-P3")
        self.assertEqual(salida["enlace"], "https://outlook.office365.com/owa/?ItemID=borrador-1")
        metodos = [(m, p) for m, p, _ in self.llamadas]
        self.assertIn(("POST", "/me/messages"), metodos)
        self.assertFalse(any(p.endswith("/send") or p == "/me/sendMail" for _, p in metodos), metodos)
        cuerpo = next(c for m, p, c in self.llamadas if p == "/me/messages")
        self.assertEqual(cuerpo["subject"], "Borrador auditoría A-P3")
        self.assertEqual(cuerpo["toRecipients"][0]["emailAddress"]["address"], "admin@proveedor.com")

    def test_enviar_sigue_exigiendo_aprobacion_y_no_guarda_borrador(self):
        salida = self._correr("_send_email_tool", {**self.ARGS, "firma": "ninguna"})
        self.assertFalse(salida["success"])
        self.assertTrue(salida.get("requiere_aprobacion"))
        metodos = [(m, p) for m, p, _ in self.llamadas]
        self.assertNotIn(("POST", "/me/messages"), metodos)
        self.assertFalse(any(p == "/me/sendMail" for _, p in metodos))

    def test_el_modelo_no_puede_activar_el_borrador_desde_ms365_mail_send(self):
        salida = self._correr("_send_email_tool", {**self.ARGS, "firma": "ninguna", "solo_borrador": True})
        self.assertTrue(salida.get("requiere_aprobacion"), salida)
        self.assertNotIn(("POST", "/me/messages"), [(m, p) for m, p, _ in self.llamadas])

    def test_sin_destinatario_valido_no_guarda_nada(self):
        salida = self._correr("_create_draft_tool", {**self.ARGS, "to": ["Juan"], "firma": "ninguna"})
        self.assertFalse(salida["success"])
        self.assertEqual(self.llamadas, [])

    def test_sin_permiso_de_escritura_explica_el_motivo(self):
        salida = self._correr(
            "_create_draft_tool",
            {**self.ARGS, "firma": "ninguna"},
            respuestas={("POST", "/me/messages"): {"error": "403 Forbidden"}},
        )
        self.assertFalse(salida["success"])
        self.assertIn("Mail.ReadWrite", salida["error"])

    def test_si_falla_un_adjunto_se_borra_el_borrador_a_medias(self):
        ruta = os.path.join(self.dir.name, "work")
        os.makedirs(ruta, exist_ok=True)
        archivo = os.path.join(ruta, "nota.txt")
        with open(archivo, "w", encoding="utf-8") as f:
            f.write("hola")
        salida = self._correr(
            "_create_draft_tool",
            {**self.ARGS, "firma": "ninguna", "attachments": [{"path": "work/nota.txt"}]},
            respuestas={("POST", "/me/messages/borrador-1/attachments"): {"error": "500"}},
        )
        self.assertFalse(salida["success"], salida)
        self.assertIn(("DELETE", "/me/messages/borrador-1"), [(m, p) for m, p, _ in self.llamadas])

    def test_la_herramienta_esta_registrada_y_declarada(self):
        registradas = []

        class Ctx:
            def register_tool(self, **kw):
                registradas.append(kw["name"])

        self.mod.register(Ctx())
        self.assertIn("ms365_mail_create_draft", registradas)
        ruta = os.path.join(os.path.dirname(self.mod.__file__), "plugin.yaml")
        with open(ruta, encoding="utf-8") as f:
            self.assertIn("ms365_mail_create_draft", f.read())


if __name__ == "__main__":
    unittest.main()


class TestRespuestaNoSeEnviaComoCorreoNuevo(TestBorradorReal):
    def test_enviar_con_asunto_re_se_rechaza_y_apunta_a_reply(self):
        for asunto in ("Re: Informe trimestral", "RE: Informe trimestral", "  re : Informe trimestral", "RV: Informe"):
            self.llamadas.clear()
            salida = self._correr(
                "_send_email_tool",
                {**self.ARGS, "subject": asunto, "firma": "ninguna", "aprobado_por_el_usuario": True},
            )
            self.assertFalse(salida["success"], asunto)
            self.assertIn("ms365_mail_reply", salida["error"])
            self.assertEqual(self.llamadas, [], asunto)

    def test_control_negativo_un_asunto_normal_sigue_su_camino(self):
        for asunto in ("Resultados del trimestre", "Reunión de seguimiento", "Reporte: Re-evaluación"):
            salida = self._correr("_send_email_tool", {**self.ARGS, "subject": asunto, "firma": "ninguna"})
            self.assertTrue(salida.get("requiere_aprobacion"), (asunto, salida))

    def test_el_borrador_con_re_no_se_bloquea(self):
        salida = self._correr(
            "_create_draft_tool", {**self.ARGS, "subject": "Re: Informe trimestral", "firma": "ninguna"}
        )
        self.assertTrue(salida["success"], salida)
