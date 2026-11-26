import io
import json
import sys
from types import SimpleNamespace

import pytest

import hermeshq.scripts.hermes_task_runner as runner

CODIGO_REAL = "9TSM2V"
DELETE_409 = {
    "success": False,
    "status_code": 409,
    "error": (
        "Schedule 'Recordatorio Batería P1' is approved, so deleting it needs the user's confirmation. Tell the user "
        f"to reply exactly 'eliminar {CODIGO_REAL}' in this chat, then call hq_schedule_delete again."
    ),
}
LISTA = {
    "success": True,
    "schedules": [{"id": "ec92d9a8-7f4f-40f6-a2af-4548722d6b6d", "name": "Recordatorio Batería P1"}],
}

INVENTADO_4281 = (
    "Voy a eliminar ese recordatorio. Como está aprobado, requiere tu confirmación final. El código de confirmación "
    "es: **4281**\nEnvíame: `eliminar 4281` y lo doy por borrado."
)
INVENTADO_DOS = (
    "Ambos son aprobados, así que requieren confirmación del usuario. Te paso los códigos:\n"
    "- **Recordatorio Batería P1** → código: `4281`\n- **Resumen semanal de correos no leídos** → código: `2107`\n"
    "Envíame algo como: `eliminar 4281` y `eliminar 2107`, y los quito de tu lista."
)
INVENTADO_4ZJN2P = (
    "El recordatorio «Resumen semanal de correos no leídos» está aprobado, así que necesito tu confirmación para "
    "eliminarlo. Revisa los datos:\n- **Frecuencia:** Todos los viernes a las 10:00 (hora Chile)\n"
    "Si confirmas, responde exactamente **eliminar 4ZJN2P** en este chat y lo elimino al instante."
)
REAL = (
    "El recordatorio «Recordatorio Batería P1» está aprobado, así que necesito tu confirmación para eliminarlo.\n"
    f"Si confirmas, responde exactamente **eliminar {CODIGO_REAL}** en este chat y lo elimino al instante."
)


def _turno(*herramientas, usuario="elimina el recordatorio «Recordatorio Batería P1»"):
    mensajes = [{"role": "user", "content": usuario}]
    for i, (nombre, resultado) in enumerate(herramientas):
        mensajes.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]}
        )
        contenido = resultado if isinstance(resultado, str) else json.dumps(resultado, ensure_ascii=False)
        mensajes.append({"role": "tool", "tool_call_id": f"c{i}", "content": contenido})
    return mensajes


def _codigos(respuesta, mensajes):
    return [p for p in runner.verificar_afirmaciones(respuesta, mensajes, 0) if p["tipo"] == "codigo"]


