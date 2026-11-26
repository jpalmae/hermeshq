from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO_REAL = (
    "[FORMATO PEDIDO EN ESTE MENSAJE: HTML interactivo. Entrega el resultado como HTML. No cambies a Excel u otro "
    "formato salvo que este mensaje lo pida.]\nUsa la skill `dashboard-interactivo` para REDISEÑAR el dashboard "
    "actual.\n \nIMPORTANTE: no quiero que simplemente mejores colores, agregues tarjetas o cambies gráficos."
)
RESPUESTA_REAL = (
    "No tengo la herramienta `sharepoint_upload_file` disponible en este turno. El archivo HTML queda en el "
    "directorio de salida y se entrega como adjunto.\n\n---\n\n## Listo — Dashboard rediseñado (v2)\n\nGeneré el "
    "**Dashboard de Accesos Demo** con una experiencia de análisis moderna, manteniendo todos los datos y "
    "cálculos correctos.\n\n**Mejoras aplicadas:**\n- **Colores diversificados**\n\n"
    "MEDIA: uploads/0e669680-39f7-4a53-802a-d6f50a164b31.html"
)


def _turno(*herramientas):
    mensajes = [{"role": "user", "content": "pedido"}]
    for i, nombre in enumerate(herramientas):
        mensajes += [
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]},
            {"role": "tool", "content": '{"success": true}', "tool_call_id": f"c{i}"},
        ]
    return mensajes


class TestDetector:
    def test_caso_real_dashboard_rediseñado_tras_solo_leer(self):
        assert runner.accion_narrada_tras_lecturas(PEDIDO_REAL, RESPUESTA_REAL) == "edicion"

    @pytest.mark.parametrize(
        ("mensaje", "respuesta", "tipo"),
        [
            ("Genera un dashboard con este Excel", "Listo ✅ Generé el dashboard de ventas con 3 páginas.", "creacion"),
            ("Mejora el dashboard", "Dashboard mejorado ✅ con filtros por sucursal.", "edicion"),
            ("Hazme un Excel con el resumen", "Listo, creé el **Excel** con el resumen por mes.", "creacion"),
            ("Súbelo a OneDrive", "Listo, lo subí a OneDrive ✅", "subida"),
            ("Envíale el correo a Ana", "Correo enviado a Ana ✅", "correo"),
        ],
    )
    def test_afirma_lo_pedido_sin_herramienta_con_efecto(self, mensaje, respuesta, tipo):
        assert runner.accion_narrada_tras_lecturas(mensaje, respuesta) == tipo

    @pytest.mark.parametrize(
        ("mensaje", "respuesta"),
        [
            (
                "Hazme un análisis del Excel de ventas",
                "Preparé el análisis del Excel: la sucursal Norte vende 32% más que el promedio.",
            ),
            ("Haz un resumen del archivo", "Revisé el archivo. Resumen: 2.794 filas, 253 reportes y 138 usuarios."),
            ("Genera un dashboard con este Excel", "Antes de generarlo, ¿quieres los filtros por sucursal o por área?"),
            ("Genera un dashboard con este Excel", "No pude leer la hoja «Accesos»: el archivo está protegido."),
            ("¿Cuántas filas tiene el Excel?", "Generé el conteo: la hoja tiene 2.794 filas."),
            ("Mejora el dashboard", "Voy a revisar primero los datos y luego te muestro una propuesta."),
        ],
    )
    def test_negativos_analisis_en_el_chat_o_sin_afirmacion(self, mensaje, respuesta):
        assert runner.accion_narrada_tras_lecturas(mensaje, respuesta) is None

    @pytest.mark.parametrize(
        ("mensaje", "respuesta"),
        [
            (
                "Reanuda la tarea programada «Prueba auditoría CRON-1».",
                "Listo. La tarea **«Prueba auditoría CRON-1»** quedó **reactivada**. Su próxima ejecución es hoy.",
            ),
            (
                "Pausa la tarea programada «Resumen semanal».",
                "Listo. La tarea «Resumen semanal» quedó pausada.",
            ),
            ("Elimina la tarea programada «Resumen semanal».", "Listo, eliminé la tarea programada."),
            ("Desactiva el recordatorio de los lunes.", "Hecho, desactivé el recordatorio."),
        ],
    )
    def test_pausar_reanudar_o_eliminar_una_tarea_programada_sin_herramienta(self, mensaje, respuesta):
        assert runner.accion_narrada_tras_lecturas(mensaje, respuesta) == "programacion"

    @pytest.mark.parametrize(
        ("mensaje", "respuesta"),
        [
            ("Reanuda la tarea programada «X».", "¿Quieres que la reanude ahora o prefieres cambiar el horario?"),
            ("Pausa la tarea programada «X».", "No encuentro esa tarea en tu lista."),
            ("¿Qué pasa si pauso la tarea programada «X»?", "Quedaría pausada y no se ejecutaría hasta reanudarla."),
            ("Pausa el reproductor de música.", "Listo, quedó pausado."),
        ],
    )
    def test_negativos_de_tareas_programadas(self, mensaje, respuesta):
        assert runner.accion_narrada_tras_lecturas(mensaje, respuesta) is None

    def test_caso_real_reanudar_tarea_programada_sin_llamar_la_herramienta(self):
        mensaje = "Reanuda la tarea programada «Prueba auditoría CRON-1»."
        respuesta = "Listo. La tarea **«Prueba auditoría CRON-1»** quedó **reactivada**. Su próxima ejecución es hoy."
        assert runner.accion_narrada(mensaje, respuesta) == "programacion"

    def test_un_codigo_de_confirmacion_ofrecido_lo_resuelve_la_guardia_de_codigos(self):
        mensaje = "elimina el recordatorio «Recordatorio Batería P1»"
        respuesta = "El código de confirmación es: **4281**\nEnvíame: `eliminar 4281` y lo doy por borrado."
        assert runner.accion_narrada(mensaje, respuesta) is None

    def test_mejores_adjetivo_no_es_pedido_de_edicion(self):
        assert runner.pedido_con_efecto("dame los mejores reportes del excel") is None

    def test_lecturas_del_turno(self):
        assert runner.lecturas_del_turno(_turno("hq_leer_documento", "read_file"), 0) == [
            "hq_leer_documento",
            "read_file",
        ]
        assert runner.lecturas_del_turno(_turno("hq_leer_documento", "hq_xlsx_generate"), 0) is None
        assert runner.lecturas_del_turno(_turno("read_file", "execute_code"), 0) is None
        assert runner.lecturas_del_turno(_turno("herramienta_desconocida"), 0) is None
        assert runner.lecturas_del_turno(_turno(), 0) is None

    def test_textos_de_nota_y_aviso(self):
        nota = runner.nota_accion_narrada("edicion", RESPUESTA_REAL, lecturas=["hq_leer_documento", "read_file"])
        assert "solo usaste herramientas de lectura (hq_leer_documento, read_file)" in nota
        assert "no llamaste ninguna herramienta" not in nota
        aviso = runner.aviso_accion_no_hecha("edicion", RESPUESTA_REAL, solo_lectura=True)
        assert "solo leí datos" in aviso and "ninguna herramienta" not in aviso


