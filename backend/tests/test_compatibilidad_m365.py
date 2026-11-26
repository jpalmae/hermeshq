import itertools
import unittest
from types import SimpleNamespace

from hermeshq.scripts import verificar_despliegue_m365 as verificar
from hermeshq.services import compatibilidad_m365 as cm
from hermeshq.services.aprobacion_central import central_activa_para

MODOS = ("heredar", "activada", "desactivada")


def _ajustes(token, habilitada, obligatoria, declarados=cm.AJUSTES_OBLIGATORIOS):
    return SimpleNamespace(
        m365_agent_token_enabled=token,
        approval_central_enabled=habilitada,
        approval_central_enforced=obligatoria,
        model_fields_set=set(declarados),
    )


def _agente(nombre="Agente", modo="heredar", m365=True):
    toolsets = ["file", "hermeshq_ms365_mail"] if m365 else ["file"]
    return SimpleNamespace(name=nombre, approval_central_mode=modo, enabled_toolsets=toolsets)


class MatrizDeCompatibilidad(unittest.TestCase):
    def test_cada_combinacion_da_el_efecto_esperado(self):
        for token, habilitada, obligatoria, modo in itertools.product(
            (False, True), (False, True), (False, True), MODOS
        ):
            esperado_central = obligatoria or not token or modo == "activada" or (modo == "heredar" and habilitada)
            with self.subTest(token=token, habilitada=habilitada, obligatoria=obligatoria, modo=modo):
                ajustes = _ajustes(token, habilitada, obligatoria)
                agente = _agente(modo=modo)
                self.assertEqual(central_activa_para(agente, ajustes), esperado_central)
                self.assertEqual(cm.efecto(agente, ajustes)["token_m365"], token)

    def test_la_configuracion_compatible_conserva_el_comportamiento_de_produccion(self):
        compatible = _ajustes(token=True, habilitada=False, obligatoria=False)
        for nombre in ("agente-pruebas", "Agente_BrunoCastro", "Agente_AnaSoto"):
            self.assertEqual(cm.cambios_respecto_a_produccion(_agente(nombre), compatible), [], nombre)

    def test_los_valores_por_defecto_retiran_tokens_y_activan_aprobaciones(self):
        por_defecto = _ajustes(token=False, habilitada=False, obligatoria=False)
        cambios = cm.cambios_respecto_a_produccion(_agente(), por_defecto)
        self.assertEqual(len(cambios), 2)
        self.assertTrue(any("token" in c for c in cambios) and any("aprobación" in c for c in cambios))

    def test_solo_operador_2_en_central_sin_tocar_a_los_demas(self):
        ajustes = _ajustes(token=True, habilitada=False, obligatoria=False)
        self.assertTrue(central_activa_para(_agente("agente-pruebas", modo="activada"), ajustes))
        self.assertFalse(central_activa_para(_agente("Agente_BrunoCastro"), ajustes))
        self.assertEqual(cm.cambios_respecto_a_produccion(_agente("Agente_BrunoCastro"), ajustes), [])

    def test_un_agente_sin_m365_no_cambia(self):
        self.assertEqual(cm.cambios_respecto_a_produccion(_agente(m365=False), _ajustes(False, False, False)), [])


class DeclaracionExplicita(unittest.TestCase):
    def test_sin_declarar_es_ambiguo_si_hay_agentes_m365(self):
        ajustes = _ajustes(False, False, False, declarados=("approval_central_enabled",))
        resultado = cm.diagnostico(ajustes, [_agente()])
        self.assertTrue(resultado["ambiguo"])
        self.assertEqual(resultado["no_declarados"], ["m365_agent_token_enabled", "approval_central_enforced"])

    def test_sin_agentes_m365_no_hay_ambiguedad(self):
        ajustes = _ajustes(False, False, False, declarados=())
        self.assertFalse(cm.diagnostico(ajustes, [_agente(m365=False)])["ambiguo"])

    def test_declarado_no_es_ambiguo_aunque_haya_cambios(self):
        resultado = cm.diagnostico(_ajustes(False, False, False), [_agente("Agente_BrunoCastro")])
        self.assertFalse(resultado["ambiguo"])
        self.assertEqual(resultado["agentes_con_cambios"], ["Agente_BrunoCastro"])


class VerificacionDeDespliegue(unittest.TestCase):
    def test_falla_con_codigo_2_si_es_ambiguo(self):
        self.assertEqual(verificar.main([_agente()], _ajustes(False, False, False, declarados=())), 2)

    def test_pasa_con_codigo_0_si_esta_declarado(self):
        self.assertEqual(verificar.main([_agente()], _ajustes(True, False, False)), 0)


if __name__ == "__main__":
    unittest.main()
