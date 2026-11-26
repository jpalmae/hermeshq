from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO = 'Agéndame una reunión mañana jueves 12 de noviembre de 15:00 a 15:30 titulada "Prueba HermesHQ lote 1", sin invitados.'
VISTA_A_MANO = (
    "Vista previa de la reunión:\n\n- **Título:** Prueba HermesHQ lote 1\n- **Fecha:** jueves 12 de noviembre de 2026\n"
    "- **Hora:** 15:00 a 15:30 (hora Chile)\n- **Asistentes:** solo tú (sin invitados)\n\n¿La creo?"
)
VISTA_SERVIDOR = (
    "Vista previa (servidor):\n- **Título:** Prueba HermesHQ lote 1\n- **Inicio:** 2026-11-12 15:00\n\n"
    "Responde «sí» en un mensaje aparte para crearla."
)


def _usuario_y(texto, herramienta=None):
    mensajes = [{"role": "user", "content": PEDIDO}]
    if herramienta:
        mensajes += [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": herramienta}}]},
            {"role": "tool", "content": "{}", "tool_call_id": "c1"},
        ]
    return mensajes + [{"role": "assistant", "content": texto}]


class TestDetector:
    def test_caso_real_vista_escrita_a_mano(self):
        assert runner.vista_calendario_a_mano(PEDIDO, VISTA_A_MANO, _usuario_y(VISTA_A_MANO), 0)

    @pytest.mark.parametrize(
        "respuesta",
        [
            "**Asunto:** Comité\n**Inicio:** 10:00\n\n¿Confirmas que la agende así?",
            "Vista previa:\nTítulo: Revisión\n¿Está bien así?",
            "Título: Revisión semanal\nHora: 09:00\n\n¿Procedo a crearla?",
        ],
    )
    def test_variantes(self, respuesta):
        assert runner.vista_calendario_a_mano(
            "agenda una reunión con Ana el lunes", respuesta, _usuario_y(respuesta), 0
        )

    def test_caso_real_mover_sin_nombrar_la_reunion(self):
        respuesta = (
            "Vista previa del cambio:\n- **Cita:** Prueba HermesHQ lote 1\n- **Antes:** jueves 12 de noviembre de 2026, "
            "15:00 a 15:30\n- **Después:** jueves 12 de noviembre de 2026, 16:00 a 16:30 (hora Chile)\n\n¿La cambio así?"
        )
        pedido = "Muévela a las 16:00 a 16:30 del mismo día."
        assert runner.vista_calendario_a_mano(pedido, respuesta, _usuario_y(respuesta), 0)

    def test_vista_previa_de_un_documento_no_es_de_calendario(self):
        respuesta = "Vista previa del cambio:\n- **Título:** Riesgos del trimestre\n\n¿Lo cambio así?"
        assert not runner.vista_calendario_a_mano("cambia el título del documento", respuesta, _usuario_y(respuesta), 0)

    def test_caso_real_listar_y_escribir_la_vista_de_cancelacion_a_mano_si_aplica(self):
        respuesta = (
            "Encontré la reunión. Te muestro la vista previa de la cancelación:\n**Título:** Prueba HermesHQ lote 1\n"
            "**Cuándo:** jueves 15:00–15:30\n\n¿Confirmas que la cancelo?"
        )
        mensajes = _usuario_y(respuesta, "ms365_calendar_list_events")
        assert runner.vista_calendario_a_mano("Cancela la reunión de mañana.", respuesta, mensajes, 0)

    @pytest.mark.parametrize("pregunta", ["¿Lo aplico así?", "¿Te sirve?", "¿Seguimos con este cambio?", "Dime si va."])
    def test_caso_real_cualquier_pregunta_tras_una_vista_previa_escrita(self, pregunta):
        respuesta = (
            "Vista previa del cambio:\n- **Reunión:** Batería K ciclo 1\n- **Nuevo horario:** 11:00 a 11:30\n\n"
            + pregunta
        )
        detecta = runner.vista_calendario_a_mano("Muévela a las 11:00", respuesta, _usuario_y(respuesta), 0)
        assert detecta is (pregunta != "Dime si va.")

    def test_nota_de_reintento_para_reuniones_pide_listar_y_usar_la_ref(self):
        nota = runner.nota_accion_narrada("programacion", "✅ Cambio aplicado. **Reunión:** Batería K")
        assert "ms365_calendar_list_events" in nota and "E1" in nota and "no le pidas" in nota
        assert "E1" not in runner.nota_accion_narrada("programacion", "Programado tu recordatorio de los lunes.")

    def test_si_llamo_la_herramienta_de_calendario_no_aplica(self):
        mensajes = _usuario_y(VISTA_SERVIDOR, "ms365_calendar_create_event")
        assert not runner.vista_calendario_a_mano(PEDIDO, VISTA_A_MANO, mensajes, 0)

    @pytest.mark.parametrize(
        ("pedido", "respuesta"),
        [
            ("hazme un excel con ventas", "Vista previa del Excel:\n- **Título:** Ventas 2026\n\n¿Lo creo así?"),
            (PEDIDO, "¿A qué hora la quieres y con quién?"),
            (PEDIDO, "**Título:** Prueba\n**Hora:** 15:00"),
            ("¿qué reuniones tengo mañana?", "**Asunto:** Comité 10:00\n**Asunto:** Daily 12:00"),
        ],
    )
    def test_negativos(self, pedido, respuesta):
        assert not runner.vista_calendario_a_mano(pedido, respuesta, _usuario_y(respuesta), 0)

    def test_pregunta_de_confirmacion_sin_vista_tambien_cuenta(self):
        respuesta = "Mañana tienes libre de 15:00 a 16:00. ¿La agendo a las 15:00?"
        consulto = _usuario_y(respuesta, "ms365_calendar_find_free_time")
        assert runner.vista_calendario_a_mano(PEDIDO, respuesta, consulto, 0)
        assert not runner.vista_calendario_a_mano(PEDIDO, respuesta, _usuario_y(respuesta), 0)

    def test_nota_pide_la_vista_previa_del_servidor(self):
        nota = runner.nota_accion_narrada("vista_calendario")
        assert "ms365_calendar_create_event" in nota and "SIN aprobado_por_el_usuario" in nota


