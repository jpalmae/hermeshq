import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from hermeshq.services import archivos_conversacion as ac

PEDIDO = "En ese procedimiento, agrega en la sección de responsabilidades este punto: «…». No cambies nada más."


class _Resultado:
    def __init__(self, filas):
        self._filas = filas

    def first(self):
        return self._filas[0] if self._filas else None


class _BD:
    def __init__(self, metadata):
        self.consultas = []
        self._metadata = metadata

    async def execute(self, consulta):
        self.consultas.append(str(consulta))
        return _Resultado([] if self._metadata is None else [(self._metadata,)])


def _reiniciada(metadata):
    return asyncio.run(ac.sesion_recien_reiniciada(_BD(metadata), "agente", "sesion"))


def _con(prompt, *, sin_historial=False, reiniciada=True, archivos=()):
    with (
        patch.object(ac, "archivos_generados", AsyncMock(return_value=list(archivos))),
        patch.object(ac, "sesion_sin_historial", AsyncMock(return_value=sin_historial)),
        patch.object(ac, "sesion_recien_reiniciada", AsyncMock(return_value=reiniciada)),
    ):
        return asyncio.run(
            ac.con_archivos_generados(
                None, "agente", "usuario", "sesion", "clave", prompt, aviso_conversacion_nueva=True
            )
        )


class TestSesionRecienReiniciada(unittest.TestCase):
    def test_si_la_ultima_tarea_es_un_reset_la_sesion_empezo_de_nuevo(self):
        self.assertTrue(_reiniciada({"reset": True, "public_session_id": "sesion"}))
        self.assertTrue(_reiniciada({"auto_reset": True}))

    def test_si_la_ultima_tarea_es_normal_hay_historial(self):
        self.assertFalse(_reiniciada({"conversation": True, "public_session_id": "sesion"}))
        self.assertFalse(_reiniciada({}))

    def test_sin_tareas_o_sin_datos_no_dice_reiniciada(self):
        self.assertFalse(_reiniciada(None))
        self.assertFalse(asyncio.run(ac.sesion_recien_reiniciada(_BD({"reset": True}), "", "sesion")))
        self.assertFalse(asyncio.run(ac.sesion_recien_reiniciada(_BD({"reset": True}), "agente", "")))

    def test_la_consulta_mira_solo_la_ultima_tarea_de_la_sesion(self):
        bd = _BD({"reset": True})
        asyncio.run(ac.sesion_recien_reiniciada(bd, "agente", "sesion"))
        consulta = bd.consultas[0].lower()
        self.assertIn("order by", consulta)
        self.assertIn("limit", consulta)


class TestAvisoTrasUnReset(unittest.TestCase):
    def test_tras_un_reset_la_alusion_a_algo_anterior_recibe_el_aviso(self):
        prompt = _con(PEDIDO)
        self.assertTrue(prompt.startswith(ac.AVISO_CONVERSACION_REINICIADA))
        self.assertIn("/reset", prompt)
        self.assertIn("pregúntale cuál es", prompt)
        self.assertTrue(prompt.endswith(PEDIDO))

    def test_sin_alusion_no_se_avisa(self):
        pedido = "Crea un procedimiento SGSI de control de acceso lógico"
        self.assertEqual(_con(pedido), pedido)

    def test_sin_reset_ni_sesion_nueva_no_se_avisa(self):
        self.assertEqual(_con(PEDIDO, reiniciada=False), PEDIDO)

    def test_la_sesion_nueva_por_tiempo_sigue_con_su_aviso(self):
        self.assertTrue(_con(PEDIDO, sin_historial=True).startswith(ac.AVISO_CONVERSACION_NUEVA))

    def test_no_se_avisa_dos_veces(self):
        una_vez = _con(PEDIDO)
        self.assertEqual(_con(una_vez), una_vez)

    def test_si_la_sesion_ya_generó_archivos_tras_el_reset_se_usa_la_nota_de_siempre(self):
        archivos = [{"ruta": "uploads/cal014.docx", "task_id": "29964e67-x", "nombre": "DOC-PRO-014"}]
        prompt = _con(PEDIDO, archivos=archivos)
        self.assertTrue(prompt.startswith(ac.ENCABEZADO))
        self.assertNotIn(ac.AVISO_CONVERSACION_REINICIADA, prompt)


if __name__ == "__main__":
    unittest.main()
