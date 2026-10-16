import importlib.util
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import HTTPException

from hermeshq.routers import internal_agents, scheduled_tasks
from hermeshq.schemas.scheduled_task import ScheduledTaskApprove, ScheduledTaskUpdate
from hermeshq.services.cron_schedule import (
    DEFAULT_TIMEZONE,
    MAX_AGENT_SCHEDULES,
    CronScheduleError,
    compute_next_run,
    generate_approval_code,
    message_contains_code,
    scheduled_task_metadata,
    validate_agent_expression,
)
from hermeshq.services.scheduler import SchedulerService

PLUGIN_TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates"

PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("hermeshq_schedule_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CronScheduleTests(unittest.TestCase):
    def test_chile_winter_offset_is_utc_minus_4(self):
        now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        self.assertEqual(compute_next_run("0 8 * * *", DEFAULT_TIMEZONE, now), datetime(2026, 7, 1, 12, 0, tzinfo=UTC))

    def test_chile_summer_offset_is_utc_minus_3(self):
        now = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
        self.assertEqual(compute_next_run("0 8 * * *", DEFAULT_TIMEZONE, now), datetime(2026, 1, 10, 11, 0, tzinfo=UTC))

    def test_legacy_utc_schedule_unchanged(self):
        now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        self.assertEqual(compute_next_run("0 8 * * *", "UTC", now), datetime(2026, 7, 1, 8, 0, tzinfo=UTC))

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(CronScheduleError):
            compute_next_run("0 8 * * *", "Mars/Olympus")

    def test_invalid_expression_rejected(self):
        with self.assertRaises(CronScheduleError):
            compute_next_run("not a cron", DEFAULT_TIMEZONE)

    def test_agent_expression_accepts_weekday_morning(self):
        now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        first = validate_agent_expression("0 8 * * 1-5", DEFAULT_TIMEZONE, now=now)
        self.assertEqual(first, datetime(2026, 7, 1, 12, 0, tzinfo=UTC))

    def test_agent_expression_rejects_six_fields(self):
        with self.assertRaises(CronScheduleError):
            validate_agent_expression("* * * * * *", DEFAULT_TIMEZONE)

    def test_agent_expression_rejects_every_minute_and_five_minutes(self):
        for expression in ("* * * * *", "*/5 * * * *", "0,10 8 * * *"):
            with self.subTest(expression=expression), self.assertRaises(CronScheduleError):
                validate_agent_expression(expression, DEFAULT_TIMEZONE)

    def test_agent_expression_accepts_every_fifteen_minutes(self):
        validate_agent_expression("*/15 * * * *", DEFAULT_TIMEZONE)

    def test_dst_boundary_does_not_trip_interval_check(self):
        now = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        validate_agent_expression("0 8 * * *", DEFAULT_TIMEZONE, now=now)

    def test_metadata_carries_owner_and_agent_flag(self):
        schedule = SimpleNamespace(id="s1", owner_user_id="u1", created_by_agent_id="a1")
        metadata = scheduled_task_metadata(schedule, manual=True)
        self.assertEqual(metadata["thread_user_id"], "u1")
        self.assertTrue(metadata["scheduled"])
        self.assertTrue(metadata["manual_trigger"])
        self.assertTrue(metadata["agent_created_schedule"])

    def test_metadata_without_owner_has_no_user(self):
        schedule = SimpleNamespace(id="s1", owner_user_id=None, created_by_agent_id=None)
        self.assertNotIn("thread_user_id", scheduled_task_metadata(schedule))

    def test_scheduler_next_run_uses_schedule_timezone(self):
        scheduler = SchedulerService(session_factory=Mock(), on_task_created=AsyncMock())
        now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        self.assertEqual(
            scheduler._get_next_run("0 8 * * *", now, DEFAULT_TIMEZONE), datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        )


class ApprovalCodeTests(unittest.TestCase):
    def test_generated_code_is_six_unambiguous_characters(self):
        for _ in range(50):
            code = generate_approval_code()
            self.assertEqual(len(code), 6)
            self.assertFalse(set(code) & set("01OIL"))

    def test_code_must_appear_as_a_whole_word_case_insensitive(self):
        self.assertTrue(message_contains_code("aprobar K7M2QX", "K7M2QX"))
        self.assertTrue(message_contains_code("Aprobar k7m2qx por favor", "K7M2QX"))
        self.assertFalse(message_contains_code("aprobar K7M2QXZ", "K7M2QX"))
        self.assertFalse(message_contains_code("aprobar", "K7M2QX"))
        self.assertFalse(message_contains_code("aprobar K7M2QX", None))
        self.assertFalse(message_contains_code(None, "K7M2QX"))


class PluginManifestTests(unittest.TestCase):
    def test_manifest_name_matches_directory_so_runtime_enables_it(self):
        import yaml

        for plugin_dir in sorted(PLUGIN_TEMPLATES_ROOT.iterdir()):
            manifest_path = plugin_dir / "plugin.yaml"
            if not manifest_path.exists():
                continue
            with self.subTest(plugin=plugin_dir.name):
                manifest = yaml.safe_load(manifest_path.read_text())
                self.assertEqual(manifest["name"], plugin_dir.name)

    def test_schedule_manifest_lists_registered_tools(self):
        import yaml

        manifest = yaml.safe_load((PLUGIN_TEMPLATES_ROOT / "hermeshq_schedule" / "plugin.yaml").read_text())
        ctx = MagicMock()
        _load_plugin().register(ctx)
        registered = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        self.assertEqual(sorted(manifest["provides_tools"]), sorted(registered))


class SchedulePluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()

    def _env(self, metadata):
        return patch.dict(
            "os.environ",
            {"HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": metadata})},
        )

    def test_blocked_inside_scheduled_run(self):
        with self._env({"scheduled": True, "thread_user_id": "u1"}), patch.object(self.plugin, "_api_request") as api:
            result = json.loads(
                self.plugin.hq_schedule_request_tool({"name": "n", "cron_expression": "0 8 * * *", "prompt": "p"})
            )
        self.assertFalse(result["success"])
        api.assert_not_called()

    def test_requires_identified_user(self):
        with self._env({}), patch.object(self.plugin, "_api_request") as api:
            result = json.loads(
                self.plugin.hq_schedule_request_tool({"name": "n", "cron_expression": "0 8 * * *", "prompt": "p"})
            )
        self.assertFalse(result["success"])
        api.assert_not_called()

    def test_requires_all_fields(self):
        with self._env({"thread_user_id": "u1"}), patch.object(self.plugin, "_api_request") as api:
            result = json.loads(self.plugin.hq_schedule_request_tool({"name": "n"}))
        self.assertFalse(result["success"])
        api.assert_not_called()

    def test_posts_user_from_task_payload_not_from_args(self):
        with self._env({"thread_user_id": "u1"}), patch.object(self.plugin, "_api_request", return_value="{}") as api:
            self.plugin.hq_schedule_request_tool(
                {"name": "n", "cron_expression": "0 8 * * *", "prompt": "p", "requested_for_user_id": "attacker"}
            )
        payload = api.call_args.args[2]
        self.assertEqual(payload["requested_for_user_id"], "u1")

    def test_approve_sends_task_id_from_payload(self):
        payload = {"metadata": {"thread_user_id": "u1"}, "task_id": "t-123"}
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(self.plugin, "_api_request", return_value="{}") as api,
        ):
            self.plugin.hq_schedule_approve_tool({"schedule_id": "s1"})
        self.assertEqual(api.call_args.args[:2], ("POST", "/agents/self/schedules/s1/approve"))
        self.assertEqual(api.call_args.args[2], {"task_id": "t-123"})

    def test_approve_blocked_inside_scheduled_run_and_without_task(self):
        scheduled = {"metadata": {"scheduled": True}, "task_id": "t-1"}
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(scheduled)}),
            patch.object(self.plugin, "_api_request") as api,
        ):
            result = json.loads(self.plugin.hq_schedule_approve_tool({"schedule_id": "s1"}))
        self.assertFalse(result["success"])
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps({"metadata": {}})}),
            patch.object(self.plugin, "_api_request") as api,
        ):
            result = json.loads(self.plugin.hq_schedule_approve_tool({"schedule_id": "s1"}))
        self.assertFalse(result["success"])
        api.assert_not_called()

    def test_registers_four_tools_in_own_toolset(self):
        ctx = MagicMock()
        self.plugin.register(ctx)
        names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        self.assertEqual(
            names, ["hq_schedule_request", "hq_schedule_approve", "hq_schedule_list", "hq_schedule_delete"]
        )
        self.assertTrue(all(call.kwargs["toolset"] == "hermeshq_schedule" for call in ctx.register_tool.call_args_list))


