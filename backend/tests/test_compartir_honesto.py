import importlib.util
from pathlib import Path

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)


def _modulo():
    spec = importlib.util.spec_from_file_location("sp_compartir_honesto", PLUGIN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw


def _descripcion():
    ctx = _Ctx()
    _modulo().register(ctx)
    return ctx.tools["sharepoint_share_file"]["schema"]["description"]


def test_la_descripcion_dice_que_no_envia_nada_y_remite_a_teams_y_correo():
    d = _descripcion()
    assert "NO se lo envía" in d and "teams_send_to_person" in d and "ms365_mail_send" in d
    assert "Nunca digas «compartido con X»" in d


def test_control_negativo_la_herramienta_sigue_pidiendo_file_path():
    ctx = _Ctx()
    _modulo().register(ctx)
    assert ctx.tools["sharepoint_share_file"]["schema"]["parameters"]["required"] == ["file_path"]


def test_el_codigo_avisa_en_la_vista_previa_y_en_el_resultado():
    fuente = PLUGIN.read_text(encoding="utf-8")
    assert '"importante"' in fuente and "NO se envía a nadie" in fuente
    assert '"envio"' in fuente and "NO se envió a nadie" in fuente
