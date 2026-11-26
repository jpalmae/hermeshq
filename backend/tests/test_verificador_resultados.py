import json
import tempfile
import unittest
from pathlib import Path

import hermeshq.scripts.hermes_task_runner as runner

RESPUESTA_ANA = (
    "El informe interactivo ha sido creado y guardado exitosamente.\n\n"
    "**MEDIA:C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Ana\\Proyectos\\Proyecto Demo\\"
    "PROYECTO DEMO\\Etapa 2 - Estructura\\Estructura-Demo.html**\n\n"
    "## Resumen del entregable\n\n"
    "El archivo **`Estructura-Demo.html`** contiene un informe interactivo con dos árboles, el modelo de roles y "
    "las tablas de seguridad por sucursal y área."
)
PEDIDO_ANA = (
    "Hace un informe interactivo de 1 sola pagina con dos árboles. GUardalo en C:\\Users\\usuario1\\OneDrive - "
    "Empresa\\Escritorio\\Ana\\Proyectos y el archivo que se llame Estructura-Demo.html"
)
INSISTENCIA_ANA = (
    "El archivo ya existe y está guardado en la carpeta que indicaste. Aquí está el enlace para abrirlo:\n\n"
    "**MEDIA:C:\\Users\\usuario1\\OneDrive - Empresa\\Escritorio\\Estructura-Demo.html**\n\n"
    "¿Quieres que también lo comparta por enlace?"
)
URL_REAL = "https://empresa-my.sharepoint.com/personal/d/Documents/Proyectos/Estructura%20Demo_v2.html"


def _turno(*herramientas, usuario="pedido"):
    mensajes = [{"role": "user", "content": usuario}]
    for i, (nombre, resultado) in enumerate(herramientas):
        mensajes.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": nombre}}]}
        )
        mensajes.append({"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(resultado)})
    return mensajes


def _tipos(pendientes):
    return sorted({p["tipo"] for p in pendientes})


class TestCasosReales(unittest.TestCase):
    def test_creado_y_guardado_sin_subida_y_media_de_su_pc(self):
        mensajes = _turno(
            ("vision_analyze", {"success": True, "analysis": "diagrama"}),
            ("write_file", {"bytes_written": 36623}),
        )
        pendientes = runner.verificar_afirmaciones(RESPUESTA_ANA, mensajes, 0, mensaje=PEDIDO_ANA)
        self.assertEqual(_tipos(pendientes), ["adjunto_pc", "subida"])
        nota = runner.nota_verificacion(pendientes)
        self.assertIn("sharepoint_upload_file", nota)
        self.assertIn("ruta del PC", nota)

    def test_insistir_en_que_esta_guardado_con_solo_vista_previa_de_compartir(self):
        mensajes = _turno(("sharepoint_share_file", {"success": False, "requiere_aprobacion": True}))
        pendientes = runner.verificar_afirmaciones(INSISTENCIA_ANA, mensajes, 0, mensaje="dame este archivo")
        self.assertEqual(_tipos(pendientes), ["adjunto_pc", "subida_pendiente"])

    def test_aviso_final_dice_la_verdad_y_quita_lo_falso(self):
        mensajes = _turno(("write_file", {"bytes_written": 10}))
        pendientes = runner.verificar_afirmaciones(RESPUESTA_ANA, mensajes, 0, mensaje=PEDIDO_ANA)
        final = runner.respuesta_verificada(RESPUESTA_ANA, pendientes)
        self.assertTrue(final.startswith("⚠️"))
        self.assertIn("No se guardó ni subió ningún archivo a OneDrive o SharePoint.", final)
        self.assertIn("No pude adjuntar «Estructura-Demo.html»", final)
        self.assertNotIn("guardado exitosamente", final)
        self.assertNotIn("MEDIA:", final)
        self.assertIn("Resumen del entregable", final)


class TestConEvidenciaNoSeToca(unittest.TestCase):
    def test_subida_exitosa_respalda_el_guardado(self):
        mensajes = _turno(("sharepoint_upload_file", {"success": True, "url": URL_REAL}))
        respuesta = f"Listo, guardé el informe en tu carpeta Proyectos de OneDrive: {URL_REAL}"
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0, mensaje=PEDIDO_ANA), [])

    def test_subida_fallida_no_respalda(self):
        mensajes = _turno(("sharepoint_upload_file", {"success": False, "error": "403"}))
        respuesta = "Listo, guardé el informe en tu carpeta Proyectos de OneDrive."
        self.assertEqual(_tipos(runner.verificar_afirmaciones(respuesta, mensajes, 0)), ["subida"])

    def test_existencia_confirmada_con_un_listado(self):
        mensajes = _turno(("sharepoint_list_files", {"success": True, "items": [{"name": "a.html", "url": URL_REAL}]}))
        respuesta = f"El archivo está guardado en la carpeta Proyectos: {URL_REAL}"
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])

    def test_media_del_workspace_que_existe(self):
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "outputs").mkdir()
            (Path(cwd) / "outputs" / "informe.html").write_text("<html></html>")
            mensajes = _turno(("write_file", {"bytes_written": 13}))
            respuesta = "Te adjunto el informe.\n\nMEDIA: outputs/informe.html"
            self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0, cwd=cwd), [])
            self.assertEqual(
                _tipos(runner.verificar_afirmaciones("MEDIA: outputs/otro.html", mensajes, 0, cwd=cwd)), ["adjunto"]
            )

    def test_correo_enviado_con_envio_real(self):
        mensajes = _turno(("ms365_mail_send", {"success": True, "id": "m1"}))
        self.assertEqual(runner.verificar_afirmaciones("Listo, envié el correo a Juan.", mensajes, 0), [])

    def test_reunion_creada_con_evento_real(self):
        mensajes = _turno(("ms365_calendar_create_event", {"success": True, "id": "e1"}))
        self.assertEqual(runner.verificar_afirmaciones("✅ Agendé la reunión para mañana a las 10.", mensajes, 0), [])

    def test_cron_pendiente_de_aprobacion_cuenta(self):
        mensajes = _turno(("hq_schedule_request", {"success": False, "requiere_aprobacion": True}))
        respuesta = "Programé el recordatorio diario; queda pendiente de tu aprobación."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])


