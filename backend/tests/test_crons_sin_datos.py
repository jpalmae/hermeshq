import json
import unittest
from types import SimpleNamespace

from test_schedule_notifications import _key, _schedule, _session, _task, _user

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services import respuesta_verificada as rv
from hermeshq.services.schedule_notifications import datos_no_obtenidos, enqueue_schedule_notification

RESPUESTA_REAL_0900 = "Fallan las tres herramientas M365 con el mismo error `UnknownError` (ms365_correo_sync plan ×2 y ms365_mail_list). No es un fallo de cuenta que yo pueda resolver; es un error de plataforma/Graph.\n\n📥 Informe horario de correo — 09:00 CL\nno pude leer el buzón (Microsoft 365: UnknownError)\n\n| Categoría | Cantidad |\n|-----------|----------|\n| 🚨 INMEDIATO | 0 |\n| 🔴 CRÍTICO | 0 |\n| 🟠 CALIENTE | 0 |\n| 🟡 TIBIO | 0 |\n| 🔵 FRÍO | 0 |\n| ⚪ OTROS | 0 |\n\n🚨 INMEDIATO\n(sin correos nuevos en esta ventana)\n\n🔴 CRÍTICO\n(sin correos nuevos en esta ventana)\n\n🟠 CALIENTE\n(sin correos nuevos en esta ventana)\n\n🟡 TIBIO\n(sin correos nuevos en esta ventana)\n\n🔵 FRÍO (accionables)\n(sin correos nuevos en esta ventana)\n\n⚪ FRÍOS CONSOLIDADOS\n0 correos fríos consolidados en el resumen.\n\nResumen del run: No se pudo revisar el inbox: las herramientas M365 devolvieron `UnknownError` (falla de plataforma/Graph, no de tu cuenta). Reintenté la lectura directa del buzón y también falló. No se enviaron correos automáticamente. Te aviso para que lo reintentes en unos minutos o revises la conexión M365; si el error persiste, puede requerir revisar el servicio de Microsoft Graph."
ERROR_PLAN = {"success": False, "error": "ms365_correo_sync(plan): no pude leer el buzón (Microsoft 365: UnknownError)"}
ERROR_LISTA = {"success": False, "error": "Microsoft 365: UnknownError"}
VENTANA = {"success": True, "since": "2026-11-11T13:00:00Z", "count": 50, "siguiente": "Llama plan"}


def _herramienta(nombre, resultado):
    return {"role": "tool", "name": nombre, "content": json.dumps(resultado, ensure_ascii=False)}


MENSAJES_FALLIDOS = [
    {"role": "user", "content": "Ejecuta el informe horario"},
    _herramienta("ms365_correo_sync", VENTANA),
    _herramienta("ms365_correo_sync", ERROR_PLAN),
    _herramienta("ms365_correo_sync", ERROR_PLAN),
    _herramienta("ms365_mail_list", ERROR_LISTA),
]


class TestInformeSinLectura(unittest.TestCase):
    def test_caso_real_0900_se_quitan_los_conteos_inventados(self):
        texto = rv.con_informe_sin_lectura(RESPUESTA_REAL_0900, MENSAJES_FALLIDOS)
        self.assertNotIn("| 0 |", texto)
        self.assertNotIn("sin correos nuevos en esta ventana", texto)
        self.assertNotIn("0 correos fríos consolidados", texto)
        self.assertNotIn("🚨 INMEDIATO\n", texto)
        self.assertIn("📥 Informe horario de correo — 09:00 CL", texto)
        self.assertIn("no pude leer el buzón (Microsoft 365: UnknownError)", texto)
        self.assertIn("Resumen del run:", texto)
        self.assertTrue(texto.endswith(rv.AVISO_BUZON_NO_LEIDO))

    def test_con_una_lectura_exitosa_no_se_toca(self):
        mensajes = MENSAJES_FALLIDOS + [_herramienta("ms365_correo_sync", {"success": True, "tablas_md": {}})]
        self.assertEqual(rv.con_informe_sin_lectura(RESPUESTA_REAL_0900, mensajes), RESPUESTA_REAL_0900)

    def test_una_bandeja_leida_y_vacia_conserva_sus_ceros(self):
        mensajes = [_herramienta("ms365_correo_sync", {"success": True, "ventana_leida": {"completa": True}})]
        self.assertEqual(rv.con_informe_sin_lectura(RESPUESTA_REAL_0900, mensajes), RESPUESTA_REAL_0900)

    def test_la_ventana_sola_no_cuenta_como_lectura(self):
        self.assertTrue(rv.lectura_de_buzon_fallida(MENSAJES_FALLIDOS))
        self.assertFalse(rv.lectura_de_buzon_fallida([_herramienta("ms365_correo_sync", VENTANA)]))

    def test_sin_herramientas_de_correo_no_se_toca(self):
        self.assertEqual(rv.con_informe_sin_lectura("| FRÍO | 0 |", []), "| FRÍO | 0 |")


