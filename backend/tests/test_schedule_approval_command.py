import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.schedule_approval import approval_code_from, approve_by_code


def _item(code="DK86ZR", owner="u1", horas=1, nombre="Informe horario correos"):
    return SimpleNamespace(
        id="s1",
        name=nombre,
        cron_expression="0 8 * * 1-5",
        timezone="America/Santiago",
        approval_code=code,
        approval_status="pending",
        enabled=False,
        next_run=None,
        owner_user_id=owner,
        created_at=datetime.now(UTC) - timedelta(hours=horas),
    )


def _db(items):
    db = MagicMock()
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = items
    db.execute = AsyncMock(return_value=resultado)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


class TestCommandParsing(unittest.TestCase):
    def test_only_a_bare_approval_message_counts(self):
        for texto in ("aprobar DK86ZR", "Aprobar dk86zr", "  APROBAR DK86ZR. ", "aprobar DK86ZR!"):
            self.assertEqual(approval_code_from(texto), "DK86ZR", texto)
        for texto in ("aprobar", "aprobar DK86ZR y además envía algo", "quiero aprobar DK86ZR", "", None, "aprobar A"):
            self.assertIsNone(approval_code_from(texto), texto)


class TestApproveByCode(unittest.IsolatedAsyncioTestCase):
    async def test_the_right_code_activates_the_schedule(self):
        item = _item()
        respuesta = await approve_by_code(_db([item]), "a1", "u1", "DK86ZR", "public_chat")
        self.assertIn("Activada", respuesta)
        self.assertEqual((item.approval_status, item.enabled, item.approval_code), ("approved", True, None))
        self.assertIsNotNone(item.next_run)

    async def test_a_wrong_code_lists_the_pending_ones_with_their_real_codes(self):
        item = _item()
        respuesta = await approve_by_code(_db([item]), "a1", "u1", "86YB5X", "public_chat")
        self.assertIn("No reconozco", respuesta)
        self.assertIn("aprobar DK86ZR", respuesta)
        self.assertEqual(item.approval_status, "pending")

    async def test_nothing_pending_or_expired_means_no_reply(self):
        self.assertIsNone(await approve_by_code(_db([]), "a1", "u1", "DK86ZR", "public_chat"))
        self.assertIsNone(await approve_by_code(_db([_item(horas=48)]), "a1", "u1", "DK86ZR", "public_chat"))


if __name__ == "__main__":
    unittest.main()


class TestDeleteByCode(unittest.IsolatedAsyncioTestCase):
    def _aprobado(self, schedule_id="020c565d", owner="u1"):
        return SimpleNamespace(
            id=schedule_id,
            name="Resumen semanal de correos no leídos",
            definition_version=1,
            approval_status="approved",
            owner_user_id=owner,
        )

    def _db(self, items):
        db = _db(items)
        db.delete = AsyncMock()
        return db

    def test_parsing(self):
        from hermeshq.services.schedule_approval import deletion_code_from

        self.assertEqual(deletion_code_from("eliminar N65MHE"), "N65MHE")
        self.assertEqual(deletion_code_from(" Eliminar n65mhe. "), "N65MHE")
        for texto in ("elimina la reunión", "eliminar", "eliminar N65MHE y algo más", "quiero eliminar N65MHE"):
            self.assertIsNone(deletion_code_from(texto), texto)

    async def test_caso_real_el_codigo_correcto_elimina_el_cron(self):
        from hermeshq.services.cron_schedule import codigo_eliminacion
        from hermeshq.services.schedule_approval import delete_by_code

        item = self._aprobado()
        db = self._db([item])
        respuesta = await delete_by_code(db, "a1", "u1", codigo_eliminacion(item), "public_chat")
        self.assertIn("Eliminé la programación «Resumen semanal de correos no leídos»", respuesta)
        db.delete.assert_awaited_once_with(item)
        db.commit.assert_awaited_once()

    async def test_codigo_ajeno_no_elimina_nada(self):
        from hermeshq.services.schedule_approval import delete_by_code

        db = self._db([self._aprobado()])
        respuesta = await delete_by_code(db, "a1", "u1", "ZZZZZZ", "public_chat")
        self.assertIn("no eliminé nada", respuesta)
        db.delete.assert_not_awaited()

    async def test_sin_crons_aprobados_sigue_el_flujo_normal(self):
        from hermeshq.services.schedule_approval import delete_by_code

        db = self._db([])
        self.assertIsNone(await delete_by_code(db, "a1", "u1", "N65MHE", "public_chat"))
        db.delete.assert_not_awaited()
