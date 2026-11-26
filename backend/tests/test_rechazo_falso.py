import json

import hermeshq.scripts.hermes_task_runner as runner

PEDIDO_REAL = (
    "En la presentación que acabas de crear, agrega un cuarto riesgo «Calidad de datos de origen: registros incompletos "
    "en sistemas legados» en la lámina de riesgos y cambia el título de la lámina «Próximos pasos» por «Hoja de ruta Q1 "
    "2027». No cambies nada más."
)
RESPUESTA_REAL = (
    "Voy a aplicar los dos cambios con la herramienta de edición de presentaciones.\n\nNo generé ningún archivo: no pude "
    "aplicar los cambios. La herramienta `hq_pptx_editar` no está disponible en mi lista de herramientas, y no existe "
    "otra que edite un .pptx existente.\n\nPara poder hacerlo necesito una de estas opciones:\n\n1. **Que uses tú la "
    "edición** — abrir el .pptx en PowerPoint…"
)
REGISTRADAS = {"hq_pptx_generate", "hq_pptx_editar", "skill_view", "execute_code"}


class TestRechazoFalso:
    def test_caso_real_niega_herramientas_registradas(self):
        assert runner.herramientas_negadas_disponibles(PEDIDO_REAL, RESPUESTA_REAL, REGISTRADAS) == ["hq_pptx_editar"]

    def test_la_nota_nombra_las_herramientas(self):
        nota = runner.nota_rechazo_falso(["hq_pptx_editar"])
        assert "hq_pptx_editar" in nota and "el usuario no la ve" in nota

    def test_sin_nombre_pero_del_mismo_tipo_de_archivo(self):
        respuesta = "No pude generar la presentación porque **no tengo la herramienta para hacerlo**."
        pedido = "arma una presentación de 5 láminas con el avance del proyecto"
        assert runner.herramientas_negadas_disponibles(pedido, respuesta, REGISTRADAS) == ["hq_pptx_generate"]

    def test_subida_negada_con_la_herramienta_registrada(self):
        respuesta = "No tengo la herramienta `sharepoint_upload_file` disponible en este turno."
        assert runner.herramientas_negadas_disponibles(
            "súbelo a OneDrive", respuesta, REGISTRADAS | {"sharepoint_upload_file"}
        ) == ["sharepoint_upload_file"]


class TestRechazoVerdadero:
    def test_la_herramienta_de_verdad_no_esta_registrada(self):
        respuesta = "No tengo la herramienta `sharepoint_upload_file` disponible en este turno."
        assert runner.herramientas_negadas_disponibles("súbelo a OneDrive", respuesta, REGISTRADAS) == []

    def test_caso_real_a3186de2_otra_capacidad_en_un_pedido_de_presentacion(self):
        pedido = "hazme una presentación con el pronóstico del clima de la semana en Valparaíso"
        respuesta = (
            "**No tengo acceso a datos del clima en tiempo real.** No cuento con una herramienta que consulte "
            "pronósticos o registros meteorológicos."
        )
        assert runner.herramientas_negadas_disponibles(pedido, respuesta, REGISTRADAS) == []

    def test_algo_no_disponible_que_no_es_una_herramienta(self):
        respuesta = "La memoria no está disponible en este entorno, así que no pude guardarla."
        assert runner.herramientas_negadas_disponibles(PEDIDO_REAL, respuesta, REGISTRADAS) == []

    def test_respuesta_sin_rechazo(self):
        respuesta = "Listo: agregué el cuarto riesgo. La lámina «Próximos pasos» no existe en esta presentación."
        assert runner.herramientas_negadas_disponibles(PEDIDO_REAL, respuesta, REGISTRADAS) == []

    def test_sin_herramientas_registradas(self):
        assert runner.herramientas_negadas_disponibles(PEDIDO_REAL, RESPUESTA_REAL, set()) == []


class TestPistaParaLaAccionNarrada:
    def test_editar_una_presentacion(self):
        assert runner.herramientas_del_pedido(PEDIDO_REAL, REGISTRADAS) == ["hq_pptx_editar"]

    def test_crear_una_presentacion(self):
        assert runner.herramientas_del_pedido("crea una presentación de 4 láminas", REGISTRADAS) == ["hq_pptx_generate"]

    def test_solo_las_registradas(self):
        assert runner.herramientas_del_pedido("cambia el título del documento Word", REGISTRADAS) == []

    def test_registradas_desde_el_agente(self):
        class Agente:
            valid_tool_names = {"a", "b"}

        assert runner._herramientas_registradas(Agente()) == {"a", "b"}

        class SinNombres:
            valid_tool_names = None
            tools = [{"function": {"name": "c"}}, {}]

        assert runner._herramientas_registradas(SinNombres()) == {"c"}


class _AgenteConHerramientas:
    valid_tool_names = REGISTRADAS

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


