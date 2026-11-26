import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.services.hermes_installation import manager


class TestSyncAlcanceYFork(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "workspaces"
        self.ws.mkdir()

    def _lib(self, nombre, destinatarios=None, cuerpo="# Demo\n"):
        carpeta = self.ws / "_biblioteca" / "skills" / nombre
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "SKILL.md").write_text(f"---\nname: {nombre}\n---\n{cuerpo}", encoding="utf-8")
        if destinatarios is not None:
            (carpeta / ".hermeshq-alcance.json").write_text(
                json.dumps({"destinatarios": destinatarios}), encoding="utf-8"
            )

    def _sync(self, agent_id):
        casa = self.ws / f"agent-{agent_id}" / ".hermes"
        (casa / "skills").mkdir(parents=True, exist_ok=True)
        with patch.object(manager, "get_settings", return_value=SimpleNamespace(workspaces_root=self.ws)):
            manager.HermesInstallationManager._sync_shared_library(casa)
        return casa / "skills" / "biblioteca"

    def test_scope_limits_which_agents_get_the_skill(self):
        self._lib("solo-a", destinatarios=["AAA"])
        self.assertTrue((self._sync("AAA") / "solo-a" / "SKILL.md").is_file())
        self.assertFalse((self._sync("BBB") / "solo-a").exists())

    def test_todos_reaches_everyone(self):
        self._lib("para-todos", destinatarios="todos")
        self.assertTrue((self._sync("AAA") / "para-todos" / "SKILL.md").is_file())
        self.assertTrue((self._sync("ZZZ") / "para-todos" / "SKILL.md").is_file())

    def test_missing_or_corrupt_or_empty_scope_reaches_nobody(self):
        self._lib("sin-alcance")
        self.assertFalse((self._sync("AAA") / "sin-alcance").exists())
        self._lib("vacio", destinatarios=[])
        self.assertFalse((self._sync("AAA") / "vacio").exists())
        self._lib("nulo-sin-autor", destinatarios=None)
        (self.ws / "_biblioteca" / "skills" / "nulo-sin-autor" / ".hermeshq-alcance.json").write_text(
            json.dumps({"destinatarios": None}), encoding="utf-8"
        )
        self.assertFalse((self._sync("AAA") / "nulo-sin-autor").exists())
        self._lib("roto")
        (self.ws / "_biblioteca" / "skills" / "roto" / ".hermeshq-alcance.json").write_text("{x", encoding="utf-8")
        self.assertFalse((self._sync("AAA") / "roto").exists())
        self._lib("tipo-raro", destinatarios={"AAA": True})
        self.assertFalse((self._sync("AAA") / "tipo-raro").exists())

    def test_a_legacy_null_manifest_with_author_still_means_todos(self):
        self._lib("legado")
        (self.ws / "_biblioteca" / "skills" / "legado" / ".hermeshq-alcance.json").write_text(
            json.dumps({"autor": "x", "destinatarios": None, "fecha": "2026-10-01"}), encoding="utf-8"
        )
        self.assertTrue((self._sync("AAA") / "legado" / "SKILL.md").is_file())

    def test_an_out_of_scope_copy_is_removed_on_sync(self):
        self._lib("solo-a", destinatarios=["AAA"])
        vieja = self.ws / "agent-BBB" / ".hermes" / "skills" / "biblioteca" / "solo-a"
        vieja.mkdir(parents=True)
        (vieja / "SKILL.md").write_text("x", encoding="utf-8")
        self._sync("BBB")
        self.assertFalse(vieja.exists())

    def test_unedited_copy_updates_but_edited_copy_is_kept(self):
        self._lib("plantilla", destinatarios="todos", cuerpo="# Version 1\n")
        copia = self._sync("AAA") / "plantilla" / "SKILL.md"
        self.assertIn("Version 1", copia.read_text(encoding="utf-8"))
        self._lib("plantilla", destinatarios="todos", cuerpo="# Version 2\n")
        self._sync("AAA")
        self.assertIn("Version 2", copia.read_text(encoding="utf-8"))
        copia.write_text(copia.read_text(encoding="utf-8") + "\nmi cambio propio\n", encoding="utf-8")
        self._lib("plantilla", destinatarios="todos", cuerpo="# Version 3\n")
        self._sync("AAA")
        texto = copia.read_text(encoding="utf-8")
        self.assertIn("mi cambio propio", texto)
        self.assertNotIn("Version 3", texto)

    def test_editing_only_a_reference_file_also_counts_as_a_fork(self):
        self._lib("con-ref", destinatarios="todos")
        ref = self.ws / "_biblioteca" / "skills" / "con-ref" / "references" / "guia.md"
        ref.parent.mkdir()
        ref.write_text("v1\n", encoding="utf-8")
        copia = self._sync("AAA") / "con-ref" / "references" / "guia.md"
        copia.write_text("v1\nmi nota\n", encoding="utf-8")
        ref.write_text("v2\n", encoding="utf-8")
        self._sync("AAA")
        self.assertIn("mi nota", copia.read_text(encoding="utf-8"))

    def test_a_legacy_skill_md_mark_is_not_mistaken_for_a_fork(self):
        self._lib("vieja-marca", destinatarios="todos", cuerpo="# V1\n")
        copia = self._sync("AAA") / "vieja-marca"
        (copia / ".hermeshq-plantilla").write_text(manager._hash_skill_md(copia), encoding="utf-8")
        self._lib("vieja-marca", destinatarios="todos", cuerpo="# V2\n")
        self._sync("AAA")
        self.assertIn("V2", (copia / "SKILL.md").read_text(encoding="utf-8"))

    def test_an_out_of_scope_edited_copy_is_kept_as_a_private_skill(self):
        self._lib("retirada", destinatarios="todos")
        copia = self._sync("BBB") / "retirada" / "SKILL.md"
        copia.write_text(copia.read_text(encoding="utf-8") + "\nmi cambio\n", encoding="utf-8")
        self._lib("retirada", destinatarios=["AAA"])
        self._sync("BBB")
        self.assertFalse(copia.exists())
        privada = self.ws / "agent-BBB" / ".hermes" / "skills" / "usuario" / "retirada"
        self.assertIn("mi cambio", (privada / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((privada / ".hermeshq-ya-no-compartida").is_file())
        self.assertFalse((privada / ".hermeshq-plantilla").exists())
        self._sync("BBB")
        self.assertTrue((privada / "SKILL.md").is_file())

    def test_the_scope_manifest_is_not_copied_to_the_recipient(self):
        self._lib("con-alcance", destinatarios=["AAA"])
        bib = self._sync("AAA")
        self.assertFalse((bib / "con-alcance" / ".hermeshq-alcance.json").exists())


if __name__ == "__main__":
    unittest.main()
