import json
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load


def _msg(frm, to=(), when="2026-10-06T12:00:00Z"):
    return {
        "from": {"emailAddress": {"name": frm[0], "address": frm[1]}},
        "toRecipients": [{"emailAddress": {"name": n, "address": a}} for n, a in to],
        "ccRecipients": [],
        "receivedDateTime": when,
    }


<<<<<<< HEAD
def _graph_factory(mail=None, sent=None, events=None, fail_all=False):
    def graph(method, path, token, **kwargs):
=======
def _graph_factory(
    mail=None, sent=None, events=None, fail_all=False, directory=None, directory_error=None, llamadas=None
):
    def graph(method, path, token, **kwargs):
        if llamadas is not None:
            llamadas.append((path, kwargs))
        if path.startswith("/users"):
            if directory_error:
                return {"error": directory_error[0], "_graph_code": directory_error[1]}
            return {"value": directory or []}
>>>>>>> origin/main
        if fail_all:
            return {"error": "boom"}
        if "/sentitems/" in path:
            return {"value": sent or []}
        if path.startswith("/me/calendarView"):
            return {"value": events or []}
        return {"value": mail or []}

    return graph


class TestFindPerson(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _run(self, args, **graph_kwargs):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=_graph_factory(**graph_kwargs)),
        ):
            return json.loads(self.mod._find_person_tool(args))

    def test_filters_by_name_and_ignores_accents(self):
        out = self._run(
            {"name": "ana torres"},
            mail=[
                _msg(("Ana Torres", "atorres@example.com"), to=[("Maria", "m@example.com")]),
                _msg(("Otra Persona", "otra@x.cl")),
            ],
        )
        self.assertEqual([c["address"] for c in out["candidates"]], ["atorres@example.com"])
        self.assertIn("confirma", out["note"].lower())

    def test_counts_and_merges_sources_across_mail_sent_and_meetings(self):
        person = ("Luis Rojas", "lrojas@example.com")
        out = self._run(
            {"name": "rojas"},
            mail=[_msg(person)],
            sent=[_msg(("Maria", "d@x.cl"), to=[person])],
            events=[
                {
                    "organizer": {"emailAddress": {"name": "Maria", "address": "d@x.cl"}},
                    "attendees": [{"emailAddress": {"name": person[0], "address": person[1]}}],
                    "start": {"dateTime": "2026-10-07T11:00:00"},
                }
            ],
        )
        self.assertEqual(len(out["candidates"]), 1)
        candidate = out["candidates"][0]
        self.assertEqual(candidate["count"], 3)
        self.assertEqual(candidate["sources"], ["correo", "enviados", "reuniones"])

    def test_most_frequent_candidate_first(self):
        a, b = ("Ana Pérez", "ana.perez@x.cl"), ("Ana Pérez", "aperez@y.cl")
        out = self._run({"name": "ana perez"}, mail=[_msg(a), _msg(b), _msg(b)])
        self.assertEqual(out["candidates"][0]["address"], "aperez@y.cl")

    def test_no_match_returns_empty_list(self):
        out = self._run({"name": "nadie"}, mail=[_msg(("Ana", "ana@x.cl"))])
        self.assertTrue(out["success"])
        self.assertEqual(out["candidates"], [])

    def test_requires_name(self):
        out = self._run({"name": " "})
        self.assertFalse(out["success"])

    def test_graph_failure_is_reported(self):
        out = self._run({"name": "ana"}, fail_all=True)
        self.assertFalse(out["success"])
<<<<<<< HEAD
=======

    def test_directory_people_are_found_even_without_history(self):
        llamadas = []
        out = self._run(
            {"name": "Elena Rojas"},
            directory=[
                {
                    "displayName": "Elena Rojas Vega",
                    "mail": "erojas@example.com",
                    "jobTitle": "Jefa de Operaciones",
                    "department": "Operaciones",
                },
                {"displayName": "Sin Correo", "mail": None},
            ],
            llamadas=llamadas,
        )
        self.assertTrue(out["success"], out)
        self.assertEqual([c["address"] for c in out["candidates"]], ["erojas@example.com"])
        candidato = out["candidates"][0]
        self.assertEqual(candidato["sources"], ["directorio"])
        self.assertEqual((candidato["cargo"], candidato["area"]), ("Jefa de Operaciones", "Operaciones"))
        self.assertNotIn("directorio_no_disponible", out)
        usuarios = [(p, k) for p, k in llamadas if p.startswith("/users")]
        self.assertEqual(len(usuarios), 1)
        self.assertEqual(usuarios[0][1]["headers"], {"ConsistencyLevel": "eventual"})
        self.assertIn("displayName%3AElena", usuarios[0][0])
        self.assertIn("displayName%3ARojas", usuarios[0][0])

    def test_a_person_in_history_and_directory_keeps_the_count_and_gains_the_source(self):
        persona = ("Ana Pérez", "aperez@example.com")
        out = self._run(
            {"name": "ana perez"},
            mail=[_msg(persona), _msg(persona)],
            directory=[{"displayName": "Ana Pérez", "mail": "aperez@example.com", "jobTitle": "Analista"}],
        )
        self.assertEqual(len(out["candidates"]), 1)
        candidato = out["candidates"][0]
        self.assertEqual(candidato["count"], 2)
        self.assertEqual(candidato["sources"], ["correo", "directorio"])
        self.assertEqual(candidato["cargo"], "Analista")

    def test_history_candidates_rank_before_directory_only_candidates(self):
        out = self._run(
            {"name": "ana"},
            mail=[_msg(("Ana Pérez", "aperez@example.com"))],
            directory=[{"displayName": "Ana Soto", "mail": "asoto@example.com"}],
        )
        self.assertEqual([c["address"] for c in out["candidates"]], ["aperez@example.com", "asoto@example.com"])

    def test_missing_permission_is_explained_but_history_results_still_come_back(self):
        out = self._run(
            {"name": "ana"},
            mail=[_msg(("Ana Pérez", "aperez@example.com"))],
            directory_error=("Sin permiso para acceder a este recurso (403).", "Authorization_RequestDenied"),
        )
        self.assertTrue(out["success"])
        self.assertEqual(len(out["candidates"]), 1)
        self.assertIn("User.ReadBasic.All", out["directorio_no_disponible"])
        self.assertIn("reconecte", out["directorio_no_disponible"])

    def test_an_email_address_is_searched_by_mail(self):
        llamadas = []
        self._run({"name": "erojas@example.com"}, llamadas=llamadas)
        ruta = next(p for p, _ in llamadas if p.startswith("/users"))
        self.assertIn("mail%3Aerojas%40example.com", ruta)
>>>>>>> origin/main
