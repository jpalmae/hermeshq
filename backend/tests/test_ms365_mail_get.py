import importlib.util
import json
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)

SIGNATURE_HTML = (
    "<html><head><title>x</title><style>.a{color:red}</style></head><body>"
    "<p>Hola&nbsp;Ana,</p><p>Adjunto el <b>informe</b> &amp; las cifras.</p>"
    "<ul><li>Uno</li><li>Dos</li></ul>"
    "<table><tr><td>Total</td><td>10</td></tr></table>"
    '<img src="data:image/png;base64,' + "A" * 5000 + '">'
    "<script>alert(1)</script></body></html>"
)


def _load():
    spec = importlib.util.spec_from_file_location("ms365_mail_get_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _alinear_costuras_broker(module)


def _graph_message(content, content_type="html", **extra):
    message = {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('x')/messages/$entity",
        "id": "AAMk=",
        "subject": "Informe",
        "from": {"emailAddress": {"name": "Ana", "address": "ana@example.com"}},
        "receivedDateTime": "2026-10-06T17:00:00Z",
        "hasAttachments": False,
        "body": {"contentType": content_type, "content": content},
    }
    message.update(extra)
    return message


class HtmlToTextTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def test_strips_markup_style_script_and_images(self):
        text = self.plugin._html_to_text(SIGNATURE_HTML)
        self.assertNotIn("<", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("base64", text)
        self.assertIn("Hola Ana,", text)
        self.assertIn("informe & las cifras.", text)

    def test_keeps_basic_structure(self):
        text = self.plugin._html_to_text("<p>Uno</p><p>Dos<br>tres<br/>cuatro</p><ul><li>a</li><li>b</li></ul>")
        self.assertEqual(text.split("\n"), ["Uno", "Dos", "tres", "cuatro", "a", "b"])

    def test_collapses_blank_runs(self):
        text = self.plugin._html_to_text("<div>a</div><div></div><div></div><div></div><div>b</div>")
        self.assertEqual(text, "a\nb")


class GetEmailTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _get(self, args, graph_result):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value=graph_result) as graph,
        ):
            result = json.loads(self.plugin._get_email_tool(args))
        return result, graph

    def test_default_returns_trimmed_plain_text(self):
        result, graph = self._get({"message_id": "AAMk="}, _graph_message(SIGNATURE_HTML))
        self.assertTrue(result["success"])
        message = result["message"]
        self.assertEqual(message["body"]["contentType"], "text")
        self.assertNotIn("<", message["body"]["content"])
        self.assertIn("Total", message["body"]["content"])
        self.assertFalse(message["body_truncated"])
        self.assertNotIn("@odata.context", message)
        self.assertLess(len(json.dumps(message)), 1500)

    def test_requests_only_needed_fields_and_text_preference(self):
        _, graph = self._get({"message_id": "AAMk="}, _graph_message("hola", "text"))
        path = graph.call_args.args[1]
        self.assertTrue(path.startswith("/me/messages/AAMk=?$select="))
        for field in ("subject", "from", "receivedDateTime", "body"):
            self.assertIn(field, path)
        self.assertEqual(graph.call_args.kwargs["headers"], {"Prefer": 'outlook.body-content-type="text"'})

    def test_plain_text_body_passes_through_tidied(self):
        result, _ = self._get({"message_id": "AAMk="}, _graph_message("Hola\r\n\r\n\r\n\r\nAna  ", "text"))
        self.assertEqual(result["message"]["body"]["content"], "Hola\n\nAna")

    def test_long_body_is_truncated_and_flagged(self):
        long_text = "palabra " * 4000
        result, _ = self._get({"message_id": "AAMk="}, _graph_message(long_text, "text"))
        message = result["message"]
        self.assertTrue(message["body_truncated"])
        self.assertEqual(message["body_chars_total"], len(long_text.strip()))
        self.assertLessEqual(len(message["body"]["content"]), 8001)
        self.assertTrue(message["body"]["content"].endswith("…"))

    def test_max_chars_is_honoured_and_clamped(self):
        long_text = "x" * 50000
        result, _ = self._get({"message_id": "AAMk=", "max_chars": 20000}, _graph_message(long_text, "text"))
        self.assertEqual(len(result["message"]["body"]["content"]), 20001)
        result, _ = self._get({"message_id": "AAMk=", "max_chars": 5}, _graph_message(long_text, "text"))
        self.assertEqual(len(result["message"]["body"]["content"]), 201)
        result, _ = self._get({"message_id": "AAMk=", "max_chars": 10**9}, _graph_message("x" * 150000, "text"))
        self.assertEqual(len(result["message"]["body"]["content"]), 100001)
        result, _ = self._get({"message_id": "AAMk=", "max_chars": "abc"}, _graph_message(long_text, "text"))
        self.assertEqual(len(result["message"]["body"]["content"]), 8001)

    def test_html_format_keeps_markup_without_text_preference(self):
        result, graph = self._get({"message_id": "AAMk=", "format": "html"}, _graph_message("<p>Hola</p>"))
        self.assertEqual(result["message"]["body"], {"contentType": "html", "content": "<p>Hola</p>"})
        self.assertIsNone(graph.call_args.kwargs["headers"])

    def test_html_format_is_capped_by_default(self):
        result, _ = self._get({"message_id": "AAMk=", "format": "html"}, _graph_message("<p>a</p>" * 20000))
        self.assertTrue(result["message"]["body_truncated"])
        self.assertEqual(len(result["message"]["body"]["content"]), 30001)

    def test_invalid_format_and_missing_id(self):
        result, graph = self._get({"message_id": "AAMk=", "format": "pdf"}, {})
        self.assertFalse(result["success"])
        graph.assert_not_called()
        result, graph = self._get({}, {})
        self.assertFalse(result["success"])
        graph.assert_not_called()

    def test_graph_error_is_propagated(self):
        result, _ = self._get({"message_id": "AAMk="}, {"error": "Elemento no encontrado (404)."})
        self.assertEqual(result, {"success": False, "error": "Elemento no encontrado (404)."})


