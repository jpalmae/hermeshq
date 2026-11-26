from datetime import UTC, datetime

from hermeshq.services.autorizacion_datos import (
    MAX_PAGINAS,
    PoliticaDatos,
    Solicitud,
    autorizar,
    cursor_para,
)

AHORA = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
SECRETO = "secreto-de-prueba-no-produccion"
POLITICA = PoliticaDatos(
    operaciones=frozenset({"list", "read", "search", "upload"}),
    carpetas_lectura=("Proyectos/Vigentes", "Compartido/Plantillas"),
    carpetas_escritura=("Salidas/HermesHQ",),
)
BASE = dict(
    usuario_id="u-prueba",
    agente_id="ag-prueba",
    tarea_id="t-1",
    sitio_id="sitio-sint",
    tarea_activa=True,
    asignado=True,
)


def decidir(**cambios):
    datos = {**BASE, **cambios}
    operacion = datos.pop("operacion", "list")
    return autorizar(Solicitud(operacion=operacion, **datos), POLITICA, SECRETO, AHORA)


class TestListadosYBusquedas:
    def test_listado_global_sin_carpeta_se_deniega(self):
        assert not decidir(carpeta=None).permitido

    def test_busqueda_global_sin_sitio_se_deniega(self):
        assert not decidir(operacion="search", sitio_id=None, carpeta="Proyectos/Vigentes").permitido

    def test_listado_dentro_de_carpeta_permitida(self):
        assert decidir(carpeta="Proyectos/Vigentes").permitido

    def test_carpeta_raiz_no_permitida(self):
        d = decidir(carpeta="")
        assert not d.permitido and d.motivo == "lectura_sin_carpeta_explicita"

    def test_carpeta_hermana_no_permitida(self):
        assert not decidir(carpeta="Proyectos/Vigentes-secreto").permitido


class TestArgumentosManipulados:
    def test_traversal_se_deniega(self):
        assert not decidir(carpeta="Proyectos/Vigentes/../../Finanzas").permitido

    def test_traversal_codificado_se_deniega(self):
        assert not decidir(carpeta="Proyectos%2f..%2fFinanzas").permitido

    def test_barra_invertida_se_deniega(self):
        assert not decidir(carpeta="Proyectos\\Finanzas").permitido

    def test_nombre_de_herramienta_o_prompt_no_amplian(self):
        d = decidir(carpeta="Finanzas", finalidad="sharepoint onedrive todo el sitio")
        assert not d.permitido


class TestPaginacion:
    def test_pagina_inicial_sin_cursor_permitida(self):
        assert decidir(carpeta="Proyectos/Vigentes", pagina=0).permitido

    def test_pagina_siguiente_sin_cursor_se_deniega(self):
        assert not decidir(carpeta="Proyectos/Vigentes", pagina=1).permitido

    def test_cursor_valido_para_la_misma_solicitud_se_acepta(self):
        c = cursor_para(SECRETO, "u-prueba", "ag-prueba", "Proyectos/Vigentes", 1, "t-1")
        assert decidir(carpeta="Proyectos/Vigentes", pagina=1, cursor=c).permitido

    def test_cursor_de_otra_carpeta_se_deniega(self):
        c = cursor_para(SECRETO, "u-prueba", "ag-prueba", "Compartido/Plantillas", 1, "t-1")
        assert not decidir(carpeta="Proyectos/Vigentes", pagina=1, cursor=c).permitido

    def test_cursor_de_otro_agente_se_deniega(self):
        c = cursor_para(SECRETO, "u-prueba", "otro-agente", "Proyectos/Vigentes", 1, "t-1")
        assert not decidir(carpeta="Proyectos/Vigentes", pagina=1, cursor=c).permitido

    def test_cursor_de_otra_tarea_se_deniega(self):
        c = cursor_para(SECRETO, "u-prueba", "ag-prueba", "Proyectos/Vigentes", 1, "t-OTRA")
        assert not decidir(carpeta="Proyectos/Vigentes", pagina=1, cursor=c).permitido

    def test_limite_de_paginas(self):
        c = cursor_para(SECRETO, "u-prueba", "ag-prueba", "Proyectos/Vigentes", MAX_PAGINAS, "t-1")
        assert not decidir(carpeta="Proyectos/Vigentes", pagina=MAX_PAGINAS, cursor=c).permitido


class TestIdentidadYTarea:
    def test_sin_tarea_activa_se_deniega(self):
        assert not decidir(carpeta="Proyectos/Vigentes", tarea_activa=False).permitido

    def test_agente_no_asignado_se_deniega(self):
        assert not decidir(carpeta="Proyectos/Vigentes", asignado=False).permitido

    def test_operacion_no_permitida_para_el_agente(self):
        assert not decidir(operacion="download", carpeta="Proyectos/Vigentes").permitido


class TestEscritura:
    def test_escritura_con_finalidad_en_carpeta_de_salida(self):
        assert decidir(operacion="upload", carpeta="Salidas/HermesHQ", finalidad="entregable_solicitado").permitido

    def test_escritura_sin_finalidad_se_deniega(self):
        assert not decidir(operacion="upload", carpeta="Salidas/HermesHQ", finalidad=None).permitido

    def test_escritura_fuera_de_salida_se_deniega(self):
        d = decidir(operacion="upload", carpeta="Proyectos/Vigentes", finalidad="entregable_solicitado")
        assert not d.permitido

    def test_registro_auditable_en_cada_decision(self):
        d = decidir(carpeta="Proyectos/Vigentes")
        assert d.registro["decision"] == "permitida" and d.registro["usuario"] == "u-prueba"
        d2 = decidir(carpeta=None)
        assert d2.registro["decision"] == "denegada" and d2.registro["motivo"]
