from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

PROPUESTA = (
    "Vista previa del cambio:\n- **Cita:** Prueba HermesHQ lote 1\n- **Antes:** jueves 12 de noviembre de 2026, 15:00 a "
    "15:30\n- **Después:** jueves 12 de noviembre de 2026, 16:00 a 16:30 (hora Chile)\n\n¿La cambio así?"
)
FALSO_EXITO = (
    "Cambio aplicado. ✅\n\n- **Cita:** Prueba HermesHQ lote 1\n- **Ahora:** jueves 12 de noviembre de 2026, 16:00 a "
    "16:30 (hora Chile)\n- **Asistentes:** solo tú"
)


class TestDetector:
    @pytest.mark.parametrize(
        "mensaje", ["sí", "Sí, por favor", "dale", "confírmalo", "sí, cámbiala así nomás", "ok", "hazlo", "Sí!"]
    )
    def test_confirmacion_tras_propuesta_y_exito_sin_herramienta(self, mensaje):
        assert runner.confirmacion_narrada(mensaje, FALSO_EXITO, PROPUESTA) == "programacion"

    def test_tipos_segun_la_respuesta(self):
        assert runner.confirmacion_narrada("sí", "Correo enviado a Ana ✅", "Borrador: … ¿Lo envío?") == "correo"
        assert runner.confirmacion_narrada("dale", "Listo, subí el archivo a OneDrive.", "¿Lo subo?") == "subida"
        assert runner.confirmacion_narrada("sí", "Listo, guardé la V2.", "¿Lo guardo así?") == "edicion"

    @pytest.mark.parametrize(
        ("mensaje", "respuesta", "anterior"),
        [
            ("sí", FALSO_EXITO, "Un cron es una tarea programada. ¿Quieres un ejemplo?"),
            ("sí", "Perfecto, ¿a qué hora la quieres?", PROPUESTA),
            ("sí", "No pude cambiarla: el servidor rechazó la hora.", PROPUESTA),
            ("sí pero a las 17", FALSO_EXITO, PROPUESTA),
            ("¿sí?", FALSO_EXITO, PROPUESTA),
            ("no", FALSO_EXITO, PROPUESTA),
            ("cámbiala a las 17 y agrega a Pedro", FALSO_EXITO, PROPUESTA),
            ("sí", FALSO_EXITO, ""),
        ],
    )
    def test_negativos(self, mensaje, respuesta, anterior):
        assert runner.confirmacion_narrada(mensaje, respuesta, anterior) is None

    def test_participios_nuevos(self):
        for texto in ("Cambio aplicado.", "Reunión movida a las 16.", "Listo, la reprogramé.", "Evento cancelado ✅"):
            assert runner.afirma_hecho(texto), texto

    def test_respuesta_anterior_salta_mensajes_de_herramienta(self):
        mensajes = [
            {"role": "user", "content": "muévela"},
            {"role": "assistant", "content": PROPUESTA},
            {"role": "user", "content": "sí"},
            {"role": "assistant", "content": FALSO_EXITO},
        ]
        assert runner._respuesta_anterior(mensajes, 2) == PROPUESTA
        assert runner._respuesta_anterior(mensajes, 0) == ""


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
                {"role": "tool", "content": '{"success": true}', "tool_call_id": "c1"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 2 if herramienta else 1}


def _correr(monkeypatch, tmp_path, respuestas, historia):
    agente = _Agente(respuestas)
    eventos = []
    payload = {
        "task_id": "t-conf",
        "prompt": "sí",
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "conversation_history": historia,
        "metadata": {"mensaje_usuario": "sí"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    monkeypatch.setattr(runner, "_abrir_salida", lambda: None)
    monkeypatch.setattr(runner, "_proteger_proceso", lambda: None)
    monkeypatch.setattr(runner, "_ocultar_token_a_los_comandos", lambda: None)
    monkeypatch.setattr(runner, "_activar_guardia", lambda p: None)
    monkeypatch.setattr(runner, "_aplicar_overrides_resumen", lambda *a: None)
    monkeypatch.setattr(runner, "_preparar_exposicion", lambda p: None)
    monkeypatch.setattr(runner, "_envio_aprobado_directo", lambda p: None)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=lambda **kw: agente))
    for clave in ("HERMES_HOME", "HERMES_QUIET", "HERMESHQ_TASK_PAYLOAD", "TERM"):
        monkeypatch.setenv(clave, "")
    monkeypatch.chdir(tmp_path)
    assert runner.main() == 0
    return next(e for e in eventos if e.get("event") == "result"), agente


HISTORIA = [{"role": "user", "content": "Muévela a las 16:00"}, {"role": "assistant", "content": PROPUESTA}]


class TestRunner:
    def test_caso_real_se_reintenta_y_llama_la_herramienta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch, tmp_path, [(FALSO_EXITO, None), ("Cambio aplicado.", "ms365_calendar_update_event")], HISTORIA
        )
        assert len(agente.llamadas) == 2
        assert "no llamaste ninguna herramienta" in agente.llamadas[1]
        assert res["accion_narrada"]["detectada"] == "programacion"
        assert res["accion_narrada"]["resultado"] == "corregida"
        assert [c["name"] for c in res["tool_calls"]] == ["ms365_calendar_update_event"]

    def test_si_insiste_se_entrega_el_aviso_honesto(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(FALSO_EXITO, None), (FALSO_EXITO, None)], HISTORIA)
        assert res["final_response"] == runner.aviso_accion_no_hecha("programacion", FALSO_EXITO)
        assert "el cambio en la reunión" in res["final_response"]
        assert "16:00" not in res["final_response"]

    def test_con_la_herramienta_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [("Cambio aplicado ✅", "ms365_calendar_update_event")], HISTORIA)
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res

    def test_sin_propuesta_previa_no_se_reintenta(self, monkeypatch, tmp_path):
        historia = [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola! ¿Te ayudo?"}]
        res, agente = _correr(monkeypatch, tmp_path, [("Listo ✅ quedó programada.", None)], historia)
        assert len(agente.llamadas) == 1
