import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_correo_sync_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

AHORA = "2026-10-26T18:00:00Z"


class CorreoSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        self._env.start()
        self.ruta = Path(self._tmp.name) / "work" / "correo_unificado_estado.json"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def call(self, **kw):
        return json.loads(plugin._correo_sync_tool(kw))

    def commit_mant(self, ahora, **kw):
        self.call(accion="plan", mensajes=[], ahora=ahora)
        return self.call(accion="commit", clasificaciones={}, ahora=ahora, **kw)

    def state(self):
        return json.loads(self.ruta.read_text(encoding="utf-8"))

    def _sembrar_dos(self):
        msgs = [
            {"id": "A" * 60, "subject": "RE: Proyecto Altia", "sender": "Ana", "received": "2026-10-26T17:30:00Z"},
            {"id": "B" * 60, "subject": "Solicita OC", "sender": "Juan", "received": "2026-10-26T17:40:00Z"},
            {
                "id": "C" * 60,
                "subject": "Resumen diario",
                "sender": "no-reply@outlook.com",
                "received": "2026-10-26T17:45:00Z",
            },
        ]
        plan = self.call(accion="plan", mensajes=msgs, ahora=AHORA)
        tok = {n["subject"]: n["token"] for n in plan["nuevos_sin_clasificar"]}
        clasifs = {
            tok["RE: Proyecto Altia"]: {
                "display_category": "CRÍTICO",
                "base_temperature": "CRÍTICO",
                "relationship": "PARA_MÍ",
                "operational_status": "PENDIENTE_DE_MÍ",
                "action_required": "Responder a Ana",
                "requires_response": True,
            },
            tok["Solicita OC"]: {
                "display_category": "TIBIO",
                "base_temperature": "TIBIO",
                "relationship": "PARA_MÍ",
                "operational_status": "PENDIENTE",
                "action_required": "Emitir OC",
                "requires_response": True,
            },
            tok["Resumen diario"]: {
                "display_category": "FRÍO",
                "base_temperature": "FRÍO",
                "relationship": "EN_COPIA",
                "operational_status": "SIN_ACCIÓN",
                "requires_response": False,
                "ruido_automatico": True,
            },
        }
        return self.call(accion="commit", clasificaciones=clasifs, ahora=AHORA, verificar_leidos=False)

    def _pendiente(self, mid, subject, display_id, **extra):
        base = {
            "display_id": display_id,
            "message_id": mid,
            "subject": subject,
            "sender_name": "X",
            "relationship": "PARA_MÍ",
            "display_category": "CRÍTICO",
            "base_temperature": "CRÍTICO",
            "operational_status": "PENDIENTE_DE_MÍ",
            "action_type": "",
            "action_required": "Revisar",
            "requires_response": True,
            "due_status": "SIN_PLAZO",
            "due_date": None,
            "first_detected": AHORA,
            "last_reviewed": AHORA,
        }
        return {**base, **extra}

    def _sembrar_pendientes(self, pendientes):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        st = {
            "version": 5,
            "pending": pendientes,
            "resolved": [],
            "discarded": [],
            "next_display_id": len(pendientes) + 1,
            "frios_consolidados": {"count": 0},
        }
        self.ruta.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def test_ventana(self):
        v = self.call(accion="ventana", mode="hourly", ahora=AHORA)
        self.assertTrue(v["success"])
        self.assertTrue(v["since"] and v["count"])

    def test_plan_commit_consolida_ruido_y_estabiliza_ids(self):
        c = self._sembrar_dos()
        self.assertTrue(c["success"], c)
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertEqual(len(st["resolved"]), 2)
        self.assertEqual(st["frios_consolidados"]["count"], 1)
        self.assertEqual({p["display_id"] for p in st["resolved"]}, {"#01", "#02"})
        self.assertTrue(all(p["resolution_type"] == "mostrado" for p in st["resolved"]))
        self.assertEqual(st["next_display_id"], 3)
        for p in st["resolved"]:
            self.assertTrue(set(plugin._CU_CAMPOS) <= set(p.keys()))

    def test_idempotencia_no_reinfla(self):
        self._sembrar_dos()
        self.commit_mant("2026-10-26T18:05:00Z", verificar_leidos=False)
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertEqual({p["display_id"] for p in st["resolved"]}, {"#01", "#02"})
        self.assertEqual(len(st["resolved"]), 2)
        self.assertEqual(st["next_display_id"], 3)

    def test_mostrado_no_escala_ni_reaparece(self):
        self._sembrar_dos()
        for ahora in ("2026-10-26T20:50:00Z", "2026-10-26T23:00:00Z"):
            self.commit_mant(ahora, verificar_leidos=False)
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertFalse(
            any(
                p.get("display_category") == "CRÍTICO" and p["subject"].startswith("Solicita OC") for p in st["pending"]
            )
        )
        msgs = [{"id": "B" * 60, "subject": "Solicita OC", "sender": "Juan", "received": "2026-10-26T17:40:00Z"}]
        plan = self.call(accion="plan", mensajes=msgs, ahora="2026-10-26T23:00:00Z")
        self.assertEqual(plan["nuevos_sin_clasificar"], [])

    def test_no_escala_si_no_accionable(self):
        self._sembrar_dos()
        st = self.state()
        st["pending"].append(
            {
                "display_id": "#09",
                "message_id": "D" * 60,
                "subject": "FYI",
                "sender_name": "x",
                "relationship": "EN_COPIA",
                "display_category": "TIBIO",
                "base_temperature": "TIBIO",
                "operational_status": "EN_COPIA",
                "action_type": "",
                "action_required": "",
                "requires_response": False,
                "due_status": "SIN_PLAZO",
                "due_date": None,
                "first_detected": "2026-10-24T00:00:00Z",
                "last_reviewed": "2026-10-24T00:00:00Z",
            }
        )
        self.ruta.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.commit_mant("2026-10-27T00:00:00Z", verificar_leidos=False)
        copia = next(p for p in self.state()["pending"] if p["display_id"] == "#09")
        self.assertEqual(copia["display_category"], "TIBIO")

    def test_dedup_fusiona_peor_caso(self):
        self._sembrar_pendientes(
            [
                self._pendiente("A" * 60, "RE: Proyecto Altia", "#01", display_category="CRÍTICO"),
                self._pendiente(
                    "A" * 60,
                    "RE: Proyecto Altia",
                    "#50",
                    display_category="FRÍO",
                    base_temperature="FRÍO",
                    operational_status="SIN_ACCIÓN",
                    requires_response=False,
                ),
            ]
        )
        self.commit_mant("2026-10-27T01:00:00Z", verificar_leidos=False)
        altia = [p for p in self.state()["pending"] if p["subject"] == "RE: Proyecto Altia"]
        self.assertEqual(len(altia), 1)
        self.assertEqual(altia[0]["display_category"], "CRÍTICO")
        self.assertEqual(altia[0]["display_id"], "#01")

    def test_resolver_id_y_resolver(self):
        self._sembrar_pendientes([self._pendiente("A" * 60, "RE: Proyecto Altia", "#01")])
        r = self.call(accion="resolver_id", display_id="#01")
        self.assertEqual(r["estado_actual"], "pending")
        self.assertEqual(self.call(accion="resolver_id", display_id="#999")["estado_actual"], "desconocido")
        rr = self.call(accion="resolver", display_ids=["#01"], destino="resolved", ahora="2026-10-27T02:00:00Z")
        self.assertEqual(rr["movidos"], 1)
        st = self.state()
        self.assertEqual(st["pending"], [])
        self.assertTrue(any(p["display_id"] == "#01" for p in st["resolved"]))

    def test_auto_resuelve_solo_respondidos(self):
        self._sembrar_pendientes(
            [self._pendiente("A" * 60, "RE: Proyecto Altia", "#01"), self._pendiente("B" * 60, "Solicita OC", "#02")]
        )
        with patch.object(plugin, "_estado_no_leidos", return_value=({"x"}, {"B" * 60})):
            self.commit_mant("2026-10-27T03:00:00Z")
        st = self.state()
        self.assertFalse(any(p["message_id"] == "B" * 60 for p in st["pending"]))
        self.assertTrue(any(p.get("message_id") == "B" * 60 for p in st["resolved"]))
        self.assertTrue(any(p.get("message_id") == "A" * 60 for p in st["pending"]))

    def test_commit_vacio_sin_estado_no_crashea(self):
        c = self.commit_mant(AHORA, verificar_leidos=False)
        self.assertTrue(c["success"], c)
        self.assertEqual(len(self.state()["pending"]), 0)


