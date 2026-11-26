import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_control" / "__init__.py"


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw


@pytest.fixture
def mundo(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CWD", str(tmp_path))
    spec = importlib.util.spec_from_file_location("hq_control_confirmacion", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    llamadas = []
    m._api_request = lambda metodo, ruta, payload=None: (
        llamadas.append((metodo, ruta, payload)) or json.dumps({"success": True})
    )
    ctx = _Ctx()
    m.register(ctx)
    return m, ctx.tools, llamadas


def _turno(tarea, mensaje):
    return patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})})


ARGS_BORRAR = {"user_id": "u-9"}
ARGS_CRON = {"agent_id": "a1", "name": "Resumen", "cron_expression": "0 8 * * *", "prompt": "haz el resumen"}


def _llamar(tools, nombre, args):
    return json.loads(tools[nombre]["handler"](args))


class TestConfirmacion:
    @pytest.mark.parametrize(
        ("nombre", "args"), [("hq_control_delete_user", ARGS_BORRAR), ("hq_control_create_schedule", ARGS_CRON)]
    )
    def test_la_primera_llamada_no_ejecuta_nada(self, mundo, nombre, args):
        _, tools, llamadas = mundo
        with _turno("t1", "borra al usuario u-9 / crea el cron"):
            r = _llamar(tools, nombre, args)
        assert r["requiere_confirmacion"] is True and r["parametros"] == args and llamadas == []

    def test_con_el_si_del_usuario_en_otro_mensaje_se_ejecuta_una_vez(self, mundo):
        _, tools, llamadas = mundo
        with _turno("t1", "borra al usuario u-9"):
            _llamar(tools, "hq_control_delete_user", ARGS_BORRAR)
        with _turno("t2", "sí"):
            r = _llamar(tools, "hq_control_delete_user", {**ARGS_BORRAR, "confirmado_por_el_usuario": True})
        assert r == {"success": True} and llamadas == [("DELETE", "/control/users/u-9", None)]
        with _turno("t3", "sí"):
            otra = _llamar(tools, "hq_control_delete_user", {**ARGS_BORRAR, "confirmado_por_el_usuario": True})
        assert otra.get("requiere_confirmacion") is True and len(llamadas) == 1

    def test_el_modelo_no_puede_confirmar_en_el_mismo_mensaje(self, mundo):
        _, tools, llamadas = mundo
        with _turno("t1", "borra al usuario u-9"):
            _llamar(tools, "hq_control_delete_user", ARGS_BORRAR)
            r = _llamar(tools, "hq_control_delete_user", {**ARGS_BORRAR, "confirmado_por_el_usuario": True})
        assert r["requiere_confirmacion"] is True and llamadas == []

    @pytest.mark.parametrize("respuesta", ["no", "mejor no", "sí, pero a otro usuario", "¿seguro?", "espera", "hola"])
    def test_una_respuesta_que_no_es_un_si_no_ejecuta(self, mundo, respuesta):
        _, tools, llamadas = mundo
        with _turno("t1", "borra al usuario u-9"):
            _llamar(tools, "hq_control_delete_user", ARGS_BORRAR)
        with _turno("t2", respuesta):
            r = _llamar(tools, "hq_control_delete_user", {**ARGS_BORRAR, "confirmado_por_el_usuario": True})
        assert r["requiere_confirmacion"] is True and llamadas == []

    def test_si_cambian_los_argumentos_hay_que_confirmar_de_nuevo(self, mundo):
        _, tools, llamadas = mundo
        with _turno("t1", "borra al usuario u-9"):
            _llamar(tools, "hq_control_delete_user", ARGS_BORRAR)
        with _turno("t2", "sí"):
            r = _llamar(tools, "hq_control_delete_user", {"user_id": "u-OTRO", "confirmado_por_el_usuario": True})
        assert r["requiere_confirmacion"] is True and llamadas == []

    def test_control_negativo_las_lecturas_no_piden_confirmacion(self, mundo):
        _, tools, llamadas = mundo
        with _turno("t1", "lista los usuarios"):
            _llamar(tools, "hq_control_list_users", {})
        assert llamadas == [("GET", "/control/users", None)]

    def test_el_esquema_declara_el_parametro_y_las_descripciones_lo_dicen(self, mundo):
        _, tools, _ = mundo
        esquema = tools["hq_control_create_schedule"]["schema"]
        assert "confirmado_por_el_usuario" in esquema["parameters"]["properties"]
        assert "confirmación del usuario" in esquema["description"]
        assert "confirmado_por_el_usuario" not in tools["hq_control_list_users"]["schema"]["parameters"].get(
            "properties", {}
        )
