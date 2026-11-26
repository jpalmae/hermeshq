from hermeshq.services.respuesta_sin_repeticion import (
    ACLARACION_ENTRADA_VACIA,
    ACLARACION_REPETICION,
    AVISO_SIN_RESPUESTA,
    filtrar_respuesta,
)

ANTERIOR = "El puerto correcto es el 9100, no el 9000."


def historial(*turnos):
    return [{"role": r, "content": c} for r, c in turnos]


class TestRespuestaSinRepeticion:
    def test_punto_que_repite_la_respuesta_anterior_pide_aclaracion(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        assert filtrar_respuesta(".", ANTERIOR, h) == ACLARACION_ENTRADA_VACIA

    def test_punto_con_sufijo_de_hora_tambien_se_detecta(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        prompt = ".\n\n[Hora actual: domingo 2026-11-08 16:17 America/Santiago]"
        assert filtrar_respuesta(prompt, ANTERIOR + "\n\n[Hora]", h) == ACLARACION_ENTRADA_VACIA

    def test_entrada_vacia_no_produce_respuesta_vacia(self):
        assert filtrar_respuesta("", "", []) == ACLARACION_ENTRADA_VACIA
        assert filtrar_respuesta("   ", "", []) == ACLARACION_ENTRADA_VACIA

    def test_repeticion_de_la_anterior_en_pregunta_sustantiva_pide_aclaracion(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        assert filtrar_respuesta("¿Y ahora?", ANTERIOR, h) == ACLARACION_REPETICION

    def test_respuesta_nueva_se_conserva(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        nueva = "Según lo que me dijiste, el puerto es 9100."
        assert filtrar_respuesta("¿Y ahora?", nueva, h) == nueva

    def test_pregunta_que_pide_repetir_conserva_la_respuesta(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        assert filtrar_respuesta("Repite el puerto, por favor", ANTERIOR, h) == ANTERIOR

    def test_sin_historial_no_hay_repeticion(self):
        assert filtrar_respuesta("¿Qué hora es?", "Son las 16:17.", []) == "Son las 16:17."

    def test_punto_con_respuesta_nueva_se_conserva(self):
        h = historial(("user", "¿Cuál es el puerto?"), ("assistant", ANTERIOR))
        nueva = "¿Qué necesitas que haga con el puerto?"
        assert filtrar_respuesta(".", nueva, h) == nueva

    def test_respuesta_corta_legitima_repetida_a_pregunta_sustantiva_se_conserva(self):
        h = historial(("user", "¿Confirmas el envío?"), ("assistant", "Sí."))
        assert filtrar_respuesta("¿Y el otro?", "Sí.", h) == "Sí."

    def test_respuesta_vacia_a_un_pedido_es_un_aviso_honesto_no_una_repeticion(self):
        assert filtrar_respuesta("¿Qué hora es?", "   ", []) == AVISO_SIN_RESPUESTA

    def test_caso_real_ana_resumen_sin_texto_tras_bloqueo_de_herramienta(self):
        h = historial(("user", "hola"), ("assistant", "Hola, ¿en qué te ayudo?"))
        assert filtrar_respuesta("dame un resumen de correos", "", h) == AVISO_SIN_RESPUESTA
        assert "misma respuesta" not in AVISO_SIN_RESPUESTA


class TestAvisoDeFalloRepetido:
    AVISO = (
        "No pude terminar esta parte: la herramienta ms365_mail_list falló varias veces seguidas sin dar un resultado "
        "distinto. Lo que sí quedó listo está arriba; si quieres, lo intento de otra forma."
    )

    def test_caso_real_el_mismo_fallo_dos_veces_se_informa_tal_cual(self):
        h = [{"role": "user", "content": "dame un resumen de correos"}, {"role": "assistant", "content": self.AVISO}]
        assert filtrar_respuesta("dame un resumen de correos", self.AVISO, h) == self.AVISO

    def test_avisos_de_corte_tampoco_se_reemplazan(self):
        aviso = "⚠️ Detuve esta respuesta: el modelo entró en un bucle (texto repetido) y no iba a terminar."
        h = [{"role": "assistant", "content": aviso}]
        assert filtrar_respuesta("hazlo de nuevo el informe", aviso, h) == aviso

    def test_una_respuesta_normal_repetida_sigue_aclarandose(self):
        respuesta = "Tienes 3 correos sin leer de hoy: uno de Ana, uno de Pedro y otro de soporte técnico."
        h = [{"role": "assistant", "content": respuesta}]
        assert filtrar_respuesta("y ahora qué más hay", respuesta, h) == ACLARACION_REPETICION


class TestPedidoRepetidoAPropósito:
    RESUMEN = "Resumen de correos: 1 correo nuevo de Ana sobre la reunión de presupuesto, sin otros pendientes hoy."

    def test_volver_a_pedir_lo_mismo_recibe_la_misma_respuesta(self):
        h = historial(("user", "dame un resumen de correos"), ("assistant", self.RESUMEN))
        assert filtrar_respuesta("dame un resumen de correos", self.RESUMEN, h) == self.RESUMEN
        assert filtrar_respuesta("dame el resumen de correos de nuevo por favor", self.RESUMEN, h) == self.RESUMEN

    def test_otro_pedido_con_la_misma_respuesta_sigue_aclarandose(self):
        h = historial(("user", "dame un resumen de correos"), ("assistant", self.RESUMEN))
        assert filtrar_respuesta("y qué reuniones tengo mañana", self.RESUMEN, h) == ACLARACION_REPETICION
