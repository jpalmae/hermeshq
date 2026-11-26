import json
import unittest

from test_correo_sync_ventana import BaseVentana, plugin

import hermeshq.scripts.hermes_task_runner as runner

CRON = "02978740-0000-4000-8000-000000000001"
T0 = "2026-11-12T12:00:00Z"

BORRADOR_REAL = (
    "El hilo está completo. Te dejo el borrador de respuesta:\n\n---\n\n**Para:** Diego Pérez (Sede Central)\n"
    "**CC:** [SUC1] Personas\n**Asunto:** RE: solicitud días de vacaciones\n\n"
    "> Muy buen día Lucía,\n>\n> Quedo atento al comprobante.\n>\n> Saluda cordialmente,\n>\n"
    "> Bruno Castro Reyes\n\n---\n\n¿Lo envío?"
)
BORRADOR_BUENO = BORRADOR_REAL.replace("Diego Pérez (Sede Central)", "Lucía Leal (Sede Central)")
MAPA = {
    "#1822": "ACME Capacitación | 📣 ¡Últimas semanas para aprovechar el precontrato 100% franquiciable!",
    "#1824": "RE: [EMPRESA] Reunión de seguimiento",
    "#1821": "RE: solicitud días de vacaciones",
}
BUSQUEDA_DIEGO = (
    "Encontré el hilo de la práctica. Hay dos correos de Lucía Leal:\n\n"
    '1. **#1824** (19-11): "Asistencia Practicante Noviembre" — pide confirmar días firmados.\n'
    '2. **#1822** (23-11): "RE: Asistencia Practicante Noviembre" — pide confirmar asistencia.'
)
CONTRADICCION = (
    "El correo #1822 no es el de la práctica: es un correo de **ACME Capacitación**.\n\n"
    'El que corresponde es **#1822** "RE: Asistencia Practicante Noviembre" (23-11).'
)


class TestBorradorIncoherente(unittest.TestCase):
    def test_caso_real_para_diego_saludo_lucia(self):
        r = runner.borrador_incoherente(BORRADOR_REAL)
        self.assertIn("Lucía", r)
        self.assertIn("Diego Pérez", r)

    def test_control_destinatario_y_saludo_coinciden(self):
        self.assertIsNone(runner.borrador_incoherente(BORRADOR_BUENO))

    def test_control_saludo_a_un_copiado_no_se_marca(self):
        txt = BORRADOR_REAL.replace("[SUC1] Personas", "Lucía Leal (Sede Central)")
        self.assertIsNone(runner.borrador_incoherente(txt))

    def test_control_saludo_generico_o_destino_solo_correo_no_se_marca(self):
        self.assertIsNone(runner.borrador_incoherente(BORRADOR_REAL.replace("Muy buen día Lucía,", "Hola equipo,")))
        self.assertIsNone(runner.borrador_incoherente(BORRADOR_REAL.replace("Diego Pérez (Sede Central)", "ras@x.cl")))

    def test_control_no_es_borrador_de_correo(self):
        self.assertIsNone(runner.borrador_incoherente("Hola Lucía, el informe tiene 3 filas."))


class TestIdsIncoherentes(unittest.TestCase):
    def test_caso_real_ids_atribuidos_a_otros_asuntos(self):
        r = runner.ids_de_correo_incoherentes(BUSQUEDA_DIEGO, MAPA)
        self.assertEqual(len(r), 2)
        self.assertTrue(any("#1824" in x and "Reunión de seguimiento" in x for x in r))
        self.assertTrue(any("#1822" in x and "ACME" in x for x in r))

    def test_caso_real_mismo_id_con_dos_asuntos(self):
        r = runner.ids_de_correo_incoherentes(CONTRADICCION, MAPA)
        self.assertEqual(len(r), 1)
        self.assertIn("#1822", r[0])

    def test_control_id_con_su_asunto_real_no_se_marca(self):
        txt = '**#1821** "RE: solicitud días de vacaciones" — Lucía Leal\n#1822 «ACME Capacitación»'
        self.assertEqual(runner.ids_de_correo_incoherentes(txt, MAPA), [])

    def test_control_numeral_de_ticket_o_id_sin_asunto_no_se_marca(self):
        self.assertEqual(runner.ids_de_correo_incoherentes("Ticket #19255 resuelto; mira el #1822", MAPA), [])

    def test_sin_mapa_no_se_inventa_nada(self):
        self.assertEqual(runner.ids_de_correo_incoherentes(BUSQUEDA_DIEGO, {}), [])

    def test_incoherencia_junta_ambas(self):
        self.assertIn("IDs que no corresponden", runner.incoherencia_de_correo(BUSQUEDA_DIEGO, MAPA))
        self.assertIn("no coinciden", runner.incoherencia_de_correo(BORRADOR_REAL, MAPA))
        self.assertIsNone(runner.incoherencia_de_correo(BORRADOR_BUENO, MAPA))


