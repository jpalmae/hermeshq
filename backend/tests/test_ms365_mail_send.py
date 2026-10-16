import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ms365_mail_get import _load


class TestRecipients(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _addresses(self, value):
        recipients, invalid = self.mod._parse_recipients(value)
        return [r["emailAddress"]["address"] for r in recipients], invalid

    def test_semicolon_string(self):
        self.assertEqual(self._addresses("a@x.cl;b@y.cl"), (["a@x.cl", "b@y.cl"], []))

    def test_comma_and_space_string(self):
        self.assertEqual(self._addresses("a@x.cl, b@y.cl"), (["a@x.cl", "b@y.cl"], []))

    def test_list_with_packed_items(self):
        self.assertEqual(self._addresses(["a@x.cl", "b@y.cl;c@z.cl"]), (["a@x.cl", "b@y.cl", "c@z.cl"], []))

    def test_json_array_as_string(self):
        self.assertEqual(self._addresses('["a@x.cl", "b@y.cl"]'), (["a@x.cl", "b@y.cl"], []))

    def test_display_name_format(self):
        self.assertEqual(self._addresses("Ana Pérez <ana@x.cl>"), (["ana@x.cl"], []))

    def test_duplicates_removed_case_insensitive(self):
        self.assertEqual(self._addresses(["A@x.cl", "a@X.cl"]), (["A@x.cl"], []))

    def test_invalid_tokens_reported(self):
        self.assertEqual(self._addresses("a@x.cl; Juan"), (["a@x.cl"], ["Juan"]))

    def test_empty(self):
        self.assertEqual(self._addresses(None), ([], []))


class TestSendWithAttachments(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.dir.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sent = []

    def _send(self, args):
        def graph(method, path, token, payload=None, **kwargs):
            self.sent.append((method, path, payload))
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(self.mod._send_email_tool(args))

    def test_multiple_recipients_in_one_message(self):
        out = self._send({"to": "a@x.cl;b@y.cl", "cc": ["c@z.cl"], "subject": "s", "body": "b"})
        self.assertTrue(out["success"])
        message = self.sent[0][2]["message"]
        self.assertEqual(len(message["toRecipients"]), 2)
        self.assertEqual(len(message["ccRecipients"]), 1)
        self.assertEqual(out["recipients"], 2)

    def test_invalid_recipient_rejected_before_sending(self):
        out = self._send({"to": ["a@x.cl", "Juan"], "subject": "s", "body": "b"})
        self.assertFalse(out["success"])
        self.assertIn("Juan", out["error"])
        self.assertEqual(self.sent, [])

    def test_file_attachment_is_added(self):
        Path(self.dir.name, "informe.pdf").write_bytes(b"%PDF-1.4 test")
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "informe.pdf"}]})
        self.assertTrue(out["success"])
        attachment = self.sent[0][2]["message"]["attachments"][0]
        self.assertEqual(attachment["name"], "informe.pdf")
        self.assertEqual(attachment["contentType"], "application/pdf")
        self.assertNotIn("isInline", attachment)
        self.assertEqual(out["attachments"], [{"name": "informe.pdf", "bytes": 13}])

    def test_plain_string_attachment_and_custom_name(self):
        Path(self.dir.name, "datos.xlsx").write_bytes(b"x" * 10)
        out = self._send(
            {
                "to": ["a@x.cl"],
                "subject": "s",
                "body": "b",
                "attachments": [{"path": "datos.xlsx", "name": "Ventas.xlsx"}],
            }
        )
        self.assertEqual(self.sent[0][2]["message"]["attachments"][0]["name"], "Ventas.xlsx")
        self.assertTrue(out["success"])

    def test_oversized_attachments_are_refused_with_alternatives(self):
        Path(self.dir.name, "grande.pptx").write_bytes(b"x" * 19_000_000)
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "grande.pptx"}]})
        self.assertFalse(out["success"])
        self.assertIn("SharePoint", out["error"])
        self.assertEqual(self.sent, [])

    def test_path_outside_workspace_is_refused(self):
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "/etc/passwd"}]})
        self.assertFalse(out["success"])
        self.assertEqual(self.sent, [])

    def test_missing_file_is_reported(self):
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "no.pdf"}]})
        self.assertFalse(out["success"])
        self.assertIn("no.pdf", out["error"])


