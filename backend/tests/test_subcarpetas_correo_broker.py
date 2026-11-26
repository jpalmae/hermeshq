import importlib.util
import json
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest

from hermeshq.services import graph_proxy as gp

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("ms365_subcarpetas_broker", PLUGIN)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

POLITICA = gp.PoliticaGraph(
    correo=gp.PoliticaCorreo(operaciones=frozenset({"mail_folders", "mail_list"}), carpetas=frozenset({gp.TODO}))
)

ARBOL = {
    None: [
        {"id": "AQMkADinbox0001=", "displayName": "Bandeja de entrada", "childFolderCount": 0, "totalItemCount": 78},
        {"id": "AQMkADorden0002=", "displayName": "Orden", "childFolderCount": 2, "totalItemCount": 0},
    ],
    "AQMkADorden0002=": [
        {"id": "AQMkADcumple003=", "displayName": "Cumpleaños", "childFolderCount": 0, "totalItemCount": 3},
        {"id": "AQMkADbi0000004=", "displayName": "BI", "childFolderCount": 1, "totalItemCount": 9},
    ],
    "AQMkADbi0000004=": [{"id": "AQMkADku0000005=", "displayName": "KU", "childFolderCount": 0, "totalItemCount": 1}],
}


class GraphSimulado:
    def __init__(self):
        self.urls = []

    def __call__(self, metodo, url, token, cuerpo, tipo, limite):
        self.urls.append(url)
        ruta, _, query = url.partition("?")
        parametros = urllib.parse.parse_qs(query)
        campos = set((parametros.get("$select") or [""])[0].split(","))
        if ruta.endswith("/childFolders"):
            padre = urllib.parse.unquote(ruta.split("/mailFolders/")[1].split("/")[0])
            items = ARBOL.get(padre, [])
        else:
            items = ARBOL[None]
        top = int((parametros.get("$top") or ["10"])[0])
        valor = [{k: v for k, v in i.items() if k in campos} for i in items[:top]]
        return 200, json.dumps({"value": valor}).encode()


@pytest.fixture
def graph(monkeypatch):
    simulado = GraphSimulado()
    monkeypatch.setattr(gp, "_llamar", simulado)
    return simulado


def _broker(metodo, ruta, cuerpo_json=None, prefer=None):
    respuesta = gp.ejecutar(metodo, ruta, None, None, POLITICA, lambda: "tok", lambda t: None, prefer)
    return respuesta["json"]


class TestSubcarpetasPorElBroker:
    def test_el_primer_nivel_pide_el_conteo_de_subcarpetas(self, graph):
        _broker("GET", "/me/mailFolders?$top=100")
        assert "childFolderCount" in graph.urls[0] and "$top=100" in graph.urls[0]

    @pytest.mark.parametrize(
        ("pedida", "esperada"),
        [
            ("Orden/Cumpleaños", "AQMkADcumple003="),
            ("orden/cumpleanos", "AQMkADcumple003="),
            ("Orden/BI/KU", "AQMkADku0000005="),
            ("KU", "AQMkADku0000005="),
        ],
    )
    def test_caso_real_la_subcarpeta_de_ana_se_resuelve(self, graph, pedida, esperada):
        with patch.object(plugin, "_broker_graph", side_effect=_broker):
            carpeta, error = plugin._resolver_carpeta(pedida)
        assert error is None, error
        assert carpeta == esperada

    def test_una_carpeta_que_no_existe_sigue_fallando_con_la_lista_completa(self, graph):
        with patch.object(plugin, "_broker_graph", side_effect=_broker):
            carpeta, error = plugin._resolver_carpeta("Orden/Inexistente")
        assert carpeta is None
        assert "Orden/Cumpleaños" in error and "Orden/BI/KU" in error