def _db_with_existing(existing, user=None):
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    lock_result = MagicMock()
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = existing
    db.execute = AsyncMock(side_effect=[lock_result, existing_result])
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _payload(**overrides):
    data = {
        "name": "Informe diario",
        "cron_expression": "0 8 * * 1-5",
        "prompt": "Ejecuta la skill informe-diario-correos.",
        "requested_for_user_id": "u1",
    }
    data.update(overrides)
    return internal_agents.InternalScheduleCreate(**data)


class InternalScheduleEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(id="agent-1")
        self.user = SimpleNamespace(id="u1", is_active=True, role="user")

    async def _create(self, db, payload, access=True):
        with patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=access)):
            return await internal_agents.create_own_schedule(payload, self.agent, db)

    async def test_creates_pending_disabled_schedule_in_chile_time(self):
        db = _db_with_existing([], self.user)
        result = await self._create(db, _payload())
        created = db.add.call_args_list[0].args[0]
        self.assertFalse(created.enabled)
        self.assertEqual(created.approval_status, "pending")
        self.assertEqual(created.timezone, DEFAULT_TIMEZONE)
        self.assertEqual(created.owner_user_id, "u1")
        self.assertEqual(created.created_by_agent_id, "agent-1")
        self.assertIsNone(created.next_run)
        self.assertEqual(result["approval_status"], "pending")
        db.commit.assert_awaited()

    async def test_rejects_without_user_context(self):
        db = _db_with_existing([], self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload(requested_for_user_id=None))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_user_without_access_to_agent(self):
        db = _db_with_existing([], self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload(), access=False)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_rejects_inactive_user(self):
        db = _db_with_existing([], SimpleNamespace(id="u1", is_active=False, role="user"))
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload())
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_rejects_too_frequent_cron(self):
        db = _db_with_existing([], self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload(cron_expression="* * * * *"))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_oversized_prompt(self):
        db = _db_with_existing([], self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload(prompt="x" * 5000))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_enforces_per_agent_limit(self):
        existing = [
            SimpleNamespace(
                id=f"s{i}",
                name=f"job {i}",
                cron_expression="0 9 * * *",
                prompt=f"p{i}",
                approval_status="approved",
                created_by_agent_id="agent-1",
            )
            for i in range(MAX_AGENT_SCHEDULES)
        ]
        db = _db_with_existing(existing, self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload())
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_rejects_duplicate_name(self):
        existing = [
            SimpleNamespace(
                id="s1",
                name="informe DIARIO",
                cron_expression="0 9 * * *",
                prompt="otro",
                approval_status="pending",
                created_by_agent_id="agent-1",
            )
        ]
        db = _db_with_existing(existing, self.user)
        with self.assertRaises(HTTPException) as ctx:
            await self._create(db, _payload())
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_delete_refuses_schedule_not_created_by_agent(self):
        db = MagicMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id="s1", agent_id="agent-1", created_by_agent_id=None, name="manual")
        )
        with self.assertRaises(HTTPException) as ctx:
            await internal_agents.delete_own_schedule("s1", self.agent, db)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_delete_refuses_other_agents_schedule(self):
        db = MagicMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(id="s1", agent_id="agent-2", created_by_agent_id="agent-2", name="x")
        )
        with self.assertRaises(HTTPException) as ctx:
            await internal_agents.delete_own_schedule("s1", self.agent, db)
        self.assertEqual(ctx.exception.status_code, 404)