class TestNotaConcreta:
    def test_caso_real_validacion_cancelacion_a_mano_nombra_la_herramienta(self):
        descartada = (
            "📅 **Vista previa de la cancelación** (esto es exactamente lo que se eliminará):\n- **Reunión:** Validación "
            "lote 19\n\n¿La elimino? Responde «sí» para confirmar."
        )
        nota = runner.nota_accion_narrada("vista_calendario", descartada)
        assert "ms365_calendar_delete_event" in nota and "SIN aprobado_por_el_usuario" in nota
        assert "ms365_calendar_create_event" not in nota

    def test_cambio_a_mano_nombra_update_y_crear_sigue_igual(self):
        cambio = "📅 **Vista previa del cambio**:\n- **Nuevo fin:** 15:00\n¿Lo aplico?"
        assert "ms365_calendar_update_event" in runner.nota_accion_narrada("vista_calendario", cambio)
        crear = "📅 **Vista previa de la reunión**:\n- **Título:** Comité\n- **Inicio:** 15:00\n¿La creo?"
        assert runner.nota_accion_narrada("vista_calendario", crear) == runner.nota_accion_narrada("vista_calendario")


class _Agente:
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
                {"role": "tool", "content": '{"requiere_aprobacion": true}', "tool_call_id": "c1"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 2 if herramienta else 1}


def _correr(monkeypatch, tmp_path, respuestas, prompt=PEDIDO, guardia=True):
    agente = _Agente(respuestas)
    eventos = []
    payload = {
        "task_id": "t-cal",
        "prompt": prompt,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "guardia_accion_narrada": guardia,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    monkeypatch.setattr(runner, "_abrir_salida", lambda: None)
    monkeypatch.setattr(runner, "_proteger_proceso", lambda: None)
    monkeypatch.setattr(runner, "_ocultar_token_a_los_comandos", lambda: None)
    monkeypatch.setattr(runner, "_activar_guardia", lambda p: None)
    monkeypatch.setattr(runner, "_aplicar_overrides_resumen", lambda *a: None)
    monkeypatch.setattr(runner, "_preparar_exposicion", lambda p: None)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=lambda **kw: agente))
    for clave in ("HERMES_HOME", "HERMES_QUIET", "HERMESHQ_TASK_PAYLOAD", "TERM"):
        monkeypatch.setenv(clave, "")
    monkeypatch.chdir(tmp_path)
    assert runner.main() == 0
    return next(e for e in eventos if e.get("event") == "result"), agente


class TestRunner:
    def test_vista_a_mano_se_reintenta_y_queda_la_del_servidor(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch, tmp_path, [(VISTA_A_MANO, None), (VISTA_SERVIDOR, "ms365_calendar_create_event")]
        )
        assert len(agente.llamadas) == 2
        assert agente.llamadas[1] == runner.nota_accion_narrada("vista_calendario")
        assert res["final_response"] == VISTA_SERVIDOR
        assert res["accion_narrada"]["detectada"] == "vista_calendario"
        assert res["accion_narrada"]["resultado"] == "corregida"
        assert [c["name"] for c in res["tool_calls"]] == ["ms365_calendar_create_event"]

    def test_caso_real_bateria_si_insiste_a_mano_no_se_entrega_una_vista_que_un_si_no_ejecutaria(
        self, monkeypatch, tmp_path
    ):
        res, agente = _correr(monkeypatch, tmp_path, [(VISTA_A_MANO, None), (VISTA_A_MANO, None)])
        assert len(agente.llamadas) == 2
        assert res["final_response"] == runner._AVISO_VISTA_SIN_REGISTRAR
        assert res["accion_narrada"]["resultado"] == "aviso_honesto"

    def test_con_la_herramienta_en_el_primer_turno_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(VISTA_SERVIDOR, "ms365_calendar_create_event")])
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res

    def test_con_la_guardia_apagada_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(VISTA_A_MANO, None)], guardia=False)
        assert len(agente.llamadas) == 1
        assert res["final_response"] == VISTA_A_MANO

    def test_tras_el_si_una_nueva_vista_a_mano_tambien_se_corrige(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [(VISTA_A_MANO, None), (VISTA_SERVIDOR, "ms365_calendar_create_event")],
            prompt="sí",
        )
        assert len(agente.llamadas) == 2
        assert res["accion_narrada"]["resultado"] == "corregida"

    def test_la_confirmacion_que_llama_la_herramienta_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [("Reunión creada.", "ms365_calendar_create_event")], prompt="sí")
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res
