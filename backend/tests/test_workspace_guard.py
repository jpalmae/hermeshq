import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD_DIR = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "guard_site"
_spec = importlib.util.spec_from_file_location("workspace_guard_under_test", GUARD_DIR / "workspace_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class TestDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name) + "/"
        self.own = Path(self.root) / "agent-aaa"
        self.other = Path(self.root) / "agent-bbb"
        for carpeta in (self.own, self.other, Path(self.root) / "_branding"):
            carpeta.mkdir()
        (self.other / "secreto.txt").write_text("x")
        (Path(self.root) / "_branding" / "logo.png").write_text("x")

    def _bloqueado(self, ruta, cwd=None):
        return guard.blocked(str(ruta), self.root, "agent-aaa", cwd or str(self.own))

    def test_own_folder_shared_folders_and_system_paths_are_allowed(self):
        self.assertIsNone(self._bloqueado(self.own / "work" / "a.txt"))
        self.assertIsNone(self._bloqueado("work/a.txt"))
        self.assertIsNone(self._bloqueado(Path(self.root) / "_branding" / "logo.png"))
        self.assertIsNone(self._bloqueado("/usr/lib/python3/os.py"))
        self.assertIsNone(self._bloqueado("/tmp/hermes_sandbox_1/script.py"))

    def test_other_agents_are_blocked_absolute_relative_and_traversal(self):
        self.assertEqual(self._bloqueado(self.other / "secreto.txt"), "agent-bbb")
        self.assertEqual(self._bloqueado("../agent-bbb/secreto.txt"), "agent-bbb")
        self.assertEqual(self._bloqueado(str(self.own / "x" / ".." / ".." / "agent-bbb")), "agent-bbb")

    def test_a_symlink_to_another_agent_is_blocked(self):
        (self.own / "atajo").symlink_to(self.other)
        self.assertEqual(self._bloqueado(self.own / "atajo" / "secreto.txt"), "agent-bbb")

    def test_commands_naming_another_agent_are_blocked(self):
        mencion = guard.command_mentions_other_agent
        self.assertEqual(mencion(["cat", f"{self.root}agent-bbb/secreto.txt"], self.root, "agent-aaa"), "agent-bbb")
        self.assertIsNone(mencion(["cat", f"{self.root}agent-aaa/a.txt"], self.root, "agent-aaa"))
        self.assertIsNone(mencion(["ls", "/tmp"], self.root, "agent-aaa"))

    def test_the_agent_folder_is_detected_from_the_working_directory(self):
        self.assertEqual(guard.own_agent_dir(str(self.own / "work")), (self.root, "agent-aaa"))
        self.assertIsNone(guard.own_agent_dir("/tmp"))


class TestInterpreter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.root = root
        self.own = root / "agent-aaa"
        self.other = root / "agent-bbb"
        self.own.mkdir()
        self.other.mkdir()
        (self.other / "secreto.txt").write_text("CANARIO")
        (self.own / "propio.txt").write_text("PROPIO")

    def _correr(self, codigo, guardia=True, cwd=None):
        entorno = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "HERMES_SESSION_CWD")}
        if guardia:
            entorno["PYTHONPATH"] = str(GUARD_DIR)
        return subprocess.run(
            [sys.executable, "-c", codigo], cwd=str(cwd or self.own), env=entorno, capture_output=True, text=True
        )

    def test_reading_another_agent_fails_but_the_own_folder_works(self):
        ruta = self.other / "secreto.txt"
        prueba = self._correr(f"print(open({str(ruta)!r}).read())")
        self.assertNotIn("CANARIO", prueba.stdout)
        self.assertIn("Acceso denegado", prueba.stderr)
        propio = self._correr("print(open('propio.txt').read())")
        self.assertIn("PROPIO", propio.stdout)

    def test_listing_copying_and_relative_paths_are_blocked(self):
        for codigo in (
            f"import os; os.listdir({str(self.other)!r})",
            f"import shutil; shutil.copyfile({str(self.other / 'secreto.txt')!r}, 'copia.txt')",
            "open('../agent-bbb/secreto.txt').read()",
            f"import glob; glob.glob({str(self.other)!r} + '/*')",
        ):
            resultado = self._correr(codigo)
            self.assertIn("PermissionError", resultado.stderr, codigo)

    def test_listing_the_common_root_and_writing_in_the_own_folder_work(self):
        self.assertEqual(self._correr(f"import os; print(sorted(os.listdir({str(self.root)!r})))").returncode, 0)
        escritura = self._correr("open('nuevo.txt','w').write('x'); print('ok')")
        self.assertIn("ok", escritura.stdout)

    def test_a_symlink_trick_and_a_subprocess_cannot_reach_the_other_folder(self):
        simbolico = self._correr(
            f"import os; os.symlink({str(self.other)!r}, 'atajo'); print(open('atajo/secreto.txt').read())"
        )
        self.assertNotIn("CANARIO", simbolico.stdout)
        comando = self._correr(
            f"import subprocess; print(subprocess.run(['cat', {str(self.other / 'secreto.txt')!r}], capture_output=True, text=True).stdout)"
        )
        self.assertNotIn("CANARIO", comando.stdout)

    def test_without_the_guard_the_same_read_succeeds_and_outside_agents_nothing_changes(self):
        ruta = self.other / "secreto.txt"
        self.assertIn("CANARIO", self._correr(f"print(open({str(ruta)!r}).read())", guardia=False).stdout)
        fuera = self._correr(f"print(open({str(ruta)!r}).read())", cwd=self.root)
        self.assertIn("CANARIO", fuera.stdout)