class TestSinEvidencia(unittest.TestCase):
    def test_correo_enviado_con_solo_vista_previa(self):
        mensajes = _turno(("ms365_mail_send", {"success": False, "requiere_aprobacion": True}))
        pendientes = runner.verificar_afirmaciones("Listo, envié el correo a Juan.", mensajes, 0)
        self.assertEqual(_tipos(pendientes), ["correo_pendiente"])
        self.assertIn("falta tu aprobación", runner.respuesta_verificada("Listo, envié el correo a Juan.", pendientes))

    def test_reunion_agendada_sin_herramienta(self):
        pendientes = runner.verificar_afirmaciones("✅ Agendé la reunión con Pedro para mañana.", _turno(), 0)
        self.assertEqual(_tipos(pendientes), ["calendario"])

    def test_cron_sin_herramienta(self):
        pendientes = runner.verificar_afirmaciones("Te avisaré todos los días a las 8 con el resumen.", _turno(), 0)
        self.assertEqual(_tipos(pendientes), ["programacion"])

    def test_enlace_inventado(self):
        respuesta = "Aquí tienes el enlace: https://empresa-my.sharepoint.com/:u:/g/inventado123"
        mensajes = _turno(("sharepoint_list_files", {"success": True, "items": [{"url": URL_REAL}]}))
        self.assertEqual(_tipos(runner.verificar_afirmaciones(respuesta, mensajes, 0)), ["enlace"])

    def test_enlace_que_dio_el_usuario_no_es_inventado(self):
        respuesta = f"Revisé el archivo de {URL_REAL} y tiene 3 hojas."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(usuario=f"revisa {URL_REAL}"), 0), [])

    def test_archivo_generado_sin_ninguna_herramienta(self):
        pendientes = runner.verificar_afirmaciones("Listo, generé el informe con los dos árboles.", _turno(), 0)
        self.assertEqual(_tipos(pendientes), ["archivo"])

    def test_la_nota_del_reintento_no_respalda_el_enlace(self):
        url = "https://empresa-my.sharepoint.com/:u:/g/inventado123"
        mensajes = _turno() + [{"role": "user", "content": f"[Nota del sistema] «{url}»: es inventado."}]
        self.assertEqual(_tipos(runner.verificar_afirmaciones(f"El enlace es {url}", mensajes, 0)), ["enlace"])


