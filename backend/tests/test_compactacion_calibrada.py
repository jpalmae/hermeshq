import importlib.util
from pathlib import Path

import pytest

compresor_mod = pytest.importorskip("agent.context_compressor")
RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
_spec = importlib.util.spec_from_file_location("runner_compactacion", RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

FIJO_PRODUCCION = 37_000


def _compresor():
    return compresor_mod.ContextCompressor(
        model="modelo-simulado",
        threshold_percent=0.75,
        summary_target_ratio=0.50,
        quiet_mode=True,
        config_context_length=96_000,
    )


def _tras_compactar(c, fijo):
    return c.tail_token_budget + fijo + c.max_summary_tokens


def test_sin_calibrar_la_compactacion_queda_sobre_el_umbral_y_se_repite():
    c = _compresor()
    assert c.threshold_tokens == 72_000 and c.tail_token_budget == 36_000
    assert _tras_compactar(c, FIJO_PRODUCCION) > c.threshold_tokens


def test_calibrada_queda_bajo_el_umbral_con_margen():
    c = _compresor()
    cola = runner.calibrar_compactacion(c, FIJO_PRODUCCION)
    assert cola == c.tail_token_budget < 36_000
    assert _tras_compactar(c, FIJO_PRODUCCION) <= c.threshold_tokens * (1 - runner.MARGEN_COMPACTACION)


def test_con_poco_fijo_no_se_reduce_la_cola():
    c = _compresor()
    assert runner.calibrar_compactacion(c, 8_000) == 36_000


def test_fijo_enorme_deja_la_cola_minima_y_sin_compresor_no_falla():
    c = _compresor()
    assert runner.calibrar_compactacion(c, 80_000) == runner.COLA_MINIMA
    assert runner.calibrar_compactacion(None, 10_000) is None
