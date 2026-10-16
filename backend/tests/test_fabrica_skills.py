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
    "documentos_fabrica_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

VALIDA = {
    "nombre": "resumen-semanal-licencias",
    "descripcion": "Resume cada semana el estado de las licencias Power BI por sucursal",
    "frases": ["resumen semanal de licencias", "cómo van las licencias esta semana"],
    "pasos": [
        {"texto": "Extraer las hojas de licencias descargadas", "herramienta": "hq_datos_extraer"},
        {"texto": "Contar asignadas y disponibles por sucursal", "herramienta": "hq_datos_consultar"},
        {"texto": "Responder con una tabla corta y la conclusión primero"},
    ],
    "reglas": ["Confirmar la fecha de corte antes de resumir"],
    "entrega": "Un resumen de 5 líneas en el chat, sin archivos",
    "ejemplo": "Dame el resumen semanal de licencias",
}


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

    def _llamar(self, **args):
        return json.loads(plugin._skill_crear(args))

    def _skill(self, **cambios):
        skill = json.loads(json.dumps(VALIDA))
        skill.update(cambios)
        return skill


class TestValidar(_Base):
    def test_a_good_skill_validates_and_previews(self):
        out = self._llamar(accion="validar", **self._skill())
        self.assertTrue(out["success"], out)
        self.assertEqual(out["errores"], [])
        self.assertIn("resumen-semanal-licencias", out["vista_previa"])
        self.assertIn("(herramienta: `hq_datos_extraer`)", out["vista_previa"])

    def test_names_must_be_valid_and_not_managed(self):
        for nombre in ("Mal Nombre", "ab", "catalogo-de-capacidades", "crear-skill"):
            out = self._llamar(accion="validar", **self._skill(nombre=nombre))
            self.assertFalse(out["success"], nombre)
        self.assertIn("administrada", self._llamar(accion="validar", **self._skill(nombre="crear-skill"))["errores"][0])

    def test_tools_that_do_not_exist_are_rejected_with_a_suggestion(self):
        skill = self._skill()
        skill["pasos"][0]["herramienta"] = "hq_datos_extrae"
        out = self._llamar(accion="validar", **skill)
        self.assertFalse(out["success"])
        self.assertTrue(any("hq_datos_extraer" in e for e in out["errores"]))
        texto = self._skill(entrega="Usa sharepoint_subir_todo y entrega")
        self.assertTrue(any("no existe" in e for e in self._llamar(accion="validar", **texto)["errores"]))

    def test_code_html_scripts_and_skill_manage_are_not_allowed(self):
        casos = (
            {"entrega": "Entrega ```print(1)``` listo"},
            {"entrega": "Genera un html con tarjetas"},
            {"reglas": ["Escribe un script que calcule todo"]},
            {"reglas": ["Edita la skill con skill_manage"]},
            {"entrega": "<style>body{}</style>"},
        )
        for cambios in casos:
            self.assertFalse(self._llamar(accion="validar", **self._skill(**cambios))["success"], cambios)

    def test_required_fields_have_limits(self):
        self.assertFalse(self._llamar(accion="validar", **self._skill(frases=["una sola"]))["success"])
        self.assertFalse(self._llamar(accion="validar", **self._skill(descripcion="corta"))["success"])
        self.assertFalse(self._llamar(accion="validar", **self._skill(pasos=[{"texto": "uno"}]))["success"])
        self.assertFalse(self._llamar(accion="validar", **self._skill(ejemplo=""))["success"])
        self.assertFalse(self._llamar(accion="validar", **self._skill(entrega=""))["success"])
        self.assertFalse(self._llamar(accion="validar", **self._skill(reglas=[f"r{i}" for i in range(9)]))["success"])

    def test_warnings_point_to_existing_tools_and_duplicates(self):
        skill = self._skill(
            nombre="analizar-planillas",
            descripcion="Analiza planillas de Excel y arma un resumen con los totales para llevar la gestión",
            frases=["analiza esta planilla de excel", "necesito un resumen del archivo csv"],
            pasos=[{"texto": "Leer la planilla de Excel y contar filas"}, {"texto": "Entregar el resumen"}],
        )
        out = self._llamar(accion="validar", **skill)
        self.assertTrue(out["success"], out)
        avisos = " ".join(out["avisos"])
        self.assertIn("hq_datos_extraer", avisos)


class TestCrear(_Base):
    def test_it_needs_the_users_approval_and_then_saves_the_skill(self):
        sin_ok = self._llamar(**self._skill())
        self.assertFalse(sin_ok["success"])
        self.assertIn("aprobación", sin_ok["error"])
        self.assertFalse((self.raiz / "hermes" / "skills").exists())
        out = self._llamar(aprobado_por_el_usuario=True, **self._skill())
        self.assertTrue(out["success"], out)
        archivo = self.raiz / "hermes" / "skills" / "usuario" / "resumen-semanal-licencias" / "SKILL.md"
        texto = archivo.read_text(encoding="utf-8")
        self.assertIn("name: resumen-semanal-licencias", texto)
        self.assertIn("Usar cuando digan 'resumen semanal de licencias'", texto)
        self.assertIn("> Dame el resumen semanal de licencias", texto)
        self.assertLess(len(texto), 4000)
        registro = (self.raiz / "espacio" / "work" / ".skills_creadas.jsonl").read_text(encoding="utf-8")
        self.assertIn("resumen-semanal-licencias", registro)

    def test_updating_needs_replace_and_keeps_a_backup(self):
        self._llamar(aprobado_por_el_usuario=True, **self._skill())
        repetida = self._llamar(aprobado_por_el_usuario=True, **self._skill())
        self.assertFalse(repetida["success"])
        self.assertTrue(any("reemplazar" in e for e in repetida["errores"]))
        cambiada = self._skill(entrega="Un resumen de 3 líneas", reemplazar=True)
        out = self._llamar(aprobado_por_el_usuario=True, **cambiada)
        self.assertTrue(out["success"], out)
        self.assertTrue(Path(out["respaldo"]).is_file())
        self.assertIn("3 líneas", Path(out["ruta"]).read_text(encoding="utf-8"))

    def test_list_and_remove_only_touch_the_users_skills(self):
        self._llamar(aprobado_por_el_usuario=True, **self._skill())
        administrada = self.raiz / "hermes" / "skills" / "hermeshq-managed" / "catalogo-de-capacidades"
        administrada.mkdir(parents=True)
        (administrada / "SKILL.md").write_text(
            "---\nname: catalogo-de-capacidades\ndescription: x\n---\n", encoding="utf-8"
        )
        listado = self._llamar(accion="listar")
        self.assertEqual(list(listado["skills"]), ["resumen-semanal-licencias"])
        self.assertFalse(self._llamar(accion="eliminar", nombre="catalogo-de-capacidades")["success"])
        self.assertTrue(self._llamar(accion="eliminar", nombre="resumen-semanal-licencias")["success"])
        self.assertEqual(self._llamar(accion="listar")["skills"], {})
        self.assertTrue(administrada.exists())
        self.assertTrue(any((self.raiz / "hermes" / "skills" / ".retiradas").iterdir()))

    def test_an_invalid_skill_is_never_saved(self):
        out = self._llamar(aprobado_por_el_usuario=True, **self._skill(entrega="Genera un html a mano"))
        self.assertFalse(out["success"])
        self.assertFalse((self.raiz / "hermes" / "skills").exists())

    def test_unknown_action_is_rejected(self):
        self.assertFalse(self._llamar(accion="borrar-todo", **self._skill())["success"])
