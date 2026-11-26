import pytest

import hermeshq.scripts.hermes_task_runner as runner


@pytest.mark.parametrize(
    "frase",
    [
        "Quedó guardado como borrador en la carpeta Borradores de Outlook.",
        "El borrador del correo quedó guardado en Outlook.",
        "Guardé el correo como borrador, sin enviarlo.",
    ],
)
def test_guardar_un_borrador_de_correo_no_cuenta_como_subida(frase):
    assert runner._tipo_de_frase(frase) is None


@pytest.mark.parametrize(
    "frase",
    [
        "Quedó guardado en tu OneDrive en la carpeta Pruebas.",
        "Subí el archivo a SharePoint.",
        "Guardé el informe en OneDrive.",
    ],
)
def test_control_negativo_una_subida_real_sigue_detectandose(frase):
    assert runner._tipo_de_frase(frase) == "subida"