class TestDatosNoObtenidos(unittest.TestCase):
    def tarea(self, mensajes):
        return SimpleNamespace(messages_json=mensajes)

    def test_todas_las_consultas_fallaron(self):
        self.assertTrue(datos_no_obtenidos(self.tarea(MENSAJES_FALLIDOS)))

    def test_una_consulta_que_funciono_basta(self):
        ok = _herramienta("ms365_calendar_list_events", {"success": True, "events": []})
        self.assertFalse(datos_no_obtenidos(self.tarea(MENSAJES_FALLIDOS + [ok])))

    def test_un_cron_sin_consultas_no_cambia(self):
        self.assertFalse(datos_no_obtenidos(self.tarea([{"role": "user", "content": "recordatorio"}])))
        self.assertFalse(datos_no_obtenidos(self.tarea(None)))


class TestAvisoDelCron(unittest.IsolatedAsyncioTestCase):
    async def test_caso_real_0900_el_aviso_lleva_la_advertencia(self):
        session = _session(_schedule(), _user(), [_key()])
        tarea = _task()
        tarea.messages_json = MENSAJES_FALLIDOS
        await enqueue_schedule_notification(session, tarea, success=True, summary=RESPUESTA_REAL_0900)
        cuerpo = session.add.call_args.args[0].body
        self.assertTrue(cuerpo.startswith("⚠️ La tarea programada no pudo obtener sus datos"), cuerpo[:120])

    async def test_un_cron_que_leyo_sus_datos_sigue_igual(self):
        session = _session(_schedule(), _user(), [_key()])
        tarea = _task()
        tarea.messages_json = [_herramienta("ms365_correo_sync", {"success": True, "tablas_md": {}})]
        await enqueue_schedule_notification(session, tarea, success=True, summary="📥 Informe horario de correo")
        self.assertEqual(session.add.call_args.args[0].body, "📥 Informe horario de correo")


class TestCorteSinNadaArriba(unittest.TestCase):
    def test_caso_real_0830_el_corte_solo_no_dice_que_hay_algo_arriba(self):
        crudo = "I stopped retrying ms365_mail_folders because it hit the tool-call guardrail (repeated_exact_failure)."
        texto = runner.limpiar_aviso_interno(crudo)
        self.assertTrue(texto.startswith("No pude terminar esta parte"), texto)
        self.assertNotIn("está arriba", texto)
        self.assertIn("Si quieres, lo intento de otra forma.", texto)

    def test_con_texto_previo_se_mantiene(self):
        crudo = (
            "Revisé las carpetas principales.\n\nI stopped retrying ms365_mail_folders because it hit the tool-call "
            "guardrail (repeated_exact_failure)."
        )
        self.assertIn("Lo que sí quedó listo está arriba", runner.limpiar_aviso_interno(crudo))

    def test_cierre_forzado_sin_texto(self):
        self.assertNotIn("arriba", runner._TEXTO_CIERRE_SIN_HERRAMIENTAS)


if __name__ == "__main__":
    unittest.main()
