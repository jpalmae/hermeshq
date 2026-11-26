import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = "en esta reunión (Prueba) quiero eliminar a Carlos Rojas y cambiar la hora de la reunión de 13:30 a 14hrs"


class TestQuitarPersona:
    def test_cambio_de_hora_sin_el_retiro_pedido_advierte(self):
        vista = {"asunto": "X", "nuevo_inicio": "2026-11-17T17:00:00Z"}
        assert runner.persona_a_quitar_sin_incluir(PEDIDO, "ms365_calendar_update_event", vista) == "Carlos Rojas"

    def test_si_la_vista_ya_quita_a_la_persona_no_advierte(self):
        vista = {"asistentes_QUITADOS_reciben_cancelacion": ["crojas@contoso.cl", "Carlos Rojas"]}
        assert runner.persona_a_quitar_sin_incluir(PEDIDO, "ms365_calendar_update_event", vista) is None

    def test_otros_pedidos_y_herramientas_no_advierten(self):
        vista = {"asunto": "X"}
        assert (
            runner.persona_a_quitar_sin_incluir("cambia la hora a las 14", "ms365_calendar_update_event", vista) is None
        )
        assert (
            runner.persona_a_quitar_sin_incluir("elimina la reunión de mañana", "ms365_calendar_update_event", vista)
            is None
        )
        assert runner.persona_a_quitar_sin_incluir(PEDIDO, "ms365_calendar_create_event", vista) is None

    def test_el_aviso_llega_al_texto_de_la_vista(self):
        mensajes = [
            {"role": "user", "content": PEDIDO},
            {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "ms365_calendar_update_event"}}]},
            {
                "role": "tool",
                "tool_call_id": "1",
                "content": '{"requiere_aprobacion": true, "vista_previa": {"asunto": "(Prueba) X", "nuevo_inicio": "2026-11-17T17:00:00Z"}}',
            },
        ]
        texto = runner.vista_calendario_del_servidor(mensajes, 0, PEDIDO)
        assert "Pediste quitar a «Carlos Rojas»" in texto
