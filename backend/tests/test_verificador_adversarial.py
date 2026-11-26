import json
import tempfile
from pathlib import Path

import pytest

import hermeshq.scripts.hermes_task_runner as runner

URL = "https://empresa-my.sharepoint.com/personal/u/Documents/Proyectos/informe.html"


def _turno(*herramientas, usuario="pedido"):
    mensajes = [{"role": "user", "content": usuario}]
    for i, (nombre, resultado) in enumerate(herramientas):
        mensajes.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]}
        )
        contenido = resultado if isinstance(resultado, str) else json.dumps(resultado)
        mensajes.append({"role": "tool", "tool_call_id": f"c{i}", "content": contenido})
    return mensajes


def _tipos(respuesta, mensajes, **kw):
    return sorted({p["tipo"] for p in runner.verificar_afirmaciones(respuesta, mensajes, 0, **kw)})


OK = {"success": True}
PENDIENTE = {"success": False, "requiere_aprobacion": True, "vista_previa": {}}
FALLO_403 = {"success": False, "error": "Error 403: Forbidden"}


class TestLoEjecutadoNoSeRechaza:
    @pytest.mark.parametrize(
        ("herramienta", "resultado", "respuesta"),
        [
            (
                "sharepoint_upload_file",
                {"success": True, "url": URL},
                f"Listo ✅ Lo guardé en tu carpeta Proyectos de OneDrive: {URL}",
            ),
            (
                "sharepoint_share_file",
                {"success": True, "url": URL},
                f"Listo, quedó guardado en tu OneDrive y este es el enlace: {URL}",
            ),
            (
                "hq_docx_generate",
                {"success": True, "url": URL, "path": "outputs/i.html"},
                "✅ Subido a tu carpeta de SharePoint.",
            ),
            ("ms365_mail_send", OK, "Listo ✅ Envié el correo a Juan."),
            ("ms365_mail_reply", OK, "Respondí el correo de Ana con copia a todos."),
            ("ms365_mail_forward", OK, "Reenvié el correo a Pedro ✅"),
            ("teams_send_to_person", OK, "Le escribí a Pedro por Teams."),
            ("ms365_calendar_create_event", OK, "✅ Agendé la reunión para mañana a las 10."),
            ("ms365_calendar_update_event", OK, "Moví la reunión a las 11 ✅"),
            ("ms365_calendar_delete_event", OK, "Cancelé la reunión del viernes."),
            ("hq_schedule_approve", OK, "Programé el recordatorio de los lunes a las 9."),
            (
                "hq_schedule_request",
                PENDIENTE,
                "Programé el recordatorio; queda pendiente de tu aprobación con el código.",
            ),
        ],
    )
    def test_con_resultado_real_no_hay_reclamo(self, herramienta, resultado, respuesta):
        assert _tipos(respuesta, _turno((herramienta, resultado))) == []

    def test_reintento_que_funciono_respalda_el_exito(self):
        mensajes = _turno(
            ("sharepoint_upload_file", FALLO_403), ("sharepoint_upload_file", {"success": True, "url": URL})
        )
        assert _tipos("Listo, lo subí a tu carpeta de OneDrive.", mensajes) == []

    def test_adjunto_que_existe(self):
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "outputs").mkdir()
            (Path(cwd) / "outputs" / "a.pdf").write_bytes(b"%PDF-1.4")
            assert _tipos("Te adjunto el PDF.\nMEDIA: outputs/a.pdf", _turno(("hq_pdf_generate", OK)), cwd=cwd) == []


class TestLoNoEjecutadoNoSeDeclaraExitoso:
    @pytest.mark.parametrize(
        ("respuesta", "tipo"),
        [
            ("Correo enviado correctamente ✅", "correo"),
            ("Listo, envié el resumen a gerencia.", "correo"),
            ("✅ Reunión agendada para mañana a las 10.", "calendario"),
            ("Agendé la reunión con Pedro.", "calendario"),
            ("✅ Guardado en tu OneDrive (carpeta Proyectos).", "subida"),
            ("Subí el informe a tu carpeta de SharePoint.", "subida"),
            ("He programado el recordatorio diario.", "programacion"),
            ("Listo, generé el informe con los dos árboles.", "archivo"),
        ],
    )
    def test_sin_herramienta_se_reclama(self, respuesta, tipo):
        assert tipo in _tipos(respuesta, _turno())

    def test_solo_leer_no_respalda_un_envio(self):
        assert _tipos("Listo, envié el correo a Juan.", _turno(("ms365_mail_list", OK))) == ["correo"]

    def test_respaldo_de_otro_tipo_no_cuenta(self):
        assert _tipos("✅ Agendé la reunión con Ana.", _turno(("ms365_mail_send", OK))) == ["calendario"]

    def test_dos_afirmaciones_solo_la_falsa_se_reclama(self):
        respuesta = "Listo ✅ Envié el correo a Juan. También agendé la reunión con él para el lunes."
        assert _tipos(respuesta, _turno(("ms365_mail_send", OK))) == ["calendario"]

    def test_adjunto_inexistente_y_ruta_del_pc(self):
        assert _tipos("MEDIA: outputs/no-existe.pdf", _turno(), cwd=tempfile.gettempdir()) == ["adjunto"]
        assert _tipos("MEDIA: C:\\Users\\ana\\Desktop\\x.pdf", _turno()) == ["adjunto_pc"]

    def test_enlace_de_compartir_inventado(self):
        assert _tipos("Aquí está: https://empresa-my.sharepoint.com/:w:/g/personal/u/EabcINVENTADO", _turno()) == [
            "enlace"
        ]


