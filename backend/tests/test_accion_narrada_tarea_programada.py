import hermeshq.scripts.hermes_task_runner as runner

RESPUESTA = (
    "La tarea de las 11:36 quedó eliminada.\\n\\n**Tarea eliminada — 11:36**\\n- ID: `220e`\\n- Estado: eliminada ✓\\n\\n"
    "Ahora solo queda la de las 11:30 (código 99T3RB), pendiente de tu confirmación."
).replace("\\n", "\n")


def test_el_caso_real_se_detecta_como_accion_narrada_de_programacion():
    assert runner.accion_narrada("Ahora elimina la de las 11:36 y dame su código.", RESPUESTA) == "programacion"


def test_variantes_de_pedido_con_el_objeto_elidido():
    for pedido in (
        "pausa la de las 9:00",
        "borra la de las 11.30",
        "reanuda la de las 08:15",
        "cancela el de las 10:00",
    ):
        assert runner.accion_narrada(pedido, "Listo, la tarea quedó pausada.") in ("programacion", None)


def test_una_respuesta_que_solo_propone_o_pregunta_no_se_marca():
    assert (
        runner.accion_narrada(
            "Ahora elimina la de las 11:36 y dame su código.", "¿Confirmas que elimine la de las 11:36?"
        )
        is None
    )


def test_hablar_de_una_tarea_cualquiera_sin_pedido_de_programacion_no_se_marca():
    assert runner.accion_narrada("¿cómo va todo?", "La tarea de limpieza quedó terminada.") is None
