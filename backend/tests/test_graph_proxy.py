import json

import pytest

from hermeshq.services import graph_proxy as gp

UNIDAD = "drv-sintetico-1"
POL = gp.PoliticaGraph(
    unidades=frozenset({UNIDAD}),
    carpetas_lectura=("Proyectos/Vigentes",),
    carpetas_escritura=("Salidas/HermesHQ",),
    sitios=frozenset({"sit-sintetico"}),
    url_sitio=frozenset({"https://contoso.example.invalid/sites/Equipo"}),
    operaciones=frozenset({"list", "read", "download", "search", "upload"}),
)


class Red:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, metodo, url, token, cuerpo, tipo, limite):
        self.llamadas.append((metodo, url))
        return self.respuestas.pop(0)


@pytest.fixture
def red(monkeypatch):
    r = Red([])
    monkeypatch.setattr(gp, "_llamar", r)
    return r


def run(metodo, ruta, **kw):
    base = dict(
        cuerpo_json=None,
        cuerpo_bytes=None,
        politica=POL,
        token_de=lambda: "tok-sintetico",
        unidad_propia_de=lambda t: UNIDAD,
    )
    base.update(kw)
    return gp.ejecutar(metodo, ruta, **base)


class TestClasificacion:
    def test_listado_de_carpeta(self):
        c = gp.clasificar("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes:/children")
        assert (c.operacion, c.unidad, c.carpeta) == ("list", UNIDAD, "Proyectos/Vigentes")

    def test_me_drive_se_marca_como_propio(self):
        c = gp.clasificar("GET", "/me/drive/root:/Proyectos/Vigentes:/children")
        assert c.es_me and c.operacion == "list"

    @pytest.mark.parametrize(
        "ruta", ["/search/query", f"/drives/{UNIDAD}/items/i1/createLink", "/me/drive/items/i1/createLink"]
    )
    def test_busqueda_global_y_compartir_no_existen(self, ruta):
        with pytest.raises(gp.RutaNoPermitida):
            gp.clasificar("POST", ruta)

    @pytest.mark.parametrize(
        "ruta",
        [
            f"/drives/{UNIDAD}/root:/Proyectos/../Finanzas:/children",
            f"/drives/{UNIDAD}/root:/Proyectos%2f..%2fFinanzas:/children",
            f"/drives/{UNIDAD}/root:/A\\B:/children",
        ],
    )
    def test_traversal_y_codificaciones(self, ruta):
        with pytest.raises(gp.RutaNoPermitida):
            gp.clasificar("GET", ruta)

    def test_ruta_desconocida(self):
        with pytest.raises(gp.RutaNoPermitida):
            gp.clasificar("GET", "/users/x/mailFolders")

    def test_escritura_por_get_no_existe(self):
        with pytest.raises(gp.RutaNoPermitida):
            gp.clasificar("DELETE", f"/drives/{UNIDAD}/items/i1")


class TestDenegacionAntesDeGraph:
    @pytest.mark.parametrize(
        "metodo,ruta,motivo",
        [
            ("GET", f"/drives/{UNIDAD}/root/children", "lectura_sin_carpeta_explicita"),
            ("GET", f"/drives/{UNIDAD}/root:/Finanzas:/children", "carpeta_fuera_del_alcance_permitido"),
            ("GET", "/drives/otra-unidad/root:/Proyectos/Vigentes:/children", "unidad_fuera_de_alcance"),
            ("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes-x:/children", "carpeta_fuera_del_alcance_permitido"),
            (
                "PUT",
                f"/drives/{UNIDAD}/root:/Proyectos/Vigentes/a.txt:/content",
                "escritura_fuera_de_carpeta_de_salida",
            ),
            ("POST", f"/drives/{UNIDAD}/root:/Salidas/HermesHQ/a.txt:/createUploadSession", None),
        ],
    )
    def test_no_llama_a_graph(self, red, metodo, ruta, motivo):
        if motivo is None:
            return
        with pytest.raises(gp.RutaNoPermitida) as e:
            run(metodo, ruta, cuerpo_bytes=b"x")
        assert e.value.motivo == motivo
        assert red.llamadas == []

    def test_politica_vacia_deniega_todo(self, red):
        with pytest.raises(gp.RutaNoPermitida):
            run("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes:/children", politica=gp.PoliticaGraph())
        assert red.llamadas == []

    def test_item_fuera_de_carpeta_permitida_se_deniega_tras_resolver(self, red):
        red.respuestas = [
            (
                200,
                json.dumps(
                    {"name": "n.xlsx", "parentReference": {"path": "/drives/x/root:/Finanzas", "driveId": UNIDAD}}
                ).encode(),
            )
        ]
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("GET", f"/drives/{UNIDAD}/items/i1/content")
        assert e.value.motivo == "carpeta_fuera_del_alcance_permitido"
        assert len(red.llamadas) == 1


