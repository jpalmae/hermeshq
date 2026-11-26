import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.routers import internal_agents
from hermeshq.services import reemplazo_cron, schedule_approval


def _cron(id_, nombre="Alertas de seguridad", owner="u1", creador="agent-1", agente="agent-1"):
    return ScheduledTask(
        id=id_,
        agent_id=agente,
        name=nombre,
        cron_expression="*/30 * * * *",
        prompt="Entrega las últimas 3 alertas.",
        owner_user_id=owner,
        created_by_agent_id=creador,
        approval_status="approved",
        enabled=True,
        timezone="America/Santiago",
    )


def _resultado(filas):
    res = MagicMock()
    res.scalars.return_value.all.return_value = filas
    return res


def _tarea(user="u1"):
    return SimpleNamespace(
        id="t1",
        agent_id="agent-1",
        status="running",
        created_by_user_id=None,
        prompt="en el cron dame las últimas 5",
        metadata_json={"thread_user_id": user, "source": "public_chat"},
    )


class TestCrearConReemplazo(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(id="agent-1")
        self.user = SimpleNamespace(id="u1", is_active=True, role="user")

    def _db(self, anterior, existentes):
        tarea = _tarea()

        def obtener(modelo, clave):
            if modelo.__name__ == "Task":
                return tarea
            if modelo.__name__ == "ScheduledTask":
                return anterior if anterior is not None and anterior.id == clave else None
            return self.user

        db = MagicMock()
        db.get = AsyncMock(side_effect=obtener)
        db.execute = AsyncMock(side_effect=[MagicMock(), _resultado(existentes)])
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        return db

    async def _crear(self, db, **extra):
        datos = {
            "name": "Alertas de seguridad (5)",
            "cron_expression": "*/30 * * * *",
            "prompt": "Entrega las últimas 5 alertas.",
            "requested_for_user_id": "u1",
            "task_id": "t1",
            **extra,
        }
        with patch.object(internal_agents, "can_access_agent", AsyncMock(return_value=True)):
            return await internal_agents.create_own_schedule(
                internal_agents.InternalScheduleCreate(**datos), self.agent, db
            )

    async def test_caso_real_cambiar_un_cron_crea_el_nuevo_pendiente_y_registra_el_reemplazo(self):
        anterior = _cron("viejo")
        db = self._db(anterior, [anterior])
        res = await self._crear(db, replaces_schedule_id="viejo")
        self.assertEqual(res["approval_status"], "pending")
        self.assertEqual(res["replaces"], {"schedule_id": "viejo", "name": "Alertas de seguridad"})
        pedidos = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ActivityLog)]
        self.assertTrue(
            any(p.event_type == reemplazo_cron.EVENTO_PEDIDO and p.details["reemplaza"] == "viejo" for p in pedidos)
        )
        self.assertTrue(anterior.enabled)

    async def test_el_cron_reemplazado_no_cuenta_para_el_limite_ni_como_duplicado(self):
        anterior = _cron("viejo")
        otros = [_cron(f"c{i}", nombre=f"Otro {i}") for i in range(4)]
        db = self._db(anterior, [anterior, *otros])
        res = await self._crear(db, replaces_schedule_id="viejo", name="Alertas de seguridad")
        self.assertEqual(res["approval_status"], "pending")

    async def test_no_reemplaza_un_cron_de_otro_usuario(self):
        ajeno = _cron("ajeno", owner="u2")
        db = self._db(ajeno, [ajeno])
        with self.assertRaises(HTTPException) as ctx:
            await self._crear(db, replaces_schedule_id="ajeno")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_no_reemplaza_un_cron_creado_en_la_web(self):
        de_la_web = _cron("web", creador=None)
        db = self._db(de_la_web, [de_la_web])
        with self.assertRaises(HTTPException) as ctx:
            await self._crear(db, replaces_schedule_id="web")
        self.assertEqual(ctx.exception.status_code, 404)


class TestAplicarReemplazo(unittest.IsolatedAsyncioTestCase):
    def _db(self, anterior, pedidos):
        db = MagicMock()
        db.execute = AsyncMock(return_value=_resultado(pedidos))
        db.get = AsyncMock(return_value=anterior)
        db.add = MagicMock()
        db.delete = AsyncMock()
        return db

    async def test_al_aprobar_el_nuevo_se_elimina_el_anterior_y_queda_su_definicion(self):
        anterior, nuevo = _cron("viejo"), _cron("nuevo", nombre="Alertas (5)")
        pedido = ActivityLog(
            agent_id="agent-1",
            event_type=reemplazo_cron.EVENTO_PEDIDO,
            details={"schedule_id": "nuevo", "reemplaza": "viejo"},
        )
        db = self._db(anterior, [pedido])
        self.assertEqual(await reemplazo_cron.aplicar_reemplazo(db, nuevo, "u1"), "Alertas de seguridad")
        db.delete.assert_awaited_once_with(anterior)
        registro = db.add.call_args.args[0]
        self.assertEqual(registro.event_type, reemplazo_cron.EVENTO_HECHO)
        self.assertEqual(registro.details["definicion"]["prompt"], "Entrega las últimas 3 alertas.")

    async def test_sin_pedido_de_reemplazo_no_borra_nada(self):
        db = self._db(_cron("viejo"), [])
        self.assertIsNone(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"))
        db.delete.assert_not_awaited()

    async def test_no_borra_un_cron_de_otro_usuario_aunque_el_registro_lo_diga(self):
        pedido = ActivityLog(
            agent_id="agent-1",
            event_type=reemplazo_cron.EVENTO_PEDIDO,
            details={"schedule_id": "nuevo", "reemplaza": "ajeno"},
        )
        db = self._db(_cron("ajeno", owner="u2"), [pedido])
        self.assertIsNone(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"))
        db.delete.assert_not_awaited()


class TestAprobarPorCodigo(unittest.IsolatedAsyncioTestCase):
    async def test_el_mensaje_de_activacion_dice_que_reemplazo_al_anterior(self):
        nuevo = _cron("nuevo", nombre="Alertas (5)")
        nuevo.approval_status, nuevo.enabled, nuevo.approval_code = "pending", False, "K7M2QX"
        nuevo.created_at = datetime.now(UTC) - timedelta(minutes=5)
        db = MagicMock()
        db.execute = AsyncMock(return_value=_resultado([nuevo]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        with patch(
            "hermeshq.services.reemplazo_cron.aplicar_reemplazo", AsyncMock(return_value="Alertas de seguridad")
        ):
            texto = await schedule_approval.approve_by_code(db, "agent-1", "u1", "K7M2QX", "public_chat")
        self.assertIn("Activada la programación «Alertas (5)»", texto)
        self.assertIn("Reemplaza a «Alertas de seguridad», que quedó eliminada", texto)
        self.assertTrue(nuevo.enabled)


class TestReemplazoNuncaBloqueaLaAprobacion(unittest.IsolatedAsyncioTestCase):
    async def test_si_la_busqueda_falla_la_aprobacion_sigue(self):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("bd caída"))
        self.assertIsNone(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"))

    async def test_sin_agente_no_busca(self):
        db = MagicMock()
        db.execute = AsyncMock()
        self.assertIsNone(await reemplazo_cron.aplicar_reemplazo(db, SimpleNamespace(id="x"), "u1"))
        db.execute.assert_not_awaited()
