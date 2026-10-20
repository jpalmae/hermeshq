import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ms365_calendar_plugin_under_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCalendarTeamsMeeting(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.calls = []

    def _run(self, tool, args, response):
        def graph(method, path, token, payload=None):
            self.calls.append((method, path, payload))
            return response

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(tool(args))

    def test_create_without_flag_does_not_request_teams(self):
        self._run(
            self.mod._create_event_tool,
            {"subject": "x", "start": "2026-10-08T10:00:00", "end": "2026-10-08T11:00:00"},
            {"id": "E1"},
        )
        payload = self.calls[0][2]
        self.assertNotIn("isOnlineMeeting", payload)

    def test_create_with_flag_requests_teams_and_returns_link(self):
        response = {"id": "E1", "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"}}
        out = self._run(
            self.mod._create_event_tool,
            {"subject": "x", "start": "2026-10-08T10:00:00", "end": "2026-10-08T11:00:00", "online_meeting": True},
            response,
        )
        payload = self.calls[0][2]
        self.assertTrue(payload["isOnlineMeeting"])
        self.assertEqual(payload["onlineMeetingProvider"], "teamsForBusiness")
        self.assertEqual(out["teams_join_url"], "https://teams.microsoft.com/l/meetup-join/abc")

    def test_update_can_add_teams_alone(self):
        out = self._run(
            self.mod._update_event_tool, {"event_id": "E1", "online_meeting": True}, {"id": "E1", "onlineMeeting": None}
        )
        self.assertTrue(out["success"])
        self.assertEqual(self.calls[0][0], "PATCH")
        self.assertTrue(self.calls[0][2]["isOnlineMeeting"])
        self.assertNotIn("teams_join_url", out)


class TestAttendeeParsing(unittest.TestCase):
    def test_splits_semicolons_commas_and_dedupes(self):
        mod = _load()
        self.assertEqual(
            mod._split_attendees(["a@x.cl;b@y.cl", "B@y.cl, c@z.cl"]),
            ["a@x.cl", "b@y.cl", "c@z.cl"],
        )

    def test_ignores_non_addresses(self):
        mod = _load()
        self.assertEqual(mod._split_attendees("Juan; a@x.cl"), ["a@x.cl"])


CURRENT = [
    {"emailAddress": {"name": "Ana", "address": "ana@x.cl"}, "type": "required", "status": {"response": "accepted"}},
    {"emailAddress": {"name": "Beto", "address": "beto@x.cl"}, "type": "optional"},
]