class TestLoPendienteNoApareceComoCompletado:
    @pytest.mark.parametrize(
        ("herramienta", "respuesta", "tipo"),
        [
            ("ms365_mail_send", "✅ Correo enviado a Juan.", "correo_pendiente"),
            ("ms365_mail_reply", "Respondí el correo de Ana ✅", "correo_pendiente"),
            ("ms365_calendar_create_event", "✅ Reunión creada para mañana.", "calendario_pendiente"),
            ("sharepoint_share_file", "Listo, quedó guardado en tu OneDrive.", "subida_pendiente"),
        ],
    )
    def test_vista_previa_no_es_hecho(self, herramienta, respuesta, tipo):
        assert _tipos(respuesta, _turno((herramienta, PENDIENTE))) == [tipo]

    def test_el_aviso_dice_que_falta_la_aprobacion(self):
        pendientes = runner.verificar_afirmaciones(
            "✅ Correo enviado a Juan.", _turno(("ms365_mail_send", PENDIENTE)), 0
        )
        assert "falta tu aprobación" in runner.respuesta_verificada("✅ Correo enviado a Juan.", pendientes)


class TestLoFallidoNoSeVuelveExito:
    @pytest.mark.parametrize(
        ("herramienta", "respuesta", "tipo"),
        [
            ("sharepoint_upload_file", "Listo ✅ Subido a tu carpeta de OneDrive.", "subida"),
            ("ms365_mail_send", "Listo, envié el correo.", "correo"),
            ("ms365_calendar_create_event", "✅ Agendé la reunión.", "calendario"),
        ],
    )
    def test_fallo_con_texto_de_exito(self, herramienta, respuesta, tipo):
        assert _tipos(respuesta, _turno((herramienta, FALLO_403))) == [tipo]

    def test_fallo_y_luego_solo_vista_previa_sigue_pendiente(self):
        mensajes = _turno(("ms365_mail_send", FALLO_403), ("ms365_mail_send", PENDIENTE))
        assert _tipos("✅ Correo enviado.", mensajes) == ["correo_pendiente"]

    def test_error_en_texto_plano(self):
        assert _tipos("Listo, lo subí a tu OneDrive.", _turno(("sharepoint_upload_file", "Error: token expirado"))) == [
            "subida"
        ]


class TestErroresExternosInformadosCorrectamente:
    @pytest.mark.parametrize(
        ("herramienta", "respuesta"),
        [
            ("sharepoint_upload_file", "No pude subirlo: SharePoint respondió 403 (sin permiso en esa carpeta)."),
            ("ms365_mail_send", "No se envió el correo: Microsoft 365 rechazó el destinatario."),
            ("ms365_calendar_create_event", "No pude crear la reunión: Microsoft 365 devolvió un error."),
            ("hq_schedule_request", "No pude programarlo: el servidor rechazó la expresión cron."),
        ],
    )
    def test_el_fallo_contado_con_honestidad_no_se_toca(self, herramienta, respuesta):
        assert _tipos(respuesta, _turno((herramienta, FALLO_403))) == []


class TestRespuestasLegitimasNoSeBloqueanPorPalabras:
    @pytest.mark.parametrize(
        "respuesta",
        [
            "Tienes 3 correos: Juan dice «ya envié la factura» y María pregunta si agendaste la reunión.",
            "| # | Asunto |\n|---|---|\n| 1 | Envié el presupuesto |\n| 2 | Reunión agendada con proveedores |",
            "> Te envié el informe ayer.\nEso escribió Pedro en su correo.",
            "Para guardar un archivo en OneDrive usa la opción «Subir» de la carpeta.",
            "¿Quieres que lo guarde en tu carpeta de OneDrive?",
            "Si me confirmas, envío el correo y agendo la reunión.",
            "El informe que guardé ayer en tu carpeta sigue disponible.",
            "La subida de precios afectó al trimestre; el estado guardado del proceso no cambió.",
            "Mañana tienes la reunión de presupuesto; fue cancelada por el organizador la semana pasada.",
            "No envié el correo todavía: falta que me confirmes el destinatario.",
            "Cuando termine el análisis, te aviso por aquí.",
            "El correo de bienvenida se envía automáticamente al crear la cuenta en el sistema de RR.HH.",
            "Resumen del documento: «La política será programada para revisión anual por el comité».",
            "Se detectaron 3 reprocesos; el equipo con más casos es EQ-0015.",
        ],
    )
    def test_no_se_reclama(self, respuesta):
        assert _tipos(respuesta, _turno()) == []

    def test_cron_que_anuncia_su_proximo_aviso(self):
        assert _tipos("Sin alertas hoy. Te avisaré mañana a las 8.", _turno(), programada=True) == []

    def test_aprobacion_ejecutada_por_el_servidor(self):
        assert _tipos("✅ Correo enviado a Juan.", _turno(), aprobado=True) == []


class TestSubjuntivoNoEsUnEnvio:
    def test_caso_real_aunque_le_mande_un_correo(self):
        assert _tipos("Aunque le mande un correo plano a la sala, no la reserva.", _turno()) == []
        assert _tipos("Si quieres, que le envíe el resumen a gerencia mañana.", _turno()) == []
