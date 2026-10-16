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
spec = importlib.util.spec_from_file_location("ms365_mail_estado_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

AHORA = "2026-10-15T11:00:00Z"


def _correo(identificador, nivel, recibido, asunto=None, remitente="Ana"):
    return {
        "id": identificador,
        "asunto": asunto or f"Asunto {identificador}",
        "remitente": remitente,
        "recibido": recibido,
        "nivel": nivel,
    }


class TestEstadoInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)
        self.ruta = Path(self.tmp.name) / "work" / "cron_inbox_estado.json"

    def _llamar(self, **args):
        args.setdefault("ahora", AHORA)
        args.setdefault("verificar_leidos", False)
        return json.loads(plugin._inbox_estado_tool(args))

    def _estado(self):
        return json.loads(self.ruta.read_text(encoding="utf-8"))

    def test_only_hot_mail_older_than_two_hours_is_critical(self):
        out = self._llamar(
            nuevos=[
                _correo("a", "CALIENTE", "2026-10-15T07:00:00Z"),
                _correo("b", "CALIENTE", "2026-10-15T10:30:00Z"),
                _correo("c", "TIBIO", "2026-10-15T06:00:00Z"),
                _correo("d", "FRIO", "2026-10-15T06:00:00Z"),
            ]
        )
        self.assertTrue(out["success"], out)
        self.assertEqual([c["asunto"] for c in out["criticos"]], ["Asunto a"])
        self.assertEqual([c["asunto"] for c in out["calientes"]], ["Asunto b"])
        self.assertEqual(out["totales"], {"pendientes": 4, "criticos": 1, "calientes": 1, "tibios": 1, "frios": 1})
        self.assertEqual(len(out["nuevos_este_run"]), 4)

    def test_cold_mail_is_dropped_on_the_next_run_and_unchanged_ones_are_only_counted(self):
        self._llamar(
            nuevos=[_correo("f", "FRIO", "2026-10-15T10:00:00Z"), _correo("t", "TIBIO", "2026-10-15T10:00:00Z")]
        )
        out = self._llamar(ahora="2026-10-15T12:00:00Z")
        self.assertEqual(out["descartados_frio_ya_reportados"], 1)
        self.assertEqual(out["totales"]["pendientes"], 1)
        self.assertEqual(out["pendientes_sin_cambios"], 1)
        self.assertEqual(out["nuevos_este_run"], [])

    def test_old_items_move_to_a_summary_and_keep_the_file_small(self):
        nuevos = [_correo(f"v{i}", "TIBIO", "2026-10-13T08:00:00Z", remitente="Rosa") for i in range(30)]
        nuevos += [_correo(f"o{i}", "CALIENTE", "2026-10-10T08:00:00Z", remitente="Luis") for i in range(40)]
        nuevos += [_correo("ok", "CALIENTE", "2026-10-15T10:55:00Z")]
        out = self._llamar(nuevos=nuevos)
        self.assertEqual(out["backlog_antiguo"]["count"], 70)
        self.assertEqual(out["backlog_antiguo"]["remitentes"], {"Rosa": 30, "Luis": 40} | {})
        self.assertEqual(out["totales"]["pendientes"], 1)
        self.assertEqual(len(self._estado()["pending"]), 1)
        self.assertLess(out["archivo_kb"], 5)

    def test_only_the_ten_most_recent_criticals_are_listed_and_the_rest_summarised(self):
        nuevos = [
            _correo(f"c{i:02d}", "CALIENTE", f"2026-10-14T{i % 24:02d}:00:00Z", remitente="Ana" if i % 2 else "Beto")
            for i in range(15)
        ]
        out = self._llamar(nuevos=nuevos)
        self.assertEqual(len(out["criticos"]), 10)
        self.assertEqual(out["criticos_mas_antiguos"]["cantidad"], 5)
        self.assertEqual(out["totales"]["criticos"], 15)
        self.assertNotIn("detalle_criticos_antiguos", out)
        primero = self._llamar(primer_informe=True)
        self.assertEqual(len(primero["detalle_criticos_antiguos"]), 5)

    def test_resolved_mail_leaves_the_pending_list(self):
        self._llamar(
            nuevos=[_correo("a", "CALIENTE", "2026-10-15T10:00:00Z"), _correo("b", "CALIENTE", "2026-10-15T10:10:00Z")]
        )
        out = self._llamar(resueltos=["a"])
        self.assertEqual([c["asunto"] for c in out["calientes"]], ["Asunto b"])

    def test_the_old_file_format_is_read_and_nothing_is_lost(self):
        self.ruta.parent.mkdir(parents=True)
        self.ruta.write_text(
            json.dumps(
                {
                    "last_run": "2026-10-14T22:00:02Z",
                    "pending": [
                        {
                            "key": "k1",
                            "subject": "Reunión DSS",
                            "from": "Ignacio Lara",
                            "first_seen": "2026-10-15T07:00:00Z",
                            "level": "CRITICO",
                        },
                        {
                            "id": "k2",
                            "subject": "Newsletter",
                            "sender": "Kaleris",
                            "first_seen": "2026-10-15T09:00:00Z",
                            "level": "FRIO",
                        },
                    ],
                    "dismissed": list(range(40)),
                }
            ),
            encoding="utf-8",
        )
        out = self._llamar()
        self.assertEqual([c["asunto"] for c in out["criticos"]], ["Reunión DSS"])
        self.assertEqual(out["descartados_frio_ya_reportados"], 1)
        guardado = self._estado()
        self.assertEqual(guardado["pending"][0]["level"], "CALIENTE")
        self.assertEqual(len(guardado["dismissed"]), 20)

    def test_running_it_twice_gives_the_same_state(self):
        nuevos = [_correo("a", "CALIENTE", "2026-10-15T07:00:00Z"), _correo("t", "TIBIO", "2026-10-15T09:00:00Z")]
        self._llamar(nuevos=nuevos)
        primero = self._estado()
        self._llamar(nuevos=nuevos)
        self.assertEqual(self._estado()["pending"], primero["pending"])

    def test_the_window_is_the_last_hour_on_weekdays_and_since_the_last_run_on_weekends(self):
        laboral = json.loads(plugin._inbox_estado_tool({"accion": "ventana", "ahora": "2026-10-15T14:00:03Z"}))
        self.assertEqual((laboral["since"], laboral["count"]), ("2026-10-15T13:00:00Z", 15))
        self._llamar(nuevos=[_correo("a", "TIBIO", "2026-10-17T14:30:00Z")], ahora="2026-10-17T14:00:03Z")
        domingo = json.loads(plugin._inbox_estado_tool({"accion": "ventana", "ahora": "2026-10-17T21:00:03Z"}))
        self.assertEqual((domingo["since"], domingo["count"]), ("2026-10-17T14:00:03Z", 20))
        self.assertEqual(domingo["pendientes_guardados"], 1)
        self.ruta.unlink()
        sin_estado = json.loads(plugin._inbox_estado_tool({"accion": "ventana", "ahora": "2026-10-17T21:00:03Z"}))
        self.assertEqual(sin_estado["since"], "2026-10-17T03:00:00Z")

    def test_bad_input_is_reported_not_raised(self):
        out = self._llamar(nuevos=["texto suelto", {"sin": "datos"}, _correo("ok", "TIBIO", "2026-10-15T10:00:00Z")])
        self.assertTrue(out["success"])
        self.assertEqual(out["totales"]["pendientes"], 1)


