import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermeshq.services import aprobacion_central

_PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin"
_spec = importlib.util.spec_from_file_location("ms365_mail_aprobacion_reenvio", _PLUGIN / "__init__.py")
mail = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mail)

REENVIOS = [
    "Si, reenvia",
    "Si, reenvía",
    "Si reenvia con todo",
    "sí, reenvíalo",
    "reenvíalo",
    "sí, responde",
    "contéstale",
]
NO = ["no, no lo reenvíes", "reenvía pero cambia el asunto", "¿lo reenviaste?", "espera, no lo reenvíes todavía"]


@pytest.mark.parametrize("texto", REENVIOS)
def test_caso_real_el_si_a_un_reenvio_es_aprobacion(texto):
    assert mail.es_aprobacion_pura(texto), texto
    assert aprobacion_central.es_aprobacion_textual(texto), texto


@pytest.mark.parametrize("texto", NO)
def test_control_negativo_dudas_cambios_y_negaciones_no_aprueban(texto):
    assert not mail.es_aprobacion_pura(texto), texto
    assert not aprobacion_central.es_aprobacion_textual(texto), texto


def test_la_orden_de_reenviar_en_el_mensaje_cuenta():
    with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"mensaje": "Si, reenvía"})}):
        assert mail._aprobacion_en_el_mensaje()
    with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"mensaje": "reenvía, no, espera"})}):
        assert not mail._aprobacion_en_el_mensaje()