class TestLargeAttachments(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.dir.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.chunks = []

    def _send(self, args, graph_fn=None):
        def default_graph(method, path, token, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            if method == "POST" and path == "/me/messages":
                return {"id": "DRAFT1"}
            if path.endswith("/createUploadSession"):
                return {"uploadUrl": "https://upload.example/session1"}
            return {}

        def put_chunk(url, chunk, start, total):
            self.chunks.append((url, len(chunk), start, total))
            return {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph_fn or default_graph),
            patch.object(self.mod, "_put_upload_chunk", side_effect=put_chunk),
        ):
            return json.loads(self.mod._send_email_tool(args))

    def _file(self, name, size):
        Path(self.dir.name, name).write_bytes(b"x" * size)

    def test_big_file_goes_through_draft_upload_session_and_send(self):
        self._file("deck.pptx", 8_000_000)
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "deck.pptx"}]})
        self.assertTrue(out["success"])
        sequence = [(m, p.split("/")[-1] if p != "/me/messages" else p) for m, p, _ in self.calls]
        self.assertEqual(sequence, [("POST", "/me/messages"), ("POST", "createUploadSession"), ("POST", "send")])
        self.assertEqual(sum(c[1] for c in self.chunks), 8_000_000)
        self.assertEqual(self.chunks[0][2], 0)
        self.assertTrue(all(c[3] == 8_000_000 for c in self.chunks))
        self.assertTrue(all(c[1] % 327_680 == 0 for c in self.chunks[:-1]))
        self.assertEqual(out["attachments"], [{"name": "deck.pptx", "bytes": 8_000_000}])
        self.assertNotIn("attachments", self.calls[0][2])

    def test_several_medium_files_use_direct_attachment_posts(self):
        self._file("a.png", 2_000_000)
        self._file("b.png", 2_000_000)
        out = self._send(
            {"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "a.png"}, {"path": "b.png"}]}
        )
        self.assertTrue(out["success"])
        posts = [p for m, p, _ in self.calls if p.endswith("/attachments")]
        self.assertEqual(len(posts), 2)
        self.assertEqual(self.chunks, [])

    def test_small_total_still_uses_single_sendmail(self):
        self._file("a.pdf", 1_000_000)
        self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "a.pdf"}]})
        self.assertEqual([(m, p) for m, p, _ in self.calls], [("POST", "/me/sendMail")])

    def test_missing_mail_readwrite_gives_clear_message(self):
        self._file("deck.pptx", 5_000_000)

        def denied(method, path, token, payload=None, **kwargs):
            return {"error": "Error 403: Access is denied. Check credentials and try again."}

        out = self._send(
            {"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "deck.pptx"}]}, graph_fn=denied
        )
        self.assertFalse(out["success"])
        self.assertIn("Mail.ReadWrite", out["error"])

    def test_failed_upload_deletes_the_draft(self):
        self._file("deck.pptx", 5_000_000)

        def graph(method, path, token, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            if path == "/me/messages":
                return {"id": "DRAFT1"}
            if path.endswith("/createUploadSession"):
                return {"error": "Error 500: boom"}
            return {}

        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "deck.pptx"}]}, graph)
        self.assertFalse(out["success"])
        self.assertIn(("DELETE", "/me/messages/DRAFT1", None), self.calls)
        self.assertNotIn("send", [p.split("/")[-1] for _, p, _ in self.calls])


class TestOneDriveFallback(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.dir.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.puts = []

    def _send(self, args, readwrite_denied=True):
        def graph(method, path, token, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            if path == "/me/messages":
                return {"error": "Error 403: Access is denied."} if readwrite_denied else {"id": "D1"}
            if path.endswith("/createUploadSession"):
                return {"uploadUrl": "https://upload.example/s1"}
            if path.endswith("/createLink"):
                return {"link": {"webUrl": "https://contoso-my.sharepoint.com/:p:/g/abc"}}
            return {}

        def put_bytes(path, token, data):
            self.puts.append((path, len(data)))
            return {"id": "ITEM1"}

        def put_chunk(url, chunk, start, total):
            self.puts.append((url, len(chunk)))
            return {"id": "ITEM1"} if start + len(chunk) >= total else {}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
            patch.object(self.mod, "_graph_put_bytes", side_effect=put_bytes),
            patch.object(self.mod, "_put_upload_chunk", side_effect=put_chunk),
        ):
            return json.loads(self.mod._send_email_tool(args))

    def test_without_mail_readwrite_big_file_goes_as_onedrive_link(self):
        Path(self.dir.name, "Plantilla PPT.pptx").write_bytes(b"x" * 3_500_000)
        out = self._send(
            {"to": ["a@x.cl"], "subject": "s", "body": "<p>Hola</p>", "attachments": [{"path": "Plantilla PPT.pptx"}]}
        )
        self.assertTrue(out["success"])
        self.assertEqual(out["delivery"], "enlaces_onedrive")
        send_call = [c for c in self.calls if c[1] == "/me/sendMail"][0]
        body = send_call[2]["message"]["body"]["content"]
        self.assertIn("https://contoso-my.sharepoint.com/:p:/g/abc", body)
        self.assertIn("Plantilla PPT.pptx", body)
        self.assertTrue(body.startswith("<p>Hola</p>"))
        self.assertNotIn("attachments", send_call[2]["message"])
        path = self.puts[0][0]
        self.assertIn("UltraBot-adjuntos/Plantilla PPT.pptx", path)

    def test_huge_file_uses_upload_session(self):
        Path(self.dir.name, "grande.pdf").write_bytes(b"x" * 9_000_000)
        out = self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "grande.pdf"}]})
        self.assertTrue(out["success"])
        self.assertEqual(sum(size for _, size in self.puts), 9_000_000)

    def test_unsafe_characters_in_name_are_not_used_in_the_path(self):
        Path(self.dir.name, "a#b?c.pdf").write_bytes(b"x" * 3_000_000)
        self._send({"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "a#b?c.pdf"}]})
        self.assertNotIn("#", self.puts[0][0].split(":/content")[0])

    def test_with_mail_readwrite_real_attachment_is_used(self):
        Path(self.dir.name, "deck.pptx").write_bytes(b"x" * 5_000_000)
        out = self._send(
            {"to": ["a@x.cl"], "subject": "s", "body": "b", "attachments": [{"path": "deck.pptx"}]},
            readwrite_denied=False,
        )
        self.assertTrue(out["success"])
        self.assertNotIn("delivery", out)
