import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1] / "hermeshq"
SKILLS = RAIZ / "skill_templates"
_spec = importlib.util.spec_from_file_location(
    "documentos_compartidas_under_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "__init__.py"
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

BASE = {
    "nombre": "resumen-semanal-licencias",
    "descripcion": "Resume cada semana el estado de las licencias Power BI por sucursal",
    "frases": [
        "resumen semanal de licencias",
        "cómo van las licencias esta semana",
        "estado de licencias por sucursal",
    ],
    "pasos": [
        {"texto": "Extraer las hojas de licencias descargadas", "herramienta": "hq_datos_extraer"},
        {"texto": "Contar asignadas y disponibles por sucursal", "herramienta": "hq_datos_consultar"},
    ],
    "reglas": ["Confirmar la fecha de corte antes de resumir"],
    "entrega": "Un resumen de 5 líneas en el chat",
    "ejemplo": "Dame el resumen semanal de licencias",
}
SCRIPT = "def contar(filas):\n    return len(filas)\n"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.espacios = self.raiz / "espacios"
        for agente in ("agent-aaaa1111", "agent-bbbb2222"):
            (self.espacios / agente / ".hermes" / "skills").mkdir(parents=True)
            (self.espacios / agente / "work").mkdir()
        self.casa = self.espacios / "agent-aaaa1111" / ".hermes"
        (self.raiz / "espacio").mkdir()
        entorno = patch.dict(
            os.environ,
            {
                "HERMESHQ_SKILL_TEMPLATES": str(SKILLS),
                "HERMES_SESSION_CWD": str(self.raiz / "espacio"),
                "HERMES_HOME": str(self.casa),
                "HERMESHQ_BIBLIOTECA": str(self.raiz / "biblioteca"),
                "HERMESHQ_WORKSPACES": str(self.espacios),
                "HERMESHQ_AGENT_ID": "aaaa1111-0000",
            },
        )
        entorno.start()
        self.addCleanup(entorno.stop)

    def _skill(self, **args):
        return json.loads(plugin._skill_crear(args))

    def _crear(self, **cambios):
        out = self._skill(aprobado_por_el_usuario=True, **{**BASE, **cambios})
        self.assertTrue(out["success"], out)
        return out

    def _otro_agente(self, nombre):
        return self.espacios / "agent-bbbb2222" / ".hermes" / "skills" / "biblioteca" / nombre / "SKILL.md"


class TestEnsenanza(_Base):
    def test_the_guide_teaches_the_good_practices(self):
        guia = self._skill(accion="guia")["guia"]
        for tema in (
            "UN trabajo",
            "frases",
            "catálogo",
            "datos personales" if False else "Nada personal",
            "3 casos",
            "scripts",
        ):
            self.assertIn(tema, guia)

    def test_validation_and_creation_come_with_concrete_tips(self):
        pobre = {
            **BASE,
            "frases": ["una", "dos"],
            "pasos": [{"texto": "hacer algo"}, {"texto": "hacer otra cosa"}],
            "reglas": [],
        }
        consejos = " ".join(self._skill(accion="validar", **pobre)["consejos"])
        self.assertIn("más variantes", consejos)
        self.assertIn("herramienta de cada paso", consejos)
        self.assertIn("regla de seguridad", consejos)
        creada = self._crear()
        self.assertTrue(any("3 casos" in c for c in creada["consejos"]))
        self.assertIn("compartirla", creada["siguiente"])


class TestCompartir(_Base):
    def test_a_plain_skill_is_published_for_everyone_at_once(self):
        self._crear()
        sin_ok = self._skill(accion="compartir", nombre=BASE["nombre"])
        self.assertFalse(sin_ok["success"])
        self.assertIn("aprobación", sin_ok["error"])
        out = self._skill(accion="compartir", nombre=BASE["nombre"], aprobado_por_el_usuario=True)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["estado"], "publicada")
        texto = self._otro_agente(BASE["nombre"]).read_text(encoding="utf-8")
        self.assertIn("compartida_por: aaaa1111", texto)
        self.assertIn("name: resumen-semanal-licencias", texto)
        listado = self._skill(accion="biblioteca")
        self.assertEqual(listado["publicadas"][BASE["nombre"]]["autor"], "aaaa1111")
        repetida = self._skill(accion="compartir", nombre=BASE["nombre"], aprobado_por_el_usuario=True)
        self.assertFalse(repetida["success"])

    def test_personal_data_blocks_sharing_and_explains_what_to_change(self):
        self._crear(
            nombre="avisar-a-mi-jefe",
            frases=["avisa a mi jefe", "manda el resumen a mi jefe", "envía el estado a mi jefe"],
            pasos=[
                {"texto": "Enviar el resumen a jefe@example.com", "herramienta": "ms365_mail_send"},
                {"texto": "Guardar en C:\\Users\\usuario\\OneDrive - Contoso\\Informes"},
            ],
        )
        out = self._skill(accion="compartir", nombre="avisar-a-mi-jefe", aprobado_por_el_usuario=True)
        self.assertFalse(out["success"])
        self.assertIn("correo electrónico", out["error"])
        self.assertIn("ruta de Windows", out["error"])
        self.assertIn("que se los pida", out["error"])
        self.assertFalse(self._otro_agente("avisar-a-mi-jefe").exists())

    def test_only_own_skills_can_be_shared(self):
        out = self._skill(accion="compartir", nombre="catalogo-de-capacidades", aprobado_por_el_usuario=True)
        self.assertFalse(out["success"])
        self.assertIn("tus propias skills", out["error"])

    def test_skills_with_scripts_wait_for_the_administrators_review(self):
        self._crear(scripts=[{"nombre": "contar_filas", "contenido": SCRIPT}])
        out = self._skill(accion="compartir", nombre=BASE["nombre"], aprobado_por_el_usuario=True)
        self.assertEqual(out["estado"], "pendiente")
        self.assertIn("revisión", out["mensaje"])
        self.assertFalse(self._otro_agente(BASE["nombre"]).exists())
        spec = importlib.util.spec_from_file_location(
            "fab_test", RAIZ / "plugin_templates" / "hermeshq_documentos" / "fabrica_skills.py"
        )
        fab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fab)
        self.assertEqual(fab.listar_biblioteca()["pendientes"], [BASE["nombre"]])
        self.assertTrue(fab.aprobar(BASE["nombre"]))
        self.assertTrue(self._otro_agente(BASE["nombre"]).is_file())
        self.assertTrue((self._otro_agente(BASE["nombre"]).parent / "scripts" / "contar_filas.py").is_file())
        self.assertEqual(fab.listar_biblioteca()["pendientes"], [])

    def test_the_administrator_can_reject_and_only_the_author_can_withdraw(self):
        self._crear(scripts=[{"nombre": "contar_filas", "contenido": SCRIPT}])
        self._skill(accion="compartir", nombre=BASE["nombre"], aprobado_por_el_usuario=True)
        spec = importlib.util.spec_from_file_location(
            "fab_test2", RAIZ / "plugin_templates" / "hermeshq_documentos" / "fabrica_skills.py"
        )
        fab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fab)
        self.assertTrue(fab.rechazar(BASE["nombre"], "usa requests"))
        rechazada = next((self.raiz / "biblioteca" / "rechazadas").iterdir())
        self.assertEqual((rechazada / "MOTIVO.txt").read_text(encoding="utf-8"), "usa requests")
        self._crear(nombre="otra-skill-simple", frases=["otra cosa uno", "otra cosa dos", "otra cosa tres"])
        self._skill(accion="compartir", nombre="otra-skill-simple", aprobado_por_el_usuario=True)
        with patch.dict(os.environ, {"HERMESHQ_AGENT_ID": "bbbb2222-0000"}):
            ajeno = self._skill(accion="retirar_compartida", nombre="otra-skill-simple")
        self.assertFalse(ajeno["success"])
        self.assertTrue(self._otro_agente("otra-skill-simple").is_file())
        propio = self._skill(accion="retirar_compartida", nombre="otra-skill-simple")
        self.assertTrue(propio["success"], propio)
        self.assertFalse(self._otro_agente("otra-skill-simple").exists())


class TestSincronizacionDeAgentesNuevos(unittest.TestCase):
    def test_new_or_restarted_agents_receive_the_published_skills_and_lose_the_withdrawn_ones(self):
        from hermeshq.services.hermes_installation import manager

        with tempfile.TemporaryDirectory() as carpeta:
            raiz = Path(carpeta)
            publicada = raiz / "workspaces" / "_biblioteca" / "skills" / "resumen-semanal"
            publicada.mkdir(parents=True)
            (publicada / "SKILL.md").write_text("---\nname: resumen-semanal\n---\n", encoding="utf-8")
            casa = raiz / "agente" / ".hermes"
            vieja = casa / "skills" / "biblioteca" / "retirada"
            vieja.mkdir(parents=True)
            (vieja / "SKILL.md").write_text("x", encoding="utf-8")
            with patch.object(
                manager, "get_settings", return_value=SimpleNamespace(workspaces_root=raiz / "workspaces")
            ):
                manager.HermesInstallationManager._sync_shared_library(casa)
            self.assertTrue((casa / "skills" / "biblioteca" / "resumen-semanal" / "SKILL.md").is_file())
            self.assertFalse(vieja.exists())
