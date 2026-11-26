import hashlib
import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)


def _cargar():
    spec = importlib.util.spec_from_file_location("sp_plugin_nombre_remoto", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestNombreRemoto(unittest.TestCase):
    def setUp(self):
        self.sp = _cargar()
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.datos = b"nota de prueba del entorno de humo\n"
        (self.base / "nota-smoke.txt").write_bytes(self.datos)
        self.prefijo = hashlib.sha256(self.datos).hexdigest()[:8]

    def test_resuelve_el_nombre_remoto_al_archivo_local_del_mismo_contenido(self):
        hallado = self.sp._archivo_local_por_nombre_remoto(
            self.base, f"UltraBot-archivos/{self.prefijo}_nota-smoke.txt"
        )
        self.assertEqual(hallado, (self.base / "nota-smoke.txt").resolve())

    def test_huella_que_no_corresponde_no_resuelve(self):
        self.assertIsNone(self.sp._archivo_local_por_nombre_remoto(self.base, "00000000_nota-smoke.txt"))

    def test_nombre_sin_huella_no_resuelve(self):
        self.assertIsNone(self.sp._archivo_local_por_nombre_remoto(self.base, "nota-smoke.txt"))

    def test_no_busca_fuera_del_espacio_de_trabajo(self):
        fuera = self.base.parent / "fuera-smoke.txt"
        fuera.write_bytes(self.datos)
        self.addCleanup(fuera.unlink)
        self.assertIsNone(self.sp._archivo_local_por_nombre_remoto(self.base, f"{self.prefijo}_fuera-smoke.txt"))
