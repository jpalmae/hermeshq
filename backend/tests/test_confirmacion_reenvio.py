from types import SimpleNamespace

import pytest

from hermeshq.services.confirmacion import es_confirmacion


def _tarea(texto):
    return SimpleNamespace(metadata_json={"mensaje_usuario": texto}, prompt=texto)


@pytest.mark.parametrize("texto", ["Si, reenvia", "Si, reenvía", "reenvíalo", "Si reenvia con todo", "sí, respóndele"])
def test_el_si_a_un_reenvio_o_respuesta_cuenta_como_confirmacion(texto):
    assert es_confirmacion(_tarea(texto))


@pytest.mark.parametrize("texto", ["reenvía el informe a Ana", "¿lo reenviaste?", "no lo reenvíes"])
def test_control_negativo_un_pedido_nuevo_o_una_duda_no_es_confirmacion(texto):
    assert not es_confirmacion(_tarea(texto))