class TestBorradorAManoSinPalabraCorreo(unittest.TestCase):
    PEDIDO = "Retomemos la respuesta a Lucia Leal"

    def _turno(self, *herramientas):
        mensajes = [{"role": "user", "content": self.PEDIDO}]
        for i, n in enumerate(herramientas):
            mensajes += [
                {"role": "assistant", "tool_calls": [{"id": f"t{i}", "function": {"name": n}}]},
                {"role": "tool", "tool_call_id": f"t{i}", "name": n, "content": "{}"},
            ]
        return mensajes

    def test_caso_real_borrador_escrito_sin_la_herramienta(self):
        r = runner.vista_envio_a_mano(self.PEDIDO, BORRADOR_REAL, self._turno("ms365_mail_search", "ms365_mail_get"), 0)
        self.assertEqual(r, "correo")

    def test_con_la_herramienta_de_respuesta_no(self):
        r = runner.vista_envio_a_mano(self.PEDIDO, BORRADOR_BUENO, self._turno("ms365_mail_get", "ms365_mail_reply"), 0)
        self.assertIsNone(r)

    def test_control_respuesta_sin_borrador_no(self):
        r = runner.vista_envio_a_mano(
            self.PEDIDO, "¿Cuál de los dos correos de Lucía?", self._turno("ms365_mail_search"), 0
        )
        self.assertIsNone(r)

    def test_la_nota_pide_la_herramienta_y_explica_la_incoherencia(self):
        runner._MAPA_IDS_CORREO.clear()
        runner._MAPA_IDS_CORREO.update(MAPA)
        try:
            nota = runner.nota_accion_narrada("correo_incoherente", BORRADOR_REAL)
        finally:
            runner._MAPA_IDS_CORREO.clear()
        self.assertIn("ms365_mail_reply", nota)
        self.assertIn("no coinciden", nota)


class TestIdCortoEnLecturas(BaseVentana):
    def _estado_con_ids(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "RE: solicitud días de vacaciones")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)

    def test_el_correo_conocido_trae_su_id_corto_y_el_desconocido_avisa(self):
        self._estado_con_ids()
        mensajes = [{"id": "M1", "subject": "x"}, {"id": "OTRO", "subject": "Asistencia Diego"}]
        sin_id = plugin._anotar_ids_cortos(mensajes)
        self.assertEqual(mensajes[0]["id_corto"], "#01")
        self.assertNotIn("id_corto", mensajes[1])
        self.assertTrue(sin_id)
        self.assertIn("no le asignes ni le inventes", plugin._AVISO_IDS_CORTOS)

    def test_control_todos_conocidos_no_hay_aviso(self):
        self._estado_con_ids()
        self.assertFalse(plugin._anotar_ids_cortos([{"id": "M1"}]))

    def test_mapa_para_el_runner(self):
        self._estado_con_ids()
        self.assertEqual(plugin.mapa_ids_correo(), {"#01": "RE: solicitud días de vacaciones"})

    def test_sin_estado_no_rompe(self):
        self.assertEqual(plugin.mapa_ids_correo(), {})
        self.assertTrue(plugin._anotar_ids_cortos([{"id": "M1"}]))

    def test_abrir_por_id_corto_devuelve_la_nota_de_a_que_correo_corresponde(self):
        from unittest.mock import patch

        self._estado_con_ids()
        mensaje = {"id": "M1", "subject": "RE: solicitud días de vacaciones"}
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_broker_token_check", return_value=("tok", "")),
            patch.object(plugin, "_fetch_message", return_value=(dict(mensaje), None)),
        ):
            r = json.loads(plugin._get_email_tool({"message_id": "#01"}))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["message"]["id_corto"], "#01")
        self.assertIn("#01 es ESTE correo: «RE: solicitud días de vacaciones»", r["nota_id"])
