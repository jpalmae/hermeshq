import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ms365_mail_get import _load

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
REAL_ID = "AQMkADAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMABGAAADbDNz7ba9ukaploOcsbVKZQcAiUd0wjrXV0Sm"


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _write(self, rel, data=b""):
        p = Path(self.tmp.name) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _manifest(self, data):
        self._write("work/firma/manifest.json", json.dumps(data).encode())


class TestIdRechazable(_Base):
    def test_cache_vacio_no_bloquea(self):
        self.assertIsNone(self.mod._id_rechazable("Wj5jWj5jWj5j"))

    def test_id_inventado_se_rechaza_con_cache(self):
        self.mod._remember_ids([{"id": REAL_ID}])
        msg = self.mod._id_rechazable("Wj5jWj5jWj5jWj5jWj5j")
        self.assertIsNotNone(msg)
        self.assertIn("no salió de una búsqueda", msg)

    def test_id_conocido_pasa(self):
        self.mod._remember_ids([{"id": REAL_ID}])
        self.assertIsNone(self.mod._id_rechazable(REAL_ID))

    def test_id_con_forma_real_pasa_aunque_no_este_en_cache(self):
        self.mod._remember_ids([{"id": REAL_ID}])
        self.assertIsNone(self.mod._id_rechazable("AAMkAGI2" + "a" * 100))

    def test_forward_rechaza_id_inventado(self):
        self.mod._remember_ids([{"id": REAL_ID}])
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
        ):
            out = json.loads(self.mod._forward_email_tool({"message_id": "Wj5jWj5jWj5j", "to": "a@x.cl"}))
        self.assertFalse(out["success"])
        self.assertIn("no salió de una búsqueda", out["error"])


class TestFirma(_Base):
    def test_firma_resuelve_imagenes_del_manifiesto(self):
        self._write("work/firma/logo.png", PNG)
        self._write("work/firma/banner.png", PNG)
        self._manifest(
            {
                "personal": [
                    {"content_id": "logo", "path": "work/firma/logo.png"},
                    {"content_id": "banner", "path": "work/firma/banner.png"},
                ]
            }
        )
        imgs, err = self.mod._firma_inline_images("personal")
        self.assertIsNone(err)
        self.assertEqual(sorted(a["contentId"] for a in imgs), ["banner", "logo"])

    def test_firma_inexistente_da_error_claro(self):
        self._manifest({"personal": []})
        imgs, err = self.mod._firma_inline_images("equipo")
        self.assertEqual(imgs, [])
        self.assertIn("No encontré la firma 'equipo'", err)

    def test_sin_manifiesto_da_error(self):
        imgs, err = self.mod._firma_inline_images("personal")
        self.assertEqual(imgs, [])
        self.assertIn("work/firma/manifest.json", err)

    def test_sin_firma_no_hace_nada(self):
        self.assertEqual(self.mod._firma_inline_images(""), ([], None))
        self.assertEqual(self.mod._firma_inline_images(None), ([], None))

    def test_send_con_firma_incluye_las_imagenes_sin_teclear_ruta(self):
        self._write("work/firma/logo.png", PNG)
        self._manifest({"personal": [{"content_id": "logo", "path": "work/firma/logo.png"}]})
        enviados = []

        def graph(method, path, token, payload=None, **kw):
            enviados.append((method, path, payload))
            return {}

        def correr(a, actual):
            with (
                patch.object(self.mod, "_current_task_id", return_value=actual),
                patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": actual, "mensaje": "sí"})}),
            ):
                return json.loads(self.mod._send_email_tool(a))

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            base = {"to": "a@x.cl", "subject": "s", "body": '<img src="cid:logo">', "firma": "personal"}
            correr(base, "tarea-1")
            out = correr({**base, "aprobado_por_el_usuario": True}, "tarea-2")
        self.assertTrue(out["success"], out)
        msg = [p for m, pth, p in enviados if pth == "/me/sendMail"][0]["message"]
        self.assertIn("logo", [a["contentId"] for a in msg.get("attachments", [])])


if __name__ == "__main__":
    unittest.main()