def _correr(monkeypatch, tmp_path, respuestas, registradas=REGISTRADAS):
    import io
    import json
    import sys
    from types import SimpleNamespace

    agente = _AgenteConHerramientas(respuestas)
    agente.valid_tool_names = registradas
    eventos = []
    payload = {
        "task_id": "t-rechazo",
        "prompt": PEDIDO_REAL,
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
        "guardia_accion_narrada": True,
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
    resultado = [e for e in eventos if e.get("event") == "result"]
    return resultado[0], agente


NEGACION = RESPUESTA_REAL.split("\n\n", 1)[1]
NARRADA_REAL = (
    "Voy a agregar el cuarto riesgo y cambiar el título de la lámina de próximos pasos.\n\n"
    "MEDIA: uploads/134dd577-c47b-44ce-bc03-b8ea0376ae94.pptx"
)


class TestRunnerConRechazoFalso:
    def test_caso_real_narra_niega_y_al_final_usa_la_herramienta(self, monkeypatch, tmp_path):
        res, agente = _correr(
            monkeypatch,
            tmp_path,
            [(NARRADA_REAL, None), (RESPUESTA_REAL, None), ("Listo: agregué el riesgo.", "hq_pptx_editar")],
        )
        assert "Para esto tienes: hq_pptx_editar." in agente.llamadas[1]
        assert "sí tienes: hq_pptx_editar" in agente.llamadas[2]
        assert res["final_response"] == "Listo: agregué el riesgo."
        assert res["accion_narrada"]["rechazo_falso"]["resultado"] == "corregida"

    def test_si_vuelve_a_negar_se_entrega_el_aviso_honesto(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(NEGACION, None), (NEGACION, None), (NEGACION, None)])
        assert len(agente.llamadas) == 3
        assert res["final_response"] == runner.AVISO_RECHAZO_FALSO
        assert res["accion_narrada"]["rechazo_falso"] == {
            "disponibles": ["hq_pptx_editar"],
            "respuesta_descartada": NEGACION[:500],
            "resultado": "aviso_honesto",
        }

    def test_rechazo_verdadero_no_se_reintenta_por_rechazo(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(NEGACION, None), (NEGACION, None)], registradas={"skill_view"})
        assert len(agente.llamadas) == 2
        assert res["final_response"] == NEGACION
        assert "rechazo_falso" not in res.get("accion_narrada", {})

    def test_si_uso_la_herramienta_y_fallo_no_se_reintenta(self, monkeypatch, tmp_path):
        res, agente = _correr(monkeypatch, tmp_path, [(NEGACION, "hq_pptx_editar")])
        assert len(agente.llamadas) == 1
        assert "accion_narrada" not in res


class TestInstrumentacionR92:
    def test_el_runner_registra_la_actividad_del_modelo_dentro_de_una_llamada(self, monkeypatch, tmp_path):
        capturados = {}
        import sys
        from types import SimpleNamespace

        def construir(**kw):
            capturados.update(kw)
            return _AgenteConHerramientas([("Listo.", None)])

        res, _ = _correr(monkeypatch, tmp_path, [("Listo.", None)])
        monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=construir))
        registro = runner.RegistroTiempos(str(tmp_path), "t-r92")
        registro.actividad("razonamiento_chars", 10)
        registro.generando_herramienta("write_file")
        registro.estado("lifecycle", "Retrying API call (2/3)")
        eventos = [
            json.loads(linea)["evento"]
            for linea in (tmp_path / ".hermes" / "tiempos" / "t-r92.jsonl").read_text().splitlines()
        ]
        assert eventos == ["actividad", "generando_herramienta", "estado"]
        _correr_con(monkeypatch, tmp_path, construir)
        assert callable(capturados["reasoning_callback"])
        assert callable(capturados["tool_gen_callback"]) and callable(capturados["status_callback"])


def _correr_con(monkeypatch, tmp_path, constructor):
    import io
    import sys
    from types import SimpleNamespace

    payload = {
        "task_id": "t-cb",
        "prompt": "hola",
        "cwd": str(tmp_path),
        "hermes_home": str(tmp_path / ".hermes"),
        "model": "m",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=constructor))
    assert runner.main() == 0


def test_caso_real_51d6abbd_dice_que_la_herramienta_no_incluye_la_accion():
    respuesta = (
        'No puedo borrar esa línea. La herramienta `hq_editar_varios` no incluye la acción "eliminar" en su lista de '
        "opciones disponibles (solo reemplazar y agregar)."
    )
    disponibles = runner.herramientas_negadas_disponibles(
        "La línea de prueba sigue en notas.txt. Bórrala con hq_editar_varios.",
        respuesta,
        {"hq_editar_varios", "read_file"},
    )
    assert disponibles == ["hq_editar_varios"]
