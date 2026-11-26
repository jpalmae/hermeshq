import unittest
from types import SimpleNamespace

from hermeshq.services import ejecucion_aprobada as ej


def _aprob(**campos):
    base = {
        "tipo_accion": "graph.calendario",
        "estado": "ejecutada",
        "envio_estado": "confirmada",
        "resultado_json": {},
    }
    return SimpleNamespace(**{**base, **campos})


class TestAvisoParaAgente(unittest.TestCase):
    def test_ejecutada_informa_que_si_se_ejecuto_con_el_asunto(self):
        aviso = ej.aviso_para_agente(_aprob(resultado_json={"respuesta": {"subject": "Revisión", "id": "evt-1"}}))
        self.assertIn(ej.MARCA_EJECUTADA, aviso)
        self.assertIn("Revisión", aviso)
        self.assertIn("No la repitas", aviso)

    def test_fallida_informa_el_motivo_y_exige_aprobacion_nueva(self):
        aviso = ej.aviso_para_agente(
            _aprob(
                estado="fallida",
                envio_estado="fallida",
                resultado_json={"paso": "preparacion", "motivo": "chat_no_creado"},
            )
        )
        self.assertIn("NO se ejecutó", aviso)
        self.assertIn("chat_no_creado", aviso)
        self.assertIn("aprobación nueva", aviso)
        self.assertNotIn(ej.MARCA_EJECUTADA, aviso)

    def test_incierta_no_afirma_exito_ni_repite(self):
        aviso = ej.aviso_para_agente(_aprob(estado="ejecutando", envio_estado="desconocida", resultado_json={}))
        self.assertIn("incierto", aviso)
        self.assertNotIn(ej.MARCA_EJECUTADA, aviso)

    def test_correo_y_aprobaciones_pendientes_no_generan_aviso(self):
        self.assertIsNone(ej.aviso_para_agente(_aprob(tipo_accion="correo.enviar", estado="ejecutada")))
        self.assertIsNone(ej.aviso_para_agente(_aprob(estado="pendiente", envio_estado="sin_envio")))
        self.assertIsNone(ej.aviso_para_agente(None))
