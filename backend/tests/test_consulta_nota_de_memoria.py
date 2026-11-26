import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = (
    "Recuerda esto para después: el código de la sala de reuniones es ZX-4471 y el responsable es Pedro Gómez. "
    "Solo confirma que lo anotaste."
)
RESPUESTA = (
    "Guardado en memoria:\n\n- Código de la sala de reuniones: **ZX-4471**\n- Responsable de la sala: **Pedro Gómez**\n\n"
    "Queda anotado. ¿Necesitas algo más?"
)


def test_una_nota_de_memoria_no_es_una_consulta_de_calendario():
    assert runner.consulta_sin_herramienta(PEDIDO, RESPUESTA, ["memory"]) is None
    assert runner.consulta_sin_herramienta(PEDIDO, RESPUESTA, []) is None


def test_que_sin_tilde_no_cuenta_como_pregunta():
    assert runner.consulta_sin_herramienta("Dime que anotaste de la agenda", "- uno\n- dos", ["memory"]) is None


def test_una_pregunta_real_de_reuniones_sigue_detectandose():
    respuesta = "Tienes 2 reuniones:\n- Comité — 10:00\n- Revisión — 15:30"
    assert runner.consulta_sin_herramienta("¿Qué reuniones tengo hoy?", respuesta, []) == (
        "reuniones",
        "ms365_calendar_list_events",
    )


def test_pregunta_sin_tilde_pero_con_interrogacion_sigue_detectandose():
    respuesta = "Tienes 2 reuniones:\n- Comité — 10:00\n- Revisión — 15:30"
    assert runner.consulta_sin_herramienta("que reuniones tengo hoy?", respuesta, []) is not None


def test_recuerda_con_pregunta_sigue_aplicando_a_datos_concretos():
    respuesta = "Tienes 2 reuniones:\n- Comité — 10:00\n- Revisión — 15:30"
    assert runner.consulta_sin_herramienta("Recuerda: ¿qué reuniones tengo hoy?", respuesta, []) is not None


def test_un_consejo_general_sobre_correos_no_es_una_consulta_de_bandeja():
    respuesta = "Tres consejos:\n- Sé específico\n- Usa pocas palabras\n- Pon la acción primero"
    assert (
        runner.consulta_sin_herramienta(
            "Dame tres consejos breves para redactar un buen asunto de correo.", respuesta, []
        )
        is None
    )


def test_una_pregunta_sobre_mi_bandeja_con_lista_sigue_detectandose():
    respuesta = "Tus correos:\n- Informe Q3\n- Reunión de comité"
    assert runner.consulta_sin_herramienta("Dame mis correos sin leer", respuesta, []) == (
        "correos",
        "ms365_mail_list",
    )


def _turno_memoria(*herramientas):
    import json

    mensajes = [{"role": "user", "content": PEDIDO}]
    for i, (nombre, resultado) in enumerate(herramientas):
        mensajes.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]}
        )
        mensajes.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(resultado)})
    return mensajes


def test_guardado_en_memoria_sin_llamar_memory_se_marca_como_preferencia():
    pendientes = runner.verificar_afirmaciones(
        "Guardado en memoria: código ZX-4471, responsable Pedro Gómez.", _turno_memoria(), 0, mensaje=PEDIDO
    )
    assert {p["tipo"] for p in pendientes} == {"preferencia"}


def test_guardado_en_memoria_con_la_herramienta_no_se_marca():
    mensajes = _turno_memoria(("memory", {"success": True}))
    assert runner.verificar_afirmaciones("Guardado en memoria: código ZX-4471.", mensajes, 0, mensaje=PEDIDO) == []