class TestControlesNegativos(unittest.TestCase):
    def test_resumen_de_correos_con_citas_y_tablas(self):
        respuesta = (
            "Tienes 3 correos nuevos:\n\n"
            "| # | Remitente | Asunto |\n|---|---|---|\n| 1 | Juan | Te envié la factura |\n\n"
            "Juan escribe: «Ya envié la factura y agendé la reunión».\n"
            "> Respondí tu consulta ayer.\n"
            "María indica que el informe fue enviado al cliente."
        )
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0), [])

    def test_listado_de_calendario_con_reunion_cancelada_por_otro(self):
        respuesta = "Mañana tienes 2 reuniones. La reunión de presupuesto ya fue cancelada por el organizador."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0), [])

    def test_ofertas_preguntas_y_negaciones(self):
        respuesta = (
            "¿Quieres que lo guarde en tu carpeta de OneDrive? No envié el correo todavía. "
            "Si me confirmas, agendo la reunión."
        )
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0), [])

    def test_referencias_a_lo_hecho_antes(self):
        respuesta = "El informe que guardé ayer en tu carpeta de OneDrive sigue ahí."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0), [])

    def test_tarea_programada_que_anuncia_su_proximo_aviso(self):
        respuesta = "Resumen de hoy: sin alertas. Te avisaré mañana a las 8 con el siguiente resumen."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0, programada=True), [])

    def test_analisis_en_el_chat_sin_afirmaciones(self):
        respuesta = "Se detectaron 3 reprocesos en el cruce de las hojas. El equipo con más casos es EQ-0015."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, _turno(), 0), [])


class TestEstadoResultado(unittest.TestCase):
    def test_estados(self):
        self.assertEqual(runner._estado_resultado('{"success": true}'), "ok")
        self.assertEqual(runner._estado_resultado('{"success": false, "requiere_aprobacion": true}'), "pendiente")
        self.assertEqual(runner._estado_resultado('{"success": false, "error": "x"}'), "fallo")
        self.assertEqual(runner._estado_resultado('{"error": "x"}'), "fallo")
        self.assertEqual(runner._estado_resultado("Error: no existe"), "fallo")
        self.assertEqual(runner._estado_resultado("contenido del archivo"), "ok")


