import asyncio
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services.m365_oauth import motivo_sin_token


def _db(fila):
    resultado = MagicMock()
    resultado.first.return_value = fila
    db = MagicMock()
    db.execute = AsyncMock(return_value=resultado)
    return db


def test_con_cuenta_conectada_pero_sin_token_valido_dice_que_la_sesion_vencio():
    assert asyncio.run(motivo_sin_token("u1", _db(("id",)))) == "sesion_m365_vencida_reconectar_en_hermeshq"


def test_sin_cuenta_conectada_sigue_diciendo_usuario_sin_cuenta():
    assert asyncio.run(motivo_sin_token("u1", _db(None))) == "usuario_sin_cuenta_m365"
