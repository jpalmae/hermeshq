import json

import pytest

import hermeshq.scripts.hermes_task_runner as runner

RESPUESTA_REAL = (
    'Ya quedaron marcados como leídos los 4 correos de la categoría **Otros**:\n\n- Proveedor Uno – "Proveedor Uno en TECH AI '
    '2026"\n\n**Sobre la preferencia:** la guardé para que las próximas corridas de tu tarea programada la respeten '
    "automáticamente."
)
MENSAJE_REAL = (
    "yo te había dicho que para mi tarea programada los correos que quedaran en categoría otros automáticamente los "
    "marques como leído y los importantes me debías preguntar"
)
MEMORIA_FALLIDA = {
    "error": "Memory is not available. It may be disabled in config or this environment.",
    "success": False,
}


def _turno(*herramientas, usuario=MENSAJE_REAL):
    mensajes = [{"role": "user", "content": usuario}]
    for i, (nombre, resultado) in enumerate(herramientas):
        mensajes.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]}
        )
        mensajes.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(resultado)})
    return mensajes


def _tipos(respuesta, mensajes, **kw):
    return sorted({p["tipo"] for p in runner.verificar_afirmaciones(respuesta, mensajes, 0, **kw)})


class TestCasoReal:
    def test_marcado_y_preferencia_sin_respaldo(self):
        mensajes = _turno(("memory", MEMORIA_FALLIDA), ("ms365_mail_list", {"success": True, "count": 0}))
        assert _tipos(RESPUESTA_REAL, mensajes, mensaje=MENSAJE_REAL) == ["marcado", "preferencia"]

    def test_el_aviso_final_dice_la_verdad(self):
        pendientes = runner.verificar_afirmaciones(RESPUESTA_REAL, _turno(("memory", MEMORIA_FALLIDA)), 0)
        final = runner.respuesta_verificada(RESPUESTA_REAL, pendientes)
        assert "No se marcó ningún correo como leído" in final and "La preferencia no quedó guardada" in final


class TestConRespaldo:
    def test_marcado_real(self):
        mensajes = _turno(("ms365_mail_mark_read", {"success": True, "marcados": ["a", "b"]}))
        assert _tipos("Listo, marqué como leídos los 2 correos de Otros.", mensajes) == []

    def test_informe_del_cron_con_su_conteo(self):
        mensajes = _turno(("ms365_correo_sync", {"success": True, "marcados_leidos": {"marcados": 0}}))
        texto = "Correos nuevos clasificados: 3 (0 marcados como leídos)."
        assert _tipos(texto, mensajes, programada=True) == []

    def test_preferencia_guardada_de_verdad(self):
        mensajes = _turno(("ms365_correo_sync", {"success": True, "vista_no_leidos_horas": 48}))
        assert _tipos("Listo, guardé tu preferencia: los no leídos de 48 h.", mensajes) == []

    def test_solicitud_al_administrador(self):
        mensajes = _turno(("hq_solicitar_capacidad", {"success": True, "request_id": "r1"}))
        assert _tipos("Quedó anotada tu preferencia para que el administrador la evalúe.", mensajes) == []


class TestNoSeMarcan:
    @pytest.mark.parametrize(
        "respuesta",
        [
            "No pude marcar estos 4 correos como leídos: la tarea no tiene permiso.",
            "¿Quieres que los marque como leídos?",
            "Si me confirmas, los marco como leídos ahora.",
            "Los correos marcados como importantes en Outlook son 3.",
            "Tu preferencia actual es no marcar nada; ¿la cambio?",
            "Puedo guardar tu preferencia si me lo pides.",
            "Ayer marqué como leídos los FRÍO del informe de las 10.",
        ],
    )
    def test_sin_afirmacion_de_hecho(self, respuesta):
        assert _tipos(respuesta, _turno(usuario="hola")) == []
