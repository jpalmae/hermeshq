import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _load():
    spec = importlib.util.spec_from_file_location("ms365_mail_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InlineImageTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "work").mkdir()
        (self.root / "work" / "logo.png").write_bytes(PNG)
        (self.root / "work" / "foto.jpg").write_bytes(JPEG)
        (self.root / "work" / "notas.txt").write_text("secreto")
        (self.root / ".env").write_text("API_KEY=secret")
        patcher = patch.dict("os.environ", {"HERMES_SESSION_CWD": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, args):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value={}) as graph,
        ):
            result = json.loads(self.plugin._send_email_tool(args))
        return result, graph

    def test_sends_inline_attachments_with_cid(self):
        result, graph = self._send(
            {
                "to": "a@x.cl",
                "subject": "s",
                "body": '<img src="cid:logo"><img src="cid:foto">',
                "inline_images": [
                    {"path": "work/logo.png", "content_id": "logo"},
                    {"path": str(self.root / "work" / "foto.jpg"), "content_id": "foto"},
                ],
            }
        )
        self.assertTrue(result["success"])
        self.assertNotIn("warning", result)
        message = graph.call_args.args[3]["message"]
        first, second = message["attachments"]
        self.assertEqual(first["contentId"], "logo")
        self.assertTrue(first["isInline"])
        self.assertEqual(first["contentType"], "image/png")
        self.assertEqual(base64.b64decode(first["contentBytes"]), PNG)
        self.assertEqual(second["contentType"], "image/jpeg")

    def test_warns_when_image_is_not_referenced(self):
        result, _ = self._send(
            {
                "to": "a@x.cl",
                "subject": "s",
                "body": "<p>sin imagen</p>",
                "inline_images": [{"path": "work/logo.png", "content_id": "logo"}],
            }
        )
        self.assertTrue(result["success"])
        self.assertIn("logo", result["warning"])

    def test_plain_send_without_images_is_unchanged(self):
        result, graph = self._send({"to": ["a@x.cl", "b@x.cl"], "subject": "s", "body": "hola"})
        self.assertTrue(result["success"])
        message = graph.call_args.args[3]["message"]
        self.assertNotIn("attachments", message)
        self.assertEqual(len(message["toRecipients"]), 2)

    def test_cc_and_bcc_are_sent_when_present(self):
        _, graph = self._send(
            {"to": "a@x.cl", "subject": "s", "body": "b", "cc": "c@x.cl", "bcc": ["d@x.cl", "e@x.cl"]}
        )
        message = graph.call_args.args[3]["message"]
        self.assertEqual(message["ccRecipients"][0]["emailAddress"]["address"], "c@x.cl")
        self.assertEqual(len(message["bccRecipients"]), 2)

    def _assert_rejected(self, images, fragment):
        result, graph = self._send({"to": "a@x.cl", "subject": "s", "body": "b", "inline_images": images})
        self.assertFalse(result["success"])
        self.assertIn(fragment, result["error"])
        graph.assert_not_called()

    def test_rejects_paths_outside_workspace(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as outside:
            outside.write(PNG)
        self.addCleanup(Path(outside.name).unlink)
        self._assert_rejected([{"path": outside.name, "content_id": "x"}], "fuera del directorio")
        self._assert_rejected([{"path": "../../etc/passwd", "content_id": "x"}], "fuera del directorio")

    def test_rejects_non_image_files_even_inside_workspace(self):
        self._assert_rejected([{"path": "work/notas.txt", "content_id": "x"}], "no es una imagen")
        self._assert_rejected([{"path": ".env", "content_id": "x"}], "no es una imagen")

    def test_rejects_renamed_non_image(self):
        (self.root / "work" / "falso.png").write_text("no soy un png")
        self._assert_rejected([{"path": "work/falso.png", "content_id": "x"}], "no es una imagen")

    def test_rejects_symlink_escaping_workspace(self):
        link = self.root / "work" / "enlace.png"
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as outside:
            outside.write(PNG)
        self.addCleanup(Path(outside.name).unlink)
        link.symlink_to(outside.name)
        self._assert_rejected([{"path": "work/enlace.png", "content_id": "x"}], "fuera del directorio")

    def test_rejects_bad_or_duplicate_content_ids(self):
        self._assert_rejected([{"path": "work/logo.png", "content_id": "con espacio"}], "content_id")
        self._assert_rejected(
            [{"path": "work/logo.png", "content_id": "a"}, {"path": "work/foto.jpg", "content_id": "a"}], "repetido"
        )

    def test_rejects_missing_file_and_oversize(self):
        self._assert_rejected([{"path": "work/no-existe.png", "content_id": "x"}], "No existe")
        (self.root / "work" / "grande.png").write_bytes(PNG + b"\x00" * 1_600_000)
        self._assert_rejected([{"path": "work/grande.png", "content_id": "x"}], "pesa más")

    def test_rejects_too_many_images(self):
        images = [{"path": "work/logo.png", "content_id": f"i{n}"} for n in range(11)]
        self._assert_rejected(images, "hasta 10")

    def test_rejects_total_size_over_limit(self):
        for name in ("a", "b"):
            (self.root / "work" / f"{name}.png").write_bytes(PNG + b"\x00" * 1_300_000)
        self._assert_rejected(
            [{"path": "work/a.png", "content_id": "a"}, {"path": "work/b.png", "content_id": "b"}], "suman más"
        )

    def test_send_still_requires_recipient(self):
        result, graph = self._send({"to": [], "subject": "s", "body": "b"})
        self.assertFalse(result["success"])
        graph.assert_not_called()

    def test_schema_documents_inline_images(self):
        from unittest.mock import MagicMock

        ctx = MagicMock()
        self.plugin.register(ctx)
        schemas = {c.kwargs["name"]: c.kwargs["schema"] for c in ctx.register_tool.call_args_list}
        properties = schemas["ms365_mail_send"]["parameters"]["properties"]
        self.assertIn("inline_images", properties)
        self.assertEqual(schemas["ms365_mail_send"]["parameters"]["required"], ["to", "subject", "body"])


if __name__ == "__main__":
    unittest.main()
