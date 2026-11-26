import importlib.util
from pathlib import Path
from types import SimpleNamespace

_RUTA = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "exposicion_herramientas.py"
_spec = importlib.util.spec_from_file_location("exposicion_diferida_prueba", _RUTA)
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _def(nombre):
    return {"type": "function", "function": {"name": nombre, "description": "x", "parameters": {}}}


class _Registro:
    def __init__(self, entradas, con_definicion=True):
        self.entradas = entradas
        self.con_definicion = con_definicion

    def get_entry(self, nombre):
        toolset = self.entradas.get(nombre)
        return SimpleNamespace(toolset=toolset) if toolset else None

    def get_definitions(self, nombres, quiet=False):
        return [_def(n) for n in nombres] if self.con_definicion else []


def _agente():
    return SimpleNamespace(tools=[_def("read_file")], valid_tool_names={"read_file"})


REGISTRO = _Registro(
    {"sharepoint_search": "hermeshq_sharepoint", "read_file": "file", "ms365_mail_send": "hermeshq_ms365_mail"}
)


def test_una_herramienta_de_un_toolset_diferido_se_activa_y_queda_valida():
    agente, estado = _agente(), {"catalogo": [], "toolset_de": {}, "activadas": set()}
    assert (
        ex.activar_diferida(agente, "sharepoint_search", ["hermeshq_sharepoint"], REGISTRO, estado)
        == "sharepoint_search"
    )
    assert "sharepoint_search" in agente.valid_tool_names
    assert [ex.nombre(h) for h in agente.tools] == ["read_file", "sharepoint_search"]
    assert (
        "sharepoint_search" in estado["activadas"]
        and estado["toolset_de"]["sharepoint_search"] == "hermeshq_sharepoint"
    )
    assert [ex.nombre(h) for h in estado["catalogo"]] == ["sharepoint_search"]


def test_dos_veces_no_duplica_el_esquema():
    agente = _agente()
    ex.activar_diferida(agente, "sharepoint_search", ["hermeshq_sharepoint"], REGISTRO)
    ex.activar_diferida(agente, "sharepoint_search", ["hermeshq_sharepoint"], REGISTRO)
    assert [ex.nombre(h) for h in agente.tools].count("sharepoint_search") == 1


def test_una_herramienta_de_otro_toolset_no_se_activa():
    agente = _agente()
    assert ex.activar_diferida(agente, "ms365_mail_send", ["hermeshq_sharepoint"], REGISTRO) is None
    assert "ms365_mail_send" not in agente.valid_tool_names


def test_un_nombre_que_no_existe_sigue_sin_existir():
    agente = _agente()
    assert ex.activar_diferida(agente, "herramienta_inventada", ["hermeshq_sharepoint"], REGISTRO) is None
    assert agente.valid_tool_names == {"read_file"}


def test_sin_toolsets_diferidos_no_hace_nada():
    assert ex.activar_diferida(_agente(), "sharepoint_search", [], REGISTRO) is None
    assert ex.activar_diferida(_agente(), "", ["hermeshq_sharepoint"], REGISTRO) is None


def test_si_el_registro_no_entrega_el_esquema_no_se_activa():
    agente = _agente()
    sin = _Registro({"sharepoint_search": "hermeshq_sharepoint"}, con_definicion=False)
    assert ex.activar_diferida(agente, "sharepoint_search", ["hermeshq_sharepoint"], sin) is None
    assert "sharepoint_search" not in agente.valid_tool_names


def test_con_el_agente_y_el_registro_reales_de_hermes():
    import pytest

    run_agent = pytest.importorskip("run_agent")
    registry = pytest.importorskip("tools.registry").registry
    runner_ruta = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
    espec = importlib.util.spec_from_file_location("runner_diferida_prueba", runner_ruta)
    runner = importlib.util.module_from_spec(espec)
    espec.loader.exec_module(runner)
    registry.register(
        name="sharepoint_search_prueba_r133",
        toolset="hermeshq_sharepoint_prueba",
        schema={
            "name": "sharepoint_search_prueba_r133",
            "description": "x",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kw: "{}",
        check_fn=None,
    )
    original = run_agent.AIAgent._repair_tool_call
    try:
        runner._activar_integraciones_bajo_demanda({"toolsets_diferidos": ["hermeshq_sharepoint_prueba"]})
        agente = object.__new__(run_agent.AIAgent)
        agente.tools = [_def("read_file")]
        agente.valid_tool_names = {"read_file"}
        assert agente._repair_tool_call("sharepoint_search_prueba_r133") == "sharepoint_search_prueba_r133"
        assert "sharepoint_search_prueba_r133" in agente.valid_tool_names
        assert any(h["function"]["name"] == "sharepoint_search_prueba_r133" for h in agente.tools)
        assert agente._repair_tool_call("herramienta_que_no_existe_r133") is None
        assert runner._ACTIVADAS_BAJO_DEMANDA == ["sharepoint_search_prueba_r133"]
    finally:
        run_agent.AIAgent._repair_tool_call = original
        registry.deregister("sharepoint_search_prueba_r133")
