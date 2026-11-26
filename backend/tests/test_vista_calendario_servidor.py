import json

import hermeshq.scripts.hermes_task_runner as runner

VISTA_REAL = {
    "accion": "crear evento",
    "asunto": "Batería K ciclo 2",
    "inicio": {"dateTime": "2026-11-12T15:00:00", "timeZone": "America/Santiago"},
    "fin": {"dateTime": "2026-11-12T15:30:00", "timeZone": "America/Santiago"},
    "asistentes": [],
    "ubicacion": None,
    "descripcion": None,
}


def _turno(herramienta, resultado):
    return [
        {"role": "user", "content": "agenda"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": herramienta}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(resultado)},
        {"role": "assistant", "content": "- **Importancia:** Alta\n- **Recordatorio:** 30 minutos antes\n¿La creo?"},
    ]


class TestVistaDelServidor:
    def test_caso_real_bateria_la_vista_no_promete_lo_que_no_quedo_registrado(self):
        resultado = {"success": False, "requiere_aprobacion": True, "vista_previa": VISTA_REAL}
        texto = runner.vista_calendario_del_servidor(_turno("ms365_calendar_create_event", resultado), 0)
        assert "Batería K ciclo 2" in texto and "jueves 12-11-2026 15:00" in texto and "15:30" in texto
        assert "Importancia" not in texto and "Recordatorio" not in texto
        assert "Invitados:** ninguno" in texto and "¿La creo?" in texto

    def test_importancia_y_recordatorio_registrados_si_se_muestran(self):
        vista = {**VISTA_REAL, "importancia": "high", "recordatorio_minutos": 30}
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        assert "**Importancia:** alta" in texto and "**Recordatorio:** 30 minutos antes" in texto

    def test_hora_en_utc_se_muestra_en_hora_de_chile(self):
        vista = {**VISTA_REAL, "inicio": {"dateTime": "2026-11-12T18:00:00", "timeZone": "UTC"}}
        assert "jueves 12-11-2026 15:00" in runner.texto_vista_calendario("ms365_calendar_create_event", vista)

    def test_cambio_y_cancelacion(self):
        cambio = {
            "accion": "actualizar evento",
            "nuevo_inicio": {"dateTime": "2026-11-12T16:00:00", "timeZone": "America/Santiago"},
        }
        assert "Nuevo inicio:** jueves 12-11-2026 16:00" in runner.texto_vista_calendario(
            "ms365_calendar_update_event", cambio
        )
        cancelar = {
            "asunto": "Batería",
            "inicio": "2026-11-12T18:00:00",
            "asistentes_que_reciben_cancelacion": ["a@x.cl"],
        }
        texto = runner.texto_vista_calendario("ms365_calendar_delete_event", cancelar)
        assert "¿La cancelo?" in texto and "a@x.cl" in texto and "15:00" in texto

    def test_caso_real_mover_nombra_la_reunion_y_su_horario_actual(self):
        cambio = {
            "accion": "actualizar evento",
            "nuevo_inicio": {"dateTime": "2026-11-12T16:00:00", "timeZone": "America/Santiago"},
            "nuevo_fin": {"dateTime": "2026-11-12T16:30:00", "timeZone": "America/Santiago"},
            "evento_actual": {
                "asunto": "Batería K ciclo 3",
                "inicio": "2026-11-12T15:00:00",
                "fin": "2026-11-12T15:30:00",
                "zona_horaria": "America/Santiago",
            },
        }
        texto = runner.texto_vista_calendario("ms365_calendar_update_event", cambio)
        assert "**Reunión:** Batería K ciclo 3" in texto
        assert "**Horario actual:** jueves 12-11-2026 15:00 (hora de Chile)" in texto
        assert "**Nuevo inicio:** jueves 12-11-2026 16:00" in texto

    def test_caso_real_pregunta_de_confirmacion_escrita_antes_de_la_herramienta(self):
        mensajes = [
            {"role": "user", "content": "cancela la reunión «Batería K ciclo 3» de mañana"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "ms365_calendar_list_events"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"success": true}'},
        ]
        respuesta = (
            "La reunión «Batería K ciclo 3» es mañana **jueves 12-11, 16:00–16:30**. Para cancelarla, confirma: "
            "**¿Eliminar la cita E2 «Batería K ciclo 3» del jueves 12-11 a las 16:00?**"
        )
        assert runner.vista_calendario_a_mano(mensajes[0]["content"], respuesta, mensajes, 0)
        for variante in ("¿La cancelo?", "¿Confirmas que la mueva a las 16:00?", "¿Quieres que la cancele?"):
            assert runner.vista_calendario_a_mano("cancela la reunión de mañana", variante, mensajes, 0), variante

    def test_preguntas_que_piden_un_dato_o_son_de_un_cron_no_cuentan(self):
        mensajes = [{"role": "user", "content": "x"}]
        for mensaje, respuesta in (
            ("agenda una reunión con Juan", "¿A qué hora la agendo?"),
            ("cancela la reunión del lunes", "Tienes dos reuniones el lunes: E1 a las 9 y E2 a las 11. ¿Cuál cancelo?"),
            ("agéndame un recordatorio todos los lunes a las 9", "¿Lo creo así?"),
            ("¿qué reuniones tengo mañana?", "Mañana tienes la reunión de presupuesto a las 10."),
        ):
            assert not runner.vista_calendario_a_mano(mensaje, respuesta, mensajes, 0), respuesta

    def test_con_la_herramienta_de_escritura_llamada_no_cuenta(self):
        mensajes = [
            {"role": "user", "content": "cancela la reunión"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "ms365_calendar_delete_event"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"requiere_aprobacion": true}'},
        ]
        assert not runner.vista_calendario_a_mano("cancela la reunión", "¿La cancelo? Responde «sí».", mensajes, 0)

    def test_si_ya_se_ejecuto_o_no_hubo_vista_no_se_reemplaza(self):
        hecho = {"success": True, "event": {"id": "E1"}}
        assert runner.vista_calendario_del_servidor(_turno("ms365_calendar_create_event", hecho), 0) is None
        assert (
            runner.vista_calendario_del_servidor(
                _turno("ms365_mail_send", {"requiere_aprobacion": True, "vista_previa": {}}), 0
            )
            is None
        )