class TestCasosReales:
    @pytest.mark.parametrize(
        ("respuesta", "esperados"),
        [(INVENTADO_4281, ["4281"]), (INVENTADO_DOS, ["4281", "2107"]), (INVENTADO_4ZJN2P, ["4ZJN2P"])],
    )
    def test_codigo_sin_ninguna_herramienta_es_inventado(self, respuesta, esperados):
        pendientes = _codigos(respuesta, _turno())
        assert [p["frase"] for p in pendientes] == esperados
        assert all(p["sin_consulta"] for p in pendientes)

    def test_codigo_real_del_servidor_no_se_toca(self):
        mensajes = _turno(("hq_schedule_list", LISTA), ("hq_schedule_delete", DELETE_409))
        assert runner.verificar_afirmaciones(REAL, mensajes, 0) == []

    def test_aprobar_con_el_codigo_de_la_solicitud(self):
        mensajes = _turno(
            ("hq_schedule_request", {"success": True, "approval_code": "3HM2UZ", "requiere_aprobacion": True}),
            usuario="recuérdame los lunes a las 9 revisar respaldos",
        )
        assert _codigos("Para activarlo responde `aprobar 3HM2UZ`.", mensajes) == []

    def test_codigo_que_escribio_el_usuario(self):
        mensajes = _turno(usuario="ya escribí aprobar DM7PQC, ¿quedó?")
        assert _codigos("Escribiste `aprobar DM7PQC`: ese es el código correcto.", mensajes) == []

    def test_consulto_pero_mostro_otro_codigo(self):
        mensajes = _turno(("hq_schedule_delete", DELETE_409))
        pendientes = _codigos("Responde **eliminar 4ZJN2P** para borrarlo.", mensajes)
        assert [p["frase"] for p in pendientes] == ["4ZJN2P"] and pendientes[0]["sin_consulta"] is False

    def test_un_numero_largo_que_lo_contiene_no_lo_respalda(self):
        mensajes = _turno(("hq_schedule_list", {"success": True, "id": "a942810b"}))
        assert [p["frase"] for p in _codigos("Envíame `eliminar 4281`.", mensajes)] == ["4281"]

    def test_la_nota_del_reintento_no_respalda_el_codigo(self):
        mensajes = _turno() + [{"role": "user", "content": "[Nota del sistema] «4281»: ese código no salió…"}]
        assert [p["frase"] for p in _codigos("Envíame `eliminar 4281`.", mensajes)] == ["4281"]

    def test_codigo_de_un_turno_anterior_se_respalda_con_la_lista(self):
        lista = {
            "success": True,
            "schedules": [{"name": "Correo lun-vie", "approval_status": "pending", "approval_code": "CX8DH6"}],
        }
        mensajes = _turno(("hq_schedule_list", lista), usuario="me parece bien")
        assert _codigos("Responde `aprobar CX8DH6` y lo activo al instante.", mensajes) == []

    def test_codigo_dado_en_un_turno_anterior_obliga_a_consultar(self):
        mensajes = [
            {"role": "user", "content": "elimina el recordatorio"},
            {"role": "assistant", "content": f"Responde eliminar {CODIGO_REAL}"},
        ] + _turno(usuario="¿cuál era el código?")
        pendientes = runner.verificar_afirmaciones(f"Era `eliminar {CODIGO_REAL}`.", mensajes, 2)
        assert [p["frase"] for p in pendientes if p["tipo"] == "codigo"] == [CODIGO_REAL]


class TestNoSonCodigos:
    @pytest.mark.parametrize(
        "respuesta",
        [
            "Cuando te llegue el código, responde `aprobar CODE` en este chat.",
            "Para activarlo responde «aprobar CÓDIGO» con el código que te doy.",
            "Escribe eliminar XXXXXX con el código que aparece en la tarjeta.",
            "Puedo eliminar EQ-0015 de la lista si me confirmas.",
            "¿Quieres eliminar TODO el contenido de la hoja?",
            "Antes de aprobar el presupuesto revisa la columna C.",
            "El código de confirmación te llegará en la tarjeta de aprobación.",
            "El código postal 2340000 corresponde a Valparaíso.",
            "La OT 4500012345 tiene código de falla ELEC.",
            "Para aprobarlo escribe el código que aparece arriba.",
        ],
    )
    def test_no_se_marca(self, respuesta):
        assert _codigos(respuesta, _turno()) == []


class TestRespuestaCorregida:
    def test_sin_consulta_no_queda_nada_inventado(self):
        pendientes = _codigos(INVENTADO_4ZJN2P, _turno())
        final = runner.respuesta_verificada(INVENTADO_4ZJN2P, pendientes)
        assert "4ZJN2P" not in final and "viernes" not in final
        assert "No consulté tus tareas programadas" in final

    def test_con_consulta_se_quitan_las_lineas_del_codigo(self):
        respuesta = (
            "El recordatorio «Recordatorio Batería P1» está aprobado y se ejecuta los martes a las 10:00 en hora de "
            "Chile, con el mensaje «Batería P1: revisar respaldos».\nResponde **eliminar 4ZJN2P** para borrarlo."
        )
        pendientes = _codigos(respuesta, _turno(("hq_schedule_delete", DELETE_409)))
        final = runner.respuesta_verificada(respuesta, pendientes)
        assert "4ZJN2P" not in final and "martes a las 10:00" in final
        assert "Quité un código de confirmación" in final

    def test_la_nota_dice_de_donde_sale_el_codigo(self):
        nota = runner.nota_verificacion(_codigos(INVENTADO_4281, _turno()))
        assert "«4281»" in nota and "hq_schedule_delete" in nota
        assert "sin mencionar esta revisión" in nota


