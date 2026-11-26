import io
import json
import sys
from types import SimpleNamespace

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO_REAL = "¿Qué chats de Teams tengo sin leer? Distingue los no leídos de los que solo están sin responder."
RESPUESTA_REAL = (
    "Tienes **2 chats sin leer** y **1 chat solo sin responder**:\n\n**Sin leer (mensajes nuevos que no has "
    'abierto):**\n- **Soporte** — de Nicolás (hace 1 h): "Listo, ya te llego"\n- **Soporte** — de Nicolás (hace 1 h): '
    '"Hola, ¿te llegó?"\n\n**Sin responder (ya los leíste, pero no respondiste):**\n- **Soporte** — de Nicolás (hace '
    '2 h): "Hola, ¿me puedes apoyar con..."'
)


class TestDeteccion:
    def test_caso_real_chats_inventados(self):
        assert runner.consulta_sin_herramienta(PEDIDO_REAL, RESPUESTA_REAL, []) == (
            "chats de Teams",
            "teams_list_chats",
        )

    def test_con_la_herramienta_de_la_fuente_no_aplica(self):
        assert runner.consulta_sin_herramienta(PEDIDO_REAL, RESPUESTA_REAL, ["teams_list_chats"]) is None

    def test_otra_herramienta_no_cuenta_como_consulta_de_la_fuente(self):
        assert runner.consulta_sin_herramienta(PEDIDO_REAL, RESPUESTA_REAL, ["skill_view"]) is not None

    def test_un_seguimiento_de_datos_ya_mostrados_no_aplica(self):
        historial = (
            "Chats sin leer: Soporte — de Nicolás (hace 1 h): Listo, ya te llego. Hola, ¿te llegó? ¿me puedes apoyar"
        )
        assert runner.consulta_sin_herramienta(PEDIDO_REAL, RESPUESTA_REAL, [], historial) is None

    def test_correos_y_reuniones(self):
        assert runner.consulta_sin_herramienta(
            "¿cuántos correos sin leer tengo?", "Tienes 5 correos sin leer:\n- Ana: Presupuesto\n- Pedro: Viaje", []
        ) == ("correos", "ms365_mail_list")
        assert runner.consulta_sin_herramienta(
            "¿qué reuniones tengo mañana?", "Mañana tienes:\n- 09:30 Comité de TI\n- 15:00 Revisión de proyecto", []
        ) == ("reuniones", "ms365_calendar_list_events")

    def test_si_consulto_cualquier_fuente_de_datos_no_aplica(self):
        detalle = "Aquí está el detalle del correo:\n- **Fecha:** 16-10-2026, 15:08\n- **De:** Microsoft Teams"
        assert (
            runner.consulta_sin_herramienta("dame el detalle del de Microsoft Teams", detalle, ["ms365_mail_get"])
            is None
        )
        pausa = "Listo. Pausé la tarea:\n- **Estado:** pausada\n- **Próxima corrida:** ninguna"
        assert (
            runner.consulta_sin_herramienta(
                "Pausa la tarea programada del resumen semanal de correos no leídos.", pausa, ["hq_schedule_pause"]
            )
            is None
        )

    def test_responder_de_memoria_lo_que_pidio_revisar_en_el_calendario(self):
        pedido = 'Revisa en mi calendario a qué hora quedó mañana la reunión "Prueba HermesHQ lote 1"'
        respuesta = 'Mañana la reunión "Prueba HermesHQ lote 1" quedó de **16:00 a 16:30** (hora Chile).'
        assert runner.consulta_sin_herramienta(pedido, respuesta, []) == ("reuniones", "ms365_calendar_list_events")

    def test_una_pregunta_sin_datos_no_aplica(self):
        assert runner.consulta_sin_herramienta(PEDIDO_REAL, "¿De qué fecha quieres revisar los chats?", []) is None

    def test_un_borrador_o_una_accion_no_son_consultas(self):
        borrador = "**Para:** ana@empresa.cl\n**Asunto:** Hola\n\n- Punto 1\n\n¿Lo envío?"
        assert runner.consulta_sin_herramienta("dame un borrador de correo para Ana", borrador, []) is None
        assert (
            runner.consulta_sin_herramienta("¿me agendas una reunión mañana a las 10?", "- 10:00 Reunión", []) is None
        )

    def test_sin_fuente_de_datos_no_aplica(self):
        assert (
            runner.consulta_sin_herramienta("¿qué es un cron?", "- Es una tarea programada\n- Corre a una hora", [])
            is None
        )