class TestFalsosPositivosRealesDelHistorico(unittest.TestCase):
    CASOS = (
        "Quedo a la espera de tu confirm del **nombre + carpeta** y del **detalle del punto 3** para proceder.",
        "**Deje el cron como está** (últimas alertas de los últimos 30 min, cada media hora), o",
        "Si lo necesitas en OneDrive, puedo reintentar la subida en un próximo turno.",
        "Cuando termine, te aviso por este chat con el documento V2 guardado en la misma carpeta de OneDrive.",
        "Entregable: archivo `.html` adjunto en el chat y/o subido a OneDrive.",
        "Listo, quedó en `sharepoint_downloads/informe_prueba_operador2_editado.docx` (el original no se tocó).",
        "✅ **Skill `crear-reuniones` actualizada.** Cambios aplicados:",
        "Rosa envió la versión actualizada de la planilla de licenciamiento (archivo en SharePoint).",
        "- **Excepciones:** quedó como pendiente (no había info en SharePoint)",
        "**Dónde están guardados** (por ejemplo, en OneDrive/SharePoint, en una carpeta específica)",
        "- El importante es un mensaje de evento: se canceló la reunión (organizada por Bruno Castro).",
        "Les escribo para corregir un detalle del correo que les envié hace un momento.",
        "El archivo quedó guardado en la misma carpeta del original (`outputs/`), con el nombre Modelo.",
    )

    def test_ninguna_se_marca(self):
        for frase in self.CASOS:
            with self.subTest(frase=frase):
                self.assertEqual(runner.verificar_afirmaciones(frase, _turno(), 0), [])

    def test_compartir_tambien_sube(self):
        mensajes = _turno(
            ("hq_pdf_generate", {"success": True, "path": "outputs/a.pdf"}),
            ("sharepoint_share_file", {"success": True, "url": URL_REAL}),
        )
        self.assertEqual(
            runner.verificar_afirmaciones("Listo ✅ Generé el PDF y lo subí a tu OneDrive.", mensajes, 0), []
        )

    def test_la_cancelacion_avisada_por_el_calendario(self):
        mensajes = _turno(("ms365_calendar_delete_event", {"success": True}))
        respuesta = "Se envió la notificación de cancelación a Equipo Datos, así que quedan liberados ✅."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])

    def test_envio_verificado_en_la_bandeja(self):
        mensajes = _turno(("ms365_mail_search", {"success": True, "items": []}))
        respuesta = "El correo se envió a Juan (lo confirmo en la bandeja de salida, hoy a las 23:30) ✅."
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])

    def test_enlace_armado_desde_el_sitio_que_devolvio_la_busqueda(self):
        mensajes = _turno(
            (
                "sharepoint_search",
                {
                    "success": True,
                    "hits": [{"url": "https://empresa.sharepoint.com/sites/Empresa/Documentos/a.docx"}],
                },
            )
        )
        respuesta = "Link: https://empresa.sharepoint.com/sites/Empresa/Documentos/Gerencia/b.docx"
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])

    def test_enlace_de_un_sitio_que_nunca_aparecio(self):
        respuesta = "Puedes entrar a https://empresa.sharepoint.com/sites/TI para ver tus carpetas."
        mensajes = _turno(("sharepoint_search", {"success": True, "hits": [{"url": URL_REAL}]}))
        self.assertEqual(_tipos(runner.verificar_afirmaciones(respuesta, mensajes, 0)), ["enlace"])

    def test_aprobacion_ejecutada_por_el_servidor(self):
        self.assertEqual(runner.verificar_afirmaciones("✅ ¡Cron activado!", _turno(), 0, aprobado=True), [])

    def test_media_con_un_enlace_no_es_un_adjunto_faltante(self):
        respuesta = f"Aquí está:\nMEDIA:{URL_REAL}"
        mensajes = _turno(("sharepoint_list_files", {"success": True, "items": [{"url": URL_REAL}]}))
        self.assertEqual(runner.verificar_afirmaciones(respuesta, mensajes, 0), [])

    def test_pregunta_de_estado_no_revisa_lo_que_relata(self):
        respuesta = "Etapa 1 terminada ✅: generé el catálogo HTML interactivo, subido a tu OneDrive."
        self.assertEqual(
            runner.verificar_afirmaciones(respuesta, _turno(), 0, mensaje="qué paso? solo explícame no hagas nada"), []
        )

    def test_subida_solo_con_vista_previa(self):
        mensajes = _turno(("sharepoint_share_file", {"success": False, "requiere_aprobacion": True}))
        pendientes = runner.verificar_afirmaciones("Quedó guardado en tu carpeta de OneDrive.", mensajes, 0)
        self.assertEqual(_tipos(pendientes), ["subida_pendiente"])
        self.assertIn(
            "espera tu aprobación", runner.respuesta_verificada("Quedó guardado en tu carpeta de OneDrive.", pendientes)
        )


