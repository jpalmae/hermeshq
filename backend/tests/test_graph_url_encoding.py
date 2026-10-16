import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGES = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages"
PLUGINS = ("ms365-mail", "ms365-calendar", "ms365-teams", "sharepoint")


def _load(name):
    path = PACKAGES / name / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"graph_url_encoding_{name.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(body=b"{}"):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class GraphUrlEncodingTests(unittest.TestCase):
    def _sent_url(self, module, path):
        with patch.object(module.urllib.request, "urlopen", return_value=_response()) as urlopen:
            module._graph("GET", path, "tok")
        return urlopen.call_args.args[0].full_url

    def test_accented_path_is_percent_encoded_in_every_plugin(self):
        for name in PLUGINS:
            with self.subTest(plugin=name):
                module = _load(name)
                url = self._sent_url(module, "/drive/root:/Planes de Continuidad/Estándar de Backups.docx:/content")
                url.encode("ascii")
                self.assertIn("Est%C3%A1ndar%20de%20Backups.docx", url)
                self.assertIn("Planes%20de%20Continuidad", url)

    def test_accented_query_is_percent_encoded_in_every_plugin(self):
        for name in PLUGINS:
            with self.subTest(plugin=name):
                module = _load(name)
                url = self._sent_url(module, '/me/messages?$search="facturación"&$top=5')
                url.encode("ascii")
                self.assertIn("facturaci%C3%B3n", url)
                self.assertIn("$top=5", url)

    def test_already_encoded_url_is_not_double_encoded(self):
        for name in PLUGINS:
            with self.subTest(plugin=name):
                module = _load(name)
                url = self._sent_url(module, "/drive/root:/Est%C3%A1ndar%20Backups.docx:/content")
                self.assertIn("Est%C3%A1ndar%20Backups.docx", url)
                self.assertNotIn("%25", url)

    def test_odata_filter_keeps_its_syntax(self):
        for name in PLUGINS:
            with self.subTest(plugin=name):
                module = _load(name)
                url = self._sent_url(
                    module,
                    "/me/messages?$filter=receivedDateTime ge 2026-10-06T17:00:00Z&$orderby=receivedDateTime desc",
                )
                self.assertIn("$filter=receivedDateTime%20ge%202026-10-06T17:00:00Z", url)
                self.assertIn("&$orderby=receivedDateTime%20desc", url)

    def test_full_https_url_is_used_as_is(self):
        for name in PLUGINS:
            with self.subTest(plugin=name):
                module = _load(name)
                url = self._sent_url(module, "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc%3D%3D")
                self.assertEqual(url, "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc%3D%3D")


class SharepointDownloadEncodingTests(unittest.TestCase):
    def test_download_url_with_accents_is_encoded(self):
        module = _load("sharepoint")
        opener = MagicMock()
        opener.open.return_value = _response(b"data")
        with patch.object(module.urllib.request, "build_opener", return_value=opener):
            data, error = module._download_bytes(
                "https://graph.microsoft.com/v1.0/drive/root:/Planes/Declaración de tiempos.docx:/content", "tok"
            )
        sent = opener.open.call_args.args[0].full_url
        sent.encode("ascii")
        self.assertIn("Declaraci%C3%B3n%20de%20tiempos.docx", sent)
        self.assertEqual(data, b"data")


if __name__ == "__main__":
    unittest.main()
