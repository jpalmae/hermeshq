import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_lote"
_spec = importlib.util.spec_from_file_location("hermeshq_lote_under_test", PLUGIN / "__init__.py")
lote = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lote)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve() / "agent-aaa"
        (self.dir / "work").mkdir(parents=True)
        (self.dir / ".hermes" / "skills" / "hermeshq-managed" / "informes").mkdir(parents=True)
        (self.dir / ".hermes" / "skills" / "usuario" / "mi-skill").mkdir(parents=True)
        (self.dir / ".hermes" / "skills" / "hermeshq-managed" / "informes" / "SKILL.md").write_text(
            "# Informes\ntexto A"
        )
        (self.dir / ".hermes" / "skills" / "usuario" / "mi-skill" / "SKILL.md").write_text("# Mi skill\ntexto B")
        (self.dir / "work" / "a.txt").write_text("alfa uno\nsegunda\n")
        (self.dir / "work" / "b.txt").write_text("beta dos\nalfa tres\n")
        (self.dir / "work" / "bin.dat").write_bytes(b"\x00\x01\x02")
        (self.dir.parent / "agent-bbb").mkdir()
        (self.dir.parent / "agent-bbb" / "secreto.txt").write_text("CANARIO")
        entorno = patch.dict(
            os.environ, {"HERMES_SESSION_CWD": str(self.dir), "HERMES_HOME": str(self.dir / ".hermes")}
        )
        entorno.start()
        self.addCleanup(entorno.stop)

    @staticmethod
    def _json(texto):
        return json.loads(texto)


class TestReadMany(_Base):
    def test_it_reads_several_files_and_reports_each_problem_on_its_own(self):
        r = self._json(lote._read_many_tool({"paths": ["work/a.txt", "work/b.txt", "work/falta.txt", "work/bin.dat"]}))
        self.assertTrue(r["success"])
        por = {f["path"]: f for f in r["files"]}
        self.assertIn("alfa uno", por["work/a.txt"]["content"])
        self.assertIn("beta dos", por["work/b.txt"]["content"])
        self.assertIn("No existe", por["work/falta.txt"]["error"])
        self.assertIn("binario", por["work/bin.dat"]["error"])

    def test_other_agents_folders_and_long_lists_are_refused(self):
        r = self._json(lote._read_many_tool({"paths": [str(self.dir.parent / "agent-bbb" / "secreto.txt")]}))
        self.assertIn("fuera de tu carpeta", r["files"][0]["error"])
        self.assertNotIn("CANARIO", json.dumps(r))
        largo = self._json(lote._read_many_tool({"paths": [f"work/{i}.txt" for i in range(11)]}))
        self.assertFalse(largo["success"])
        self.assertFalse(self._json(lote._read_many_tool({}))["success"])

    def test_content_is_trimmed_to_max_chars(self):
        (self.dir / "work" / "grande.txt").write_text("x" * 5000)
        r = self._json(lote._read_many_tool({"paths": ["work/grande.txt"], "max_chars": 300}))
        self.assertEqual(len(r["files"][0]["content"]), 300)
        self.assertTrue(r["files"][0]["truncated"])


class TestSearchMany(_Base):
    def test_each_pattern_gets_its_total_and_matches(self):
        r = self._json(lote._search_many_tool({"patterns": ["alfa", "beta", "zzz"], "path": "work", "glob": "*.txt"}))
        por = {x["pattern"]: x for x in r["results"]}
        self.assertEqual(por["alfa"]["total"], 2)
        self.assertEqual(sorted(m["file"] for m in por["alfa"]["matches"]), ["work/a.txt", "work/b.txt"])
        self.assertEqual(por["beta"]["matches"][0]["line"], 1)
        self.assertEqual(por["zzz"]["total"], 0)

    def test_regex_and_scope_are_checked(self):
        r = self._json(lote._search_many_tool({"patterns": ["^beta"], "path": "work", "regex": True}))
        self.assertEqual(r["results"][0]["total"], 1)
        malo = self._json(lote._search_many_tool({"patterns": ["("], "regex": True}))
        self.assertFalse(malo["success"])
        fuera = self._json(lote._search_many_tool({"patterns": ["x"], "path": str(self.dir.parent / "agent-bbb")}))
        self.assertFalse(fuera["success"])


class TestSkills(_Base):
    def test_several_skills_are_loaded_by_name_and_missing_ones_are_listed(self):
        r = self._json(lote._skills_tool({"names": ["informes", "mi-skill", "no-existe"]}))
        self.assertTrue(r["success"])
        self.assertEqual([s["name"] for s in r["skills"]], ["informes", "mi-skill"])
        self.assertIn("texto A", r["skills"][0]["content"])
        self.assertEqual(r["no_encontradas"], ["no-existe"])
        self.assertIn("informes", r["disponibles"])

    def test_nothing_found_is_a_failure(self):
        self.assertFalse(self._json(lote._skills_tool({"names": ["x"]}))["success"])


