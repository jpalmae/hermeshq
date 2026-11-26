import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ms365_mail_get import _load

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SIG = '<div id="firma">FIRMA EMPRESA<img src="cid:logo"><img src="cid:banner"></div>'


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

    def _imgs(self):
        self._write("work/firma/logo.png", PNG)
        self._write("work/firma/banner.png", PNG)

    def _entrada_imagenes(self):
        return [
            {"content_id": "logo", "path": "work/firma/logo.png"},
            {"content_id": "banner", "path": "work/firma/banner.png"},
        ]


class TestFirmaConfig(_Base):
    def test_html_inline(self):
        self._imgs()
        self._manifest({"personal": {"imagenes": self._entrada_imagenes(), "html": SIG}})
        imgs, html, err = self.mod._firma_config("personal")
        self.assertIsNone(err)
        self.assertEqual(sorted(a["contentId"] for a in imgs), ["banner", "logo"])
        self.assertEqual(html, SIG)

    def test_html_path(self):
        self._imgs()
        self._write("work/firma/sig.html", SIG.encode())
        self._manifest({"personal": {"imagenes": self._entrada_imagenes(), "html_path": "work/firma/sig.html"}})
        imgs, html, err = self.mod._firma_config("personal")
        self.assertIsNone(err)
        self.assertEqual(html, SIG)

    def test_html_path_fuera_del_cwd_da_error(self):
        self._imgs()
        self._manifest({"personal": {"imagenes": self._entrada_imagenes(), "html_path": "../../../etc/passwd"}})
        _imgs, _html, err = self.mod._firma_config("personal")
        self.assertIsNotNone(err)
        self.assertIn("fuera del directorio", err)

    def test_formato_viejo_lista_sigue_funcionando(self):
        self._imgs()
        self._manifest({"personal": self._entrada_imagenes()})
        imgs, html, err = self.mod._firma_config("personal")
        self.assertIsNone(err)
        self.assertEqual(sorted(a["contentId"] for a in imgs), ["banner", "logo"])
        self.assertIsNone(html)

    def test_inexistente_da_error(self):
        self._manifest({"personal": {"imagenes": [], "html": SIG}})
        _imgs, _html, err = self.mod._firma_config("equipo")
        self.assertIn("No encontré la firma 'equipo'", err)

    def test_inline_images_wrapper_compat(self):
        self._imgs()
        self._manifest({"personal": {"imagenes": self._entrada_imagenes(), "html": SIG}})
        imgs, err = self.mod._firma_inline_images("personal")
        self.assertIsNone(err)
        self.assertEqual(sorted(a["contentId"] for a in imgs), ["banner", "logo"])


class TestAplicarFirmaHtml(_Base):
    def test_agrega_cuando_falta(self):
        firma_imgs = [{"contentId": "logo"}, {"contentId": "banner"}]
        out = self.mod._aplicar_firma_html("<p>Hola</p>", firma_imgs, SIG)
        self.assertIn("FIRMA EMPRESA", out)
        self.assertIn("<p>Hola</p>", out)

    def test_no_duplica_si_ya_esta(self):
        firma_imgs = [{"contentId": "logo"}, {"contentId": "banner"}]
        body = "<p>Hola</p>" + SIG
        out = self.mod._aplicar_firma_html(body, firma_imgs, SIG)
        self.assertEqual(out, body)
        self.assertEqual(out.count("FIRMA EMPRESA"), 1)

    def test_sin_html_no_toca(self):
        out = self.mod._aplicar_firma_html("<p>Hola</p>", [{"contentId": "logo"}], None)
        self.assertEqual(out, "<p>Hola</p>")

    def test_cuerpo_vacio_queda_solo_firma(self):
        out = self.mod._aplicar_firma_html("", [{"contentId": "logo"}], SIG)
        self.assertEqual(out, SIG)