class TestElusionesConocidas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name)) / "workspaces"
        self.own = self.root / "agent-aaaaaaaa-1111-2222-3333-444444444444"
        self.other = self.root / "agent-bbbbbbbb-1111-2222-3333-444444444444"
        self.own.mkdir(parents=True)
        self.other.mkdir()
        (self.other / "secreto.txt").write_text("CANARIO")
        self.raiz = str(self.root) + "/"

    def _bloq(self, ruta):
        return guard.blocked(str(ruta), self.raiz, self.own.name, str(self.own))

    def test_proc_self_root_lleva_a_otro_agente(self):
        self.assertEqual(self._bloq(f"/proc/self/root{self.other}/secreto.txt"), self.other.name)

    def test_symlink_fuera_de_la_raiz_hacia_otro_agente(self):
        atajo = Path(self.tmp.name) / "atajo"
        atajo.symlink_to(self.other)
        self.assertEqual(self._bloq(atajo / "secreto.txt"), self.other.name)

    def test_archivos_sensibles_de_otros_procesos(self):
        self.assertTrue(self._bloq("/proc/1/environ"))
        self.assertTrue(self._bloq("/proc/1/root/etc/hostname"))
        self.assertIsNone(self._bloq(f"/proc/{os.getpid()}/status"))
        self.assertIsNone(self._bloq("/proc/self/status"))
        self.assertIsNone(self._bloq("/proc/cpuinfo"))

    def test_comandos_con_ruta_relativa_o_comodin(self):
        mencion = guard.command_mentions_other_agent
        self.assertTrue(mencion(["sh", "-c", f"cd .. && cat {self.other.name}/secreto.txt"], self.raiz, self.own.name))
        self.assertTrue(mencion(["sh", "-c", "cat ../agent-*/secreto.txt"], self.raiz, self.own.name))
        self.assertIsNone(mencion(["sh", "-c", f"cat {self.own.name}/a.txt; ls"], self.raiz, self.own.name))

    def test_por_el_interprete_proc_self_root_no_lee_al_otro(self):
        entorno = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "HERMES_SESSION_CWD")}
        entorno["PYTHONPATH"] = str(GUARD_DIR)
        codigo = f"print(open('/proc/self/root{self.other}/secreto.txt').read())"
        r = subprocess.run(
            [sys.executable, "-c", codigo], cwd=str(self.own), env=entorno, capture_output=True, text=True
        )
        self.assertNotIn("CANARIO", r.stdout)


class TestRunnerSwitch(unittest.TestCase):
    def test_the_runner_honours_the_off_switch(self):
        runner_path = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
        spec = importlib.util.spec_from_file_location("runner_under_test", runner_path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        previo = os.environ.get("PYTHONPATH")
        runner._activar_guardia({"cwd": "/tmp", "workspace_guard": False})
        self.assertEqual(os.environ.get("PYTHONPATH"), previo)


if __name__ == "__main__":
    unittest.main()
