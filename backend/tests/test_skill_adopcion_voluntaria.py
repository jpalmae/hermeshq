import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.services.hermes_installation import manager


class TestAdopcionVoluntaria(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "workspaces"
        self.ws.mkdir()
        carpeta = self.ws / "_biblioteca" / "skills" / "flujo"
        carpeta.mkdir(parents=True)
        (carpeta / "SKILL.md").write_text("---\nname: flujo\n---\n# Demo\n", encoding="utf-8")
        (carpeta / ".hermeshq-alcance.json").write_text(json.dumps({"destinatarios": "todos"}), encoding="utf-8")

    def _sync(self, agent_id):
        casa = self.ws / f"agent-{agent_id}" / ".hermes"
        (casa / "skills").mkdir(parents=True, exist_ok=True)
        with patch.object(manager, "get_settings", return_value=SimpleNamespace(workspaces_root=self.ws)):
            manager.HermesInstallationManager._sync_shared_library(casa)
        return casa / "skills"

    def test_borrada_no_vuelve_en_la_siguiente_sincronizacion(self):
        raiz = self._sync("AAA")
        self.assertTrue((raiz / "biblioteca" / "flujo" / "SKILL.md").is_file())
        manager.rechazar_skill_compartida(raiz, "flujo")
        import shutil

        shutil.rmtree(raiz / "biblioteca" / "flujo")
        raiz = self._sync("AAA")
        self.assertFalse((raiz / "biblioteca" / "flujo").exists())

    def test_control_negativo_sin_rechazo_vuelve(self):
        raiz = self._sync("AAA")
        import shutil

        shutil.rmtree(raiz / "biblioteca" / "flujo")
        raiz = self._sync("AAA")
        self.assertTrue((raiz / "biblioteca" / "flujo" / "SKILL.md").is_file())

    def test_el_rechazo_es_por_agente(self):
        raiz_a = self._sync("AAA")
        manager.rechazar_skill_compartida(raiz_a, "flujo")
        self.assertFalse((self._sync("AAA") / "biblioteca" / "flujo").exists())
        self.assertTrue((self._sync("BBB") / "biblioteca" / "flujo" / "SKILL.md").is_file())

    def test_adoptar_la_trae_de_vuelta(self):
        raiz = self._sync("AAA")
        manager.rechazar_skill_compartida(raiz, "flujo")
        self.assertFalse((self._sync("AAA") / "biblioteca" / "flujo").exists())
        self.assertTrue(manager.adoptar_skill_compartida(raiz, "flujo"))
        self.assertFalse(manager.adoptar_skill_compartida(raiz, "flujo"))
        self.assertTrue((self._sync("AAA") / "biblioteca" / "flujo" / "SKILL.md").is_file())

    def test_archivo_roto_no_bloquea(self):
        raiz = self._sync("AAA")
        (raiz / manager.RECHAZADAS_ARCHIVO).write_text("{x", encoding="utf-8")
        self.assertEqual(manager.leer_rechazadas(raiz), set())
        self.assertTrue((self._sync("AAA") / "biblioteca" / "flujo").exists())