GUARDA_PATH = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
guarda_spec = importlib.util.spec_from_file_location("documentos_guard_cu_under_test", GUARDA_PATH)
guarda = importlib.util.module_from_spec(guarda_spec)
guarda_spec.loader.exec_module(guarda)


class CorreoSyncHallazgosTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        self._env.start()
        self.ruta = Path(self._tmp.name) / "work" / "correo_unificado_estado.json"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def call(self, **kw):
        return json.loads(plugin._correo_sync_tool(kw))

    def state(self):
        return json.loads(self.ruta.read_text(encoding="utf-8"))

    def _clasif(self, tok, **extra):
        base = {
            "display_category": "CALIENTE",
            "base_temperature": "CALIENTE",
            "relationship": "PARA_MÍ",
            "operational_status": "PENDIENTE_DE_MÍ",
            "action_required": "Revisar",
            "requires_response": True,
        }
        return {**base, **extra}

    def test_display_id_no_colisiona_con_resueltos(self):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(
            json.dumps(
                {
                    "version": 5,
                    "pending": [
                        {
                            "display_id": "#01",
                            "message_id": "P" * 60,
                            "subject": "Pend",
                            "sender_name": "X",
                            "display_category": "CALIENTE",
                            "base_temperature": "CALIENTE",
                            "operational_status": "PENDIENTE",
                            "first_detected": AHORA,
                            "last_reviewed": AHORA,
                        }
                    ],
                    "resolved": [
                        {
                            "display_id": "#03",
                            "message_id": "R" * 60,
                            "subject": "Viejo",
                            "sender_name": "Y",
                            "display_category": "TIBIO",
                            "base_temperature": "TIBIO",
                            "operational_status": "PENDIENTE",
                            "first_detected": AHORA,
                            "last_reviewed": AHORA,
                        }
                    ],
                    "next_display_id": 1,
                }
            ),
            encoding="utf-8",
        )
        msgs = [{"id": "N" * 60, "subject": "Nuevo", "sender": "Ana", "received": "2026-10-26T17:50:00Z"}]
        plan = self.call(accion="plan", mensajes=msgs, ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        self.call(accion="commit", clasificaciones={tok: self._clasif(tok)}, ahora=AHORA, verificar_leidos=False)
        st = self.state()
        ids = [p["display_id"] for sec in ("pending", "resolved", "discarded") for p in st.get(sec, [])]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertIn("#04", ids)

    def test_plan_acepta_wrapper_de_mail_list(self):
        msgs = {
            "success": True,
            "count": 1,
            "messages": [{"id": "W" * 60, "subject": "Wrapper", "sender": "Ana", "received": "2026-10-26T17:55:00Z"}],
        }
        plan = self.call(accion="plan", mensajes=msgs, ahora=AHORA)
        self.assertEqual(len(plan["nuevos_sin_clasificar"]), 1)

    def test_remitente_objeto_graph_se_normaliza(self):
        msgs = [
            {
                "id": "G" * 60,
                "subject": "Graph",
                "received": "2026-10-26T17:56:00Z",
                "from": {"emailAddress": {"name": "Ana Pérez", "address": "ana@ejemplo.cl"}},
            }
        ]
        plan = self.call(accion="plan", mensajes=msgs, ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        self.call(accion="commit", clasificaciones={tok: self._clasif(tok)}, ahora=AHORA, verificar_leidos=False)
        mostrado = next(p for p in self.state()["resolved"] if p["message_id"] == "G" * 60)
        self.assertEqual(mostrado["sender_name"], "Ana Pérez")

    def test_frio_accionable_aparece_en_otros_relevantes(self):
        msgs = [
            {"id": "F" * 60, "subject": "Certificado vence", "sender": "Proveedor", "received": "2026-10-26T17:57:00Z"}
        ]
        plan = self.call(accion="plan", mensajes=msgs, ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        c = self.call(
            accion="commit",
            clasificaciones={
                tok: self._clasif(
                    tok, display_category="FRÍO", base_temperature="FRÍO", operational_status="PENDIENTE_DE_MÍ"
                )
            },
            ahora=AHORA,
            verificar_leidos=False,
        )
        self.assertEqual(len(c["otros_relevantes"]), 1)
        self.assertEqual(c["otros_relevantes"][0]["categoria"], "FRÍO")

    def test_guarda_cubre_terminal_busqueda_y_codigo_con_el_estado(self):
        bloqueados = [
            ("read_file", {"path": "work/correo_unificado_estado.json"}),
            ("terminal", {"command": "cat work/correo_unificado_estado.json"}),
            ("search_files", {"path": "work", "pattern": "correo_unificado*"}),
            ("execute_code", {"code": "import glob; open(glob.glob('work/correo_unif*')[0])"}),
        ]
        for tool, args in bloqueados:
            with self.subTest(tool=tool):
                self.assertIsNotNone(guarda._motivo_bloqueo(tool, args))
        self.assertIsNone(guarda._motivo_bloqueo("read_file", {"path": "work/otro.txt"}))
        self.assertIsNone(guarda._motivo_bloqueo("terminal", {"command": "ls work"}))

    def _nuevo_y_viejo(self, marcar=None):
        viejo = {"id": "V" * 60, "subject": "Pendiente viejo", "sender": "Ana", "received": "2026-10-20T10:00:00Z"}
        self.call(accion="commit", clasificaciones={}, ahora=AHORA, verificar_leidos=False)
        plan_v = self.call(accion="plan", mensajes=[viejo], ahora=AHORA)
        tok_v = plan_v["nuevos_sin_clasificar"][0]["token"]
        self.call(accion="commit", clasificaciones={tok_v: self._clasif(tok_v)}, ahora=AHORA, verificar_leidos=False)
        nuevo = {"id": "N" * 60, "subject": "Correo nuevo", "sender": "Juan", "received": "2026-10-26T17:50:00Z"}
        plan = self.call(accion="plan", mensajes=[viejo, nuevo], ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        clasif = self._clasif(
            tok,
            display_category="TIBIO",
            base_temperature="TIBIO",
            operational_status="SIN_ACCIÓN",
            requires_response=False,
        )
        args = {"accion": "commit", "clasificaciones": {tok: clasif}, "ahora": AHORA, "verificar_leidos": False}
        if marcar is not None:
            args["marcar_leidos"] = marcar
        llamadas = []

        def fake_graph(method, path, token, *a, **k):
            llamadas.append((method, path, k.get("payload", a[0] if a else None)))
            return {}

        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", side_effect=fake_graph),
        ):
            digest = json.loads(plugin._correo_sync_tool(args))
        return digest, llamadas

    def test_commit_marca_leido_solo_los_nuevos_de_la_corrida(self):
        digest, llamadas = self._nuevo_y_viejo(marcar=True)
        patches = [c[1] for c in llamadas if c[0] == "PATCH"]
        self.assertEqual(patches, [f"/me/messages/{'N' * 60}"])
        self.assertEqual(digest["marcados_leidos"], {"marcados": 1, "fallos": 0})

    def test_marcar_leidos_false_no_toca_el_buzon(self):
        digest, llamadas = self._nuevo_y_viejo(marcar=False)
        self.assertFalse([c for c in llamadas if c[0] == "PATCH"])
        self.assertNotIn("marcados_leidos", digest)

    def test_mostrado_se_marca_leido_aunque_pida_accion_y_sale_del_estado(self):
        nuevo = {"id": "N" * 60, "subject": "Firma pendiente", "sender": "Gino", "received": "2026-10-26T17:50:00Z"}
        plan = self.call(accion="plan", mensajes=[nuevo], ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        clasif = self._clasif(tok, display_category="CRÍTICO", base_temperature="CRÍTICO")
        args = {
            "accion": "commit",
            "clasificaciones": {tok: clasif},
            "ahora": AHORA,
            "verificar_leidos": False,
            "marcar_leidos": True,
        }
        llamadas = []
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", side_effect=lambda *a, **k: llamadas.append(a) or {}),
        ):
            digest = json.loads(plugin._correo_sync_tool(args))
        self.assertEqual([c[1] for c in llamadas if c[0] == "PATCH"], [f"/me/messages/{'N' * 60}"])
        self.assertEqual(digest["marcados_leidos"], {"marcados": 1, "fallos": 0})
        self.assertEqual(self.state()["pending"], [])
        self.assertIn("N" * 60, [r.get("message_id") for r in self.state()["resolved"]])

    def test_mostrado_no_reaparece_en_el_siguiente_run(self):
        nuevo = {"id": "N" * 60, "subject": "Firma pendiente", "sender": "Gino", "received": "2026-10-26T17:50:00Z"}
        self.call(accion="commit", clasificaciones={}, ahora=AHORA, verificar_leidos=False)
        plan = self.call(accion="plan", mensajes=[nuevo], ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", return_value={}),
        ):
            self.call(accion="commit", clasificaciones={tok: self._clasif(tok)}, ahora=AHORA, verificar_leidos=False)
        siguiente = self.call(accion="plan", mensajes=[nuevo], ahora=AHORA)
        self.assertEqual(siguiente["nuevos_sin_clasificar"], [])

    def test_digest_separa_nuevos_del_acumulado(self):
        viejo = {"id": "V" * 60, "subject": "Pendiente viejo", "sender": "Ana", "received": "2026-10-20T10:00:00Z"}
        self.call(accion="commit", clasificaciones={}, ahora=AHORA, verificar_leidos=False)
        plan_v = self.call(accion="plan", mensajes=[viejo], ahora=AHORA)
        tok_v = plan_v["nuevos_sin_clasificar"][0]["token"]
        self.call(accion="commit", clasificaciones={tok_v: self._clasif(tok_v)}, ahora=AHORA, verificar_leidos=False)
        nuevo = {"id": "N" * 60, "subject": "Correo nuevo", "sender": "Juan", "received": "2026-10-26T17:50:00Z"}
        plan = self.call(accion="plan", mensajes=[viejo, nuevo], ahora=AHORA)
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        clasif = self._clasif(
            tok,
            display_category="TIBIO",
            base_temperature="TIBIO",
            operational_status="SIN_ACCIÓN",
            requires_response=False,
        )
        digest = self.call(
            accion="commit", clasificaciones={tok: clasif}, ahora=AHORA, verificar_leidos=False, marcar_leidos=False
        )
        self.assertEqual(digest["totales_nuevos"]["tibio"], 1)
        self.assertEqual(digest["totales_nuevos"]["caliente"], 0)
        self.assertEqual(len(digest["filas_nuevas"]), 1)
        self.assertEqual(digest["filas_nuevas"][0]["categoria"], "TIBIO")
        self.assertEqual(
            digest["totales"], {"inmediato": 0, "critico": 0, "caliente": 0, "tibio": 0, "frio": 0, "otros": 0}
        )

    def test_guarda_impide_que_el_agente_reescriba_sus_skills(self):
        skill = "/app/workspaces/agent-x/.hermes/skills/ms365/redactar-correos/SKILL.md"
        bloqueados = [
            ("write_file", {"path": skill, "content": "x"}),
            ("patch", {"path": skill, "old_string": "a", "new_string": "b"}),
            ("execute_code", {"code": f"p = '{skill}'\nwith open(p, 'w') as f:\n    f.write('x')"}),
            ("terminal", {"command": f"sed -i 's/a/b/' {skill}"}),
        ]
        for tool, args in bloqueados:
            with self.subTest(tool=tool):
                self.assertIsNotNone(guarda._motivo_bloqueo(tool, args))

    def test_guarda_permite_leer_skills_y_escribir_fuera_de_ellas(self):
        self.assertIsNone(
            guarda._motivo_bloqueo(
                "execute_code",
                {"code": "p = '/x/.hermes/skills/ms365/SKILL.md'\nprint(open(p, encoding='utf-8').read()[:100])"},
            )
        )
        self.assertIsNone(guarda._motivo_bloqueo("write_file", {"path": "work/reporte.html", "content": "x"}))

    def test_guarda_permite_empaquetar_skills_en_un_zip(self):
        codigo = (
            "import zipfile\n"
            "src = '/app/workspaces/agent-x/work/backup/flujo/composicion-visual/SKILL.md'\n"
            "with zipfile.ZipFile('outputs/skills.zip', 'w') as zf:\n"
            "    zf.write(src, 'composicion-visual/SKILL.md')\n"
        )
        self.assertIsNone(guarda._motivo_bloqueo("execute_code", {"code": codigo}))
        desde_skills = codigo.replace("work/backup/flujo", ".hermes/skills/flujo")
        self.assertIsNone(guarda._motivo_bloqueo("execute_code", {"code": desde_skills}))

    def test_guarda_sigue_bloqueando_escribir_sobre_un_skill_md_aunque_haya_un_zip(self):
        codigo = (
            "import zipfile\n"
            "open('/app/workspaces/agent-x/.hermes/skills/a/SKILL.md', 'w').write('x')\n"
            "zipfile.ZipFile('o.zip', 'w')\n"
        )
        self.assertIsNotNone(guarda._motivo_bloqueo("execute_code", {"code": codigo}))
        self.assertIsNotNone(guarda._motivo_bloqueo("execute_code", {"code": "open('SKILL.md', 'w').write('x')"}))

    def test_vision_analyze_sobre_un_html_se_orienta_a_hq_html_capturar(self):
        motivo = guarda._motivo_bloqueo(
            "vision_analyze", {"image_url": "file:///Users/x/work/informe.html", "question": "?"}
        )
        self.assertIn("hq_html_capturar", motivo)
        self.assertIsNone(
            guarda._motivo_bloqueo("vision_analyze", {"image_url": "outputs/captura.png", "question": "?"})
        )
        self.assertIsNone(
            guarda._motivo_bloqueo("vision_analyze", {"image_url": "https://x.cl/a.jpg?x=1", "question": "?"})
        )


if __name__ == "__main__":
    unittest.main()


class PlanLeeLaBandejaEnServidor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_sin_lista_el_servidor_lee_la_bandeja_y_clasifica_los_nuevos(self):
        bandeja = {
            "value": [
                {
                    "id": "AAA1",
                    "subject": "Pedido de informe",
                    "from": {"emailAddress": {"name": "Ana", "address": "ana@ejemplo.cl"}},
                    "receivedDateTime": "2026-10-29T14:10:00Z",
                    "bodyPreview": "Hola, necesito el informe",
                    "isRead": False,
                }
            ]
        }
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", return_value=bandeja) as graph,
        ):
            r = json.loads(
                plugin._correo_sync_tool(
                    {"accion": "plan", "since": "2026-10-29T14:00:00Z", "ahora": "2026-10-29T15:00:00Z"}
                )
            )
        self.assertTrue(r["success"])
        self.assertEqual(r["nuevos_sin_clasificar"][0]["id"], "AAA1")
        self.assertIn("receivedDateTime%20ge%202026-10-29T14:00:00Z", graph.call_args_list[0].args[1])

    def test_sin_token_informa_error_sin_tocar_el_estado(self):
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=(None, None)),
        ):
            r = json.loads(plugin._correo_sync_tool({"accion": "plan", "ahora": "2026-10-29T15:00:00Z"}))
        self.assertFalse(r["success"])
        self.assertIn("sin token", r["error"])

    def test_con_lista_explicita_sigue_funcionando(self):
        mensajes = [
            {
                "id": "BBB2",
                "subject": "Hola",
                "from": {"emailAddress": {"name": "Luis", "address": "l@x.cl"}},
                "receivedDateTime": "2026-10-29T14:20:00Z",
                "bodyPreview": "x",
                "isRead": False,
            }
        ]
        with patch.object(plugin, "_graph") as graph:
            r = json.loads(
                plugin._correo_sync_tool({"accion": "plan", "mensajes": mensajes, "ahora": "2026-10-29T15:00:00Z"})
            )
        self.assertTrue(r["success"])
        self.assertEqual(r["nuevos_sin_clasificar"][0]["id"], "BBB2")
        graph.assert_not_called()
