import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from hermeshq.services import autorizacion_comunicacion as ac

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
AHORA = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
ORIGEN = types.SimpleNamespace(id="a")
DESTINO = types.SimpleNamespace(id="b")


def exc(**extra):
    base = {
        "id": "ex1",
        "origen": "a",
        "destino": "b",
        "operaciones": ["direct"],
        "expira": "2099-01-01T00:00:00+00:00",
        "revocada": False,
        "aprobada_por": "admin-1",
    }
    base.update(extra)
    return base


def correr(excepciones, operacion="direct", origen=ORIGEN, destino=DESTINO):
    ac.get_settings = lambda: types.SimpleNamespace(cross_user_delegation_exceptions=excepciones)
    return ac.evaluar_excepcion(excepciones, origen, destino, operacion, AHORA)


DUENOS_DISTINTOS = {"a": {"u1"}, "b": {"u2"}}


class TestEvaluarExcepcion:
    def test_sin_excepciones_deniega(self):
        assert correr([]) is None

    def test_excepcion_vigente_cubre_la_operacion_declarada(self):
        assert correr([exc()], "direct") == "ex1"

    def test_excepcion_directa_no_autoriza_broadcast_ni_delegacion(self):
        assert correr([exc()], "broadcast") is None
        assert correr([exc()], "delegate") is None

    def test_operacion_desconocida_se_deniega(self):
        assert correr([exc(operaciones=["direct", "sms"])], "sms") is None

    def test_expirada_se_deniega(self):
        assert correr([exc(expira=(AHORA - timedelta(seconds=1)).isoformat())]) is None

    def test_fecha_sin_zona_se_deniega(self):
        assert correr([exc(expira="2026-11-09T00:00:00")]) is None

    def test_revocada_se_deniega(self):
        assert correr([exc(revocada=True)]) is None

    def test_sin_aprobador_se_deniega(self):
        assert correr([exc(aprobada_por="")]) is None

    def test_par_inverso_no_se_cubre(self):
        assert correr([exc()], origen=DESTINO, destino=ORIGEN) is None

    def test_otro_destino_no_se_cubre(self):
        otro = types.SimpleNamespace(id="c")
        assert correr([exc()], destino=otro) is None

    def test_entradas_legadas_en_texto_se_ignoran(self):
        assert correr(["a>b"]) is None


class TestExigirConDuenos:
    def test_mismo_propietario_permite_sin_excepcion(self):
        ac.get_settings = lambda: types.SimpleNamespace(cross_user_delegation_exceptions=[])
        ac.exigir_con_duenos({"a": {"u1"}, "b": {"u1"}}, ORIGEN, DESTINO, "bloqueado")

    def test_propietarios_distintos_deniegan_por_defecto(self):
        ac.get_settings = lambda: types.SimpleNamespace(cross_user_delegation_exceptions=[])
        with pytest.raises(HTTPException) as e:
            ac.exigir_con_duenos(DUENOS_DISTINTOS, ORIGEN, DESTINO, "bloqueado")
        assert e.value.status_code == 403

    def test_excepcion_directa_permite_direct_y_deniega_broadcast(self):
        ac.get_settings = lambda: types.SimpleNamespace(cross_user_delegation_exceptions=[exc()])
        ac.exigir_con_duenos(DUENOS_DISTINTOS, ORIGEN, DESTINO, "bloqueado", operacion="direct")
        with pytest.raises(HTTPException):
            ac.exigir_con_duenos(DUENOS_DISTINTOS, ORIGEN, DESTINO, "bloqueado", operacion="broadcast")

    def test_la_decision_no_recibe_metadata_ni_texto(self):
        import inspect

        assert set(inspect.signature(ac.exigir_con_duenos).parameters) == {
            "duenos",
            "origen",
            "destino",
            "mensaje",
            "operacion",
        }

    def test_routers_pasan_la_operacion(self):
        assert 'operacion="broadcast"' in (RAIZ / "routers" / "comms.py").read_text(encoding="utf-8")
        assert 'operacion="direct"' in (RAIZ / "routers" / "internal_agents.py").read_text(encoding="utf-8")
