import importlib.util
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_correo_sync_ventana import BandejaFalsa  # noqa: E402

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_correo_concurrencia_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

CTX = multiprocessing.get_context("fork")
CRON_A = "aaaaaaaa-0000-4000-8000-00000000000a"
CRON_B = "bbbbbbbb-0000-4000-8000-00000000000b"
PAUSA = 0.5
AHORA = "2026-11-10T13:00:00Z"
MEDICIONES = []


def carga_cron(sid, tid):
    return {"task_id": tid, "metadata": {"thread_user_id": "u1", "scheduled_task_id": sid, "scheduled": True}}


def carga_chat(tid):
    return {"task_id": tid, "metadata": {"thread_user_id": "u1"}}


def clasif(**extra):
    base = {
        "display_category": "CALIENTE",
        "base_temperature": "CALIENTE",
        "operational_status": "PENDIENTE_DE_MÍ",
        "action_required": "Revisar",
        "requires_response": True,
    }
    return {**base, **extra}


def commit_args(tokens, **extra):
    return {
        "accion": "commit",
        "clasificaciones": {t: clasif() for t in tokens},
        "ahora": AHORA,
        "verificar_leidos": False,
        "marcar_leidos": False,
        **extra,
    }


def _hijo(cola, nombre, carga, pasos, pausa):
    os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps(carga)
    os.environ["HQ_PRUEBA_PAUSA"] = str(pausa or 0)
    salida = []
    inicio = time.monotonic()
    for args in pasos:
        salida.append(json.loads(plugin._correo_sync_tool(args)))
    cola.put((nombre, salida, time.monotonic() - inicio))


def _hijo_con_bloqueo(listo, ruta):
    with plugin._cu_bloqueo():
        Path(ruta).write_text("tomado", encoding="utf-8")
        listo.set()
        time.sleep(60)


