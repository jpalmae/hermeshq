import asyncio
import base64
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.services import toolsets_pedido
from hermeshq.services.hermes_installation.manager import HermesInstallationManager

_RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
_spec = importlib.util.spec_from_file_location(
    "documentos_imagenes", _RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

TOOLSETS_OPERADOR_3 = [
    "safe", "browser", "file", "skills", "memory", "todo", "messaging", "delegation", "vision", "hermeshq_comms",
    "hermeshq_pdf", "hermeshq_audio", "code_execution", "image_gen", "hermeshq_docx", "hermeshq_pptx", "hermeshq_xlsx",
    "hermeshq_schedule", "hermeshq_documentos", "hermeshq_lote", "terminal",
]  # fmt: skip


class TestSeleccionDeToolsets(unittest.TestCase):
    def test_caso_real_135d17e5_un_pedido_de_imagen_carga_el_toolset_del_generador(self):
        elegidos = toolsets_pedido.toolsets_para_pedido("creame la imagen de un perro superman", TOOLSETS_OPERADOR_3)
        self.assertIn("hermeshq_documentos", elegidos)

    def test_cada_palabra_de_imagen_carga_el_generador(self):
        for pedido in ("hazme un logo para el equipo", "dibuja un barco", "una foto de un lago", "una ilustracion"):
            with self.subTest(pedido=pedido):
                self.assertIn("hermeshq_documentos", toolsets_pedido.toolsets_para_pedido(pedido, TOOLSETS_OPERADOR_3))

    def test_un_pedido_sin_relacion_sigue_sin_cargarlo(self):
        elegidos = toolsets_pedido.toolsets_para_pedido("revisa mi correo de hoy", TOOLSETS_OPERADOR_3)
        self.assertNotIn("hermeshq_documentos", elegidos)


class TestRechazoFalsoDeImagen(unittest.TestCase):
    def test_editar_una_imagen_no_sugiere_generar_otra(self):
        self.assertEqual(runner.herramientas_del_pedido("quítale el fondo a esta imagen", {"hq_pptx_generate"}), [])


class TestSkillAdministradaRetirada(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        raiz = self.home / "skills" / "hermeshq-managed"
        for nombre in ("skill-retirada", "otra-skill-retirada"):
            carpeta = raiz / nombre
            carpeta.mkdir(parents=True)
            (carpeta / "SKILL.md").write_text("vieja", encoding="utf-8")
            (carpeta / ".hermeshq-skill.json").write_text(
                json.dumps({"name": nombre, "identifier": f"local/{nombre}", "managed": True}), encoding="utf-8"
            )
        self.raiz = raiz

    def sincronizar(self):
        manager = HermesInstallationManager(session_factory=None, secret_vault=None, version_manager=None)
        agente = SimpleNamespace(skills=["local/skill-retirada", "local/otra-skill-retirada"])
        return asyncio.run(manager._sync_managed_skills(agente, self.home, []))

    def test_caso_real_b0dc06a6_la_copia_de_una_skill_que_ya_no_existe_se_retira(self):
        instaladas = self.sincronizar()
        self.assertFalse((self.raiz / "skill-retirada").exists())
        self.assertNotIn("skill-retirada", [s["name"] for s in instaladas])


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


class _Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
