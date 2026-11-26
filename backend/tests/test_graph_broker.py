from datetime import UTC, datetime

import pytest

from hermeshq.services.autorizacion_datos import PoliticaDatos
from hermeshq.services.graph_broker import AccesoDenegado, GraphBroker, ProveedorGraphSimulado, SolicitudBroker

AHORA = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
SECRETO = "secreto-broker-prueba"
SITIO = "sitio-sintetico-1"
ARCHIVOS = {
    (SITIO, "Proyectos/Vigentes"): [{"id": f"f{i}", "nombre": f"doc-{i}.txt", "tipo": "file"} for i in range(45)],
    (SITIO, "Finanzas/Privado"): [{"id": "x", "nombre": "nomina-sintetica.xlsx", "tipo": "file"}],
}
POLITICAS = {
    "ag-a": PoliticaDatos(
        operaciones=frozenset({"list", "read", "upload"}),
        carpetas_lectura=("Proyectos/Vigentes",),
        carpetas_escritura=("Salidas/HermesHQ",),
    )
}


def montar(tareas_activas=("t-1",), asignados=("ag-a",)):
    proveedor = ProveedorGraphSimulado(ARCHIVOS)
    fabricas = []

    async def fabrica(usuario_id):
        fabricas.append(usuario_id)
        return proveedor

    auditoria = []
    broker = GraphBroker(
        fabrica,
        POLITICAS,
        SECRETO,
        tarea_activa=lambda u, a, t: t in tareas_activas,
        asignado=lambda u, a: a in asignados,
        auditoria=auditoria,
    )
    return broker, proveedor, auditoria, fabricas


def sol(**kw):
    base = dict(
        usuario_id="u-a",
        agente_id="ag-a",
        tarea_id="t-1",
        operacion="list",
        sitio_id=SITIO,
        carpeta="Proyectos/Vigentes",
    )
    base.update(kw)
    return SolicitudBroker(**base)


class TestCaminoAutorizado:
    async def test_listado_permitido_devuelve_items_sin_credenciales(self):
        broker, prov, aud, fab = montar()
        r = await broker.ejecutar(sol(), AHORA)
        assert len(r["items"]) == 20
        assert prov.llamadas == ["listar:sitio-sintetico-1:Proyectos/Vigentes:0"]
        assert aud[-1]["decision"] == "permitida"
        assert set(r) == {"items", "siguiente_cursor"}

    async def test_paginacion_con_cursor_de_la_misma_tarea(self):
        broker, prov, _, _ = montar()
        p0 = await broker.ejecutar(sol(), AHORA)
        p1 = await broker.ejecutar(sol(pagina=1, cursor=p0["siguiente_cursor"]), AHORA)
        assert p1["items"][0]["id"] == "f20"

    async def test_escritura_con_finalidad_en_salida(self):
        broker, prov, _, _ = montar()
        await broker.ejecutar(
            sol(
                operacion="upload",
                carpeta="Salidas/HermesHQ",
                finalidad="entregable_solicitado",
                nombre="resumen.txt",
                contenido=b"sintetico",
            ),
            AHORA,
        )
        assert prov.escrituras == [f"subir:{SITIO}:Salidas/HermesHQ:resumen.txt:9"]