class TestNegativaSinConsultar:
    def test_caso_real_no_tengo_ningun_recordatorio(self):
        pedido = "elimina los recordatorios «Recordatorio Batería P1» y «Resumen semanal de correos no leídos»"
        respuesta = (
            "Voy a revisar los recordatorios que tienes antes de eliminar. No tengo ningún recordatorio programado a "
            "tu nombre en este momento, así que no puedo eliminar «Recordatorio Batería P1» ni «Resumen semanal de "
            "correos no leídos»: ninguno aparece en tu lista."
        )
        assert runner.accion_narrada(pedido, respuesta) == "busqueda"

    @pytest.mark.parametrize(
        ("pedido", "respuesta"),
        [
            ("revisa mi calendario de mañana", "No tienes reuniones mañana."),
            ("busca el correo de Juan sobre la factura", "No hay correos de Juan sobre la factura."),
            ("muéstrame mis crons", "Ninguno aparece activo en tu cuenta."),
            ("abre el informe de ventas", "No existe ningún archivo con ese nombre."),
        ],
    )
    def test_variantes(self, pedido, respuesta):
        assert runner.accion_narrada(pedido, respuesta) == "busqueda"

    @pytest.mark.parametrize(
        ("pedido", "respuesta"),
        [
            ("revisa los archivos de mi PC", "No tengo acceso a tu PC ni a archivos locales."),
            ("busca en internet el boletín", "No tengo una herramienta conectada a internet para buscar documentos."),
            ("revisa mis correos", "¿No tienes correos en otra carpeta? Dime cuál."),
            ("hola", "No tienes recordatorios activos."),
            ("busca la factura de Ana", "Encontré 2 facturas de Ana. ¿Cuál quieres?"),
        ],
    )
    def test_no_se_marca(self, pedido, respuesta):
        assert runner.accion_narrada(pedido, respuesta) is None

    def test_nota_de_busqueda_nombra_las_herramientas(self):
        nota = runner.nota_accion_narrada("busqueda")
        assert "hq_schedule_list" in nota and "ms365_calendar_list_events" in nota
        assert "sin mencionar esta revisión" in nota


class TestDisculpaPorLaRevision:
    def test_caso_real_disculpa_tenias_razon(self):
        texto = (
            "El archivo **sí existe** en tu OneDrive:\n- **Carpeta:** Escritorio/Pruebas/Bateria\n"
            "Disculpa — tenías razón: no lo había buscado antes de responder. Está en la carpeta "
            "**Escritorio/Pruebas/Bateria**."
        )
        limpio = runner.quitar_disculpa_por_revision(texto)
        assert "Disculpa" not in limpio and "tenías razón" not in limpio
        assert limpio.endswith("Está en la carpeta **Escritorio/Pruebas/Bateria**.")
        assert "- **Carpeta:** Escritorio/Pruebas/Bateria" in limpio

    @pytest.mark.parametrize(
        "texto",
        [
            "Disculpa la demora: aquí está el informe con las 3 tablas.",
            "Tienes razón, el archivo está en la carpeta Proyectos.",
            "Lo siento, Microsoft 365 rechazó el envío: el destinatario no existe.",
            "El correo de Ana dice «disculpa, no lo había revisado antes de responder».",
            "Lo encontré. Tienes razón: el correo con la **fecha límite 7 de octubre** está en tu bandeja, y mi error "
            "fue buscar sin revisar el cuerpo.",
        ],
    )
    def test_disculpas_legitimas_no_se_tocan(self, texto):
        assert runner.quitar_disculpa_por_revision(texto) == texto

    @pytest.mark.parametrize(
        "nota",
        [
            runner._NOTA_IDIOMA,
            runner._NOTA_VISTA_CALENDARIO,
            runner.nota_accion_narrada("edicion"),
            runner.nota_verificacion([{"tipo": "correo", "frase": "Envié el correo."}]),
        ],
    )
    def test_todas_las_notas_piden_no_mencionar_la_revision(self, nota):
        assert nota.endswith(runner._SIN_MENCIONAR_REVISION)


