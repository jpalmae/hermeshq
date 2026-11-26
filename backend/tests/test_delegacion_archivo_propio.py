import importlib.util
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
_spec = importlib.util.spec_from_file_location("documentos_delegacion", _PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

GOAL_REAL = (
    "Analiza el archivo Word DOC-MAR-01 - Marco Demo.docx (ubicado "
    "en sharepoint_downloads/be34a4be_DOC-MAR-01 - Marco Demo.docx) "
    "y compara su contenido contra la plantilla oficial de Marco"
)


def test_caso_real_delegar_un_archivo_del_espacio_se_bloquea():
    motivo = plugin._motivo_bloqueo("delegate_task", {"goal": GOAL_REAL})
    assert motivo and "hq_leer_documento" in motivo


def test_delegar_una_busqueda_web_sigue_permitido():
    goal = "Fetch https://www.csiirt.gob.cl and report the full text content, especially any alert."
    assert plugin._motivo_bloqueo("delegate_task", {"goal": goal, "toolsets": ["web"]}) is None


def test_delegar_sin_archivo_sigue_permitido():
    assert plugin._motivo_bloqueo("delegate_task", {"goal": "Busca un Excel llamado Ventas 2026 en SharePoint"}) is None


def test_la_regla_pide_no_nombrar_subagentes_ni_herramientas():
    from hermeshq.services.background_style import REGLA_HERRAMIENTAS

    assert "no menciones subagentes" in REGLA_HERRAMIENTAS