class TestSendInyectaFirma(_Base):
    def _correr(self, args, actual):
        mensaje = "sí" if args.get("aprobado_por_el_usuario") else "prepáralo"
        carga = json.dumps({"task_id": actual, "metadata": {}, "mensaje": mensaje})
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
            patch.object(self.mod, "_current_task_id", return_value=actual),
        ):
            return json.loads(self.mod._send_email_tool(args))

    def _enviar(self, body):
        self._imgs()
        self._manifest({"personal": {"imagenes": self._entrada_imagenes(), "html": SIG}})
        enviados = []

        def graph(method, path, token, payload=None, **kw):
            enviados.append((method, path, payload))
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
        ):
            base = {"to": "a@x.cl", "subject": "s", "body": body, "firma": "personal"}
            self._correr(base, "tarea-1")
            out = self._correr({**base, "aprobado_por_el_usuario": True}, "tarea-2")
        sent = [p for m, pth, p in enviados if pth == "/me/sendMail"][0]["message"]
        return out, sent

    def test_cuerpo_sin_cid_recibe_la_firma_y_sin_warning(self):
        out, sent = self._enviar("<p>Hola, esto es una prueba.</p>")
        self.assertTrue(out["success"], out)
        self.assertIn("FIRMA EMPRESA", sent["body"]["content"])
        self.assertIn("cid:logo", sent["body"]["content"])
        self.assertIn("logo", [a["contentId"] for a in sent.get("attachments", [])])
        self.assertNotIn("warning", out)

    def test_cuerpo_con_cid_no_duplica_la_firma(self):
        out, sent = self._enviar("<p>Hola</p>" + SIG)
        self.assertTrue(out["success"], out)
        self.assertEqual(sent["body"]["content"].count("FIRMA EMPRESA"), 1)
        self.assertIn("logo", [a["contentId"] for a in sent.get("attachments", [])])


if __name__ == "__main__":
    unittest.main()


class TestFirmaDeOtraPersona(_Base):
    FIRMA_ANA = '<div>Ana Soto Rivas<br>E: asoto@contoso.cl<br><img src="cid:logo"></div>'

    def _envio(self, propia, firma_html, nombre="personal"):
        self._imgs()
        self._manifest({nombre: {"imagenes": self._entrada_imagenes(), "html": firma_html}})

        def graph(method, path, token, payload=None, **kw):
            if path.startswith("/me?"):
                return {"mail": propia, "userPrincipalName": propia}
            return {}

        carga = json.dumps({"task_id": "t1", "metadata": {}, "mensaje": "prepáralo"})
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_aprobacion_central_activa", return_value=False),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_resolver_directorio", return_value={}),
            patch.object(self.mod, "_current_task_id", return_value="t1"),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": carga}),
        ):
            return json.loads(
                self.mod._send_email_tool({"to": "a@x.cl", "subject": "s", "body": "hola", "firma": nombre})
            )

    def test_caso_real_operador2_con_la_firma_de_ana_no_se_envia(self):
        res = self._envio("usuario1@contoso.cl", self.FIRMA_ANA)
        self.assertFalse(res["success"])
        self.assertIn("asoto@contoso.cl", res["error"])
        self.assertIn("firma='ninguna'", res["error"])

    def test_la_firma_propia_pasa(self):
        res = self._envio("asoto@contoso.cl", self.FIRMA_ANA)
        self.assertTrue(res.get("requiere_aprobacion"), res)

    def test_firma_de_equipo_con_otro_nombre_no_se_revisa(self):
        res = self._envio("asoto@empresa.cl", "<div>Equipo Datos<br>equipo-datos@empresa.cl</div>", nombre="equipo")
        self.assertTrue(res.get("requiere_aprobacion"), res)

    def test_firma_sin_correos_o_con_correos_externos_pasa(self):
        self.assertTrue(self._envio("ana@contoso.cl", "<div>Ana<br>Gerencia TI</div>").get("requiere_aprobacion"))
        externo = "<div>Ana<br>soporte@proveedor.com</div>"
        self.assertTrue(self._envio("ana@contoso.cl", externo).get("requiere_aprobacion"))
