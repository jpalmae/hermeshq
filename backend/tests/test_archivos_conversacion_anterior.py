import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from hermeshq.services import archivos_conversacion as ac

PEDIDO = "En ese procedimiento, agrega en la sección de responsabilidades este punto: «…». No cambies nada más."


def _con(prompt, *, archivos=(), sin_historial=True, aviso=True, reiniciada=False):
    with (
        patch.object(ac, "archivos_generados", AsyncMock(return_value=list(archivos))),
        patch.object(ac, "sesion_sin_historial", AsyncMock(return_value=sin_historial)),
        patch.object(ac, "sesion_recien_reiniciada", AsyncMock(return_value=reiniciada)),
    ):
        return asyncio.run(
            ac.con_archivos_generados(
                None, "agente", "usuario", "sesion", "clave", prompt, aviso_conversacion_nueva=aviso
            )
        )


class TestConversacionNuevaQueAludeAAlgoAnterior(unittest.TestCase):
    def test_caso_real_la_alusion_detecta_ese_procedimiento(self):
        self.assertTrue(ac.alude_a_algo_anterior(PEDIDO))
        self.assertTrue(ac.alude_a_algo_anterior("[Hora actual: sábado] cambia la presentación que me enviaste"))
        self.assertFalse(ac.alude_a_algo_anterior("Crea un procedimiento SGSI de control de acceso"))

    def test_caso_real_sin_historial_el_agente_recibe_el_aviso_y_ningun_archivo_ajeno(self):
        prompt = _con(PEDIDO)
        self.assertTrue(prompt.startswith(ac.AVISO_CONVERSACION_NUEVA))
        self.assertIn("pregúntale cuál es", prompt)
        self.assertNotIn(ac.ENCABEZADO, prompt)

    def test_con_historial_en_la_sesion_no_se_avisa(self):
        self.assertEqual(_con(PEDIDO, sin_historial=False), PEDIDO)

    def test_un_trabajo_en_segundo_plano_no_recibe_el_aviso(self):
        self.assertEqual(_con(PEDIDO, aviso=False), PEDIDO)

    def test_sin_alusion_no_se_avisa(self):
        self.assertEqual(
            _con("Crea un procedimiento SGSI de control de acceso"), "Crea un procedimiento SGSI de control de acceso"
        )

    def test_si_la_sesion_generó_archivos_se_usa_la_nota_de_siempre(self):
        archivos = [{"ruta": "uploads/cal014.docx", "task_id": "29964e67-x", "nombre": "DOC-PRO-014"}]
        prompt = _con(PEDIDO, archivos=archivos)
        self.assertTrue(prompt.startswith(ac.ENCABEZADO))
        self.assertNotIn(ac.AVISO_CONVERSACION_NUEVA, prompt)


if __name__ == "__main__":
    unittest.main()