class GraphHeadersTests(unittest.TestCase):
    def test_extra_headers_are_sent_alongside_auth(self):
        plugin = _load()
        response = MagicMock()
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch.object(plugin.urllib.request, "urlopen", return_value=response) as urlopen:
            plugin._graph("GET", "/me/messages/x", "tok", headers={"Prefer": 'outlook.body-content-type="text"'})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual(request.get_header("Prefer"), 'outlook.body-content-type="text"')
        self.assertEqual(request.get_header("Content-type"), "application/json")


class SearchEmailsTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _search(self, args, graph_result=None):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", return_value=graph_result or {"items": []}) as graph,
        ):
            result = json.loads(self.plugin._search_emails_tool(args))
        return result, graph

    @staticmethod
    def _broker_acepta(path):
        from hermeshq.services import kql_correo

        literal = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["$search"][0]
        try:
            kql_correo.validar(literal[1:-1])
            return True
        except kql_correo.KqlNoPermitido:
            return False

    def test_special_characters_in_query_are_encoded_and_rejected_by_the_broker_grammar(self):
        _, graph = self._search({"query": "AT&T facturación"})
        path = graph.call_args.args[1]
        self.assertIn("$search=%22%22AT%26T%22%20facturaci%C3%B3n%22", path)
        self.assertEqual(path.count("&"), 1)
        self.assertTrue(self._broker_acepta(path))

    def test_double_quotes_in_query_cannot_break_the_search_clause(self):
        _, graph = self._search({"query": 'foo" bar'})
        self.assertFalse(self._broker_acepta(graph.call_args.args[1]))

    def test_query_with_only_quotes_is_rejected_by_the_broker(self):
        _, graph = self._search({"query": '""'})
        self.assertFalse(self._broker_acepta(graph.call_args.args[1]))

    def test_previews_are_capped_like_in_list(self):
        messages = {"items": [{"id": "1", "bodyPreview": "x" * 255}]}
        result, _ = self._search({"query": "a", "preview_chars": 50}, messages)
        self.assertEqual(len(result["messages"][0]["bodyPreview"]), 51)
        result, _ = self._search({"query": "a"}, {"items": [{"id": "1", "bodyPreview": "x" * 255}]})
        self.assertEqual(len(result["messages"][0]["bodyPreview"]), 201)


