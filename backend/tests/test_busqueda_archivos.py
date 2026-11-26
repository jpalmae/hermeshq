import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

file_operations = pytest.importorskip("tools.file_operations")

import hermeshq.scripts.hermes_task_runner as runner  # noqa: E402


class _EntornoLocal:
    def __init__(self, cwd):
        self.cwd = cwd

    def execute(self, command, cwd=None, timeout=None, **_kwargs):
        r = subprocess.run(["sh", "-c", command], cwd=cwd or self.cwd, capture_output=True, text=True, timeout=60)
        return {"output": r.stdout, "returncode": r.returncode}


class TestBusquedaDeArchivosSinRipgrep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "outputs").mkdir()
        (Path(self.tmp.name) / "outputs" / "equipo-datos_3.pptx").write_bytes(b"x")
        (Path(self.tmp.name) / "outputs" / "otro.pdf").write_bytes(b"x")
        clase = file_operations.ShellFileOperations
        originales = (clase.search, clase._search_files, getattr(clase, "_hq_busqueda_corregida", False))
        self.addCleanup(self._restaurar, clase, originales)
        if getattr(clase, "_hq_busqueda_corregida", False):
            clase.search, clase._search_files = clase._hq_originales
            clase._hq_busqueda_corregida = False
        self.ops = clase(_EntornoLocal(self.tmp.name), cwd=self.tmp.name)
        self.ops._command_cache["rg"] = False

    @staticmethod
    def _restaurar(clase, originales):
        clase.search, clase._search_files, marca = originales
        clase._hq_busqueda_corregida = marca

    def buscar(self, **kwargs):
        return self.ops.search(path=self.tmp.name, target="files", **kwargs).files

    def test_caso_real_sin_la_correccion_no_encuentra_el_archivo(self):
        self.assertEqual(self.buscar(pattern="equipo"), [])

    def test_caso_real_un_nombre_parcial_encuentra_el_archivo(self):
        runner._activar_busqueda_de_archivos()
        encontrados = self.buscar(pattern="Gobernanza")
        self.assertEqual([Path(f).name for f in encontrados], ["equipo-datos_3.pptx"])

    def test_caso_real_punto_asterisco_con_file_glob_usa_el_glob(self):
        runner._activar_busqueda_de_archivos()
        encontrados = self.buscar(pattern=".*", file_glob="*.pptx")
        self.assertEqual([Path(f).name for f in encontrados], ["equipo-datos_3.pptx"])

    def test_un_patron_con_comodines_se_respeta(self):
        runner._activar_busqueda_de_archivos()
        self.assertEqual([Path(f).name for f in self.buscar(pattern="*.pdf")], ["otro.pdf"])


if __name__ == "__main__":
    unittest.main()
