import importlib.util
import pathlib

import pytest

from hermeshq.services import kql_correo as k

RUTA = (
    pathlib.Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-mail"
    / "plugin"
    / "__init__.py"
)
spec = importlib.util.spec_from_file_location("mail_plugin_kql", RUTA)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def kql(**args):
    return plugin._build_search_kql(args)[0]


class TestConstruccionKql:
    def test_campo_con_varias_palabras_es_una_frase(self):
        assert kql(**{"from": "Juan Pérez"}) == 'from:"Juan Pérez"'

    def test_operadores_dentro_de_un_valor_son_texto_no_terminos(self):
        assert kql(**{"from": 'ana" OR subject:"x'}) == 'from:"ana OR subject: x"'
        assert k.validar(kql(**{"from": 'ana" OR subject:"x'})) == 'from:"ana OR subject: x"'
        with pytest.raises(k.KqlNoPermitido):
            k.validar('from:"ana" OR subject:"x"')
        assert k.validar(kql(**{"from": "ana OR subject x"})) == 'from:"ana OR subject x"'

    def test_consulta_libre_se_pasa_tal_cual_al_validador(self):
        assert kql(query="from:ana presupuesto") == "from:ana presupuesto"

    def test_consulta_libre_invalida_se_rechaza_en_el_broker(self):
        with pytest.raises(k.KqlNoPermitido):
            k.validar(kql(query="x OR subject:y"))

    def test_fecha_invalida_se_rechaza(self):
        assert plugin._build_search_kql({"since": "2026-02-30"})[1]

    def test_sin_criterios(self):
        assert plugin._build_search_kql({})[1]
