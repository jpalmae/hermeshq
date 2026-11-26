import re
import unittest
from pathlib import Path
from unittest import mock

from hermeshq.services import graph_proxy

OFICIAL = "https://graph.microsoft.com/v1.0"
SIMULADO = "http://127.0.0.1:9999/v1"
PAQUETES = Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages"
PLUGINS_GRAPH = ("ms365-mail", "ms365-calendar", "ms365-teams", "sharepoint")


class EndpointGraphDelBackend(unittest.TestCase):
    def _base(self, **entorno):
        with mock.patch.dict("os.environ", entorno, clear=True):
            return graph_proxy._graph_base()

    def test_sin_configuracion_usa_el_endpoint_oficial(self):
        self.assertEqual(self._base(), OFICIAL)

    def test_produccion_ignora_la_base_simulada(self):
        self.assertEqual(self._base(HERMESHQ_GRAPH_BASE_SIMULADO=SIMULADO, HERMESHQ_GRAPH_TOKEN_SIMULADO="t"), OFICIAL)
        self.assertEqual(
            self._base(
                HERMESHQ_ENTORNO="produccion", HERMESHQ_GRAPH_BASE_SIMULADO=SIMULADO, HERMESHQ_GRAPH_TOKEN_SIMULADO="t"
            ),
            OFICIAL,
        )

    def test_aislado_sin_token_simulado_no_desvia_tokens_reales(self):
        self.assertEqual(self._base(HERMESHQ_ENTORNO="aislado", HERMESHQ_GRAPH_BASE_SIMULADO=SIMULADO), OFICIAL)

    def test_aislado_con_base_y_token_simulados_usa_el_simulador(self):
        base = self._base(
            HERMESHQ_ENTORNO="aislado", HERMESHQ_GRAPH_BASE_SIMULADO=SIMULADO + "/", HERMESHQ_GRAPH_TOKEN_SIMULADO="t"
        )
        self.assertEqual(base, SIMULADO)


class PluginsUsanSoloElEndpointOficial(unittest.TestCase):
    def test_cada_plugin_fija_el_endpoint_oficial_sin_override(self):
        for nombre in PLUGINS_GRAPH:
            codigo = (PAQUETES / nombre / "plugin" / "__init__.py").read_text(encoding="utf-8")
            asignaciones = re.findall(r"^GRAPH_BASE\s*=\s*(.+)$", codigo, re.MULTILINE)
            self.assertEqual(asignaciones, [f'"{OFICIAL}"'], nombre)
            self.assertNotIn("GRAPH_BASE_SIMULADO", codigo, nombre)


RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
ENDPOINT_OFICIAL_FIJO = {
    "services/graph_proxy.py",
    "services/graph_broker.py",
    "services/public_chat_service.py",
    "services/integration_oauth_checks.py",
    "services/correo_central.py",
}


class InventarioDeEndpoints(unittest.TestCase):
    def test_ningun_archivo_nuevo_fija_el_endpoint_de_graph_sin_decision_explicita(self):
        encontrados = set()
        for ruta in RAIZ.rglob("*.py"):
            relativa = ruta.relative_to(RAIZ).as_posix()
            if relativa.startswith(("integration_packages/", "alembic/versions/")):
                continue
            if "graph.microsoft.com" in ruta.read_text(encoding="utf-8", errors="ignore"):
                encontrados.add(relativa)
        self.assertEqual(encontrados - ENDPOINT_OFICIAL_FIJO, set())


class ProveedorSimuladoDelBroker(unittest.TestCase):
    def _ruta(self, **entorno):
        from hermeshq.routers.m365_broker import ruta_graph_simulado

        with mock.patch.dict("os.environ", entorno, clear=True):
            return ruta_graph_simulado()

    def test_fuera_del_aislado_se_ignora(self):
        self.assertEqual(self._ruta(HERMESHQ_GRAPH_SIMULADO_JSON="/tmp/falso.json"), "")
        self.assertEqual(self._ruta(HERMESHQ_GRAPH_SIMULADO_JSON="/tmp/falso.json", HERMESHQ_ENTORNO="produccion"), "")

    def test_en_el_aislado_se_usa(self):
        self.assertEqual(
            self._ruta(HERMESHQ_GRAPH_SIMULADO_JSON="/tmp/falso.json", HERMESHQ_ENTORNO="aislado"), "/tmp/falso.json"
        )


if __name__ == "__main__":
    unittest.main()
