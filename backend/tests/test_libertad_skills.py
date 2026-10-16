import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
SKILLS = RAIZ / "skill_templates"
_spec = importlib.util.spec_from_file_location(
    "documentos_libertad_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

BASE = {
    "nombre": "vigilar-correos-clave",
    "descripcion": "Revisa el inbox por remitentes clave y avisa solo lo que importa",
    "frases": ["vigila los correos de gerencia", "avísame si llega algo de proveedores"],
    "pasos": [
        {"texto": "Buscar correos nuevos de los remitentes indicados", "herramienta": "ms365_mail_search"},
        {"texto": "Filtrar con el script adjunto y resumir"},
    ],
    "entrega": "Una lista corta en el chat",
    "ejemplo": "Vigila los correos de gerencia",
}
SCRIPT_BUENO = "import json\n\ndef filtrar(correos, palabras):\n    return [c for c in correos if any(p in c.get('asunto', '').lower() for p in palabras)]\n\nprint('ok')\n"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        (self.raiz / "espacio").mkdir()
        entorno = patch.dict(
            os.environ,
            {
                "HERMESHQ_SKILL_TEMPLATES": str(SKILLS),
                "HERMES_SESSION_CWD": str(self.raiz / "espacio"),
                "HERMES_HOME": str(self.raiz / "hermes"),
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)

    def _skill(self, **args):
        return json.loads(plugin._skill_crear(args))

    def _con_script(self, contenido, nombre="filtrar_correos"):
        return {**BASE, "scripts": [{"nombre": nombre, "contenido": contenido}]}


class TestScriptsPropios(_Base):
    def test_a_user_script_is_validated_saved_and_referenced(self):
        out = self._skill(aprobado_por_el_usuario=True, **self._con_script(SCRIPT_BUENO))
        self.assertTrue(out["success"], out)
        carpeta = Path(out["ruta"]).parent
        self.assertEqual((carpeta / "scripts" / "filtrar_correos.py").read_text(encoding="utf-8"), SCRIPT_BUENO)
        texto = Path(out["ruta"]).read_text(encoding="utf-8")
        self.assertIn("runpy.run_path", texto)
        self.assertIn(str(carpeta / "scripts" / "filtrar_correos.py"), texto)

    def test_dangerous_or_broken_scripts_are_rejected_with_the_reason(self):
        casos = {
            "import subprocess\nsubprocess.run(['ls'])\n": "subprocess",
            "import requests\nrequests.get('http://x')\n": "requests",
            "import os\nprint(os.environ['KEY'])\n": "variables de entorno",
            "eval('1+1')\n": "eval",
            "open('/etc/passwd').read()\n": "/etc/passwd",
            "import os\nos.system('rm -rf x')\n": "comandos del sistema",
            "def roto(:\n    pass\n": "sintaxis",
        }
        for codigo, esperado in casos.items():
            out = self._skill(accion="validar", **self._con_script(codigo))
            self.assertFalse(out["success"], codigo)
            self.assertTrue(any(esperado in e for e in out["errores"]), (esperado, out["errores"]))

    def test_script_limits(self):
        self.assertFalse(self._skill(accion="validar", **self._con_script("x = 1\n" * 3000))["success"])
        self.assertFalse(
            self._skill(accion="validar", **self._con_script(SCRIPT_BUENO, nombre="Mal-Nombre"))["success"]
        )
        cuatro = {**BASE, "scripts": [{"nombre": f"script_{i}", "contenido": SCRIPT_BUENO} for i in range(4)]}
        self.assertFalse(self._skill(accion="validar", **cuatro)["success"])


class TestAjustesDeSkillsAdministradas(_Base):
    def _ajustar(self, **extra):
        return self._skill(
            accion="ajustar",
            base="catalogo-de-capacidades",
            reglas=["Siempre agrega un capítulo de conclusiones"],
            **extra,
        )

    def test_it_needs_a_managed_base_valid_rules_and_approval(self):
        self.assertFalse(
            self._skill(accion="ajustar", base="mi-skill", reglas=["x"], aprobado_por_el_usuario=True)["success"]
        )
        self.assertFalse(self._ajustar()["success"])
        self.assertFalse(
            self._skill(accion="ajustar", base="catalogo-de-capacidades", reglas=[], aprobado_por_el_usuario=True)[
                "success"
            ]
        )
        self.assertFalse(
            self._skill(
                accion="ajustar",
                base="catalogo-de-capacidades",
                reglas=["```print(1)```"],
                aprobado_por_el_usuario=True,
            )["success"]
        )

    def test_the_rules_are_saved_and_listed(self):
        out = self._ajustar(aprobado_por_el_usuario=True)
        self.assertTrue(out["success"], out)
        self.assertIn("catalogo-de-capacidades-ajustes", self._skill(accion="listar")["skills"])
        nueva = self._skill(
            accion="ajustar",
            base="catalogo-de-capacidades",
            reglas=["Máximo 3 capítulos"],
            aprobado_por_el_usuario=True,
        )
        self.assertTrue(Path(nueva["respaldo"]).is_file())


if __name__ == "__main__":
    unittest.main()