class TestLeidosConsultados(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _llamar(self, no_leidos, **args):
        with (
            patch.object(plugin, "_task_user_id", return_value="u1"),
            patch.object(plugin, "_get_m365_token", return_value=("tok", None)),
            patch.object(plugin, "_graph", return_value={"value": [{"id": i} for i in no_leidos]}),
        ):
            return json.loads(plugin._inbox_estado_tool({"ahora": AHORA, **args}))

    def test_mail_already_read_in_the_inbox_leaves_the_pending_list(self):
        largo_a, largo_b = "A" * 80, "B" * 80
        self._llamar(
            [largo_a, largo_b],
            nuevos=[
                _correo(largo_a, "CALIENTE", "2026-10-15T10:00:00Z"),
                _correo(largo_b, "CALIENTE", "2026-10-15T10:10:00Z"),
            ],
        )
        out = self._llamar([largo_b])
        self.assertEqual([c["asunto"] for c in out["calientes"]], [f"Asunto {largo_b}"])

    def test_short_legacy_keys_and_unavailable_inbox_never_remove_anything(self):
        self._llamar([], nuevos=[_correo("clave-corta", "CALIENTE", "2026-10-15T10:00:00Z")])
        self.assertEqual(self._llamar([])["totales"]["pendientes"], 1)
        with patch.object(plugin, "_task_user_id", return_value=None):
            out = json.loads(plugin._inbox_estado_tool({"ahora": AHORA}))
        self.assertEqual(out["totales"]["pendientes"], 1)
