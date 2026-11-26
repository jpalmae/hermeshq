import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermeshq.services.public_chat_service import _prompt_con_referencia


def _db(tareas):
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = tareas
    db = MagicMock()

    async def execute(*_args, **_kwargs):
        return resultado

    db.execute = execute
    return db


def _tarea(respuesta, **metadata):
    return SimpleNamespace(response=respuesta, metadata_json={"thread_user_id": "u1", **metadata})


class TestRespuestaCorta(unittest.TestCase):
    def test_un_si_suelto_recibe_la_ultima_respuesta_del_chat(self):
        db = _db(
            [
                _tarea(
                    "¿Lo envío tal cual a Ana?",
                )
            ]
        )
        out = asyncio.run(_prompt_con_referencia(db, "a1", "u1", "si"))
        self.assertIn("¿Lo envío tal cual a Ana?", out)
        self.assertTrue(out.endswith("si"))

    def test_los_mensajes_largos_no_cambian(self):
        db = _db([_tarea("respuesta previa")])
        largo = "Cuántos correos críticos tengo pendientes desde esta semana por favor"
        self.assertEqual(asyncio.run(_prompt_con_referencia(db, "a1", "u1", largo)), largo)

    def test_reinicios_y_trabajos_de_segundo_plano_no_son_referencia(self):
        db = _db(
            [
                _tarea("🧹 reinicio", reset=True),
                _tarea("trabajo", background_job=True),
                _tarea("otra", thread_user_id="u2"),
            ]
        )
        self.assertEqual(asyncio.run(_prompt_con_referencia(db, "a1", "u1", "si")), "si")

    def test_los_corchetes_de_la_respuesta_no_rompen_el_formato(self):
        db = _db([_tarea("Opciones [a] o [b]")])
        out = asyncio.run(_prompt_con_referencia(db, "a1", "u1", "a"))
        self.assertNotIn("[a]", out)


if __name__ == "__main__":
    unittest.main()