class _Agente:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.llamadas.append(user_message)
        texto, herramientas = self.respuestas.pop(0)
        mensajes = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        for i, nombre in enumerate(herramientas):
            mensajes += [
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]},
                {"role": "tool", "content": '{"success": true}', "tool_call_id": f"c{i}"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 1 + len(herramientas)}


def _correr(monkeypatch, tmp_path, respuestas, mensaje=PEDIDO_REAL):
    agente = _Agente(respuestas)
    eventos = []
    payload = {
        "task_id": "t-lect",
        "prompt": mensaje,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "conversation_history": [],
        "metadata": {},
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


LEER = ["hq_leer_documento", "read_file"]


class TestRunner:
    def test_caso_real_se_reintenta_y_genera_el_dashboard(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [(RESPUESTA_REAL, LEER), ("Listo, Excel v2 generado.", ["hq_xlsx_generate"])],
        )
        assert len(agente.llamadas) == 2
        assert "solo usaste herramientas de lectura" in agente.llamadas[1]
        assert res["accion_narrada"]["detectada"] == "edicion"
        assert res["accion_narrada"]["solo_lectura"] == LEER
        assert res["accion_narrada"]["resultado"] == "corregida"

    def test_si_vuelve_a_solo_leer_y_afirmar_se_entrega_el_aviso_honesto(self, monkeypatch, tmp_path):
        res, _ = _correr(monkeypatch, tmp_path, [(RESPUESTA_REAL, LEER), (RESPUESTA_REAL, ["read_file"])])
        assert res["accion_narrada"]["resultado"] == "aviso_honesto"
        assert res["final_response"].startswith("No pude completar el cambio: solo leí datos")
        assert "MEDIA:" not in res["final_response"]

    def test_con_herramienta_generadora_no_se_reintenta(self, monkeypatch, tmp_path):
        (tmp_path / "uploads").mkdir()
        (tmp_path / "uploads" / "0e669680-39f7-4a53-802a-d6f50a164b31.html").write_text("<html></html>")
        res, agente = _correr(monkeypatch, tmp_path, [(RESPUESTA_REAL, LEER + ["hq_xlsx_generate"])])
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res

    def test_analisis_en_el_chat_tras_leer_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [("Preparé el análisis del Excel: Norte vende 32% más que el promedio.", LEER)],
            mensaje="Hazme un análisis del Excel de ventas",
        )
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res

    def test_reintento_que_solo_lee_tras_cero_herramientas_ya_no_cuenta_como_corregido(self, monkeypatch, tmp_path):
        res, _ = _correr(
            monkeypatch,
            tmp_path,
            [("Listo ✅ Generé el dashboard.", []), ("Listo ✅ Generé el dashboard.", ["read_file"])],
            mensaje="Genera un dashboard con este Excel",
        )
        assert res["accion_narrada"]["resultado"] == "aviso_honesto"
        assert "solo leí datos" in res["final_response"]


class TestMensajeParaHerramientas:
    def test_usa_el_original_completo_sin_el_corte_de_600(self):
        largo = "Hace un informe " + "con mucho detalle " * 60 + "y agrega dos árbol jerárquico"
        payload = {"prompt": "[FORMATO] " + "x" * 3000, "metadata": {"mensaje_usuario": largo}}
        assert runner._mensaje_para_herramientas(payload) == largo

    def test_sin_original_usa_el_prompt(self):
        assert runner._mensaje_para_herramientas({"prompt": "hola", "metadata": {}}) == "hola"