class TestEditMany(_Base):
    def test_all_edits_are_applied_together(self):
        r = self._json(
            lote._edit_many_tool(
                {
                    "path": "work/a.txt",
                    "edits": [{"old": "alfa uno", "new": "ALFA"}, {"old": "segunda", "new": "SEGUNDA"}],
                }
            )
        )
        self.assertTrue(r["success"])
        self.assertEqual((self.dir / "work" / "a.txt").read_text(), "alfa uno\nsegunda\n")
        self.assertEqual((self.dir / "work" / "a_v2.txt").read_text(), "ALFA\nSEGUNDA\n")
        self.assertTrue(r["original_intacto"])

    def test_if_one_fails_nothing_changes(self):
        antes = (self.dir / "work" / "b.txt").read_text()
        r = self._json(
            lote._edit_many_tool(
                {
                    "path": "work/b.txt",
                    "edits": [
                        {"old": "beta dos", "new": "X"},
                        {"old": "alfa", "new": "Y"},
                        {"old": "nada", "new": "Z"},
                    ],
                }
            )
        )
        self.assertFalse(r["success"])
        self.assertEqual(len(r["problemas"]), 1)
        self.assertIn("Edición 3", r["problemas"][0])
        self.assertEqual((self.dir / "work" / "b.txt").read_text(), antes)

    def test_ambiguous_text_and_foreign_paths_are_refused(self):
        (self.dir / "work" / "c.txt").write_text("igual igual")
        r = self._json(lote._edit_many_tool({"path": "work/c.txt", "edits": [{"old": "igual", "new": "x"}]}))
        self.assertFalse(r["success"])
        self.assertIn("2 veces", r["problemas"][0])
        ajeno = str(self.dir.parent / "agent-bbb" / "secreto.txt")
        self.assertFalse(
            self._json(lote._edit_many_tool({"path": ajeno, "edits": [{"old": "C", "new": "D"}]}))["success"]
        )
        self.assertEqual((self.dir.parent / "agent-bbb" / "secreto.txt").read_text(), "CANARIO")


class TestHtmlCapture(_Base):
    def _html(self, nombre="informe.html"):
        (self.dir / "work" / nombre).write_text(
            "<html><body><h1>Resumen</h1><p>Texto uno</p><div style='page-break-before:always'><h2>Detalle</h2>"
            "<p>Palabra clave aquí</p></div></body></html>",
            encoding="utf-8",
        )

    def test_it_renders_pages_to_images_and_locates_texts(self):
        self._html()
        r = self._json(lote._html_capture_tool({"path": "work/informe.html", "find": ["clave", "inexistente"]}))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["pages_total"], 2)
        self.assertEqual([i["page"] for i in r["images"]], [1, 2])
        for imagen in r["images"]:
            self.assertGreater((self.dir / imagen["path"]).stat().st_size, 100)
        self.assertEqual(r["found_on_pages"], {"clave": [2], "inexistente": []})
        self.assertIn("JavaScript", r["note"])

    def test_it_can_pick_pages_and_refuses_bad_paths(self):
        self._html()
        r = self._json(lote._html_capture_tool({"path": "work/informe.html", "pages": [2, 9]}))
        self.assertEqual([i["page"] for i in r["images"]], [2])
        self.assertFalse(self._json(lote._html_capture_tool({}))["success"])
        self.assertFalse(self._json(lote._html_capture_tool({"path": "work/no-existe.html"}))["success"])
        ajeno = str(self.dir.parent / "agent-bbb" / "secreto.txt")
        self.assertFalse(self._json(lote._html_capture_tool({"path": ajeno}))["success"])


class TestRegistration(unittest.TestCase):
    def test_the_five_tools_are_registered_and_listed_in_the_manifest(self):
        class Ctx:
            def __init__(self):
                self.nombres = []

            def register_tool(self, **kwargs):
                self.nombres.append(kwargs["name"])

        ctx = Ctx()
        lote.register(ctx)
        manifiesto = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        esperadas = ["hq_leer_varios", "hq_buscar_varios", "hq_skills_ver", "hq_editar_varios", "hq_html_capturar"]
        self.assertEqual(sorted(ctx.nombres), sorted(esperadas))
        for nombre in esperadas:
            self.assertIn(nombre, manifiesto)

    def test_the_toolset_is_part_of_the_standard_set(self):
        from hermeshq.services.runtime_profiles import STANDARD_ENABLED_TOOLSETS

        self.assertIn("hermeshq_lote", STANDARD_ENABLED_TOOLSETS)


if __name__ == "__main__":
    unittest.main()
