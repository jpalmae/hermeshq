import asyncio
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from hermeshq.services import delegacion_entre_usuarios as d


def _agente(agent_id, system=False):
    return SimpleNamespace(id=agent_id, is_system_agent=system)


DUENOS = {"ana": {"u-ana"}, "carlos": {"u-carlos"}, "op2": {"u-op2"}}


class TestQuienEsDeOtroUsuario(unittest.TestCase):
    def test_agente_de_otra_persona(self):
        self.assertTrue(d.es_de_otro_usuario(DUENOS, _agente("ana"), _agente("carlos")))

    def test_agente_de_sistema_o_sin_dueno_no_cuenta(self):
        self.assertFalse(d.es_de_otro_usuario(DUENOS, _agente("ana"), _agente("hq-operator", system=True)))
        self.assertFalse(d.es_de_otro_usuario(DUENOS, _agente("ana"), _agente("sin-dueno")))

    def test_agentes_del_mismo_usuario(self):
        duenos = dict(DUENOS, otro_de_ana={"u-ana"})
        self.assertFalse(d.es_de_otro_usuario(duenos, _agente("ana"), _agente("otro_de_ana")))


class TestAutorizacionPura(unittest.TestCase):
    def test_si_autoriza(self):
        for texto in ("sí", "autorizo", "ok, hazlo", "sí, adelante"):
            self.assertTrue(d.es_autorizacion_pura(texto), texto)

    def test_no_autoriza(self):
        for texto in ("no", "sí pero solo a Carlos", "¿a quién?", "arregla el dashboard", ""):
            self.assertFalse(d.es_autorizacion_pura(texto), texto)


class _Db:
    def __init__(self, tareas, anterior_id):
        self.tareas = tareas
        self.get = AsyncMock(side_effect=lambda _modelo, tid: self.tareas.get(tid))
        resultado = MagicMock()
        resultado.scalar_one_or_none.return_value = anterior_id
        self.execute = AsyncMock(return_value=resultado)


def _tarea(tid, prompt, agent_id="ana", source="public_chat", minutos=0, usuario="u-ana", sesion="S1"):
    return SimpleNamespace(
        id=tid,
        agent_id=agent_id,
        prompt=prompt,
        metadata_json={"source": source, "thread_user_id": usuario, "public_session_id": sesion},
        queued_at=datetime.now(UTC) + timedelta(minutes=minutos),
    )


class TestFlujoDeAutorizacion(unittest.TestCase):
    def setUp(self):
        d._pendientes.clear()
        d._autorizados.clear()

    def _pedir(self, db, tid):
        return asyncio.run(d.autorizacion_pendiente(db, _agente("ana"), _agente("carlos"), "Agente_Carlos", tid))

    def test_primera_vez_se_rechaza_y_pide_autorizacion(self):
        db = _Db({"T1": _tarea("T1", "arregla mi dashboard")}, None)
        motivo = self._pedir(db, "T1")
        self.assertIn("Requiere autorización", motivo)

    def test_si_en_el_turno_siguiente_autoriza(self):
        tareas = {"T1": _tarea("T1", "arregla mi dashboard"), "T2": _tarea("T2", "sí", minutos=1)}
        self.assertIsNotNone(self._pedir(_Db(tareas, None), "T1"))
        db = _Db(tareas, "T1")
        self.assertIsNone(self._pedir(db, "T2"))
        self.assertIsNone(self._pedir(db, "T2"))

    def test_si_a_otra_cosa_no_autoriza(self):
        tareas = {
            "T1": _tarea("T1", "arregla mi dashboard"),
            "T2": _tarea("T2", "hola", minutos=1),
            "T3": _tarea("T3", "sí", minutos=2),
        }
        self._pedir(_Db(tareas, None), "T1")
        self.assertIsNotNone(self._pedir(_Db(tareas, "T2"), "T3"))

    def test_no_autoriza_en_la_misma_tarea(self):
        tareas = {"T1": _tarea("T1", "sí")}
        self._pedir(_Db(tareas, None), "T1")
        self.assertIsNotNone(self._pedir(_Db(tareas, None), "T1"))

    def test_tareas_programadas_o_sin_tarea_nunca(self):
        tareas = {"T1": _tarea("T1", "sí", source="scheduled")}
        self.assertIn("no tiene un usuario", self._pedir(_Db(tareas, None), "T1"))
        self.assertIn("no tiene un usuario", self._pedir(_Db({}, None), None))

    def test_pendiente_vencido_no_vale(self):
        tareas = {"T1": _tarea("T1", "arregla"), "T2": _tarea("T2", "sí", minutos=1)}
        self._pedir(_Db(tareas, None), "T1")
        d._pendientes[("ana", "carlos")]["ts"] = time.time() - 7200
        self.assertIsNotNone(self._pedir(_Db(tareas, "T1"), "T2"))

    def test_si_desde_otra_conversacion_no_autoriza(self):
        tareas = {"T1": _tarea("T1", "arregla mi dashboard"), "T2": _tarea("T2", "sí", minutos=1, sesion="S2")}
        self._pedir(_Db(tareas, None), "T1")
        self.assertIn("Requiere autorización", self._pedir(_Db(tareas, "T1"), "T2"))

    def test_si_de_otro_usuario_en_la_misma_sesion_no_autoriza(self):
        tareas = {"T1": _tarea("T1", "arregla mi dashboard"), "T2": _tarea("T2", "sí", minutos=1, usuario="u-otro")}
        self._pedir(_Db(tareas, None), "T1")
        self.assertIn("Requiere autorización", self._pedir(_Db(tareas, "T1"), "T2"))

    def test_sin_clave_de_conversacion_no_autoriza(self):
        tareas = {"T1": _tarea("T1", "arregla", sesion=""), "T2": _tarea("T2", "sí", minutos=1, sesion="")}
        for tarea in tareas.values():
            tarea.metadata_json["thread_user_id"] = ""
        self._pedir(_Db(tareas, None), "T1")
        self.assertIn("Requiere autorización", self._pedir(_Db(tareas, "T1"), "T2"))
