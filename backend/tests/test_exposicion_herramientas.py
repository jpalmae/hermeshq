import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from hermeshq.services import toolsets_pedido as tp

_RUTA = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "exposicion_herramientas.py"
_spec = importlib.util.spec_from_file_location("exposicion_test", _RUTA)
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)


def _h(nombre, descripcion=""):
    return {"type": "function", "function": {"name": nombre, "description": descripcion, "parameters": {}}}


CATALOGO = [
    _h("read_file", "Read a file"),
    _h("search_files", "Search files"),
    _h("write_file", "Write a file"),
    _h("patch", "Patch"),
    _h("todo", "Todo list"),
    _h("skill_view", "View skill"),
    _h("skills_list", "List skills"),
    _h("execute_code", "Run a Python script that can call Hermes tools programmatically"),
    _h("hq_leer_documento", "Lee archivos Excel, CSV, Word, PowerPoint y PDF ya descargados"),
    _h("hq_pptx_generate", "Genera una presentación PowerPoint .pptx"),
    _h("hq_docx_generate", "Genera un documento Word .docx"),
    _h("ms365_mail_send", "Envía un correo desde el buzón Microsoft 365"),
    _h("sharepoint_search", "Busca archivos en SharePoint y OneDrive"),
    _h("vision_analyze", "Analyze an image"),
    _h("hq_pptx_editar", "Edita láminas existentes: textos, títulos y notas"),
    _h("hq_docx_editar", "Edita un Word existente sin rehacerlo"),
]
TOOLSET_DE = {
    "read_file": "file",
    "search_files": "file",
    "write_file": "file",
    "patch": "file",
    "todo": "todo",
    "skill_view": "skills",
    "skills_list": "skills",
    "execute_code": "code_execution",
    "hq_leer_documento": "hermeshq_documentos",
    "hq_pptx_generate": "hermeshq_pptx",
    "hq_docx_generate": "hermeshq_docx",
    "ms365_mail_send": "hermeshq_ms365_mail",
    "sharepoint_search": "hermeshq_sharepoint",
    "vision_analyze": "vision",
    "hq_pptx_editar": "hermeshq_pptx",
    "hq_docx_editar": "hermeshq_documentos",
}


class TestExpuestas:
    def test_sin_senal_solo_viaja_el_nucleo_y_la_metaherramienta(self):
        visibles = ex.expuestas(CATALOGO, toolset_de=TOOLSET_DE, toolsets_pedido=[])
        assert visibles == {
            "read_file",
            "search_files",
            "write_file",
            "patch",
            "todo",
            "skill_view",
            "skills_list",
            ex.META,
        }

    def test_lo_pedido_lo_usado_y_lo_activado_viajan(self):
        visibles = ex.expuestas(
            CATALOGO,
            toolset_de=TOOLSET_DE,
            toolsets_pedido=["hermeshq_documentos"],
            usadas={"sharepoint_search"},
            activadas={"hq_pptx_generate"},
            herramientas_pedido=["ms365_mail_send", "no_existe"],
        )
        assert {"hq_leer_documento", "sharepoint_search", "hq_pptx_generate", "ms365_mail_send"} <= visibles
        assert "execute_code" not in visibles and "no_existe" not in visibles

    def test_sin_configuracion_de_pedido_se_expone_todo(self):
        assert ex.expuestas(CATALOGO, toolset_de=TOOLSET_DE, toolsets_pedido=None) >= {ex.nombre(h) for h in CATALOGO}

    def test_filtrar_conserva_el_orden_y_agrega_la_meta(self):
        salida = ex.filtrar(CATALOGO, {"todo", "read_file", ex.META})
        assert [ex.nombre(h) for h in salida] == ["read_file", "todo", ex.META]

    def test_usadas_en_historial(self):
        historial = [
            {"role": "user", "content": "resume el excel"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "hq_leer_documento", "arguments": "{}"}}]},
            {"role": "tool", "name": "hq_leer_documento", "content": "{}"},
        ]
        assert ex.usadas_en_historial(historial) == {"hq_leer_documento"}


