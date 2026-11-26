import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services.toolsets_pedido import toolsets_para_pedido

AGENTE = ["safe", "file", "hermeshq_ms365_calendar", "hermeshq_ms365_mail", "hermeshq_sharepoint"]
PEDIDO_CORREO = (
    'quiero responder a este correo, adjuntando este archivo "Formulario_firmado.pdf" y escribiendo el '
    'siguiente mensaje "Hola rosa, hago envío del formulario firmado"'
)
BORRADOR = (
    "**Para:** jmora@empresa.cl **Asunto:** RE: Solicitud de reunión - Proyecto Demo\n\n"
    "Hola Rosa, te envío el formulario firmado.\n\n¿Lo envío?"
)


def test_ana_10_31_un_borrador_de_correo_que_cita_una_reunion_no_es_una_vista_de_calendario():
    assert runner.vista_calendario_a_mano(PEDIDO_CORREO, BORRADOR, [{"role": "user"}], 0) is False


def test_control_negativo_un_pedido_de_calendario_sigue_detectandose():
    vista = "**Título:** Prueba\n**Inicio:** 13:00\n\n¿La creo?"
    assert runner.vista_calendario_a_mano("crea una reunión mañana a las 13:00", vista, [{"role": "user"}], 0) is True


def test_ana_11_47_reu_y_asistencia_cargan_el_calendario():
    pedido = "Dame un resumen de la confirmación de asistencia de la reu Capacitación Interna (SUC1)"
    assert "hermeshq_ms365_calendar" in toolsets_para_pedido(pedido, AGENTE)


def test_control_negativo_sin_senales_no_carga_el_calendario():
    assert "hermeshq_ms365_calendar" not in toolsets_para_pedido("cuánto es 2 + 2", AGENTE)