class _Agente:
    valid_tool_names = {"teams_list_chats", "ms365_mail_list"}

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.llamadas.append(user_message)
        texto, herramienta = self.respuestas.pop(0)
        mensajes = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        if herramienta:
            mensajes += [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": herramienta}}]},
                {"role": "tool", "content": "{}", "tool_call_id": "c1"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 2 if herramienta else 1}


def _correr(monkeypatch, tmp_path, respuestas, historial=None):
    agente = _Agente(respuestas)
    eventos = []
    payload = {
        "task_id": "t-consulta",
        "prompt": PEDIDO_REAL,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "guardia_accion_narrada": True,
        "conversation_history": historial or [],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    for nombre in (
        "_abrir_salida",
        "_proteger_proceso",
        "_ocultar_token_a_los_comandos",
        "_activar_guardia",
        "_aplicar_overrides_resumen",
        "_preparar_exposicion",
    ):
        monkeypatch.setattr(runner, nombre, lambda *a: None)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=lambda **kw: agente))
    for clave in ("HERMES_HOME", "HERMES_QUIET", "HERMESHQ_TASK_PAYLOAD", "TERM"):
        monkeypatch.setenv(clave, "")
    monkeypatch.chdir(tmp_path)
    assert runner.main() == 0
    return [e for e in eventos if e.get("event") == "result"][0], agente


class TestRunner:
    def test_caso_real_reintenta_y_consulta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch, tmp_path, [(RESPUESTA_REAL, None), ("No tienes chats sin leer.", "teams_list_chats")]
        )
        assert "teams_list_chats" in agente.llamadas[1] and "no son reales" in agente.llamadas[1]
        assert res["final_response"] == "No tienes chats sin leer."
        assert res["accion_narrada"]["detectada"] == "consulta"
        assert res["accion_narrada"]["resultado"] == "corregida"

    def test_si_vuelve_a_inventar_se_entrega_el_aviso_honesto(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(RESPUESTA_REAL, None), (RESPUESTA_REAL, None)])
        assert len(agente.llamadas) == 2
        assert res["final_response"] == runner.aviso_consulta_sin_herramienta("chats de Teams")
        assert "Nicolás" not in res["final_response"]

    def test_un_seguimiento_con_los_datos_en_el_historial_no_se_reintenta(self, monkeypatch, tmp_path):
        historial = [{"role": "assistant", "content": RESPUESTA_REAL}]
        res, agente = _correr(monkeypatch, tmp_path, [(RESPUESTA_REAL, None)], historial=historial)
        assert len(agente.llamadas) == 1
        assert res["final_response"] == RESPUESTA_REAL


class TestCifrasDePlanilla:
    PEDIDO = (
        "Con esos mismos datos (work/hse-tablero/datos.json, hoja Inc. Seguridad): cual fue la zona de lesion mas "
        "afectada en la sucursal Centro en 2026 y cuantos casos tuvo?"
    )
    RESPUESTA = (
        "**Centro — 2026**\n- Zona de lesión más afectada: **Mano y dedos**\n- Casos: **1**\n\n*(En 2026, Centro "
        "solo registró 1 caso con dato de parte del cuerpo, y corresponde a mano y dedos.)*"
    )
    HISTORIAL = (
        "Listo. Informe HSE interactivo de accidentabilidad solo Norte.\n- Eventos: 854\n- Zona de lesión (desde "
        "2025, 47 casos): Cabeza y cara (11), Hombro y brazo (7), Pie y tobillo (6), Mano y dedos (5)"
    )

    def test_caso_real_cifra_de_otra_sucursal_sin_consultar(self):
        assert runner.consulta_sin_herramienta(self.PEDIDO, self.RESPUESTA, [], self.HISTORIAL) == (
            "planillas",
            "hq_leer_documento",
        )

    def test_con_la_consulta_no_aplica(self):
        assert runner.consulta_sin_herramienta(self.PEDIDO, self.RESPUESTA, ["hq_leer_documento"]) is None

    def test_una_oracion_con_cifra_tambien_cuenta(self):
        respuesta = "En Centro 2026 la zona más afectada fue Mano y dedos, con 9 casos."
        assert runner.consulta_sin_herramienta(
            "¿cuántos accidentes de mano tuvo la sucursal Centro?", respuesta, []
        ) == (
            "planillas",
            "hq_leer_documento",
        )

    def test_repetir_una_linea_ya_dicha_no_aplica(self):
        historial = "Norte 2022–2026\n- Incidentes: **836** · Accidentes: **18**"
        respuesta = "- Incidentes: 836 · Accidentes: 18"
        assert runner.consulta_sin_herramienta("¿cuántos accidentes tuvo Norte?", respuesta, [], historial) is None

    def test_generar_el_informe_no_es_consulta(self):
        pedido = "Genera el informe HSE de Norte con work/hse-tablero/datos.json, hoja Inc. Seguridad"
        assert runner.consulta_sin_herramienta(pedido, "Listo:\n- 836 incidentes", []) is None

    def test_sin_cifras_no_aplica(self):
        assert runner.consulta_sin_herramienta(self.PEDIDO, "¿Quieres que lo consulte en la hoja?", []) is None