class TestUpdateAttendees(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.calls = []

    def _run(self, args):
        def graph(method, path, token, payload=None):
            self.calls.append((method, path, payload))
            if method == "GET":
                return {"attendees": CURRENT}
            return {"id": "E1"}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(self.mod._update_event_tool({"event_id": "E1", **args}))

    def _patched(self):
        return [c for c in self.calls if c[0] == "PATCH"][0][2]["attendees"]

    def test_add_keeps_existing_attendees_and_their_data(self):
        out = self._run({"add_attendees": ["nuevo@y.cl;otro@y.cl"]})
        self.assertTrue(out["success"])
        addresses = [a["emailAddress"]["address"] for a in self._patched()]
        self.assertEqual(addresses, ["ana@x.cl", "beto@x.cl", "nuevo@y.cl", "otro@y.cl"])
        self.assertEqual(self._patched()[1]["type"], "optional")

    def test_add_is_idempotent(self):
        self._run({"add_attendees": ["ANA@x.cl"]})
        self.assertEqual(len(self._patched()), 2)

    def test_remove_drops_only_the_requested_ones(self):
        self._run({"remove_attendees": ["beto@x.cl"]})
        self.assertEqual([a["emailAddress"]["address"] for a in self._patched()], ["ana@x.cl"])

    def test_full_list_replaces_but_reuses_known_entries(self):
        self._run({"attendees": ["beto@x.cl", "nuevo@y.cl"]})
        patched = self._patched()
        self.assertEqual([a["emailAddress"]["address"] for a in patched], ["beto@x.cl", "nuevo@y.cl"])
        self.assertEqual(patched[0]["type"], "optional")
        self.assertEqual(patched[1]["type"], "required")

    def test_full_list_only_does_not_need_a_lookup_failure_to_succeed(self):
        out = self._run({"attendees": ["solo@y.cl"]})
        self.assertTrue(out["success"])

    def test_without_attendee_args_nothing_is_fetched(self):
        self._run({"subject": "Nuevo"})
        self.assertEqual([c[0] for c in self.calls], ["PATCH"])
        self.assertNotIn("attendees", self.calls[0][2])


class TestEditEverything(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.calls = []

    def _run(self, tool, args, current=None):
        def graph(method, path, token, payload=None):
            self.calls.append((method, path, payload))
            if method == "GET":
                return current or {}
            return {"id": "E1"}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(tool(args))

    def _patch(self):
        return [c for c in self.calls if c[0] == "PATCH"][0][2]

    def test_teams_can_be_turned_off(self):
        out = self._run(self.mod._update_event_tool, {"event_id": "E1", "online_meeting": False})
        self.assertTrue(out["success"])
        self.assertEqual(self._patch(), {"isOnlineMeeting": False})

    def test_teams_string_false_is_respected(self):
        self._run(self.mod._update_event_tool, {"event_id": "E1", "online_meeting": "false"})
        self.assertEqual(self._patch(), {"isOnlineMeeting": False})

    def test_new_start_keeps_current_duration(self):
        current = {
            "start": {"dateTime": "2026-10-10T13:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-10T14:30:00.0000000", "timeZone": "UTC"},
        }
        self._run(
            self.mod._update_event_tool,
            {"event_id": "E1", "start": "2026-10-12T15:00:00", "timezone": "America/Santiago"},
            current,
        )
        patch_body = self._patch()
        self.assertEqual(patch_body["start"], {"dateTime": "2026-10-12T15:00:00", "timeZone": "America/Santiago"})
        self.assertEqual(patch_body["end"], {"dateTime": "2026-10-12T16:30:00", "timeZone": "America/Santiago"})

    def test_new_start_with_offset_is_normalized_to_utc(self):
        current = {
            "start": {"dateTime": "2026-10-10T13:00:00.0000000"},
            "end": {"dateTime": "2026-10-10T14:00:00.0000000"},
        }
        self._run(self.mod._update_event_tool, {"event_id": "E1", "start": "2026-10-12T10:00:00-03:00"}, current)
        patch_body = self._patch()
        self.assertEqual(patch_body["start"], {"dateTime": "2026-10-12T13:00:00", "timeZone": "UTC"})
        self.assertEqual(patch_body["end"], {"dateTime": "2026-10-12T14:00:00", "timeZone": "UTC"})

    def test_explicit_duration_with_new_start(self):
        self._run(
            self.mod._update_event_tool,
            {"event_id": "E1", "start": "2026-10-12T09:00:00", "duration_minutes": 45},
        )
        self.assertEqual(self._patch()["end"]["dateTime"], "2026-10-12T09:45:00")
        self.assertEqual([c[0] for c in self.calls], ["PATCH"])

    def test_duration_alone_uses_current_start(self):
        current = {"start": {"dateTime": "2026-10-10T13:00:00.0000000", "timeZone": "UTC"}}
        self._run(self.mod._update_event_tool, {"event_id": "E1", "duration_minutes": 60}, current)
        self.assertEqual(self._patch()["end"], {"dateTime": "2026-10-10T14:00:00", "timeZone": "UTC"})

    def test_explicit_start_and_end_need_no_lookup(self):
        self._run(
            self.mod._update_event_tool,
            {"event_id": "E1", "start": "2026-10-12T09:00:00", "end": "2026-10-12T10:00:00"},
        )
        self.assertEqual([c[0] for c in self.calls], ["PATCH"])

    def test_clear_location_and_body_with_empty_string(self):
        self._run(self.mod._update_event_tool, {"event_id": "E1", "location": "", "body": ""})
        patch_body = self._patch()
        self.assertEqual(patch_body["location"], {"displayName": ""})
        self.assertEqual(patch_body["body"]["content"], "")

    def test_importance_reminder_showas_allday_categories(self):
        self._run(
            self.mod._update_event_tool,
            {
                "event_id": "E1",
                "importance": "HIGH",
                "show_as": "oof",
                "reminder_minutes": 30,
                "all_day": True,
                "categories": "Roja; Azul",
            },
        )
        patch_body = self._patch()
        self.assertEqual(patch_body["importance"], "high")
        self.assertEqual(patch_body["showAs"], "oof")
        self.assertEqual(patch_body["reminderMinutesBeforeStart"], 30)
        self.assertTrue(patch_body["isReminderOn"])
        self.assertTrue(patch_body["isAllDay"])
        self.assertEqual(patch_body["categories"], ["Roja", "Azul"])

    def test_invalid_importance_is_rejected_without_calling_graph(self):
        out = self._run(self.mod._update_event_tool, {"event_id": "E1", "importance": "urgente"})
        self.assertFalse(out["success"])
        self.assertEqual(self.calls, [])

    def test_add_optional_attendee_on_update(self):
        current = {"attendees": [{"emailAddress": {"address": "ana@x.cl"}, "type": "required"}]}
        self._run(self.mod._update_event_tool, {"event_id": "E1", "add_optional_attendees": ["opt@y.cl"]}, current)
        attendees = self._patch()["attendees"]
        self.assertEqual(
            [(a["emailAddress"]["address"], a["type"]) for a in attendees],
            [("ana@x.cl", "required"), ("opt@y.cl", "optional")],
        )

    def test_create_with_optional_attendees_and_extras(self):
        self._run(
            self.mod._create_event_tool,
            {
                "subject": "x",
                "start": "2026-10-12T09:00:00",
                "end": "2026-10-12T10:00:00",
                "attendees": ["a@x.cl"],
                "optional_attendees": ["b@x.cl", "a@x.cl"],
                "importance": "high",
                "online_meeting": True,
            },
        )
        payload = self.calls[0][2]
        self.assertEqual(
            [(a["emailAddress"]["address"], a["type"]) for a in payload["attendees"]],
            [("a@x.cl", "required"), ("b@x.cl", "optional")],
        )
        self.assertEqual(payload["importance"], "high")
        self.assertTrue(payload["isOnlineMeeting"])


class TestListEventsDates(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.paths = []

    def _list(self, args):
        def graph(method, path, token, payload=None):
            self.paths.append(path)
            return {"value": []}

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=graph),
        ):
            return json.loads(self.mod._list_events_tool(args))

    def _dates(self):
        path = self.paths[-1]
        start = path.split("startDateTime=")[1].split("&")[0]
        end = path.split("endDateTime=")[1].split("&")[0]
        return start, end

    def test_default_range_has_no_plus_sign_or_microseconds(self):
        self.assertTrue(self._list({})["success"])
        for value in self._dates():
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_offset_dates_are_converted_to_utc(self):
        self._list({"start": "2026-10-08T10:00:00-03:00", "end": "2026-10-08T12:00:00-03:00"})
        self.assertEqual(self._dates(), ("2026-10-08T13:00:00Z", "2026-10-08T15:00:00Z"))

    def test_plus_offset_and_microseconds_are_accepted(self):
        self._list({"start": "2026-10-08T16:38:18.816893+00:00", "end": "2026-10-09T16:38:18.816893+00:00"})
        self.assertEqual(self._dates(), ("2026-10-08T16:38:18Z", "2026-10-09T16:38:18Z"))

    def test_plus_turned_into_space_by_the_model_is_repaired(self):
        self._list({"start": "2026-10-08T16:38:18.816893 00:00", "end": "2026-10-09T16:38:18 00:00"})
        self.assertEqual(self._dates(), ("2026-10-08T16:38:18Z", "2026-10-09T16:38:18Z"))

    def test_naive_dates_are_treated_as_utc(self):
        self._list({"start": "2026-10-08T00:00:00", "end": "2026-10-09T00:00:00"})
        self.assertEqual(self._dates(), ("2026-10-08T00:00:00Z", "2026-10-09T00:00:00Z"))

    def test_garbage_date_gives_a_clear_error_without_calling_graph(self):
        out = self._list({"start": "mañana", "end": "2026-10-09T00:00:00Z"})
        self.assertFalse(out["success"])
        self.assertIn("ISO 8601", out["error"])
        self.assertEqual(self.paths, [])