class _Agente:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def run_conversation(self, user_message, task_id=None, system_message=None, conversation_history=None):
        self.llamadas.append(user_message)
        texto, herramientas = self.respuestas.pop(0)
        mensajes = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        for i, (nombre, resultado) in enumerate(herramientas):
            mensajes += [
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]},
                {"role": "tool", "content": json.dumps(resultado, ensure_ascii=False), "tool_call_id": f"c{i}"},
            ]
        mensajes.append({"role": "assistant", "content": texto})
        return {"final_response": texto, "messages": mensajes, "api_calls": 1 + len(herramientas)}


def _correr(monkeypatch, tmp_path, respuestas, mensaje):
    agente = _Agente(respuestas)
    eventos = []
    payload = {
        "task_id": "t-cod",
        "prompt": mensaje,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "conversation_history": [],
        "metadata": {},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(runner, "_emit", lambda e: eventos.append(e))
    for nombre in ("_abrir_salida", "_proteger_proceso", "_ocultar_token_a_los_comandos"):
        monkeypatch.setattr(runner, nombre, lambda: None)
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


class TestEnElRunner:
    PEDIDO = "elimina el recordatorio «Recordatorio Batería P1»"

    def test_codigo_inventado_se_reintenta_y_sale_el_real(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [(INVENTADO_4281, []), (REAL, [("hq_schedule_list", LISTA), ("hq_schedule_delete", DELETE_409)])],
            self.PEDIDO,
        )
        assert len(agente.llamadas) == 2 and "«4281»" in agente.llamadas[1]
        assert res["verificacion"]["resultado"] == "corregida"
        assert CODIGO_REAL in res["final_response"] and "4281" not in res["final_response"]

    def test_si_insiste_sin_consultar_no_llega_ningun_codigo(self, monkeypatch, tmp_path):
        res, _ = _correr(monkeypatch, tmp_path, [(INVENTADO_4281, []), (INVENTADO_4ZJN2P, [])], self.PEDIDO)
        assert res["verificacion"]["resultado"] == "aviso_honesto"
        assert "4281" not in res["final_response"] and "4ZJN2P" not in res["final_response"]
        assert "No consulté tus tareas programadas" in res["final_response"]

    def test_negativa_sin_consultar_se_reintenta_y_consulta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [
                ("No tengo ningún recordatorio programado a tu nombre: ninguno aparece en tu lista.", []),
                (REAL, [("hq_schedule_list", LISTA), ("hq_schedule_delete", DELETE_409)]),
            ],
            self.PEDIDO,
        )
        assert "hq_schedule_list" in agente.llamadas[1]
        assert res["accion_narrada"]["resultado"] == "corregida"
        assert CODIGO_REAL in res["final_response"]

    def test_si_insiste_en_la_negativa_sin_consultar_se_avisa(self, monkeypatch, tmp_path):
        negativa = "No tienes ningún recordatorio activo."
        res, _ = _correr(monkeypatch, tmp_path, [(negativa, []), (negativa, [])], self.PEDIDO)
        assert res["accion_narrada"]["resultado"] == "aviso_honesto"
        assert res["final_response"] == runner._AVISO_NEGATIVA_SIN_CONSULTA

    def test_la_disculpa_tras_el_reintento_no_llega(self, monkeypatch, tmp_path):
        corregida = f"Disculpa, tenías razón: no lo había revisado antes de responder.\n{REAL}"
        res, _ = _correr(
            monkeypatch,
            tmp_path,
            [(INVENTADO_4281, []), (corregida, [("hq_schedule_delete", DELETE_409)])],
            self.PEDIDO,
        )
        assert res["final_response"].startswith("El recordatorio «Recordatorio Batería P1» está aprobado")

    def test_respuesta_con_codigo_real_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [(REAL, [("hq_schedule_list", LISTA), ("hq_schedule_delete", DELETE_409)])],
            self.PEDIDO,
        )
        assert len(agente.llamadas) == 1 and "verificacion" not in res and "accion_narrada" not in res
