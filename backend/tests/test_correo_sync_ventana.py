import importlib.util
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_correo_sync_ventana_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

CRON_HORARIO = "02978740-0000-4000-8000-000000000001"
CRON_DIARIO = "7dd867c2-0000-4000-8000-000000000002"
CRON_FINDE = "067745a6-0000-4000-8000-000000000003"


def iso(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def z(fecha):
    return fecha.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BandejaFalsa:
    def __init__(self, como_broker=False):
        self.mensajes = []
        self.llamadas = []
        self.como_broker = como_broker
        self.sin_leer_total = None

    def agregar(self, ident, recibido, asunto=None, imid=None):
        self.mensajes.append(
            {
                "id": ident,
                "internetMessageId": imid if imid is not None else f"<{ident}@ejemplo.cl>",
                "subject": asunto or f"Asunto {ident}",
                "from": {"emailAddress": {"name": "Ana", "address": "ana@ejemplo.cl"}},
                "receivedDateTime": recibido,
                "bodyPreview": "hola",
                "isRead": False,
                "toRecipients": [{"emailAddress": {"address": "bruno@ejemplo.cl"}}],
                "ccRecipients": [],
            }
        )

    def __call__(self, metodo, ruta, token, *a, **k):
        self.llamadas.append((metodo, ruta))
        if metodo != "GET":
            return {}
        if ruta.startswith("/me?"):
            return {"mail": "bruno@ejemplo.cl", "userPrincipalName": "bruno@ejemplo.cl"}
        if ruta.startswith("/me/mailFolders/inbox?$select=unreadItemCount") and self.sin_leer_total is not None:
            return {"unreadItemCount": self.sin_leer_total}
        if "/mailFolders/inbox/messages" not in ruta or "isRead%20eq%20false" in ruta:
            return {"value": []}
        sin_base = ruta.split("/v1.0", 1)[1] if ruta.startswith("https://") else ruta
        q = urllib.parse.parse_qs(sin_base.split("?", 1)[1])
        filtro = (q.get("$filter") or [""])[0]
        desde = filtro.split("ge ", 1)[1].strip() if "ge " in filtro else "0000"
        top = int((q.get("$top") or ["10"])[0])
        skip = 0 if self.como_broker else int((q.get("$skip") or ["0"])[0])
        orden = (q.get("$orderby") or ["receivedDateTime desc"])[0]
        descendente = self.como_broker or orden.endswith("desc")
        filas = sorted(
            (m for m in self.mensajes if m["receivedDateTime"] >= desde),
            key=lambda m: m["receivedDateTime"],
            reverse=descendente,
        )
        pagina = [dict(m) for m in filas[skip : skip + top]]
        salida = {"value": pagina}
        if skip + top < len(filas):
            base = {k: v[0] for k, v in q.items() if k != "$skip"}
            base["$skip"] = str(skip + top)
            salida["@odata.nextLink"] = (
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?" + urllib.parse.urlencode(base)
            )
        return salida

    def filtros(self):
        return [r for m, r in self.llamadas if "/mailFolders/inbox/messages" in r and "isRead%20eq%20false" not in r]


class BaseVentana(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name, "HERMESHQ_APROBACION_CENTRAL": ""})
        self._env.start()
        self.ruta = Path(self._tmp.name) / "work" / "correo_unificado_estado.json"
        self.bandeja = BandejaFalsa()
        self._parches = [
            patch.object(plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(plugin, "_graph", side_effect=lambda *a, **k: self.bandeja(*a, **k)),
        ]
        for p in self._parches:
            p.start()

    def tearDown(self):
        for p in self._parches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    def estado(self):
        return json.loads(self.ruta.read_text(encoding="utf-8"))

    def llamar(self, cron, **kw):
        meta = {"thread_user_id": "u1"}
        if cron:
            meta.update({"scheduled_task_id": cron, "scheduled": True})
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t1", "metadata": meta})}):
            return json.loads(plugin._correo_sync_tool(kw))

    def clasif(self, **extra):
        base = {
            "display_category": "CALIENTE",
            "base_temperature": "CALIENTE",
            "operational_status": "PENDIENTE_DE_MÍ",
            "action_required": "Revisar",
            "requires_response": True,
        }
        return {**base, **extra}

    def corrida(self, cron, ahora, clasificar=True, mode="hourly", commit=True, **extra):
        v = self.llamar(cron, accion="ventana", mode=mode, ahora=ahora, **extra)
        plan = self.llamar(cron, accion="plan", since=v["since"], ahora=ahora, **extra)
        self.assertTrue(plan["success"], plan)
        resultado = None
        if commit:
            clasifs = {n["token"]: self.clasif() for n in plan["nuevos_sin_clasificar"]} if clasificar else {}
            resultado = self.llamar(
                cron,
                accion="commit",
                clasificaciones=clasifs,
                ahora=ahora,
                verificar_leidos=False,
                marcar_leidos=False,
                mode=mode,
            )
            self.assertTrue(resultado["success"], resultado)
        return v, plan, resultado

    def ids_plan(self, plan):
        return [n["id"] for n in plan["nuevos_sin_clasificar"]]


class TestVentanaPorCron(BaseVentana):
    def test_d1_corrida_sin_commit_no_pierde_su_hora(self):
        self.bandeja.agregar("M1", "2026-11-09T12:30:00Z")
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.bandeja.agregar("M2", "2026-11-09T13:20:00Z")
        self.corrida(CRON_HORARIO, "2026-11-09T14:00:00Z", commit=False)
        self.bandeja.agregar("M3", "2026-11-09T14:30:00Z")
        v, plan, _ = self.corrida(CRON_HORARIO, "2026-11-09T15:00:00Z")
        self.assertEqual(sorted(self.ids_plan(plan)), ["M2", "M3"])
        self.assertLessEqual(v["since"], "2026-11-09T13:00:00Z")

    def test_d1_marca_solo_avanza_en_commit_y_respeta_solapamiento(self):
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.corrida(CRON_HORARIO, "2026-11-09T14:00:00Z", commit=False)
        v = self.llamar(CRON_HORARIO, accion="ventana", ahora="2026-11-09T15:00:00Z", solapamiento_min=5)
        self.assertEqual(v["since"], "2026-11-09T12:55:00Z")
        self.assertEqual(self.estado()["marcas"][f"cron:{CRON_HORARIO}"]["hasta"], "2026-11-09T13:00:00Z")

    def test_d2_diario_usa_su_propia_marca_y_cubre_la_noche(self):
        self.corrida(CRON_HORARIO, "2026-11-09T22:00:00Z")
        self.corrida(CRON_DIARIO, "2026-11-09T22:00:00Z", mode="daily")
        self.bandeja.agregar("NOCHE", "2026-11-09T23:30:00Z")
        self.bandeja.agregar("MADRUGADA", "2026-11-10T10:00:00Z")
        vistos = []
        for hora in range(11, 23):
            _, plan, _ = self.corrida(CRON_HORARIO, f"2026-11-10T{hora:02d}:00:00Z")
            vistos += self.ids_plan(plan)
        v, plan, _ = self.corrida(CRON_DIARIO, "2026-11-10T22:00:00Z", mode="daily")
        vistos += self.ids_plan(plan)
        self.assertEqual(sorted(vistos), ["MADRUGADA", "NOCHE"])
        self.assertEqual(v["since"], "2026-11-09T21:50:00Z")

    def test_d2_fin_de_semana_cubre_el_viernes_en_la_noche(self):
        self.corrida(CRON_HORARIO, "2026-11-06T22:00:00Z")
        self.bandeja.agregar("VIERNES-NOCHE", "2026-11-06T23:30:00Z")
        _, plan, _ = self.corrida(CRON_FINDE, "2026-11-07T14:00:00Z")
        self.assertIn("VIERNES-NOCHE", self.ids_plan(plan))

    def test_ventana_horas_limita_el_hueco_y_lo_declara(self):
        self.corrida(CRON_HORARIO, "2026-11-02T13:00:00Z")
        v = self.llamar(CRON_HORARIO, accion="ventana", ahora="2026-11-09T13:00:00Z", ventana_horas=24)
        self.assertEqual(v["since"], "2026-11-08T13:00:00Z")
        self.assertTrue(v["recortada"])
        self.assertIn("24 h", v["modo"])

    def test_sin_marca_mira_hacia_atras_48_horas(self):
        v = self.llamar(CRON_HORARIO, accion="ventana", ahora="2026-11-09T13:00:00Z")
        self.assertEqual(v["since"], "2026-11-07T13:00:00Z")

    def test_manual_y_cron_tienen_marcas_separadas(self):
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.corrida(None, "2026-11-09T14:00:00Z")
        marcas = self.estado()["marcas"]
        self.assertEqual(marcas[f"cron:{CRON_HORARIO}"]["hasta"], "2026-11-09T13:00:00Z")
        self.assertEqual(marcas["manual"]["hasta"], "2026-11-09T14:00:00Z")


class TestClasificacionIncompleta(BaseVentana):
    def test_d3_commit_vacio_deja_pendientes_que_reaparecen(self):
        for i in range(3):
            self.bandeja.agregar(f"P{i}", f"2026-11-09T12:{10 + i}:00Z")
        _, _, c = self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z", clasificar=False)
        self.assertEqual(c["pendientes_de_clasificar"], 3)
        self.assertIn("no se clasificaron", c["aviso"])
        _, plan, c2 = self.corrida(CRON_HORARIO, "2026-11-09T15:00:00Z")
        self.assertEqual(sorted(self.ids_plan(plan)), ["P0", "P1", "P2"])
        self.assertEqual(c2["pendientes_de_clasificar"], 0)
        self.assertEqual(sum(c2["totales_nuevos"].values()), 3)

    def test_d3_clasificacion_parcial_conserva_solo_lo_no_clasificado(self):
        for i in range(4):
            self.bandeja.agregar(f"Q{i}", f"2026-11-09T12:{10 + i}:00Z")
        self.llamar(CRON_HORARIO, accion="ventana", ahora="2026-11-09T13:00:00Z")
        plan = self.llamar(CRON_HORARIO, accion="plan", ahora="2026-11-09T13:00:00Z")
        primeros = {n["token"]: self.clasif() for n in plan["nuevos_sin_clasificar"][:2]}
        c = self.llamar(
            CRON_HORARIO,
            accion="commit",
            clasificaciones=primeros,
            ahora="2026-11-09T13:00:00Z",
            verificar_leidos=False,
            marcar_leidos=False,
        )
        self.assertEqual(c["pendientes_de_clasificar"], 2)
        _, plan2, _ = self.corrida(CRON_HORARIO, "2026-11-09T14:00:00Z")
        self.assertEqual(sorted(self.ids_plan(plan2)), ["Q2", "Q3"])


class TestTopeDeTamano(BaseVentana):
    def test_d4_tope_poda_resueltos_antiguos_y_no_suprime_los_nuevos(self):
        resueltos = []
        for i in range(700):
            fecha = z(iso("2026-11-04T00:00:00Z") + timedelta(minutes=10 * i))
            resueltos.append(
                {
                    "display_id": f"#{i + 1:03d}",
                    "message_id": f"R{i:04d}" + "x" * 60,
                    "subject": f"Viejo {i} " + "y" * 40,
                    "sender_name": "Z",
                    "relationship": "Para mí",
                    "display_category": "TIBIO",
                    "base_temperature": "TIBIO",
                    "operational_status": "SIN_ACCIÓN",
                    "action_type": "",
                    "action_required": "",
                    "requires_response": False,
                    "due_status": "SIN_PLAZO",
                    "due_date": None,
                    "first_detected": fecha,
                    "last_reviewed": fecha,
                    "resolved_at": fecha,
                    "resolution_type": "mostrado",
                }
            )
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(
            json.dumps({"version": 5, "pending": [], "resolved": resueltos, "discarded": [], "next_display_id": 701}),
            encoding="utf-8",
        )
        self.assertGreater(self.ruta.stat().st_size, 80 * 1024)
        for i in range(5):
            self.bandeja.agregar(f"T{i}", f"2026-11-09T12:{10 + i}:00Z")
        v = self.llamar(CRON_HORARIO, accion="ventana", ahora="2026-11-09T13:00:00Z")
        plan = self.llamar(CRON_HORARIO, accion="plan", since=v["since"], ahora="2026-11-09T13:00:00Z")
        tibio = {
            "display_category": "TIBIO",
            "base_temperature": "TIBIO",
            "operational_status": "SIN_ACCIÓN",
            "requires_response": False,
        }
        c = self.llamar(
            CRON_HORARIO,
            accion="commit",
            ahora="2026-11-09T13:00:00Z",
            verificar_leidos=False,
            marcar_leidos=False,
            clasificaciones={n["token"]: tibio for n in plan["nuevos_sin_clasificar"]},
        )
        self.assertEqual(c["totales_nuevos"]["tibio"], 5)
        self.assertEqual(len(c["tablas_md"]["TIBIO"].splitlines()), 7)
        st = self.estado()
        secciones = {k: st.get(k) for k in ("pending", "resolved", "discarded", "history")}
        self.assertLessEqual(len(json.dumps(secciones, ensure_ascii=False, separators=(",", ":")).encode()), 80 * 1024)
        fechas = sorted(r["resolved_at"] for r in st["resolved"])
        self.assertGreater(fechas[0], "2026-11-04T00:00:00Z")
        self.assertEqual(sum(1 for r in st["resolved"] if r["resolved_at"] == "2026-11-09T13:00:00Z"), 5)
        self.assertEqual(st["frios_consolidados"]["count"], 0)


class TestPaginacion(BaseVentana):
    def _cargar(self, n, base="2026-11-09T10:00:00Z"):
        for i in range(n):
            self.bandeja.agregar(f"G{i:03d}", z(iso(base) + timedelta(minutes=i)))

    def test_d6_sigue_nextlink_y_nada_se_pierde_tras_el_tope_de_40(self):
        self._cargar(120)
        vistos = []
        for hora in ("13", "14", "15", "16"):
            _, plan, c = self.corrida(CRON_HORARIO, f"2026-11-09T{hora}:00:00Z")
            vistos += self.ids_plan(plan)
        self.assertEqual(len(vistos), 120)
        self.assertEqual(len(set(vistos)), 120)
        self.assertEqual(c["pendientes_de_clasificar"], 0)

    def test_d6_tope_de_paginas_avisa_y_la_marca_avanza_solo_hasta_lo_leido(self):
        self._cargar(120)
        _, plan, c = self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z", max_paginas=1)
        self.assertFalse(c["ventana_leida"]["completa"])
        self.assertIn("más correos", c["aviso"])
        self.assertEqual(self.estado()["marcas"][f"cron:{CRON_HORARIO}"]["hasta"], "2026-11-09T10:49:00Z")
        vistos = self.ids_plan(plan)
        for hora in ("14", "15", "16", "17"):
            _, plan, _ = self.corrida(CRON_HORARIO, f"2026-11-09T{hora}:00:00Z", max_paginas=1)
            vistos += self.ids_plan(plan)
        self.assertEqual(len(set(vistos)), 120)
        self.assertEqual(len(vistos), 120)

    def test_d6_broker_sin_paginacion_no_entra_en_bucle_ni_avanza_la_marca(self):
        self.bandeja.como_broker = True
        self.corrida(CRON_HORARIO, "2026-11-09T09:00:00Z")
        self._cargar(80)
        _, plan, c = self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.assertLessEqual(len(self.bandeja.filtros()), 1 + plugin._CU_PAGINAS_MAX)
        self.assertFalse(c["ventana_leida"]["completa"])
        self.assertEqual(self.estado()["marcas"][f"cron:{CRON_HORARIO}"]["hasta"], "2026-11-09T09:00:00Z")
        self.assertIn("más correos", c["aviso"])

    def test_d6_la_consulta_pide_orden_ascendente_e_internet_message_id(self):
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        ruta = self.bandeja.filtros()[0]
        self.assertIn("internetMessageId", ruta)
        self.assertIn("$orderby=receivedDateTime%20asc", ruta)


class TestIdentidad(BaseVentana):
    def test_d8_mismo_internet_message_id_con_otro_id_graph_no_reaparece(self):
        self.bandeja.agregar("ID-ORIGINAL", "2026-11-09T12:30:00Z", imid="<unico@ejemplo.cl>")
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.bandeja.mensajes.clear()
        self.bandeja.agregar("ID-MOVIDO", "2026-11-09T12:30:00Z", imid="<UNICO@ejemplo.cl>")
        plan = self.llamar(CRON_DIARIO, accion="plan", since="2026-11-09T12:00:00Z", ahora="2026-11-09T22:00:00Z")
        self.assertTrue(plan["success"], plan)
        self.assertEqual(self.ids_plan(plan), [])

    def test_d8_entregado_hace_10_dias_no_reaparece_tras_la_purga(self):
        self.bandeja.agregar("ANTIGUO", "2026-10-30T12:30:00Z")
        self.corrida(CRON_HORARIO, "2026-10-30T13:00:00Z")
        self.corrida(CRON_HORARIO, "2026-11-07T13:00:00Z")
        self.assertFalse(any(r.get("message_id") == "ANTIGUO" for r in self.estado()["resolved"]))
        plan = self.llamar(
            CRON_HORARIO, accion="plan", since="2026-10-30T12:00:00Z", ventana_horas=250, ahora="2026-11-09T13:00:00Z"
        )
        self.assertEqual(self.ids_plan(plan), [])

    def test_migracion_estado_v5_existente_no_reentrega_lo_mostrado(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(
            json.dumps(
                {
                    "version": 5,
                    "pending": [],
                    "discarded": [],
                    "next_display_id": 8,
                    "last_run": "2026-11-09T12:00:00Z",
                    "resolved": [
                        {
                            "display_id": "#07",
                            "message_id": "LEGADO",
                            "subject": "Asunto LEGADO",
                            "resolved_at": "2026-11-09T12:00:00Z",
                            "resolution_type": "mostrado",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.bandeja.agregar("LEGADO", "2026-11-09T11:30:00Z")
        self.bandeja.agregar("NUEVO", "2026-11-09T12:40:00Z")
        _, plan, c = self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.assertEqual(self.ids_plan(plan), ["NUEVO"])
        st = self.estado()
        self.assertEqual(st["version"], 5)
        self.assertIn("#08", [r["display_id"] for r in st["resolved"]])


class TestInformeYEscritura(BaseVentana):
    def test_d9_el_informe_declara_la_ventana_realmente_leida(self):
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        _, _, c = self.corrida(CRON_HORARIO, "2026-11-09T15:00:00Z")
        leida = c["ventana_leida"]
        self.assertEqual(leida["desde"], "2026-11-09T12:50:00Z")
        self.assertEqual(leida["hasta"], "2026-11-09T15:00:00Z")
        self.assertTrue(leida["completa"])
        self.assertIn("receivedDateTime%20ge%202026-11-09T12:50:00Z", self.bandeja.filtros()[-1])

    def test_el_informe_declara_cuantos_correos_leyo_cuantos_sin_leer_y_el_ultimo_recibido(self):
        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        self.bandeja.agregar("UNO", "2026-11-09T14:10:00Z")
        self.bandeja.agregar("DOS", "2026-11-09T14:40:00Z")
        _, _, c = self.corrida(CRON_HORARIO, "2026-11-09T15:00:00Z")
        leida = c["ventana_leida"]
        self.assertEqual(leida["mensajes_leidos"], 2)
        self.assertEqual(leida["sin_leer_en_la_lectura"], 2)
        self.assertEqual(leida["ultimo_recibido"], "2026-11-09T14:40:00Z")

    def test_d11_escritura_no_deja_el_estado_ausente_si_falla_el_temporal(self):
        plugin._cu_escribir({"version": 5, "pending": [], "marca": 1})
        (self.ruta.parent / f"{self.ruta.name}.tmp").mkdir()
        try:
            plugin._cu_escribir({"version": 5, "pending": [], "marca": 2})
        except OSError:
            pass
        self.assertTrue(self.ruta.is_file())
        self.assertIn(json.loads(self.ruta.read_text(encoding="utf-8"))["marca"], (1, 2))

    def test_d11_estado_corrupto_se_recupera_del_respaldo(self):
        plugin._cu_escribir({"version": 5, "pending": [{"display_id": "#01", "message_id": "K"}]})
        plugin._cu_escribir({"version": 5, "pending": [{"display_id": "#01", "message_id": "K"}]})
        self.ruta.write_text("{corrupto", encoding="utf-8")
        self.assertEqual(plugin._cu_load()["pending"][0]["message_id"], "K")


class TestCompatibilidadBroker(BaseVentana):
    def test_la_ruta_de_lectura_sigue_permitida_por_el_broker(self):
        from hermeshq.services import graph_proxy as gp

        self.corrida(CRON_HORARIO, "2026-11-09T13:00:00Z")
        ruta = self.bandeja.filtros()[0]
        politica = gp.politica_desde_configuracion(
            {"correo": {"operaciones": sorted(gp.OPERACIONES_CORREO), "carpetas": ["inbox", "sentitems"]}}
        )
        with patch.object(gp, "_llamar", return_value=(200, b'{"value": []}')):
            resultado = gp.ejecutar("GET", ruta, None, None, politica, lambda: "t", lambda t: None)
        self.assertEqual(resultado["status"], 200)


if __name__ == "__main__":
    unittest.main()
