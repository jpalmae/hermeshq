import json
import unittest

from hermeshq.services.respuesta_verificada import (
    AVISO_DESCARTE_NO_CONFIRMADO,
    AVISO_MARCADO_NO_CONFIRMADO,
    con_aviso_descarte_o_marcado_no_confirmado,
)


def _sync(**campos):
    return {"role": "tool", "name": "ms365_correo_sync", "content": json.dumps({"success": True, **campos})}


def _marca(ok=True):
    return {"role": "tool", "name": "ms365_mail_mark_read", "content": json.dumps({"success": ok})}


class TestDescarte(unittest.TestCase):
    def test_caso_real_descarte_con_cero_movidos_se_corrige(self):
        texto = "Listo. Descarté los correos **#1553** y **#1554**. Salieron de la lista de pendientes."
        r = con_aviso_descarte_o_marcado_no_confirmado(texto, [_sync(movidos=0, destino="discarded")])
        self.assertIn(AVISO_DESCARTE_NO_CONFIRMADO, r)

    def test_control_negativo_con_movidos_no_cambia(self):
        texto = "Listo. Descarté el correo #1553."
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, [_sync(movidos=1)]), texto)

    def test_si_ya_estaban_en_ese_estado_no_se_corrige(self):
        texto = "Ese correo ya estaba descartado."
        mensajes = [_sync(movidos=0, ya_estaban_en_ese_estado=["#1"])]
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, mensajes), texto)

    def test_una_negacion_no_cuenta_como_afirmacion(self):
        texto = "No descarté ningún correo porque los #ID no existen."
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, [_sync(movidos=0)]), texto)


class TestMarcadoComoLeido(unittest.TestCase):
    def test_caso_real_marcado_sin_llamar_la_herramienta(self):
        texto = "Correo #1445 descartado y marcado como leído.\n- 📖 Marcado como leído."
        r = con_aviso_descarte_o_marcado_no_confirmado(texto, [_sync(movidos=0)])
        self.assertIn(AVISO_MARCADO_NO_CONFIRMADO, r)

    def test_control_negativo_con_la_herramienta_ok_no_cambia(self):
        texto = "Listo, lo marqué como leído."
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, [_marca(True)]), texto)

    def test_informe_programado_que_marca_con_el_sync_no_recibe_aviso(self):
        texto = "Siguen sin leer de antes: 8 (4 FRÍO marcados como leídos)."
        mensajes = [_sync(marcados_leidos={"marcados": 4})]
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, mensajes), texto)

    def test_marcar_leidos_del_sync_con_marcados_en_la_raiz(self):
        texto = "Listo, los marqué como leídos."
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, [_sync(marcados=3)]), texto)

    def test_sync_que_marco_cero_si_se_corrige(self):
        texto = "(4 FRÍO marcados como leídos)"
        mensajes = [_sync(marcados_leidos={"marcados": 0})]
        self.assertIn(AVISO_MARCADO_NO_CONFIRMADO, con_aviso_descarte_o_marcado_no_confirmado(texto, mensajes))

    def test_herramienta_fallida_si_se_corrige(self):
        r = con_aviso_descarte_o_marcado_no_confirmado("Lo marqué como leído.", [_marca(False)])
        self.assertIn(AVISO_MARCADO_NO_CONFIRMADO, r)

    def test_sin_mencion_no_cambia(self):
        texto = "Estos son tus correos pendientes."
        self.assertEqual(con_aviso_descarte_o_marcado_no_confirmado(texto, []), texto)