if __name__ == "__main__":
    unittest.main()


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _run(self, args, messages=None):
        captured = {}

        def fake_graph(method, path, token, *a, **k):
            captured["path"] = path
            return {"items": messages or []}

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
        ):
            return json.loads(self.plugin._search_emails_tool(args)), captured.get("path")

    def _validada(self, out):
        from hermeshq.services import kql_correo

        return kql_correo.validar(out["search"])

    def test_filters_are_combined_into_one_kql_search(self):
        out, path = self._run(
            {
                "query": "cuentas",
                "from": "Filial Sur",
                "since": "2026-10-07",
                "has_attachments": True,
                "folder": "inbox",
            }
        )
        self.assertTrue(out["success"])
        self.assertIn("mailFolders/inbox/messages", path)
        self.assertEqual(out["search"], 'cuentas from:"Filial Sur" received>=2026-10-07 hasattachments:true')
        self.assertEqual(self._validada(out), out["search"])

    def test_free_query_is_not_reinterpreted_and_the_broker_decides(self):
        from hermeshq.services import kql_correo

        out, _ = self._run({"query": 'subject:"Ticket 14.436, 15.691" from:"persona@example.com" informe'})
        self.assertEqual(out["search"], 'subject:"Ticket 14.436, 15.691" from:"persona@example.com" informe')
        self.assertEqual(kql_correo.validar(out["search"]), out["search"])
        with self.assertRaises(kql_correo.KqlNoPermitido):
            kql_correo.validar('subject:"x" OR from:"y"')
        out, _ = self._run({"subject": "Ticket 14.436, 15.691", "from": "persona@example.com", "query": "informe"})
        self.assertEqual(out["search"], 'informe from:"persona@example.com" subject:"Ticket 14.436 15.691"')
        self.assertEqual(self._validada(out), out["search"])

    def test_field_values_are_single_quoted_phrases_without_inner_quotes(self):
        cases = [
            {"from": "Carolina Marin"},
            {"from": "user@dominio.cl"},
            {"subject": "Ticket 14.436"},
            {"subject": 'Reporte "final"'},
            {"from": "Carolina Marin", "since": "2026-10-07"},
            {"subject": "Ticket 14.436", "until": "2026-10-10"},
            {"from": "user@dominio.cl", "subject": "Ticket 14.436", "since": "2026-10-01", "until": "2026-10-10"},
        ]
        for args in cases:
            out, _ = self._run(dict(args))
            self.assertTrue(out["success"], args)
            for campo in ("from", "subject"):
                if campo in args:
                    valor = out["search"].split(f'{campo}:"', 1)[1].split('"', 1)[0]
                    self.assertNotIn('"', valor, args)
            self.assertEqual(self._validada(out), out["search"], args)

    def test_from_name_is_one_phrase(self):
        out, _ = self._run({"from": "Carolina Marin"})
        self.assertEqual(out["search"], 'from:"Carolina Marin"')

    def test_from_email_is_one_phrase(self):
        out, _ = self._run({"from": "user@dominio.cl"})
        self.assertEqual(out["search"], 'from:"user@dominio.cl"')

    def test_subject_tokens_stay_bound_to_subject(self):
        out, _ = self._run({"subject": "Ticket 14.436"})
        self.assertEqual(out["search"], 'subject:"Ticket 14.436"')

    def test_subject_quote_is_dropped_not_sent_to_graph(self):
        out, _ = self._run({"subject": 'Reporte "final"'})
        self.assertEqual(out["search"], 'subject:"Reporte final"')

    def test_from_subject_with_since_and_until(self):
        out, _ = self._run(
            {
                "from": "user@dominio.cl",
                "subject": "Ticket 14.436",
                "since": "2026-10-01",
                "until": "2026-10-10",
            }
        )
        self.assertEqual(
            out["search"],
            'from:"user@dominio.cl" subject:"Ticket 14.436" received>=2026-10-01 received<=2026-10-10',
        )

    def test_unavailable_search_carries_a_hint(self):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(
                self.plugin,
                "_graph",
                return_value={
                    "error": "Acceso denegado por la política de correo de HermesHQ (busqueda_no_disponible)."
                },
            ),
        ):
            out = json.loads(self.plugin._search_emails_tool({"query": "x"}))
        self.assertFalse(out["success"])
        self.assertIn("Simplifica", out["error"])

    def test_results_come_newest_first_from_the_broker_and_empty_search_is_rejected(self):
        from hermeshq.services import graph_proxy as gp

        old = {"id": "1", "receivedDateTime": "2026-10-01T10:00:00Z", "parentFolderId": "INBOX-ID"}
        new = {"id": "2", "receivedDateTime": "2026-10-09T10:00:00Z", "parentFolderId": "INBOX-ID"}

        def red(metodo, url, token, cuerpo, tipo, limite):
            if "$search" in url:
                return 200, json.dumps({"value": [old, new]}).encode()
            return 200, b'{"id": "INBOX-ID"}'

        out, path = self._run({"query": "informe", "folder": "inbox"})
        politica = gp.PoliticaGraph(
            correo=gp.PoliticaCorreo(operaciones=frozenset({"mail_search"}), carpetas=frozenset({"inbox"}))
        )
        with patch.object(gp, "_llamar", red):
            res = gp.ejecutar("GET", path, None, None, politica, lambda: "tok", lambda t: None)
        self.assertEqual([m["id"] for m in res["json"]["items"]], ["2", "1"])
        out, _ = self._run({})
        self.assertFalse(out["success"])
        out, _ = self._run({"since": "no es fecha"})
        self.assertFalse(out["success"])


class GetManyTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load()

    def _run(self, args):
        def fake_graph(method, path, token, *a, **k):
            if "bad" in path:
                return {"error": "no encontrado"}
            return _graph_message("<p>Hola mundo</p>", id=path.split("/")[-1].split("?")[0])

        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(self.plugin, "_graph", side_effect=fake_graph),
        ):
            return json.loads(self.plugin._get_emails_tool(args))

    def test_reads_several_in_one_call_keeping_order_and_errors(self):
        out = self._run({"message_ids": ["a", "bad", "c", "a"]})
        self.assertTrue(out["success"])
        self.assertEqual([m.get("id") or m.get("message_id") for m in out["messages"]], ["a", "bad", "c"])
        self.assertEqual(out["messages"][1]["error"], "no encontrado")
        self.assertEqual(out["messages"][0]["body"]["content"], "Hola mundo")

    def test_caps_at_ten_and_requires_ids(self):
        out = self._run({"message_ids": [f"m{i}" for i in range(13)]})
        self.assertEqual(out["count"], 10)
        self.assertEqual(out["omitted_ids"], ["m10", "m11", "m12"])
        self.assertFalse(self._run({"message_ids": []})["success"])


class IdRepairTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        self.plugin = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_a_truncated_copy_of_a_known_id_is_completed(self):
        real = "AAMkAGQ2" + "x" * 140 + "AAA="
        self.plugin._remember_ids([{"id": real}, {"id": "AAMkOTRO" + "y" * 140}])
        self.assertEqual(self.plugin._resolve_message_id(real[:120]), real)
        self.assertEqual(self.plugin._resolve_message_id(real), real)

    def test_an_id_that_lost_its_middle_segment_is_recovered_by_head_and_tail(self):
        base = "AAMkADAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMABGAAAAAADBmIefJsqbT5w5CYTGyV4ABwDDBbe_xmITSpzki-dR2kZm"
        a = base + "AAAAz3uAADDBbe_xmITSpzki-dR2kZmAAMIlwlHAAA="
        b = base + "AAAAz3uAADDBbe_xmITSpzki-dR2kZmAAMIlwlGAAA="
        c = base + "AAAAz3OAADDBbe_xmITSpzki-dR2kZmAALtxly6AAA="
        self.plugin._remember_ids([{"id": a}, {"id": b}, {"id": c}])
        self.assertEqual(self.plugin._resolve_message_id(base + "AAMIlwlHAAA="), a)
        self.assertEqual(self.plugin._resolve_message_id(base + "AAMIlwlGAAA="), b)
        self.assertEqual(self.plugin._resolve_message_id(base + "AAMIlwlZAAA="), base + "AAMIlwlZAAA=")

    def test_unknown_or_ambiguous_ids_are_left_alone(self):
        self.plugin._remember_ids([{"id": "AAMk" + "a" * 100 + "1"}, {"id": "AAMk" + "a" * 100 + "2"}])
        self.assertEqual(self.plugin._resolve_message_id("AAMk" + "a" * 70), "AAMk" + "a" * 70)
        self.assertEqual(self.plugin._resolve_message_id("ZZZ" + "b" * 80), "ZZZ" + "b" * 80)


def _alinear_costuras_broker(module):
    module._broker_token_check = lambda user_id: module._get_m365_token(user_id)
    module._broker_graph = lambda metodo, ruta, cuerpo_json=None, prefer=None: module._graph(
        metodo,
        ruta,
        "tok",
        **({"payload": cuerpo_json} if cuerpo_json is not None else {}),
        **({"headers": {"Prefer": prefer}} if prefer else {}),
    )
    return module