class TestCaminoAutorizado:
    def test_listado_permitido(self, red):
        red.respuestas = [(200, json.dumps({"value": [{"id": "1", "name": "a.txt"}]}).encode())]
        r = run("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes:/children")
        assert r["json"]["value"][0]["name"] == "a.txt"
        assert red.llamadas[0][1].startswith("https://graph.microsoft.com/v1.0/drives/")

    def test_busqueda_filtrada_a_carpetas_permitidas(self, red):
        cuerpo = {
            "value": [
                {"name": "ok.txt", "parentReference": {"path": "/drives/x/root:/Proyectos/Vigentes"}},
                {"name": "no.txt", "parentReference": {"path": "/drives/x/root:/Finanzas"}},
            ]
        }
        red.respuestas = [(200, json.dumps(cuerpo).encode())]
        r = run("GET", f"/drives/{UNIDAD}/root/search(q='plan')")
        assert [v["name"] for v in r["json"]["value"]] == ["ok.txt"]

    def test_descarga_devuelve_bytes_codificados(self, red):
        red.respuestas = [(200, b"contenido-sintetico")]
        r = run("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes/a.txt:/content")
        import base64

        assert base64.b64decode(r["base64"]) == b"contenido-sintetico"

    def test_subida_en_salida_con_contenido(self, red):
        red.respuestas = [(201, b'{"id":"nuevo"}')]
        r = run("PUT", f"/drives/{UNIDAD}/root:/Salidas/HermesHQ/r.txt:/content", cuerpo_bytes=b"abc")
        assert r["json"]["id"] == "nuevo"

    def test_me_drive_se_resuelve_a_la_unidad_propia_permitida(self, red):
        red.respuestas = [(200, b'{"value":[]}')]
        run("GET", "/me/drive/root:/Proyectos/Vigentes:/children")
        assert f"/drives/{UNIDAD}/" in red.llamadas[0][1]

    def test_respuesta_excesiva_se_corta(self, red, monkeypatch):
        monkeypatch.setattr(gp, "MAX_BYTES_DESCARGA", 4)
        import hermeshq.services.graph_proxy as mod

        monkeypatch.setattr(gp, "_llamar", mod._llamar_real)

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"x" * n

        monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout: Resp())
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("GET", f"/drives/{UNIDAD}/root:/Proyectos/Vigentes/a.txt:/content")
        assert e.value.motivo == "respuesta_supera_el_limite"


class TestRutasDelPlugin:
    def test_me_drives_filtrado_a_unidades_permitidas(self, red):
        red.respuestas = [(200, json.dumps({"value": [{"id": UNIDAD}, {"id": "otra"}]}).encode())]
        r = run("GET", "/me/drives")
        assert [u["id"] for u in r["json"]["value"]] == [UNIDAD]

    def test_sitio_fuera_de_alcance_no_llama_a_graph(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("GET", "/sites/otro.example.invalid:/sites/Otro:/drive/root:/Proyectos/Vigentes:/children")
        assert e.value.motivo == "sitio_fuera_de_alcance"
        assert red.llamadas == []

    def test_sitio_permitido_resuelve_unidad_y_lista(self, red):
        red.respuestas = [
            (200, json.dumps({"id": "sit-1"}).encode()),
            (200, json.dumps({"id": UNIDAD}).encode()),
            (200, json.dumps({"value": [{"id": "a"}]}).encode()),
        ]
        r = run("GET", "/sites/contoso.example.invalid:/sites/Equipo:/drive/root:/Proyectos/Vigentes:/children")
        assert r["json"]["value"][0]["id"] == "a"
        assert red.llamadas[-1][1].endswith(f"/drives/{UNIDAD}/root:/Proyectos/Vigentes:/children")

    def test_busqueda_con_comillas_escapadas_se_clasifica(self):
        c = gp.clasificar("GET", f"/drives/{UNIDAD}/root/search(q='juan''s plan')")
        assert c.operacion == "search"

    def test_busqueda_global_sigue_denegada(self, red):
        with pytest.raises(gp.RutaNoPermitida):
            run("POST", "/search/query", cuerpo_json={})
        assert red.llamadas == []


class TestRutaDeSitioConItem:
    def test_lectura_de_archivo_por_ruta_de_sitio(self):
        c = gp.clasificar("GET", "/sites/contoso.example.invalid:/sites/Equipo:/drive/root:/Proyectos/Vigentes/a.txt")
        assert (c.operacion, c.carpeta, c.es_sitio_ruta) == ("read", "Proyectos/Vigentes/a.txt", True)

    def test_listado_de_carpeta_por_ruta_de_sitio(self):
        c = gp.clasificar(
            "GET", "/sites/contoso.example.invalid:/sites/Equipo:/drive/root:/Proyectos/Vigentes:/children"
        )
        assert (c.operacion, c.carpeta) == ("list", "Proyectos/Vigentes")

    def test_descarga_por_ruta_de_sitio(self):
        c = gp.clasificar("GET", "/sites/contoso.example.invalid:/sites/Equipo:/drive/root:/Proyectos/a.txt:/content")
        assert (c.operacion, c.carpeta) == ("download", "Proyectos/a.txt")


class TestFormasDeRutaDrive:
    @pytest.mark.parametrize(
        "ruta,op,carpeta",
        [
            (f"/drives/{UNIDAD}/root:/Proyectos/Vigentes/a.txt", "read", "Proyectos/Vigentes/a.txt"),
            (f"/drives/{UNIDAD}/root:/Proyectos/Vigentes:/children", "list", "Proyectos/Vigentes"),
            (f"/drives/{UNIDAD}/root:/Proyectos/a.txt:/content", "download", "Proyectos/a.txt"),
            (f"/drives/{UNIDAD}/root/children", "list", ""),
        ],
    )
    def test_formas(self, ruta, op, carpeta):
        c = gp.clasificar("GET", ruta)
        assert (c.operacion, c.carpeta) == (op, carpeta)
