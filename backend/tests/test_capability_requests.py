import importlib.util
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.routers import capability_requests as admin_router
from hermeshq.routers import internal_agents
from hermeshq.services.capability_requests import clean_files, fingerprint

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
_spec = importlib.util.spec_from_file_location("schedule_plugin_requests_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


class TestHelpers(unittest.TestCase):
    def test_similar_titles_share_a_fingerprint(self):
        self.assertEqual(
            fingerprint("Dashboard con páginas de licencias"), fingerprint("licencias: dashboard con paginas")
        )
        self.assertNotEqual(fingerprint("Informe de compras"), fingerprint("Informe de accidentes"))
        self.assertEqual(fingerprint("!!"), "sin-titulo")

    def test_files_are_cleaned_and_capped(self):
        self.assertEqual(clean_files(["a.xlsx", "", "  b.pdf "]), ["a.xlsx", "b.pdf"])
        self.assertEqual(len(clean_files([f"f{i}" for i in range(20)])), 8)
        self.assertEqual(clean_files("no es lista"), [])


class TestTool(unittest.TestCase):
    def _llamar(self, args, metadata=None):
        payload = {"task_id": "t1", "metadata": metadata or {}}
        with (
            patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_api_request", return_value=json.dumps({"success": True})) as api,
        ):
            return json.loads(plugin.hq_solicitar_capacidad_tool(args)), api

    def test_it_sends_the_request_with_the_live_task(self):
        resultado, api = self._llamar(
            {
                "titulo": "Informe de compras",
                "que_necesita": "Un informe mensual",
                "ejemplo": "enero",
                "archivos": ["work/x.xlsx"],
            }
        )
        self.assertTrue(resultado["success"])
        ruta, cuerpo = api.call_args.args[1], api.call_args.args[2]
        self.assertEqual(ruta, "/agents/self/capability-requests")
        self.assertEqual(
            (cuerpo["task_id"], cuerpo["title"], cuerpo["files"], cuerpo["attempts"]),
            ("t1", "Informe de compras", ["work/x.xlsx"], None),
        )

    def test_title_and_need_are_required(self):
        resultado, api = self._llamar({"titulo": "x"})
        self.assertFalse(resultado["success"])
        api.assert_not_called()


def _origen(**metadata):
    base = {"source": "public_chat", "thread_user_id": "u1"}
    base.update(metadata)
    return SimpleNamespace(id="t1", agent_id="a1", status="running", metadata_json=base)


def _db(origen, abiertas, recientes):
    db = MagicMock()
    db.get = AsyncMock(return_value=origen)
    primero, segundo = MagicMock(), MagicMock()
    primero.scalars.return_value.first.return_value = abiertas[0] if abiertas else None
    segundo.scalars.return_value.all.return_value = recientes
    db.execute = AsyncMock(side_effect=[primero, segundo])
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


class TestEndpoint(unittest.IsolatedAsyncioTestCase):
    def _payload(self, **extra):
        base = {"task_id": "t1", "title": "Dashboard con páginas", "description": "Quiere un menú lateral"}
        base.update(extra)
        return internal_agents.InternalCapabilityRequest(**base)

    async def test_a_new_request_is_stored_with_the_user_and_files(self):
        db = _db(_origen(), [], [])
        resultado = await internal_agents.create_capability_request(
            self._payload(files=["work/a.xlsx"], example="Licencias"), agent=SimpleNamespace(id="a1"), db=db
        )
        self.assertTrue(resultado["success"])
        self.assertFalse(resultado["ya_existia"])
        item = next(c.args[0] for c in db.add.call_args_list if c.args[0].__class__.__name__ == "CapabilityRequest")
        self.assertEqual(
            (item.requested_by_user_id, item.files_json, item.example), ("u1", ["work/a.xlsx"], "Licencias")
        )
        self.assertEqual(item.fingerprint, fingerprint("Dashboard con páginas"))

    async def test_an_equal_open_request_only_counts_another_person(self):
        existente = SimpleNamespace(id="r1", me_too_count=1, example=None)
        db = _db(_origen(), [existente], [])
        resultado = await internal_agents.create_capability_request(
            self._payload(example="otro ejemplo"), agent=SimpleNamespace(id="a1"), db=db
        )
        self.assertTrue(resultado["ya_existia"])
        self.assertEqual((existente.me_too_count, existente.example), (2, "otro ejemplo"))
        db.add.assert_not_called()

    async def test_there_is_a_daily_limit_per_agent(self):
        db = _db(_origen(), [], [object()] * 8)
        with self.assertRaises(HTTPException) as contexto:
            await internal_agents.create_capability_request(self._payload(), agent=SimpleNamespace(id="a1"), db=db)
        self.assertEqual(contexto.exception.status_code, 429)

    async def test_it_needs_a_live_conversation_and_content(self):
        for origen in (
            None,
            SimpleNamespace(id="t1", agent_id="otro", status="running", metadata_json={}),
            SimpleNamespace(id="t1", agent_id="a1", status="completed", metadata_json={}),
        ):
            db = _db(origen, [], [])
            db.get = AsyncMock(return_value=origen)
            with self.assertRaises(HTTPException) as contexto:
                await internal_agents.create_capability_request(self._payload(), agent=SimpleNamespace(id="a1"), db=db)
            self.assertEqual(contexto.exception.status_code, 403)
        with self.assertRaises(HTTPException) as contexto:
            await internal_agents.create_capability_request(
                self._payload(description="   "), agent=SimpleNamespace(id="a1"), db=_db(_origen(), [], [])
            )
        self.assertEqual(contexto.exception.status_code, 400)


class TestAdminRouter(unittest.IsolatedAsyncioTestCase):
    async def test_only_admins_can_list_and_update(self):
        with patch.object(admin_router, "is_admin", return_value=False):
            with self.assertRaises(HTTPException) as contexto:
                await admin_router.list_capability_requests(status=None, current_user=SimpleNamespace(), db=MagicMock())
            self.assertEqual(contexto.exception.status_code, 403)

    async def test_resolving_sets_the_date_and_a_bad_status_is_rejected(self):
        item = SimpleNamespace(status="nuevo", resolved_at=None, resolution=None)
        db = MagicMock()
        db.get = AsyncMock(return_value=item)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        vista = SimpleNamespace(agent_name=None)
        with (
            patch.object(admin_router, "is_admin", return_value=True),
            patch.object(admin_router, "_view", AsyncMock(return_value=vista)),
        ):
            await admin_router.update_capability_request(
                "r1",
                admin_router.CapabilityRequestUpdate(status="resuelto", resolution=" Se creó hq_x "),
                SimpleNamespace(),
                db,
            )
            self.assertEqual((item.status, item.resolution), ("resuelto", "Se creó hq_x"))
            self.assertIsInstance(item.resolved_at, datetime)
            self.assertEqual(item.resolved_at.tzinfo, UTC)
            await admin_router.update_capability_request(
                "r1", admin_router.CapabilityRequestUpdate(status="nuevo"), SimpleNamespace(), db
            )
            self.assertIsNone(item.resolved_at)
            with self.assertRaises(HTTPException) as contexto:
                await admin_router.update_capability_request(
                    "r1", admin_router.CapabilityRequestUpdate(status="raro"), SimpleNamespace(), db
                )
            self.assertEqual(contexto.exception.status_code, 400)
