import json
from unittest.mock import patch

from test_correo_sync_ventana import BaseVentana, plugin

CRON = "02978740-0000-4000-8000-000000000001"
T0 = "2026-11-12T12:00:00Z"
T1 = "2026-11-12T13:00:00Z"


class BaseVista(BaseVentana):
    def informe(self, resultado):
        return [fila["id"] for fila in resultado["filas_nuevas"] + resultado["otros_relevantes"]]

    def marcar_leido(self, ident):
        for m in self.bandeja.mensajes:
            if m["id"] == ident:
                m["isRead"] = True

    def escrituras(self):
        return [(m, r) for m, r in self.bandeja.llamadas if m != "GET"]


class TestVistaNoLeidos(BaseVista):
    def test_caso_pedido_los_no_leidos_vuelven_con_su_id_y_lo_leido_sale(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Factura proveedor")
        self.bandeja.agregar("M2", "2026-11-12T11:00:00Z", "Reunión comité")
        _, _, r1 = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(sorted(self.informe(r1)), ["#01", "#02"])
        self.assertEqual(r1["vista_no_leidos"]["nuevos"], 2)

        self.marcar_leido("M1")
        self.bandeja.agregar("M3", "2026-11-12T12:30:00Z", "Pedido urgente")
        _, plan2, r2 = self.corrida(CRON, T1, vista_no_leidos_horas=48)
        self.assertEqual(self.ids_plan(plan2), ["M3"], "solo lo nuevo se clasifica")
        self.assertEqual(sorted(self.informe(r2)), ["#02", "#03"])
        self.assertEqual(r2["vista_no_leidos"]["nuevos"], 1)
        self.assertEqual(r2["vista_no_leidos"]["siguen_sin_leer"], 1)
        self.assertIn("#02", json.dumps(r2["tablas_md"], ensure_ascii=False))
        self.assertEqual(self.escrituras(), [], "nada se marca como leído")

    def test_lo_que_supera_las_48_horas_deja_de_aparecer(self):
        self.bandeja.agregar("VIEJO", "2026-11-10T13:30:00Z", "Casi 48 h")
        _, _, r1 = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(self.informe(r1), ["#01"])
        _, _, r2 = self.corrida(CRON, "2026-11-12T14:00:00Z", vista_no_leidos_horas=48)
        self.assertEqual(self.informe(r2), [])
        self.assertIn("sin correos no leídos en las últimas 48 h", r2["tablas_md"]["CALIENTE"])

    def test_la_ventana_y_el_plan_miran_48_horas_aunque_haya_marca_de_agua(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)
        v = self.llamar(CRON, accion="ventana", ahora=T1, vista_no_leidos_horas=48)
        self.assertEqual(v["since"], "2026-11-10T13:00:00Z")
        self.assertIn("vista de no leídos", v["modo"])
        self.llamar(CRON, accion="plan", since=v["since"], ahora=T1, vista_no_leidos_horas=48)
        self.assertIn("ge%202026-11-10T13:00:00Z", self.bandeja.filtros()[-1])

    def test_un_correo_leido_antes_de_la_corrida_no_se_clasifica_ni_aparece(self):
        self.bandeja.agregar("LEIDO", "2026-11-12T10:00:00Z")
        self.marcar_leido("LEIDO")
        self.bandeja.agregar("NO_LEIDO", "2026-11-12T11:00:00Z")
        _, plan, r = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(self.ids_plan(plan), ["NO_LEIDO"])
        self.assertEqual(self.informe(r), ["#01"])

    def test_el_id_repetido_se_resuelve_al_mismo_correo(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Factura proveedor")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)
        _, _, r2 = self.corrida(CRON, T1, vista_no_leidos_horas=48)
        self.assertEqual(self.informe(r2), ["#01"])
        resuelto = self.llamar(None, accion="resolver_id", display_id="#01")
        self.assertEqual(resuelto["message_id"], "M1")
        guardado = [r for r in self.estado()["resolved"] if r.get("display_id") == "#01"]
        self.assertEqual(len(guardado), 1, "no se duplica el registro")
        self.assertEqual(guardado[0]["mostrado_at"], T1)

    def test_tope_de_filas_avisa_lo_que_queda_fuera(self):
        for i in range(4):
            self.bandeja.agregar(f"M{i}", f"2026-11-12T1{i}:00:00Z")
        with patch.object(plugin, "_CU_VISTA_MAX_FILAS", 3):
            _, _, r = self.corrida(CRON, "2026-11-12T15:00:00Z", vista_no_leidos_horas=48)
        self.assertEqual(len(self.informe(r)), 3)
        self.assertEqual(r["vista_no_leidos"]["fuera_del_informe_por_tope"], 1)
        self.assertIn("quedaron fuera del informe", r["aviso"])

    def test_el_digest_le_dice_al_modelo_que_no_diga_marcados(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z")
        _, _, r = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertIn("VISTA DE NO LEÍDOS", r["siguiente"])
        self.assertIn("siguen sin leer en Outlook", r["siguiente"])


class TestDescartarLoYaMostrado(BaseVista):
    def _dos_mostrados(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Cuarentena Microsoft")
        self.bandeja.agregar("M2", "2026-11-12T11:00:00Z", "Itaú informa")
        self.bandeja.agregar("M3", "2026-11-12T11:30:00Z", "Otro correo")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)

    def test_descartar_un_correo_ya_mostrado_lo_saca_del_informe(self):
        self._dos_mostrados()
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01", "#02"], destino="discarded")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["movidos"], 2)
        estado = self.estado()
        self.assertEqual(sorted(x["display_id"] for x in estado["discarded"]), ["#01", "#02"])
        self.assertEqual({x["resolution_type"] for x in estado["discarded"]}, {"descartado_por_usuario"})
        self.assertEqual([x["display_id"] for x in estado["resolved"]], ["#03"])
        _, _, r2 = self.corrida(CRON, T1, vista_no_leidos_horas=48)
        self.assertEqual(self.informe(r2), ["#03"], "los descartados siguen sin leer en Outlook pero no vuelven")

    def test_resolver_un_correo_ya_mostrado_tambien_lo_saca(self):
        self._dos_mostrados()
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#02"], destino="resolved")
        self.assertEqual(r["movidos"], 1)
        _, _, r2 = self.corrida(CRON, T1, vista_no_leidos_horas=48)
        self.assertEqual(sorted(self.informe(r2)), ["#01", "#03"])

    def test_control_negativo_sin_descartar_los_tres_siguen_apareciendo(self):
        self._dos_mostrados()
        _, _, r2 = self.corrida(CRON, T1, vista_no_leidos_horas=48)
        self.assertEqual(sorted(self.informe(r2)), ["#01", "#02", "#03"])

    def test_por_message_id_tambien_funciona(self):
        self._dos_mostrados()
        r = self.llamar(None, accion="resolver", ahora=T1, ids=["M1"], destino="discarded")
        self.assertEqual(r["movidos"], 1)

    def test_un_id_que_no_existe_no_se_da_por_descartado(self):
        self._dos_mostrados()
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#99"], destino="discarded")
        self.assertFalse(r["success"])
        self.assertEqual(r["movidos"], 0)
        self.assertIn("NO digas que los descartaste", r["error"])

    def test_descartar_dos_veces_dice_que_ya_estaba(self):
        self._dos_mostrados()
        self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01"], destino="discarded")
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01"], destino="discarded")
        self.assertTrue(r["success"])
        self.assertEqual(r["movidos"], 0)
        self.assertEqual(r["ya_estaban_en_ese_estado"], ["#01"])


class TestSinVistaSigueIncremental(BaseVista):
    def test_sin_la_vista_solo_aparece_lo_nuevo(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z")
        self.corrida(CRON, T0)
        self.bandeja.agregar("M2", "2026-11-12T12:30:00Z")
        _, _, r2 = self.corrida(CRON, T1)
        self.assertEqual(self.informe(r2), ["#02"])
        self.assertNotIn("vista_no_leidos", r2)


class TestPreferencia(BaseVista):
    def test_el_usuario_la_activa_en_el_chat_y_los_crons_la_usan(self):
        respuesta = self.llamar(None, accion="preferencias", vista_no_leidos_horas=48)
        self.assertTrue(respuesta["success"], respuesta)
        self.assertEqual(respuesta["vista_no_leidos_horas"], 48)
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z")
        self.corrida(CRON, T0)
        _, _, r2 = self.corrida(CRON, T1)
        self.assertEqual(self.informe(r2), ["#01"])
        self.assertEqual(r2["vista_no_leidos"]["siguen_sin_leer"], 1)

    def test_una_tarea_automatica_no_la_cambia(self):
        respuesta = self.llamar(CRON, accion="preferencias", vista_no_leidos_horas=48)
        self.assertFalse(respuesta["success"])
        self.assertFalse(self.ruta.exists() and self.estado().get("vista_no_leidos_horas"))

    def test_cero_la_desactiva(self):
        self.llamar(None, accion="preferencias", vista_no_leidos_horas=48)
        respuesta = self.llamar(None, accion="preferencias", vista_no_leidos_horas=0)
        self.assertEqual(respuesta["vista_no_leidos_horas"], 0)
        self.assertIn("solo los correos nuevos", respuesta["mensaje"])


class TestMarcarLosDelUltimoInforme(BaseVista):
    def test_las_categorias_del_ultimo_informe_incluyen_los_repetidos(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.bandeja.agregar("M2", "2026-11-12T12:30:00Z")
        self.corrida(CRON, T1, vista_no_leidos_horas=48)
        marcados = []
        with (
            patch.object(plugin, "_marcado_autorizado_tarea", return_value=True),
            patch.object(
                plugin, "_cu_marcar_leidos", side_effect=lambda mids: marcados.extend(mids) or {"marcados": len(mids)}
            ),
        ):
            resultado = plugin._cu_marcar_leidos_ahora({"categorias": ["CRÍTICO", "CALIENTE"]})
        self.assertTrue(resultado["success"], resultado)
        self.assertEqual(sorted(marcados), ["M1", "M2"], "M1 subió a CRÍTICO por llevar 2 h esperando respuesta")


class TestSoloFrios(BaseVista):
    def test_cron_autorizado_marca_solo_los_frios_mostrados_y_consolidados(self):
        self.llamar(None, accion="preferencias", marcar_leidos_categorias=["FRÍO"])
        self.llamar(None, accion="preferencias", vista_no_leidos_horas=48)
        self.bandeja.agregar("CALIENTE1", "2026-11-12T10:00:00Z", "Firma pendiente")
        self.bandeja.agregar("FRIO1", "2026-11-12T10:30:00Z", "Boletín semanal")
        self.bandeja.agregar("FRIO_AUTO", "2026-11-12T11:00:00Z", "Notificación automática")
        frio = {
            "display_category": "FRÍO",
            "base_temperature": "FRÍO",
            "operational_status": "SIN_ACCIÓN",
            "action_required": "",
            "requires_response": False,
        }
        clasif = {
            "CALIENTE1": self.clasif(),
            "FRIO1": {**frio, "action_required": "Leer si hay tiempo"},
            "FRIO_AUTO": {**frio, "ruido_automatico": True},
        }
        marcados = []
        with (
            patch.object(plugin, "_marcado_autorizado_tarea", return_value=True),
            patch.object(
                plugin,
                "_cu_marcar_leidos",
                side_effect=lambda mids: marcados.extend(mids) or {"marcados": len(mids), "fallos": 0},
            ),
        ):
            v = self.llamar(CRON, accion="ventana", ahora=T0)
            plan = self.llamar(CRON, accion="plan", since=v["since"], ahora=T0)
            clasifs = {n["token"]: clasif[n["id"]] for n in plan["nuevos_sin_clasificar"]}
            r = self.llamar(CRON, accion="commit", clasificaciones=clasifs, ahora=T0, verificar_leidos=False)
        self.assertTrue(r["success"], r)
        self.assertEqual(sorted(marcados), ["FRIO1", "FRIO_AUTO"])
        self.assertEqual(r["categorias_que_se_marcan_leidas"], ["FRÍO"])

    def _corrida_clasificada(self, ahora, clasif, autorizado):
        marcados = []
        with (
            patch.object(plugin, "_marcado_autorizado_tarea", return_value=autorizado),
            patch.object(
                plugin,
                "_cu_marcar_leidos",
                side_effect=lambda mids: marcados.extend(mids) or {"marcados": len(mids), "fallos": 0},
            ),
        ):
            v = self.llamar(CRON, accion="ventana", ahora=ahora)
            plan = self.llamar(CRON, accion="plan", since=v["since"], ahora=ahora)
            clasifs = {n["token"]: clasif[n["id"]] for n in plan["nuevos_sin_clasificar"]}
            r = self.llamar(CRON, accion="commit", clasificaciones=clasifs, ahora=ahora, verificar_leidos=False)
        self.assertTrue(r["success"], r)
        return r, marcados

    def test_caso_real_11_00_el_frio_repetido_tambien_se_marca(self):
        self.llamar(None, accion="preferencias", marcar_leidos_categorias=["FRÍO"])
        self.llamar(None, accion="preferencias", vista_no_leidos_horas=48)
        frio = {
            "display_category": "FRÍO",
            "base_temperature": "FRÍO",
            "operational_status": "SIN_ACCIÓN",
            "action_required": "Leer si hay tiempo",
            "requires_response": False,
        }
        self.bandeja.agregar("FRIO_ANTES", "2026-11-12T10:00:00Z", "Boletín semanal")
        self.bandeja.agregar("CALIENTE_ANTES", "2026-11-12T10:10:00Z", "Firma pendiente")
        _, marcados1 = self._corrida_clasificada(
            T0, {"FRIO_ANTES": frio, "CALIENTE_ANTES": self.clasif()}, autorizado=False
        )
        self.assertEqual(marcados1, [], "antes del despliegue el cron no tenía autorización")

        self.bandeja.agregar("FRIO_NUEVO", "2026-11-12T12:30:00Z", "Newsletter")
        r, marcados2 = self._corrida_clasificada(T1, {"FRIO_NUEVO": frio}, autorizado=True)
        self.assertEqual(r["vista_no_leidos"]["siguen_sin_leer"], 2)
        self.assertEqual(sorted(marcados2), ["FRIO_ANTES", "FRIO_NUEVO"], "el CALIENTE repetido sigue sin leer")
        self.assertIn("Se marcaron como leídos 2 correos FRÍO", r["siguiente"])
        self.assertNotIn("No digas que se marcaron", r["siguiente"])
        self.assertEqual(
            r["linea_vista"],
            "Ventana: no leídos de las últimas 48 h (hasta 10:00 CL). Nuevos: 1 · Siguen sin leer de antes: 2 "
            "(2 FRÍO marcados como leídos).",
            "cron de las 12:00 (01d03e71): la herramienta marcó 13 y el informe dijo «12 FRÍO marcados»",
        )
        self.assertIn("EXACTAMENTE linea_vista", r["siguiente"])

        for ident in marcados2:
            self.marcar_leido(ident)
        r3, _ = self._corrida_clasificada("2026-11-12T14:00:00Z", {}, autorizado=True)
        filas = json.dumps(r3["tablas_md"], ensure_ascii=False)
        self.assertNotIn("Boletín semanal", filas, "marcado, ya no vuelve a mostrarse")
        self.assertIn("Firma pendiente", filas)

    def test_sin_autorizacion_no_marca_nada(self):
        self.llamar(None, accion="preferencias", marcar_leidos_categorias=["FRÍO"])
        self.bandeja.agregar("FRIO1", "2026-11-12T10:30:00Z", "Boletín semanal")
        marcados = []
        with (
            patch.object(plugin, "_marcado_autorizado_tarea", return_value=False),
            patch.object(plugin, "_cu_marcar_leidos", side_effect=lambda mids: marcados.extend(mids) or {}),
        ):
            self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(marcados, [])


class TestDescartarYMarcarLeido(BaseVista):
    def _dos(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Cuarentena")
        self.bandeja.agregar("M2", "2026-11-12T11:00:00Z", "Itaú")
        self.corrida(CRON, T0, vista_no_leidos_horas=48)

    def _leidos(self):
        marcados = {r.rsplit("/", 1)[-1] for m, r in self.escrituras() if m == "PATCH"}
        return {m["id"]: m["id"] in marcados for m in self.bandeja.mensajes}

    def test_con_marcar_leido_true_el_descartado_queda_leido_en_outlook(self):
        self._dos()
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01"], destino="discarded", marcar_leido=True)
        self.assertEqual(r["movidos"], 1)
        self.assertEqual(r["marcado_leido"]["marcados"], 1)
        self.assertTrue(self._leidos()["M1"])
        self.assertFalse(self._leidos()["M2"])

    def test_sin_marcar_leido_solo_sale_del_informe_y_sigue_sin_leer(self):
        self._dos()
        r = self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01"], destino="discarded")
        self.assertNotIn("marcado_leido", r)
        self.assertFalse(self._leidos()["M1"])

    def test_la_preferencia_descartar_implica_leido_se_aplica_sola(self):
        self._dos()
        p = self.llamar(None, accion="preferencias", descartar_marca_leido=True)
        self.assertTrue(p["descartar_marca_leido"])
        self.llamar(None, accion="resolver", ahora=T1, display_ids=["#02"], destino="discarded")
        self.assertTrue(self._leidos()["M2"])
        self.llamar(None, accion="preferencias", descartar_marca_leido=False)

    def test_marcar_leido_false_explicito_gana_a_la_preferencia(self):
        self._dos()
        self.llamar(None, accion="preferencias", descartar_marca_leido=True)
        self.llamar(None, accion="resolver", ahora=T1, display_ids=["#01"], destino="discarded", marcar_leido=False)
        self.assertFalse(self._leidos()["M1"])

    def test_una_tarea_automatica_no_cambia_la_preferencia_ni_marca(self):
        self._dos()
        r = self.llamar(CRON, accion="preferencias", descartar_marca_leido=True)
        self.assertFalse(r["success"])


class TestAntiguosSinLeer(BaseVista):
    def test_el_dato_de_antiguos_queda_en_el_digest_pero_no_en_el_texto_del_usuario(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Factura proveedor")
        self.bandeja.sin_leer_total = 4917
        _, _, r = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(r["vista_no_leidos"]["antiguos_sin_leer"], 4916)
        self.assertNotIn("Además hay", r["linea_vista"])
        self.assertNotIn("4.916", r["linea_vista"])
        self.assertNotIn("más de 48", r["linea_vista"])
        self.assertNotIn("no se detallan", r["siguiente"])
        self.assertNotIn("4.916", json.dumps(r["siguiente"], ensure_ascii=False))

    def test_control_negativo_sin_antiguos_la_linea_no_cambia(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Factura proveedor")
        self.bandeja.sin_leer_total = 1
        _, _, r = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertEqual(r["vista_no_leidos"]["antiguos_sin_leer"], 0)
        self.assertNotIn("Además hay", r["linea_vista"])

    def test_control_negativo_si_no_se_pudo_consultar_el_contador_no_se_inventa(self):
        self.bandeja.agregar("M1", "2026-11-12T10:00:00Z", "Factura proveedor")
        _, _, r = self.corrida(CRON, T0, vista_no_leidos_horas=48)
        self.assertIsNone(r["vista_no_leidos"]["antiguos_sin_leer"])
        self.assertNotIn("Además hay", r["linea_vista"])


class TestVistaNoLeidosHilosRespondidos(BaseVista):
    def _commit(self, plan, ahora, respondidos):
        clasifs = {n["token"]: self.clasif() for n in plan["nuevos_sin_clasificar"]}
        with patch.object(plugin, "_estado_no_leidos", return_value=(None, respondidos)):
            return self.llamar(
                CRON, accion="commit", clasificaciones=clasifs, ahora=ahora, marcar_leidos=False, mode="hourly"
            )

    def test_un_no_leido_con_envio_posterior_en_el_hilo_sigue_en_el_informe(self):
        self.bandeja.agregar("RE1", "2026-11-12T10:00:00Z", "RE: Contrato proveedor")
        self.bandeja.agregar("M2", "2026-11-12T11:00:00Z", "Reunión comité")
        v = self.llamar(CRON, accion="ventana", mode="hourly", ahora=T0, vista_no_leidos_horas=48)
        plan = self.llamar(CRON, accion="plan", since=v["since"], ahora=T0, vista_no_leidos_horas=48)
        ids = {n["id"]: n["token"] for n in plan["nuevos_sin_clasificar"]}
        r = self._commit(plan, T0, {"RE1"})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["vista_no_leidos"]["nuevos"], 2)
        self.assertIn("RE: Contrato proveedor", json.dumps(r["tablas_md"], ensure_ascii=False))
        self.assertEqual(len(ids), 2)

    def test_control_sin_vista_el_respondido_sigue_saliendo_de_pendientes(self):
        self.bandeja.agregar("RE1", "2026-11-12T10:00:00Z", "RE: Contrato proveedor")
        v = self.llamar(CRON, accion="ventana", mode="hourly", ahora=T0)
        plan = self.llamar(CRON, accion="plan", since=v["since"], ahora=T0)
        r = self._commit(plan, T0, {"RE1"})
        self.assertTrue(r["success"], r)
        self.assertNotIn("RE: Contrato proveedor", json.dumps(r.get("tablas_md") or {}, ensure_ascii=False))
