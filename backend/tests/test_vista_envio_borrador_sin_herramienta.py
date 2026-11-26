import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = "Envíame a mí un correo, solo a mí para validar, con asunto «RE: prueba de firma» y cuerpo «Muy buen día.»"
RESPUESTA = "Voy a preparar el borrador para que lo revises.\n\n¿Lo envío?"


def _turno(*llamadas):
    salida = [{"role": "user", "content": PEDIDO}]
    for nombre in llamadas:
        salida.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c", "function": {"name": nombre, "arguments": "{}"}}],
            }
        )
    return salida


def test_caso_real_borrador_anunciado_sin_llamar_la_herramienta():
    assert runner.vista_envio_a_mano(PEDIDO, RESPUESTA, _turno(), 0) == "correo"


def test_control_negativo_si_llamo_la_herramienta_de_envio_no_se_marca():
    assert runner.vista_envio_a_mano(PEDIDO, RESPUESTA, _turno("ms365_mail_send"), 0) is None


def test_control_negativo_sin_pregunta_de_envio_no_se_marca():
    assert runner.vista_envio_a_mano(PEDIDO, "Dejé el borrador en tu carpeta de borradores.", _turno(), 0) is None


def test_control_negativo_un_pedido_que_no_es_de_envio_no_se_marca():
    pedido = "Explícame qué es un borrador en Outlook"
    assert runner.vista_envio_a_mano(pedido, RESPUESTA, [{"role": "user", "content": pedido}], 0) is None
