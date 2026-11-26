import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_RUTA_SP = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "sharepoint" / "plugin" / "__init__.py"
)
_SPEC = importlib.util.spec_from_file_location("sharepoint_carpeta_alias", _RUTA_SP)
SP = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SP)


class TestCarpetaAlias(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "outputs").mkdir()
        (self.ws / "outputs" / "doc.txt").write_text("hola", encoding="utf-8")
        env = patch.dict(
            os.environ,
            {
                "HERMES_SESSION_CWD": self.tmp.name,
                "HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t", "metadata": {}}),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def _carpeta_usada(self, args):
        vistas = []
        original = SP._normalize_drive_folder

        def espia(valor):
            vistas.append(valor)
            return original(valor)

        with (
            patch.object(SP, "_task_user_id", return_value="u"),
            patch.object(SP, "_get_m365_token", return_value=("tok", None, None)),
            patch.object(SP, "_normalize_drive_folder", side_effect=espia),
        ):
            try:
                SP._upload_file_tool(args)
            except Exception:  # noqa: BLE001
                pass
        return vistas[0] if vistas else None

    def test_folder_path_se_toma_como_la_carpeta(self):
        carpeta = "Escritorio/Ana/Informes/Resumen Semanal"
        assert self._carpeta_usada({"file_path": "outputs/doc.txt", "folder_path": carpeta}) == carpeta

    def test_control_negativo_folder_sigue_mandando(self):
        assert self._carpeta_usada({"file_path": "outputs/doc.txt", "folder": "A/B", "folder_path": "C/D"}) == "A/B"
