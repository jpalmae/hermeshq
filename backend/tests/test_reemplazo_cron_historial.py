import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.services import reemplazo_cron


def _cron(id_):
    return ScheduledTask(
        id=id_,
        agent_id="agent-1",
        name="Alertas",
        cron_expression="*/30 * * * *",
        prompt="p",
        owner_user_id="u1",
        created_by_agent_id="agent-1",
        approval_status="approved",
        enabled=True,
        timezone="America/Santiago",
    )


def _resultado(filas):
    res = MagicMock()
    res.scalars.return_value.all.return_value = filas
    return res


def _ejecucion(id_, **extra):
    return SimpleNamespace(id=id_, metadata_json={"scheduled_task_id": "viejo", "scheduled": True, **extra})


class TestHistorialDelCronEditado(unittest.IsolatedAsyncioTestCase):
    def _db(self, anterior, pedidos, ejecuciones):
        db = MagicMock()
        db.get = AsyncMock(return_value=anterior)
        db.execute = AsyncMock(side_effect=[_resultado(pedidos), _resultado(ejecuciones)])
        db.add = MagicMock()
        db.delete = AsyncMock()
        return db

    def _pedido(self):
        return ActivityLog(
            agent_id="agent-1",
            event_type=reemplazo_cron.EVENTO_PEDIDO,
            details={"schedule_id": "nuevo", "reemplaza": "viejo"},
        )

    async def test_las_ejecuciones_pasan_al_cron_nuevo_y_recuerdan_el_anterior(self):
        corridas = [_ejecucion("r1"), _ejecucion("r2", manual=True)]
        db = self._db(_cron("viejo"), [self._pedido()], corridas)
        self.assertEqual(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"), "Alertas")
        for corrida in corridas:
            self.assertEqual(corrida.metadata_json["scheduled_task_id"], "nuevo")
            self.assertEqual(corrida.metadata_json["scheduled_task_id_anterior"], "viejo")
            self.assertTrue(corrida.metadata_json["scheduled"])
        self.assertEqual(corridas[1].metadata_json["manual"], True)
        db.delete.assert_awaited_once()

    async def test_si_falla_el_traspaso_el_reemplazo_sigue(self):
        db = MagicMock()
        db.get = AsyncMock(return_value=_cron("viejo"))
        db.execute = AsyncMock(side_effect=[_resultado([self._pedido()]), RuntimeError("bd")])
        db.add = MagicMock()
        db.delete = AsyncMock()
        self.assertEqual(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"), "Alertas")
        db.delete.assert_awaited_once()

    async def test_control_negativo_sin_pedido_no_toca_ninguna_ejecucion(self):
        corridas = [_ejecucion("r1")]
        db = self._db(_cron("viejo"), [], corridas)
        self.assertIsNone(await reemplazo_cron.aplicar_reemplazo(db, _cron("nuevo"), "u1"))
        self.assertEqual(corridas[0].metadata_json["scheduled_task_id"], "viejo")


if __name__ == "__main__":
    unittest.main()