class TestMetaherramienta:
    def test_buscar_y_activar(self):
        activadas = set()
        r = json.loads(
            ex.responder_meta({"buscar": "hacer una presentación pptx"}, CATALOGO, activadas, toolset_de=TOOLSET_DE)
        )
        assert r["success"] and r["encontradas"][0]["nombre"] == "hq_pptx_generate"
        assert "hq_pptx_generate" in activadas
        visibles = ex.expuestas(CATALOGO, toolset_de=TOOLSET_DE, toolsets_pedido=[], activadas=activadas)
        assert "hq_pptx_generate" in visibles

    def test_activar_por_nombre_e_inexistente(self):
        activadas = set()
        r = json.loads(ex.responder_meta({"activar": ["execute_code", "inventada"]}, CATALOGO, activadas))
        assert r["activadas"] == ["execute_code"] and r["no_existen"] == ["inventada"]

    def test_sin_resultados_lo_dice(self):
        r = json.loads(ex.responder_meta({"buscar": "zzz qqq"}, CATALOGO, set()))
        assert r["success"] is False and "No encontré" in r["error"]


EVALUACION = [
    ("[Archivos adjuntos: ventas.xlsx] resume este Excel por zona", "hq_leer_documento"),
    ("hazme una presentación de 5 láminas sobre el proyecto", "hq_pptx_generate"),
    ("redacta un procedimiento en Word", "hq_docx_generate"),
    ("envía un correo a Juan con el informe", "ms365_mail_send"),
    ("busca el archivo del contrato en SharePoint", "sharepoint_search"),
    ("¿qué hay en esta captura de pantalla? .png", "vision_analyze"),
    ("calcula la desviación estándar con un script de python", "execute_code"),
    ("modifica la presentación", "hq_pptx_editar"),
    ("la lámina 3 está en blanco", "hq_pptx_editar"),
    ("edita este Word", "hq_docx_editar"),
    (
        "[Archivos adjuntos, ya disponibles en tu workspace:\n- acta.docx (20 KB) -> uploads/acta.docx]\n"
        "corrige el título del documento",
        "hq_docx_editar",
    ),
    ("cámbiale el título a la diapositiva 2 del deck", "hq_pptx_editar"),
]


def test_saludo_no_carga_integraciones_de_datos_ni_herramientas_de_producto():
    tarea = SimpleNamespace(prompt="hola, ¿cómo estás?", metadata_json={"source": "public_chat"})
    habilitados, pedidos = tp.seleccion_progresiva(tarea, sorted(set(TOOLSET_DE.values())))
    assert pedidos == []
    assert "hermeshq_ms365_mail" not in habilitados and "hermeshq_sharepoint" not in habilitados
    assert "hermeshq_pptx" in habilitados, "las de producto quedan habilitadas para activarse a demanda"


def test_bandera_por_agente():
    agente = SimpleNamespace(id="id-1", name="agente-pruebas", slug="agente-pruebas")
    assert not tp.exposicion_activa(agente, SimpleNamespace(tool_exposure_progressive_agents=""))
    assert tp.exposicion_activa(agente, SimpleNamespace(tool_exposure_progressive_agents="agente-pruebas"))
    assert not tp.exposicion_activa(agente, SimpleNamespace(tool_exposure_progressive_agents="Agente_BrunoCastro"))
    assert tp.exposicion_activa(agente, SimpleNamespace(tool_exposure_progressive_agents="*"))


def test_un_trabajo_en_segundo_plano_lleva_execute_code_desde_el_inicio():
    tarea = SimpleNamespace(
        prompt="cruza las hojas y arma el resultado", metadata_json={"source": "background_job", "background_job": True}
    )
    _, completos, _ = tp.seleccion_progresiva_detallada(tarea, ["file", "code_execution", "hermeshq_documentos"])
    assert "code_execution" in completos
