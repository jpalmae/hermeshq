import pytest

from hermeshq.services.efectos_herramientas import SIN_EFECTOS_FUERA_DEL_ESPACIO, efectos_posibles

LECTURAS_DEL_CRON = (
    ["skill_view"] + ["ms365_mail_list"] * 8 + ["ms365_calendar_list_events"] * 2 + ["ms365_mail_list"] * 6
)

CON_EFECTOS = (
    "ms365_mail_send",
    "ms365_mail_forward",
    "ms365_mail_reply",
    "ms365_mail_mark_read",
    "ms365_correo_sync",
    "ms365_inbox_estado",
    "ms365_calendar_create_event",
    "ms365_calendar_update_event",
    "ms365_calendar_delete_event",
    "ms365_calendar_respond_event",
    "sharepoint_upload_file",
    "sharepoint_share_file",
    "teams_send_chat_message",
    "teams_send_to_person",
    "hq_schedule_request",
    "hq_schedule_approve",
    "hq_schedule_delete",
    "hq_schedule_run",
    "hq_schedule_pause",
    "hq_schedule_resume",
    "hq_trabajo_largo",
    "hq_direct_message",
    "hq_delegate_task",
    "delegate_task",
    "hq_solicitar_capacidad",
    "hq_skill_crear",
    "skill_manage",
    "memory",
    "hq_ajustes_diseno",
    "hq_imagen_generar",
    "browser_navigate",
    "clarify",
)


class TestEfectosPosibles:
    def test_el_cron_que_solo_leia_correo_y_calendario_no_tiene_efectos(self):
        assert len(LECTURAS_DEL_CRON) == 17
        assert efectos_posibles(17, LECTURAS_DEL_CRON) is False

    def test_sin_herramientas_no_hay_efectos(self):
        assert efectos_posibles(0, []) is False
        assert efectos_posibles(0, None) is False

    @pytest.mark.parametrize("nombre", CON_EFECTOS)
    def test_una_herramienta_que_cambia_algo_cuenta_como_efecto(self, nombre):
        assert efectos_posibles(17, LECTURAS_DEL_CRON[:-1] + [nombre]) is True

    def test_una_herramienta_desconocida_cuenta_como_efecto(self):
        assert efectos_posibles(1, ["herramienta_nueva_de_un_plugin"]) is True

    def test_una_herramienta_iniciada_sin_terminar_cuenta(self):
        assert efectos_posibles(0, ["ms365_mail_send"]) is True

    def test_mas_terminadas_que_inicios_registrados_cuenta_como_efecto(self):
        assert efectos_posibles(17, LECTURAS_DEL_CRON[:16]) is True

    def test_sin_registro_de_inicios_y_con_herramientas_terminadas_cuenta_como_efecto(self):
        assert efectos_posibles(1, None) is True

    @pytest.mark.parametrize("nombre", CON_EFECTOS)
    def test_la_lista_no_incluye_herramientas_con_efectos(self, nombre):
        assert nombre not in SIN_EFECTOS_FUERA_DEL_ESPACIO