class TestDenegacionAntesDelProveedor:
    @pytest.mark.parametrize(
        "cambios,esperado",
        [
            ({"carpeta": None}, "lectura_sin_carpeta_explicita"),
            ({"sitio_id": None}, "listado_o_busqueda_global_sin_sitio"),
            ({"carpeta": "Finanzas/Privado"}, "carpeta_fuera_del_alcance_permitido"),
            ({"carpeta": "Proyectos/Vigentes/../../Finanzas/Privado"}, "ruta_con_segmentos_prohibidos"),
            ({"carpeta": "Proyectos%2f..%2fFinanzas"}, "ruta_con_segmentos_prohibidos"),
            ({"tarea_id": "t-otra"}, "sin_tarea_activa_del_usuario"),
            ({"operacion": "download"}, "operacion_no_permitida_para_el_agente"),
            ({"operacion": "upload", "carpeta": "Salidas/HermesHQ"}, "escritura_sin_finalidad_declarada"),
            (
                {"operacion": "upload", "carpeta": "Proyectos/Vigentes", "finalidad": "entregable_solicitado"},
                "escritura_fuera_de_carpeta_de_salida",
            ),
        ],
    )
    async def test_denegado_sin_fabricar_ni_llamar_al_proveedor(self, cambios, esperado):
        broker, prov, aud, fab = montar()
        with pytest.raises(AccesoDenegado) as e:
            await broker.ejecutar(sol(**cambios), AHORA)
        assert e.value.motivo == esperado
        assert prov.llamadas == [] and prov.escrituras == []
        assert fab == []
        assert aud[-1]["decision"] == "denegada"

    async def test_agente_no_asignado_queda_denegado(self):
        broker, prov, aud, _ = montar(asignados=())
        with pytest.raises(AccesoDenegado) as e:
            await broker.ejecutar(sol(), AHORA)
        assert e.value.motivo == "agente_no_asignado_al_usuario"
        assert prov.llamadas == []

    async def test_agente_sin_politica_queda_denegado_por_defecto(self):
        broker, prov, _, _ = montar()
        with pytest.raises(AccesoDenegado):
            await broker.ejecutar(sol(agente_id="ag-sin-politica"), AHORA)
        assert prov.llamadas == []


class TestCursoresYBypass:
    async def test_cursor_de_otra_tarea_no_sirve(self):
        broker, prov, _, _ = montar(tareas_activas=("t-1", "t-2"))
        p0 = await broker.ejecutar(sol(), AHORA)
        n = len(prov.llamadas)
        with pytest.raises(AccesoDenegado):
            await broker.ejecutar(sol(tarea_id="t-2", pagina=1, cursor=p0["siguiente_cursor"]), AHORA)
        assert len(prov.llamadas) == n

    async def test_proveedor_no_responde_sin_capacidad_del_broker(self):
        _, prov, _, _ = montar()
        with pytest.raises(PermissionError):
            await prov.listar(None, SITIO, "Proyectos/Vigentes", 0, 20)
        with pytest.raises(PermissionError):
            await prov.listar("otra", SITIO, "Proyectos/Vigentes", 0, 20)

    async def test_superficie_publica_del_broker(self):
        broker, _, _, _ = montar()
        publicos = [a for a in dir(broker) if not a.startswith("_")]
        assert publicos == ["ejecutar"]

    async def test_cada_decision_queda_registrada(self):
        broker, _, aud, _ = montar()
        await broker.ejecutar(sol(), AHORA)
        with pytest.raises(AccesoDenegado):
            await broker.ejecutar(sol(carpeta=None), AHORA)
        assert [a["decision"] for a in aud] == ["permitida", "denegada"]
        assert all(a["usuario"] == "u-a" and a["tarea"] == "t-1" for a in aud)


class TestPoliticaDesdeConfiguracion:
    def test_configuracion_malformada_da_politica_vacia(self):
        from hermeshq.services.graph_broker import politica_desde_configuracion

        for cfg in (None, "x", {}, {"operaciones": "list"}, {"operaciones": ["list"], "carpetas_lectura": "x"}):
            p = politica_desde_configuracion(cfg)
            assert p.operaciones == frozenset() or p.carpetas_lectura == ()

    def test_operaciones_desconocidas_se_descartan(self):
        from hermeshq.services.graph_broker import politica_desde_configuracion

        p = politica_desde_configuracion({"operaciones": ["list", "delete_all"], "carpetas_lectura": ["A/B"]})
        assert p.operaciones == frozenset({"list"})
        assert p.carpetas_lectura == ("A/B",)