def _agent_pending_item(owner="u1", code="K7M2QX", age_hours=1):
    item = _pending_item(owner)
    item.approval_code = code
    item.created_at = datetime.now(UTC) - timedelta(hours=age_hours)
    return item


def _chat_task(prompt="aprobar K7M2QX", user="u1", status="running", agent="agent-1", extra=None):
    metadata = {"thread_user_id": user, "source": "public_chat", **(extra or {})}
    return SimpleNamespace(id="t1", agent_id=agent, status=status, prompt=prompt, metadata_json=metadata)


class ChatApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(id="agent-1")

    async def _approve(self, item, task, task_id="t1"):
        db = MagicMock()
        db.get = AsyncMock(side_effect=lambda model, key: task if model.__name__ == "Task" else item)
        db.add = MagicMock()
        db.commit = AsyncMock()
        body = internal_agents.InternalScheduleApprove(task_id=task_id)
        return await internal_agents.approve_own_schedule("s1", body, self.agent, db)

    async def test_approves_when_user_message_contains_code(self):
        item = _agent_pending_item()
        result = await self._approve(item, _chat_task())
        self.assertEqual(item.approval_status, "approved")
        self.assertTrue(item.enabled)
        self.assertIsNone(item.approval_code)
        self.assertIsNotNone(item.next_run)
        self.assertEqual(result["approval_status"], "approved")

    async def _assert_rejected(self, item, task, status_code):
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, task)
        self.assertEqual(ctx.exception.status_code, status_code)
        self.assertEqual(item.approval_status, "pending")
        self.assertFalse(item.enabled)

    async def test_rejects_when_user_message_lacks_code(self):
        await self._assert_rejected(_agent_pending_item(), _chat_task(prompt="si, apruebala"), 403)

    async def test_rejects_wrong_code(self):
        await self._assert_rejected(_agent_pending_item(), _chat_task(prompt="aprobar ZZZZZZ"), 403)

    async def test_rejects_other_user(self):
        await self._assert_rejected(_agent_pending_item(), _chat_task(user="intruder"), 403)

    async def test_rejects_scheduled_run(self):
        await self._assert_rejected(_agent_pending_item(), _chat_task(extra={"scheduled": True}), 403)

    async def test_rejects_task_of_another_agent_or_not_running(self):
        await self._assert_rejected(_agent_pending_item(), _chat_task(agent="agent-2"), 403)
        await self._assert_rejected(_agent_pending_item(), _chat_task(status="completed"), 403)

    async def test_rejects_without_task(self):
        item = _agent_pending_item()
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, None, task_id="missing")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_rejects_expired_code(self):
        await self._assert_rejected(_agent_pending_item(age_hours=49), _chat_task(), 410)

    async def test_rejects_schedule_not_created_by_agent(self):
        item = _agent_pending_item()
        item.created_by_agent_id = None
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, _chat_task())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_rejects_already_approved(self):
        item = _agent_pending_item()
        item.approval_status = "approved"
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, _chat_task())
        self.assertEqual(ctx.exception.status_code, 409)


