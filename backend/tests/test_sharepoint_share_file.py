import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("sharepoint_plugin_share", _PLUGIN)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


class TestShareFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.dir.name})
        env.start()
        self.addCleanup(env.stop)
        self.puts = []
        self.graph_calls = []

    def _share(self, args, put_result=None, session_urls=True):
        def graph(method, path, token, payload=None):
            self.graph_calls.append((method, path, payload))
            if path.endswith("/createUploadSession"):
                return {"uploadUrl": "https://upload.example/s"}
            if path.endswith("/createLink"):
                return {"link": {"webUrl": "https://org-my.sharepoint.com/:b:/g/abc"}}
            return {}

        def put(url, data, token=None, content_range=None):
            self.puts.append((url, len(data), content_range))
            if put_result is not None:
                return put_result
            return {"id": "ITEM1", "name": "informe.pdf"}

        with (
            patch.object(sp, "_task_user_id", return_value="u1"),
            patch.object(sp, "_get_m365_token", return_value=("tok", None, "")),
            patch.object(sp, "_graph", side_effect=graph),
            patch.object(sp, "_put_binary", side_effect=put),
        ):
            return json.loads(sp._share_file_tool(args))

    def _file(self, name, size):
        Path(self.dir.name, name).write_bytes(b"x" * size)

    def test_small_file_is_uploaded_and_linked(self):
        self._file("informe.pdf", 100_000)
        out = self._share({"file_path": "informe.pdf"})
        self.assertTrue(out["success"])
        self.assertEqual(out["url"], "https://org-my.sharepoint.com/:b:/g/abc")
        self.assertEqual(out["scope"], "organization")
        self.assertIn("organización", out["note"])
        self.assertIn("UltraBot-archivos/informe.pdf", self.puts[0][0].replace("%20", " "))
        link = [c for c in self.graph_calls if c[1].endswith("/createLink")][0]
        self.assertEqual(link[2], {"type": "view", "scope": "organization"})

    def test_large_file_uses_an_upload_session_in_chunks(self):
        self._file("deck.pptx", 8_000_000)
        out = self._share({"file_path": "deck.pptx"})
        self.assertTrue(out["success"])
        self.assertEqual(sum(size for _, size, _ in self.puts), 8_000_000)
        self.assertTrue(self.puts[0][2].startswith("bytes 0-"))
        self.assertTrue(self.puts[-1][2].endswith("/8000000"))

    def test_anonymous_scope_is_passed_through(self):
        self._file("a.png", 10)
        out = self._share({"file_path": "a.png", "scope": "anonymous"})
        self.assertEqual(out["scope"], "anonymous")
        self.assertNotIn("note", out)

    def test_invalid_scope_is_rejected(self):
        self._file("a.png", 10)
        self.assertFalse(self._share({"file_path": "a.png", "scope": "publico"})["success"])
        self.assertEqual(self.puts, [])

    def test_path_outside_workspace_is_refused(self):
        out = self._share({"file_path": "/etc/passwd"})
        self.assertFalse(out["success"])
        self.assertEqual(self.puts, [])

    def test_missing_file_and_oversize(self):
        self.assertFalse(self._share({"file_path": "no.pdf"})["success"])
        self._file("enorme.bin", 50_000_001)
        out = self._share({"file_path": "enorme.bin"})
        self.assertIn("máximo", out["error"])

    def test_upload_error_is_reported(self):
        self._file("informe.pdf", 100)
        out = self._share(
            {"file_path": "informe.pdf"}, put_result={"error": "Sin permiso para escribir en OneDrive (403)"}
        )
        self.assertFalse(out["success"])
        self.assertIn("403", out["error"])

    def test_unsafe_characters_never_reach_the_remote_path(self):
        self._file("a#b?c.pdf", 100)
        self._share({"file_path": "a#b?c.pdf"})
        self.assertNotIn("#", self.puts[0][0].split(":/content")[0])