class BaseConcurrencia(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = patch.dict(
            os.environ,
            {"HERMES_SESSION_CWD": self._tmp.name, "HERMESHQ_APROBACION_CENTRAL": "", "HQ_PRUEBA_PAUSA": "0"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.work = Path(self._tmp.name) / "work"
        self.ruta = self.work / "correo_unificado_estado.json"
        self.bandeja = BandejaFalsa()
        original = plugin._cu_load

        def carga_lenta():
            datos = original()
            pausa = float(os.environ.get("HQ_PRUEBA_PAUSA") or 0)
            if pausa:
                time.sleep(pausa)
            return datos

        for p in (
            patch.object(plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(plugin, "_graph", side_effect=lambda *a, **k: self.bandeja(*a, **k)),
            patch.object(plugin, "_cu_load", side_effect=carga_lenta),
        ):
            p.start()
            self.addCleanup(p.stop)

    def estado(self):
        return json.loads(self.ruta.read_text(encoding="utf-8"))

    def llamar(self, carga, **kw):
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(carga)}):
            return json.loads(plugin._correo_sync_tool(kw))

    def plan(self, carga, **kw):
        r = self.llamar(carga, accion="plan", ahora=AHORA, **kw)
        self.assertTrue(r["success"], r)
        return r

    def en_paralelo(self, *trabajos, medir=None):
        cola = CTX.Queue()
        procesos = [CTX.Process(target=_hijo, args=(cola, n, c, pasos, pausa)) for n, c, pasos, pausa in trabajos]
        inicio = time.monotonic()
        for p in procesos:
            p.start()
        resultados = {}
        for _ in procesos:
            nombre, salida, dur = cola.get(timeout=60)
            resultados[nombre] = (salida, dur)
        for p in procesos:
            p.join(10)
        if medir:
            MEDICIONES.append(
                (medir, round(time.monotonic() - inicio, 3), {n: round(d, 3) for n, (_, d) in resultados.items()})
            )
        return {n: s for n, (s, _) in resultados.items()}

    def mostrados(self):
        return [r for r in self.estado().get("resolved", []) if r.get("resolution_type") == "mostrado"]

    def asuntos_digest(self, digest):
        return [f["asunto"] for f in digest.get("filas_nuevas", []) + digest.get("otros_relevantes", [])]

    def sembrar_pendiente(self, did="#01", mid="K1", asunto="Contrato pendiente"):
        plugin._cu_escribir(
            {
                "version": 5,
                "pending": [
                    {
                        "display_id": did,
                        "message_id": mid,
                        "subject": asunto,
                        "sender_name": "Ana",
                        "display_category": "CALIENTE",
                        "base_temperature": "CALIENTE",
                        "operational_status": "PENDIENTE_DE_MÍ",
                        "requires_response": True,
                        "first_detected": "2026-11-10T12:00:00Z",
                        "last_reviewed": "2026-11-10T12:00:00Z",
                    }
                ],
                "next_display_id": int(did.strip("#")) + 1,
            }
        )


class TestPerdidaDeActualizaciones(BaseConcurrencia):
    def test_commit_del_cron_y_descarte_del_chat_no_se_pisan(self):
        self.sembrar_pendiente()
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        r = self.en_paralelo(
            ("cron", cron, [commit_args(["N1"])], PAUSA),
            (
                "chat",
                carga_chat("tarea-chat"),
                [{"accion": "resolver", "display_ids": ["#01"], "destino": "discarded", "ahora": AHORA}],
                PAUSA,
            ),
            medir="commit cron + descarte chat",
        )
        self.assertTrue(r["cron"][0]["success"], r["cron"])
        self.assertEqual(r["chat"][0]["movidos"], 1)
        estado = self.estado()
        self.assertEqual([d["message_id"] for d in estado["discarded"]], ["K1"])
        self.assertNotIn("K1", [p.get("message_id") for p in estado["pending"]])
        self.assertEqual([m["message_id"] for m in self.mostrados()], ["M1"])

    def test_preferencia_del_usuario_sobrevive_al_commit_del_cron(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        r = self.en_paralelo(
            ("cron", cron, [commit_args(["N1"])], PAUSA),
            (
                "chat",
                carga_chat("tarea-chat"),
                [{"accion": "preferencias", "marcar_leidos_categorias": ["FRÍO"]}],
                PAUSA,
            ),
            medir="commit cron + preferencias chat",
        )
        self.assertTrue(r["chat"][0]["success"], r["chat"])
        estado = self.estado()
        self.assertEqual(estado.get("marcar_leidos_categorias"), ["FRÍO"])
        self.assertTrue(estado.get("marcar_leidos_por_usuario"))
        self.assertEqual([m["message_id"] for m in self.mostrados()], ["M1"])

    def test_dos_crons_con_correos_distintos_no_reusan_display_id(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        ca, cb = carga_cron(CRON_A, "tarea-a"), carga_cron(CRON_B, "tarea-b")
        pa = self.plan(ca, mensajes=[self.bandeja.mensajes[0]])
        self.bandeja.agregar("M2", "2026-11-10T12:40:00Z")
        pb = self.plan(cb, mensajes=[self.bandeja.mensajes[1]])
        self.assertEqual([n["id"] for n in pa["nuevos_sin_clasificar"]], ["M1"])
        self.assertEqual([n["id"] for n in pb["nuevos_sin_clasificar"]], ["M2"])
        r = self.en_paralelo(
            ("a", ca, [commit_args(["N1"])], PAUSA),
            ("b", cb, [commit_args(["N1"])], PAUSA),
            medir="dos commits de crons distintos",
        )
        self.assertEqual(self.asuntos_digest(r["a"][0]), ["Asunto M1"])
        self.assertEqual(self.asuntos_digest(r["b"][0]), ["Asunto M2"])
        mostrados = self.mostrados()
        self.assertEqual(sorted(m["message_id"] for m in mostrados), ["M1", "M2"])
        self.assertEqual(len({m["display_id"] for m in mostrados}), 2, mostrados)
        for m in mostrados:
            det = self.llamar(carga_chat("detalle"), accion="resolver_id", display_id=m["display_id"])
            self.assertEqual(det["message_id"], m["message_id"])

    def test_estres_seis_procesos_mezclando_commits_y_acciones_del_chat(self):
        pendientes = [
            {
                "display_id": f"#{i:02d}",
                "message_id": f"K{i}",
                "subject": f"P{i}",
                "sender_name": "Ana",
                "display_category": "CALIENTE",
                "base_temperature": "CALIENTE",
                "operational_status": "PENDIENTE_DE_MÍ",
                "requires_response": True,
                "first_detected": "2026-11-10T12:00:00Z",
            }
            for i in range(1, 9)
        ]
        plugin._cu_escribir({"version": 5, "pending": pendientes, "next_display_id": 9})
        rev0 = self.estado()["revision"]
        trabajos = []
        for c in range(2):
            mid = f"MC{c}"
            self.bandeja.agregar(mid, f"2026-11-10T12:3{c}:00Z")
            carga = carga_cron(CRON_A if c == 0 else CRON_B, f"cron-{c}")
            self.plan(carga, mensajes=[self.bandeja.mensajes[-1]])
            trabajos.append((f"cron{c}", carga, [commit_args(["N1"])], 0.1))
        for k in range(4):
            pasos = [
                {
                    "accion": "resolver",
                    "display_ids": [f"#{2 * k + j:02d}"],
                    "destino": "discarded" if j == 1 else "resolved",
                    "ahora": AHORA,
                }
                for j in (1, 2)
            ]
            trabajos.append((f"chat{k}", carga_chat(f"chat-{k}"), pasos, 0.1))
        self.en_paralelo(*trabajos, medir="estrés 6 procesos / 10 escrituras")
        estado = self.estado()
        self.assertEqual(estado["pending"], [])
        movidos = sorted(r["message_id"] for r in estado["resolved"] + estado["discarded"])
        self.assertEqual(movidos, sorted([f"K{i}" for i in range(1, 9)] + ["MC0", "MC1"]))
        self.assertEqual(estado["revision"], rev0 + 10)
        ids = [r["display_id"] for r in estado["resolved"] + estado["discarded"]]
        self.assertEqual(len(ids), len(set(ids)))


class TestPlanPorEjecucion(BaseConcurrencia):
    def test_el_plan_del_chat_no_pisa_el_del_cron(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron, chat = carga_cron(CRON_A, "tarea-cron"), carga_chat("tarea-chat")
        self.plan(cron)
        otro = {
            "id": "M9",
            "internetMessageId": "<M9@ejemplo.cl>",
            "subject": "Asunto M9",
            "receivedDateTime": "2026-11-10T12:50:00Z",
        }
        self.plan(chat, mensajes=[otro])
        rc = self.llamar(cron, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(rc), ["Asunto M1"])
        rh = self.llamar(chat, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(rh), ["Asunto M9"])
        self.assertEqual(sorted(m["message_id"] for m in self.mostrados()), ["M1", "M9"])

    def test_commit_sin_plan_propio_no_consume_el_plan_ajeno(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        ajeno = self.llamar(carga_chat("tarea-chat"), **commit_args(["N1"]))
        self.assertFalse(ajeno["success"], ajeno)
        self.assertTrue(ajeno.get("sin_lectura"))
        self.assertFalse(self.ruta.exists() and self.mostrados())
        propio = self.llamar(cron, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(propio), ["Asunto M1"])


class TestCommitSinLectura(BaseConcurrencia):
    def caer_exchange(self):
        self.bandeja = lambda *a, **k: {
            "error": "Microsoft 365 respondió un error interno",
            "_graph_code": "UnknownError",
        }

    def test_plan_fallido_y_commit_no_entrega_informe_en_cero(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.caer_exchange()
        plan = self.llamar(cron, accion="plan", ahora=AHORA)
        self.assertFalse(plan["success"])
        self.assertIn("no pude leer el buzón", plan["error"])
        self.assertIn("UnknownError", plan["error"])
        self.assertIn("sin nombrar herramientas", plan["error"])
        self.assertIn("no las ofrezcas como alternativa", plan["error"])
        r = self.llamar(cron, accion="commit", clasificaciones={}, ahora=AHORA, verificar_leidos=False)
        self.assertFalse(r["success"], r)
        self.assertTrue(r["sin_lectura"])
        self.assertNotIn("tablas_md", r)
        self.assertIn("no se pudo leer el buzón", r["error"])
        self.assertIn("sin nombrar herramientas", r["error"])
        self.assertFalse(self.ruta.exists() and self.estado().get("marcas"))

    def test_commit_sin_plan_no_avanza_la_marca(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-1")
        self.plan(cron)
        self.llamar(cron, **commit_args(["N1"]))
        marca = self.estado()["marcas"]
        siguiente = carga_cron(CRON_A, "tarea-2")
        self.caer_exchange()
        self.llamar(siguiente, accion="plan", ahora="2026-11-10T14:00:00Z")
        r = self.llamar(siguiente, **{**commit_args([]), "ahora": "2026-11-10T14:00:00Z"})
        self.assertFalse(r["success"])
        self.assertEqual(self.estado()["marcas"], marca)

    def test_commit_repetido_devuelve_el_informe_del_primero(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        primero = self.llamar(cron, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(primero), ["Asunto M1"])
        segundo = self.llamar(cron, **commit_args([]))
        self.assertTrue(segundo["success"])
        self.assertTrue(segundo["repetido"])
        self.assertEqual(self.asuntos_digest(segundo), ["Asunto M1"])
        self.assertIn("ya se hizo", segundo["aviso"])
        self.assertEqual([m["message_id"] for m in self.mostrados()], ["M1"])

    def test_plan_nuevo_tras_commit_se_procesa_y_no_repite(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        self.llamar(cron, **commit_args(["N1"]))
        self.bandeja.agregar("M2", "2026-11-10T13:30:00Z")
        self.assertTrue(self.llamar(cron, accion="plan", ahora="2026-11-10T14:00:00Z")["success"])
        r = self.llamar(cron, **{**commit_args(["N1"]), "ahora": "2026-11-10T14:00:00Z"})
        self.assertFalse(r.get("repetido"))
        self.assertEqual(self.asuntos_digest(r), ["Asunto M2"])

    def test_plan_vacio_exitoso_sigue_dando_su_informe(self):
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        r = self.llamar(cron, **commit_args([]))
        self.assertTrue(r["success"], r)
        self.assertFalse(r.get("repetido"))
        self.assertIn("tablas_md", r)

    def test_los_planes_quedan_en_archivos_distintos_por_tarea(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        self.plan(carga_cron(CRON_A, "tarea-a"))
        self.plan(carga_cron(CRON_B, "tarea-b"))
        nombres = sorted(p.name for p in (self.work / ".correo_sync_planes").iterdir())
        self.assertEqual(len(nombres), 2, nombres)
        self.assertFalse((self.work / ".correo_sync_plan.json").exists())


class TestDeduplicacionConcurrente(BaseConcurrencia):
    def test_dos_ejecuciones_simultaneas_no_muestran_el_mismo_correo_dos_veces(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        ca, cb = carga_cron(CRON_A, "tarea-a"), carga_cron(CRON_B, "tarea-b")
        self.plan(ca)
        self.plan(cb)
        r = self.en_paralelo(
            ("a", ca, [commit_args(["N1"])], PAUSA),
            ("b", cb, [commit_args(["N1"])], PAUSA),
            medir="dos commits del mismo correo",
        )
        vistos = self.asuntos_digest(r["a"][0]) + self.asuntos_digest(r["b"][0])
        self.assertEqual(vistos, ["Asunto M1"])
        self.assertEqual([m["message_id"] for m in self.mostrados()], ["M1"])
        perdedor = r["a"][0] if not self.asuntos_digest(r["a"][0]) else r["b"][0]
        self.assertEqual(perdedor.get("ya_entregados_por_otra_ejecucion"), 1)

    def test_internet_message_id_estable_aunque_cambie_el_id_de_graph(self):
        ca, cb = carga_cron(CRON_A, "tarea-a"), carga_cron(CRON_B, "tarea-b")
        m1 = {
            "id": "G-ANTES",
            "internetMessageId": "<unico@ejemplo.cl>",
            "subject": "Movido",
            "receivedDateTime": "2026-11-10T12:30:00Z",
        }
        m2 = {**m1, "id": "G-DESPUES"}
        self.plan(ca, mensajes=[m1])
        self.plan(cb, mensajes=[m2])
        r = self.en_paralelo(("a", ca, [commit_args(["N1"])], PAUSA), ("b", cb, [commit_args(["N1"])], PAUSA))
        vistos = self.asuntos_digest(r["a"][0]) + self.asuntos_digest(r["b"][0])
        self.assertEqual(vistos, ["Movido"])
        self.assertEqual(len(self.mostrados()), 1)


class TestRevision(BaseConcurrencia):
    def test_revision_sube_en_cada_escritura_y_el_commit_avisa_el_cambio(self):
        self.sembrar_pendiente()
        rev0 = self.estado()["revision"]
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        plan = self.plan(cron)
        self.assertEqual(plan["revision_estado"], rev0)
        self.llamar(carga_chat("chat"), accion="resolver", display_ids=["#01"], destino="discarded", ahora=AHORA)
        self.assertEqual(self.estado()["revision"], rev0 + 1)
        r = self.llamar(cron, **commit_args(["N1"]))
        self.assertEqual(self.estado()["revision"], rev0 + 2)
        self.assertEqual(r["revision_estado"], rev0 + 2)
        self.assertIn("cambió", r.get("aviso") or "")
        self.assertEqual([d["message_id"] for d in self.estado()["discarded"]], ["K1"])


class TestReinicio(BaseConcurrencia):
    def test_proceso_muerto_con_el_bloqueo_tomado_lo_suelta(self):
        self.sembrar_pendiente()
        listo = CTX.Event()
        marca = self.work / "hijo.txt"
        hijo = CTX.Process(target=_hijo_con_bloqueo, args=(listo, str(marca)))
        hijo.start()
        self.assertTrue(listo.wait(10))
        with patch.object(plugin, "_CU_BLOQUEO_ESPERA_S", 0.3):
            ocupado = self.llamar(
                carga_chat("chat"), accion="resolver", display_ids=["#01"], destino="discarded", ahora=AHORA
            )
        self.assertFalse(ocupado["success"], ocupado)
        self.assertIn("ocupado", ocupado["error"])
        self.assertEqual(self.estado()["pending"][0]["message_id"], "K1")
        os.kill(hijo.pid, signal.SIGKILL)
        hijo.join(10)
        inicio = time.monotonic()
        libre = self.llamar(
            carga_chat("chat"), accion="resolver", display_ids=["#01"], destino="discarded", ahora=AHORA
        )
        espera = time.monotonic() - inicio
        MEDICIONES.append(("resolver tras SIGKILL con bloqueo tomado", round(espera, 3), {}))
        self.assertTrue(libre["success"], libre)
        self.assertLess(espera, 1.0)
        self.assertEqual(self.estado()["discarded"][0]["message_id"], "K1")

    def test_muerte_a_mitad_del_commit_no_corrompe_ni_bloquea(self):
        self.sembrar_pendiente()
        rev0 = self.estado()["revision"]
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        cron = carga_cron(CRON_A, "tarea-cron")
        self.plan(cron)
        cola = CTX.Queue()
        hijo = CTX.Process(target=_hijo, args=(cola, "cron", cron, [commit_args(["N1"])], 5))
        hijo.start()
        time.sleep(1.0)
        os.kill(hijo.pid, signal.SIGKILL)
        hijo.join(10)
        self.assertEqual(self.estado()["revision"], rev0)
        r = self.llamar(cron, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(r), ["Asunto M1"])
        self.assertEqual(self.estado()["revision"], rev0 + 1)

    def test_plan_huerfano_no_bloquea_ni_duplica_y_se_limpia(self):
        self.bandeja.agregar("M1", "2026-11-10T12:30:00Z")
        ca, cb = carga_cron(CRON_A, "tarea-muerta"), carga_cron(CRON_B, "tarea-viva")
        self.plan(ca)
        self.plan(cb)
        rb = self.llamar(cb, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(rb), ["Asunto M1"])
        ra = self.llamar(ca, **commit_args(["N1"]))
        self.assertEqual(self.asuntos_digest(ra), [])
        self.assertEqual(ra.get("ya_entregados_por_otra_ejecucion"), 1)
        self.assertEqual(len(self.mostrados()), 1)
        self.plan(ca)
        planes = self.work / ".correo_sync_planes"
        viejo = next(planes.iterdir())
        hace = time.time() - 2 * 24 * 3600
        os.utime(viejo, (hace, hace))
        self.plan(carga_chat("otra"))
        self.assertFalse(viejo.exists())


class TestEstadoLegadoYCacheDeIds(BaseConcurrencia):
    def test_inbox_estado_legado_no_pierde_nuevos_en_paralelo(self):
        def hijo(cola, ident):
            os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps(carga_chat(f"t-{ident}"))
            original = plugin._estado_cargar

            def lento(ahora):
                r = original(ahora)
                time.sleep(PAUSA)
                return r

            plugin._estado_cargar = lento
            r = json.loads(
                plugin._inbox_estado_tool(
                    {
                        "nuevos": [{"id": ident, "subject": ident, "level": "CALIENTE"}],
                        "verificar_leidos": False,
                        "ahora": AHORA,
                    }
                )
            )
            cola.put(r)

        cola = CTX.Queue()
        procesos = [CTX.Process(target=hijo, args=(cola, i)) for i in ("X1", "X2")]
        for p in procesos:
            p.start()
        salidas = [cola.get(timeout=30) for _ in procesos]
        for p in procesos:
            p.join(10)
        self.assertTrue(all(s["success"] for s in salidas), salidas)
        estado = json.loads((self.work / "cron_inbox_estado.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p["id"] for p in estado["pending"]), ["X1", "X2"])

    def test_cache_de_ids_no_pierde_ids_en_paralelo(self):
        def hijo(cola, prefijo):
            for i in range(30):
                plugin._remember_ids([{"id": f"{prefijo}{i}"}])
            cola.put(prefijo)

        cola = CTX.Queue()
        procesos = [CTX.Process(target=hijo, args=(cola, p)) for p in ("A", "B", "C")]
        for p in procesos:
            p.start()
        for _ in procesos:
            cola.get(timeout=30)
        for p in procesos:
            p.join(10)
        conocidos = plugin._known_ids()
        self.assertEqual(len(conocidos), 90, sorted(conocidos)[:5])


def tearDownModule():
    if MEDICIONES:
        sys.stderr.write("\nMEDICIONES " + json.dumps(MEDICIONES, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