def _pending_item(owner="u1"):
    return SimpleNamespace(
        id="s1",
        agent_id="agent-1",
        name="Informe",
        cron_expression="0 8 * * 1-5",
        timezone=DEFAULT_TIMEZONE,
        prompt="p",
        enabled=False,
        approval_status="pending",
        owner_user_id=owner,
        created_by_agent_id="agent-1",
        notify_public_chat=False,
        next_run=None,
        last_run=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def _approve(self, item, user_id, prompt=None, cron=None):
        db = MagicMock()
        db.get = AsyncMock(return_value=item)
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        user = SimpleNamespace(id=user_id, role="user")
        with patch.object(scheduled_tasks, "ensure_agent_access", AsyncMock()):
            body = ScheduledTaskApprove(
                cron_expression=cron if cron is not None else item.cron_expression,
                prompt=prompt if prompt is not None else item.prompt,
            )
            return await scheduled_tasks.approve_scheduled_task("s1", body, user, db)

    async def test_owner_can_approve(self):
        item = _pending_item()
        result = await self._approve(item, "u1")
        self.assertEqual(item.approval_status, "approved")
        self.assertTrue(item.enabled)
        self.assertIsNotNone(item.next_run)
        self.assertEqual(result.approval_status, "approved")

    async def test_other_user_cannot_approve(self):
        item = _pending_item()
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, "someone-else")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(item.approval_status, "pending")

    async def test_approval_rejected_when_prompt_differs_from_reviewed_one(self):
        item = _pending_item()
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, "u1", prompt="otro prompt")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(item.approval_status, "pending")
        self.assertFalse(item.enabled)

    async def test_approval_rejected_when_cron_differs_from_reviewed_one(self):
        item = _pending_item()
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, "u1", cron="* * * * *")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(item.approval_status, "pending")

    async def test_cannot_approve_twice(self):
        item = _pending_item()
        item.approval_status = "approved"
        with self.assertRaises(HTTPException) as ctx:
            await self._approve(item, "u1")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_update_cannot_enable_pending_schedule(self):
        item = _pending_item()
        db = MagicMock()
        db.get = AsyncMock(return_value=item)
        with (
            patch.object(scheduled_tasks, "ensure_agent_access", AsyncMock()),
            self.assertRaises(HTTPException) as ctx,
        ):
            await scheduled_tasks.update_scheduled_task(
                "s1", ScheduledTaskUpdate(enabled=True), SimpleNamespace(id="u1"), db
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertFalse(item.enabled)


if __name__ == "__main__":
    unittest.main()
