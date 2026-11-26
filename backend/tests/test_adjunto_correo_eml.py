import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermeshq.routers import attachments
from hermeshq.services.correo_adjunto import CorreoIlegible, eml_a_texto
from hermeshq.services.workspace_manager import WorkspaceManager

CORREO = (
    "From: Ana Pérez <ana@ejemplo.cl>\r\n"
    "To: Beto <beto@ejemplo.cl>\r\n"
    "Subject: =?utf-8?q?Enlace_de_fibra_=C3=B3ptica?=\r\n"
    "Date: Tue, 18 Nov 2026 12:30:00 -0300\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
    "--XX\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n"
    "<html><head><style>p{color:red}</style></head><body><p>Hola Beto</p><script>alert(1)</script>"
    "<p>Necesito la cláusula 4.</p></body></html>\r\n"
    "--XX\r\n"
    'Content-Type: application/pdf; name="contrato.pdf"\r\n'
    'Content-Disposition: attachment; filename="contrato.pdf"\r\n'
    "Content-Transfer-Encoding: base64\r\n\r\n"
    "JVBERi0xLjQKJSVFT0YK\r\n"
    "--XX--\r\n"
).encode()


class TestEmlATexto(unittest.TestCase):
    def test_extrae_cabeceras_cuerpo_y_lista_adjuntos(self):
        texto = eml_a_texto(CORREO)
        self.assertIn("De: Ana Pérez <ana@ejemplo.cl>", texto)
        self.assertIn("Asunto: Enlace de fibra óptica", texto)
        self.assertIn("Hola Beto", texto)
        self.assertIn("Necesito la cláusula 4.", texto)
        self.assertIn("contrato.pdf", texto)

    def test_no_deja_pasar_scripts_ni_estilos(self):
        texto = eml_a_texto(CORREO)
        self.assertNotIn("alert", texto)
        self.assertNotIn("color:red", texto)
        self.assertNotIn("<p>", texto)

    def test_texto_plano(self):
        crudo = b"From: a@b.cl\r\nSubject: Hola\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nCuerpo simple\r\n"
        self.assertIn("Cuerpo simple", eml_a_texto(crudo))

    def test_basura_no_es_un_correo(self):
        with self.assertRaises(CorreoIlegible):
            eml_a_texto(b"\x00\x01\x02 no es un correo")


class TestSubidaDeEml(unittest.TestCase):
    def _cliente(self, raiz):
        from hermeshq.core.security import get_current_user
        from hermeshq.database import get_db_session

        app = FastAPI()
        app.include_router(attachments.router, prefix="/api")
        app.state.workspace_manager = WorkspaceManager(Path(raiz))
        app.state.workspace_manager.create_workspace("ag1", "Agente", None, None)
        usuario = type("U", (), {"id": "u1", "username": "u", "role": "user"})()
        app.dependency_overrides[get_current_user] = lambda: usuario
        app.dependency_overrides[get_db_session] = lambda: None
        return TestClient(app), app

    def test_eml_permitido_y_guardado_como_texto_legible(self):
        self.assertIn(".eml", attachments.ALLOWED_EXTENSIONS)
        with TemporaryDirectory() as raiz, patch.object(attachments, "ensure_agent_access", AsyncMock()):
            cliente, app = self._cliente(raiz)
            r = cliente.post("/api/agents/ag1/attachments", files={"file": ("Enlace.eml", CORREO, "message/rfc822")})
            self.assertEqual(r.status_code, 200, r.text)
            cuerpo = r.json()
            self.assertTrue(cuerpo["path"].endswith(".txt"))
            self.assertEqual(cuerpo["media_type"], "text/plain")
            guardado = app.state.workspace_manager.build_workspace_path("ag1") / "uploads" / Path(cuerpo["path"]).name
            self.assertIn("Necesito la cláusula 4.", guardado.read_text(encoding="utf-8"))
            self.assertEqual(len(list(guardado.parent.glob(f"{cuerpo['file_id']}.*"))), 1)

    def test_eml_ilegible_se_rechaza_con_motivo(self):
        with TemporaryDirectory() as raiz, patch.object(attachments, "ensure_agent_access", AsyncMock()):
            cliente, _ = self._cliente(raiz)
            r = cliente.post("/api/agents/ag1/attachments", files={"file": ("x.eml", b"\x00\x01", "message/rfc822")})
            self.assertEqual(r.status_code, 400)
            self.assertIn("No pude leer el correo", json.loads(r.text)["detail"])

    def test_msg_sigue_sin_aceptarse(self):
        self.assertNotIn(".msg", attachments.ALLOWED_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
