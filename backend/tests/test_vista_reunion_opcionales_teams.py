import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from hermeshq.scripts import hermes_task_runner as runner
from hermeshq.services import respuesta_sin_repeticion as rsr

RUTA = (
    Path(__file__).resolve().parents[1]
    / "hermeshq"
    / "integration_packages"
    / "ms365-calendar"
    / "plugin"
    / "__init__.py"
)
ARGS = {
    "subject": "Prueba HermesHQ",
    "start": "2026-11-12T14:00:00",
    "end": "2026-11-12T14:30:00",
    "timezone": "America/Santiago",
    "online_meeting": True,
    "attendees": ["persona.externa@example.com"],
    "optional_attendees": ["equipo-datos@contoso.cl"],
}


def _cargar():
    spec = importlib.util.spec_from_file_location("calendario_vista_opcionales", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestVistaConOpcionalesYTeams(unittest.TestCase):
    def _vista(self, args):
        mod = _cargar()
        with (
            patch.object(mod, "_task_user_id", return_value="u1"),
            patch.object(mod, "_central_activa", return_value=False),
            patch.object(mod, "_modo_automatico", return_value=False, create=True),
            patch.object(mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(mod, "_resolver_directorio", return_value={}),
            patch.object(mod, "_graph", return_value={"value": []}),
        ):
            return json.loads(mod._create_event_tool(args))

    def test_caso_real_la_vista_dice_quien_es_opcional_y_que_hay_teams(self):
        r = self._vista(ARGS)
        self.assertTrue(r.get("requiere_aprobacion"), r)
        vista = r["vista_previa"]
        self.assertEqual(vista["opcionales"], ["equipo-datos@contoso.cl"])
        self.assertTrue(vista["reunion_teams"])
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        self.assertIn("- **Invitados:** persona.externa@example.com\n", texto)
        self.assertIn("- **Opcionales:** equipo-datos@contoso.cl", texto)
        self.assertIn("- **Reunión de Teams:** sí", texto)

    def test_sin_opcionales_ni_teams_la_vista_no_cambia(self):
        r = self._vista({k: v for k, v in ARGS.items() if k not in ("optional_attendees", "online_meeting")})
        vista = r["vista_previa"]
        self.assertNotIn("opcionales", vista)
        self.assertNotIn("reunion_teams", vista)
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        self.assertNotIn("Opcionales", texto)
        self.assertNotIn("Teams", texto)

    def test_sin_teams_explicito_lo_dice(self):
        r = self._vista({**ARGS, "online_meeting": False})
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", r["vista_previa"])
        self.assertIn("- **Reunión de Teams:** no", texto)


class TestRepeticionDeLaVistaPrevia(unittest.TestCase):
    VISTA = (
        "📅 **Vista previa de la reunión** (esto es exactamente lo que se creará):\n- **Título:** Prueba HermesHQ\n\n"
        "¿La creo? Responde «sí» para confirmar."
    )

    def test_caso_real_la_misma_vista_previa_no_se_reemplaza(self):
        historial = [{"role": "user", "content": "crea una reunión"}, {"role": "assistant", "content": self.VISTA}]
        self.assertEqual(rsr.filtrar_respuesta("tiene link de teams?", self.VISTA, historial), self.VISTA)

    def test_una_respuesta_comun_repetida_sigue_reemplazandose(self):
        texto = "Tienes 7 eventos para hoy, jueves 12 de noviembre, y el primero es a las 09:30 en Teams."
        historial = [{"role": "user", "content": "agenda"}, {"role": "assistant", "content": texto}]
        self.assertEqual(rsr.filtrar_respuesta("y qué más?", texto, historial), rsr.ACLARACION_REPETICION)