class TestEnElRunner:
    PEDIDO = "Hace un informe con dos árboles. Guárdalo en mi OneDrive, carpeta Proyectos"
    FALSO = "El informe interactivo ha sido creado y guardado exitosamente en tu carpeta Proyectos de OneDrive."

    def _correr(self, monkeypatch, tmp_path, respuestas):
        import test_accion_tras_lecturas as base

        return base._correr(monkeypatch, tmp_path, respuestas, mensaje=self.PEDIDO)

    def test_el_reintento_sube_el_archivo(self, monkeypatch, tmp_path):
        res, agente = self._correr(
            monkeypatch,
            tmp_path,
            [
                (self.FALSO, ["write_file"]),
                ("Listo, lo subí a tu carpeta Proyectos de OneDrive.", ["sharepoint_upload_file"]),
            ],
        )
        assert len(agente.llamadas) == 2 and "sharepoint_upload_file" in agente.llamadas[1]
        assert res["verificacion"]["resultado"] == "corregida"
        assert res["final_response"].startswith("Listo, lo subí")

    def test_si_insiste_sin_subir_se_corrige_la_respuesta(self, monkeypatch, tmp_path):
        res, agente = self._correr(monkeypatch, tmp_path, [(self.FALSO, ["write_file"]), (self.FALSO, [])])
        assert len(agente.llamadas) == 2
        assert res["verificacion"]["resultado"] == "aviso_honesto"
        assert "No se guardó ni subió ningún archivo" in res["final_response"]
        assert "guardado exitosamente" not in res["final_response"]

    def test_si_el_reintento_falla_se_corrige_la_primera(self, monkeypatch, tmp_path):
        res, agente = self._correr(monkeypatch, tmp_path, [(self.FALSO, ["write_file"])])
        assert res["verificacion"]["resultado"] == "aviso_honesto"
        assert "error_reintento" in res["verificacion"]
        assert "No se guardó ni subió ningún archivo" in res["final_response"]

    def test_con_subida_real_no_hay_reintento(self, monkeypatch, tmp_path):
        res, agente = self._correr(monkeypatch, tmp_path, [(self.FALSO, ["write_file", "sharepoint_upload_file"])])
        assert len(agente.llamadas) == 1 and "verificacion" not in res


class TestIdiomaEnElRunner:
    INGLES = (
        "I don't have a tool to collect security alerts. Let me check what's available. I have no tool that connects "
        "to a security monitoring system, so I can't collect the alerts for you."
    )

    def _correr(self, monkeypatch, tmp_path, respuestas):
        import test_accion_tras_lecturas as base

        return base._correr(monkeypatch, tmp_path, respuestas, mensaje="Recopila las alertas de seguridad de hoy")

    def test_respuesta_en_ingles_se_pide_en_espanol(self, monkeypatch, tmp_path):
        es = "No tengo una herramienta conectada a un sistema de alertas de seguridad, así que no puedo recopilarlas."
        res, agente = self._correr(monkeypatch, tmp_path, [(self.INGLES, []), (es, [])])
        assert len(agente.llamadas) == 2 and "español" in agente.llamadas[1]
        assert res["final_response"] == es and res["idioma_corregido"] is True

    def test_si_el_reintento_falla_queda_la_original(self, monkeypatch, tmp_path):
        res, agente = self._correr(monkeypatch, tmp_path, [(self.INGLES, [])])
        assert res["final_response"] == self.INGLES and "idioma_corregido" not in res


class TestPreguntaDeUbicacion:
    def test_casos_reales_preguntar_donde_quedo_exige_revisar_onedrive(self):
        for mensaje in (
            "no veo el archivo Prueba-V1-arbol en la carpeta, ¿dónde quedó?",
            "no lo veo en la carpeta",
            "pero el que acabas de crear donde quedo",
        ):
            respuesta = "El archivo se generó correctamente. Aquí está para que lo recibas como adjunto."
            pendientes = runner.verificar_afirmaciones(respuesta, _turno(), 0, mensaje=mensaje)
            assert _tipos(pendientes) == ["ubicacion"], mensaje

    def test_si_reviso_onedrive_no_hay_pendiente(self):
        mensajes = _turno(("sharepoint_list_files", {"success": True, "items": [{"url": URL_REAL}]}))
        respuesta = f"Está en Escritorio/Pruebas/Bateria: {URL_REAL}"
        assert runner.verificar_afirmaciones(respuesta, mensajes, 0, mensaje="no lo veo en la carpeta") == []

    def test_preguntas_que_no_son_de_un_archivo_no_cuentan(self):
        for mensaje in ("¿dónde está la sala de la reunión?", "no lo veo claro, explícamelo de nuevo"):
            assert runner.verificar_afirmaciones("Es en la sala 3.", _turno(), 0, mensaje=mensaje) == []

    def test_aviso_final_de_ubicacion(self):
        final = runner.respuesta_verificada("El archivo se generó.", [{"tipo": "ubicacion", "frase": "no lo veo"}])
        assert "No revisé OneDrive ni SharePoint" in final
