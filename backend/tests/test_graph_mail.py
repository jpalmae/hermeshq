import json

import pytest

from hermeshq.services import graph_proxy as gp

POL = gp.PoliticaGraph(
    correo=gp.PoliticaCorreo(
        operaciones=frozenset({"mail_folders", "mail_list", "mail_read", "mail_search"}),
        carpetas=frozenset({"inbox"}),
    )
)
SIN_CORREO = gp.PoliticaGraph(
    correo=gp.PoliticaCorreo(operaciones=frozenset({"mail_folders"}), carpetas=frozenset({"inbox"}))
)


class Red:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, metodo, url, token, cuerpo, tipo, limite):
        self.llamadas.append((metodo, url, token))
        return self.respuestas.pop(0)


@pytest.fixture
def red(monkeypatch):
    r = Red([])
    monkeypatch.setattr(gp, "_llamar", r)
    return r


def run(ruta, politica=POL, metodo="GET", prefer=None):
    return gp.ejecutar(metodo, ruta, None, None, politica, lambda: "tok-servidor-sintetico", lambda t: None, prefer)


class TestClasificacionCorreo:
    def test_listar_mensajes_de_carpeta(self):
        c = gp.clasificar("GET", "/me/mailFolders/inbox/messages?$top=10")
        assert (c.operacion, c.carpeta, c.es_correo) == ("mail_list", "inbox", True)

    def test_listar_carpetas(self):
        assert gp.clasificar("GET", "/me/mailFolders").operacion == "mail_folders"

    @pytest.mark.parametrize(
        "metodo,ruta",
        [
            ("POST", "/me/sendMail"),
            ("POST", "/me/messages/m1/send"),
            ("POST", "/me/messages/m1/forward"),
            ("DELETE", "/me/messages/m1"),
            ("PATCH", "/me/messages/m1"),
        ],
    )
    def test_escritura_de_correo_no_clasifica_como_lectura(self, metodo, ruta):
        with pytest.raises(gp.RutaNoPermitida):
            gp.clasificar(metodo, ruta)


class TestAutorizacionCorreo:
    def test_carpeta_autorizada_llama_a_graph_con_token_del_servidor(self, red):
        red.respuestas = [(200, json.dumps({"value": [{"id": "m1", "subject": "sintetico"}]}).encode())]
        r = run("/me/mailFolders/inbox/messages?$top=10")
        assert r["json"]["value"][0]["subject"] == "sintetico"
        assert red.llamadas[0][2] == "tok-servidor-sintetico"
        assert "/me/mailFolders/inbox/messages" in red.llamadas[0][1]

    def test_carpeta_no_autorizada_no_llama_a_graph(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("/me/mailFolders/sentitems/messages")
        assert e.value.motivo == "carpeta_de_correo_fuera_del_alcance"
        assert red.llamadas == []

    def test_operacion_no_listada_no_llama_a_graph(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("/me/mailFolders/inbox/messages", politica=SIN_CORREO)
        assert e.value.motivo == "operacion_de_correo_no_permitida"
        assert red.llamadas == []

    def test_sin_politica_de_correo_deniega(self, red):
        with pytest.raises(gp.RutaNoPermitida):
            run("/me/mailFolders/inbox/messages", politica=gp.PoliticaGraph())
        assert red.llamadas == []

    def test_listado_de_carpetas(self, red):
        red.respuestas = [(200, b'{"value":[{"id":"f1","displayName":"Bandeja"}]}')]
        r = run("/me/mailFolders")
        assert r["json"]["value"][0]["displayName"] == "Bandeja"

    def test_configuracion_de_correo_se_lee_de_la_politica_del_agente(self):
        p = gp.politica_desde_configuracion({"correo": {"operaciones": ["mail_list", "send"], "carpetas": ["inbox"]}})
        assert p.correo.operaciones == frozenset({"mail_list"})
        assert p.correo.carpetas == frozenset({"inbox"})


class TestParametrosDeLectura:
    def test_top_se_acota_y_filtro_permitido_se_envia(self, red):
        red.respuestas = [(200, b'{"value":[]}')]
        run("/me/mailFolders/inbox/messages?$top=999&$filter=isRead eq false")
        url = red.llamadas[0][1]
        assert "$top=50" in url and "isRead%20eq%20false" in url

    def test_filtro_arbitrario_se_deniega_sin_llamar(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            run("/me/mailFolders/inbox/messages?$filter=subject eq 'x' or true")
        assert e.value.motivo == "filtro_de_correo_no_permitido"
        assert red.llamadas == []


class TestLecturaPorId:
    MSG = "AAMkAGI_sintetico-1234567890"
    OTRA = "AAMkAGI_otra-carpeta-999999"

    def _red_mensaje(self, red, padre, existe=True):
        if not existe:
            red.respuestas = [(404, b"{}")]
            return
        resp = [
            (200, json.dumps({"parentFolderId": padre}).encode()),
            (200, json.dumps({"id": "carpeta-inbox-id"}).encode()),
        ]
        if padre == "carpeta-inbox-id":
            resp.append(
                (
                    200,
                    json.dumps(
                        {
                            "id": self.MSG,
                            "subject": "sintetico",
                            "body": {"content": "x" * 150_000},
                            "webLink": "https://outlook.example/x",
                            "internetMessageHeaders": [{"name": "X", "value": "y"}],
                        }
                    ).encode(),
                )
            )
        red.respuestas = resp

    def test_mensaje_autorizado_devuelve_contenido_acotado(self, red):
        self._red_mensaje(red, "carpeta-inbox-id")
        r = run(f"/me/messages/{self.MSG}")
        assert r["json"]["subject"] == "sintetico"
        assert len(r["json"]["body"]["content"]) == gp._MAX_CUERPO and r["json"]["body"]["truncado"] is True
        assert gp._MAX_CUERPO == 100_000
        assert "internetMessageHeaders" not in gp._CAMPOS_MENSAJE and "webLink" in gp._CAMPOS_MENSAJE

    def test_carpeta_real_prohibida_se_deniega(self, red):
        self._red_mensaje(red, "carpeta-sentitems-id")
        with pytest.raises(gp.RutaNoPermitida) as e:
            run(f"/me/messages/{self.MSG}")
        assert e.value.motivo == "mensaje_fuera_de_carpeta_autorizada"
        assert len(red.llamadas) == 2

    def test_id_valido_de_otra_carpeta_se_deniega(self, red):
        self._red_mensaje(red, "carpeta-sentitems-id")
        with pytest.raises(gp.RutaNoPermitida):
            run(f"/me/messages/{self.OTRA}")
        assert all("$select=id,subject" not in u for _, u, _ in red.llamadas)
        assert all(self.MSG not in u for _, u, _ in red.llamadas)

    def test_mensaje_inexistente_responde_como_no_autorizado(self, red):
        self._red_mensaje(red, "", existe=False)
        with pytest.raises(gp.RutaNoPermitida) as e:
            run(f"/me/messages/{self.MSG}")
        assert e.value.motivo == "mensaje_no_autorizado"

    @pytest.mark.parametrize("ident", ["../../me/mailFolders", "a%2fb%2fc", "x", "AAA'OR'1", "A" * 401])
    def test_identificador_manipulado_no_llama_a_graph(self, red, ident):
        with pytest.raises(gp.RutaNoPermitida):
            run(f"/me/messages/{ident}")
        assert red.llamadas == []

    def test_preferencia_no_permitida_se_ignora(self, red):
        self._red_mensaje(red, "carpeta-inbox-id")
        r = run(f"/me/messages/{self.MSG}", prefer='outlook.timezone="x"')
        assert r["json"]["subject"] == "sintetico"


class TestBusquedaCorreo:
    SEC = "secreto-busqueda-sintetico"
    CTX = {"usuario": "u1", "agente": "a1", "tarea": "t1"}

    def _filas(self, n, padre="carpeta-inbox-id"):
        return {"value": [{"id": f"m{i}", "subject": f"plan {i}", "parentFolderId": padre} for i in range(n)]}

    def _run(self, ruta, red_resp=None, **kw):
        base = dict(
            cuerpo_json=None,
            cuerpo_bytes=None,
            politica=POL,
            token_de=lambda: "tok-servidor-sintetico",
            unidad_propia_de=lambda t: None,
            prefer=None,
            contexto=self.CTX,
            secreto=self.SEC,
        )
        base.update(kw)
        return gp.ejecutar("GET", ruta, **base)

    def _preparar(self, red, filas):
        red.respuestas = [(200, json.dumps({"id": "carpeta-inbox-id"}).encode()), (200, json.dumps(filas).encode())]

    def test_busqueda_legitima_devuelve_items_sin_parentFolderId(self, red):
        self._preparar(red, self._filas(2))
        r = self._run('/me/mailFolders/inbox/messages?$search="plan"&$top=5')
        assert [i["subject"] for i in r["json"]["items"]] == ["plan 0", "plan 1"]
        assert all("parentFolderId" not in i for i in r["json"]["items"])
        assert r["json"]["siguiente_cursor"] is None

    def test_carpeta_prohibida_no_llama_a_graph(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            self._run('/me/mailFolders/sentitems/messages?$search="plan"')
        assert e.value.motivo == "carpeta_de_correo_fuera_del_alcance"
        assert red.llamadas == []

    @pytest.mark.parametrize(
        "q",
        [
            "?$search=plan",
            '?$search="plan" OR subject:x',
            '?$search="a"',
            '?$search="pl\'an"',
            '?$search="plan"&$filter=isRead eq false',
            '?$search="plan"&$skip=5000',
            '?$search="plan"&$select=body',
        ],
    )
    def test_consulta_o_parametros_manipulados_no_llaman_a_graph(self, red, q):
        with pytest.raises(gp.RutaNoPermitida):
            self._run("/me/mailFolders/inbox/messages" + q)
        assert red.llamadas == []

    def test_top_fuera_de_limite(self, red):
        with pytest.raises(gp.RutaNoPermitida) as e:
            self._run('/me/mailFolders/inbox/messages?$search="plan"&$top=500')
        assert e.value.motivo == "parametro_top_fuera_de_limite"

    def test_busqueda_vacia(self, red):
        self._preparar(red, {"value": []})
        r = self._run('/me/mailFolders/inbox/messages?$search="sinresultados"')
        assert r["json"] == {"items": [], "siguiente_cursor": None}

    def test_resultado_de_otra_carpeta_se_descarta(self, red):
        filas = {
            "value": [
                {"id": "ok", "subject": "a", "parentFolderId": "carpeta-inbox-id"},
                {"id": "fuera", "subject": "b", "parentFolderId": "carpeta-sentitems-id"},
            ]
        }
        self._preparar(red, filas)
        r = self._run('/me/mailFolders/inbox/messages?$search="plan"')
        assert [i["id"] for i in r["json"]["items"]] == ["ok"]

    def test_paginacion_emite_cursor_propio_y_pagina_siguiente(self, red):
        self._preparar(red, self._filas(3))
        p0 = self._run('/me/mailFolders/inbox/messages?$search="plan"&$top=2')
        assert len(p0["json"]["items"]) == 2 and p0["json"]["siguiente_cursor"]
        assert "nextLink" not in str(p0)
        self._preparar(red, self._filas(3))
        cur = p0["json"]["siguiente_cursor"]
        p1 = self._run(f'/me/mailFolders/inbox/messages?$search="plan"&$top=2&cursor={cur}')
        assert [i["subject"] for i in p1["json"]["items"]] == ["plan 2"]
        assert p1["json"]["siguiente_cursor"] is None

    def test_cursor_de_otra_consulta_se_rechaza(self, red):
        self._preparar(red, self._filas(3))
        p0 = self._run('/me/mailFolders/inbox/messages?$search="plan"&$top=2')
        n = len(red.llamadas)
        with pytest.raises(gp.RutaNoPermitida) as e:
            self._run(f'/me/mailFolders/inbox/messages?$search="otra"&$top=2&cursor={p0["json"]["siguiente_cursor"]}')
        assert e.value.motivo == "cursor_no_valido"
        assert len(red.llamadas) == n

    def test_cursor_de_otra_tarea_se_rechaza(self, red):
        self._preparar(red, self._filas(3))
        p0 = self._run('/me/mailFolders/inbox/messages?$search="plan"&$top=2')
        with pytest.raises(gp.RutaNoPermitida):
            self._run(
                f'/me/mailFolders/inbox/messages?$search="plan"&$top=2&cursor={p0["json"]["siguiente_cursor"]}',
                contexto={"usuario": "u1", "agente": "a1", "tarea": "t2"},
            )
