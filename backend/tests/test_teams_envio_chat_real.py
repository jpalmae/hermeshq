import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermeshq.scripts import hermes_task_runner as runner
from hermeshq.services import respuesta_verificada as rv

RUTA = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)
REAL = "19:110322l8j49k74i2h756j21h0j0jh7il@thread.v2"
INVENTADO = "19:8477a5f1e9a24b6b92f3c91a6b6b6b6b@thread.v2"
TEXTO = "Hola, ¿coordinamos el horario de salida del viernes?"
CHATS = [
    {
        "id": REAL,
        "topic": "Club de trabajo",
        "chatType": "group",
        "members": [{"displayName": "Marta Gil"}, {"displayName": "Ana Soto (Sede Central)"}],
    },
    {
        "id": "19:otro@thread.v2",
        "topic": "Club de lectura",
        "chatType": "group",
        "members": [{"displayName": "Julia Mora (Sede Central)"}],
    },
    {
        "id": "19:directo@unq.gbl.spaces",
        "topic": None,
        "chatType": "oneOnOne",
        "members": [{"displayName": "Ana Soto (Sede Central)"}, {"displayName": "Nicolás Parra (Sede Central)"}],
    },
]


def _cargar():
    spec = importlib.util.spec_from_file_location("teams_envio_chat_real", RUTA)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _cargar()
        self.llamadas = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        entorno = patch.dict(os.environ, {"HERMES_SESSION_CWD": self.tmp.name})
        entorno.start()
        self.addCleanup(entorno.stop)

    def _graph(self, method, path, token, payload=None):
        self.llamadas.append((method, path))
        if method == "POST" and path.endswith("/messages"):
            return {"id": "m1"}
        if path.startswith("/me/chats?"):
            return {"value": CHATS}
        for c in CHATS:
            if path.startswith(f"/me/chats/{c['id']}?"):
                return c
        return {"error": "Chat o elemento no encontrado (404)."}

    def _turno(self, tarea, mensaje, **args):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_central_activa", return_value=False),
            patch.object(self.mod, "_modo_automatico", return_value=False),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=self._graph),
            patch.object(self.mod, "_current_task_id", return_value=tarea),
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": tarea, "mensaje": mensaje})}),
        ):
            return json.loads(self.mod._send_chat_message_tool(args))

    def _enviados(self):
        return [p for m, p in self.llamadas if m == "POST" and p.endswith("/messages")]


class TestChatReal(_Base):
    def test_caso_real_por_nombre_la_confirmacion_del_turno_siguiente_envia(self):
        previa = self._turno(
            "t-1159", "envía un mensaje al grupo Club de Trabajo", chat_nombre="Club de Trabajo", message=TEXTO
        )
        self.assertTrue(previa["requiere_aprobacion"], previa)
        self.assertEqual(previa["vista_previa"]["chat"], "Club de trabajo")
        self.assertIn("chat_nombre=«Club de trabajo»", previa["siguiente"])
        self.assertEqual(self._enviados(), [])
        envio = self._turno(
            "t-1200", "sí, envía", chat_nombre="club de trabajo", message=TEXTO, aprobado_por_el_usuario=True
        )
        self.assertTrue(envio.get("success"), envio)
        self.assertEqual(self._enviados(), [f"/me/chats/{REAL}/messages"])

    def test_caso_real_un_chat_id_inventado_se_rechaza_sin_vista_previa(self):
        r = self._turno("t-1200", "Envía", chat_id=INVENTADO, message=TEXTO, aprobado_por_el_usuario=True)
        self.assertFalse(r["success"])
        self.assertNotIn("requiere_aprobacion", r)
        self.assertIn("no inventes IDs", r["error"])
        self.assertIn("chat_nombre", r["error"])
        self.assertEqual(self._enviados(), [])

    def test_id_inventado_con_nombre_usa_el_chat_real(self):
        r = self._turno("t-1", "envía", chat_id=INVENTADO, chat_nombre="Club de trabajo", message=TEXTO)
        self.assertEqual(r["vista_previa"]["chat"], "Club de trabajo")
        envio = self._turno("t-2", "enviar", chat_id=REAL, message=TEXTO, aprobado_por_el_usuario=True)
        self.assertTrue(envio.get("success"), "la misma vista previa por id real o por nombre")

    def test_nombre_ambiguo_pregunta_y_nombre_de_persona_encuentra_el_directo(self):
        r = self._turno("t-1", "envía", chat_nombre="Club", message=TEXTO)
        self.assertFalse(r["success"])
        self.assertIn("Club de trabajo", r["error"])
        self.assertIn("Club de lectura", r["error"])
        directo = self._turno("t-1", "envía", chat_nombre="Nicolás Parra", message="hola")
        self.assertIn("Nicolás Parra", directo["vista_previa"]["chat"])

    def test_caso_real_validacion_el_nombre_que_muestra_la_lista_para_un_chat_sin_tema(self):
        for nombre in (
            "Ana Soto (Sede Central), Nicolás Parra (Sede Central)",
            "Nicolás Parra (Sede Central), Ana Soto (Sede Central)",
        ):
            r = self._turno("t-1", "envía", chat_nombre=nombre, message="hola")
            self.assertTrue(r.get("requiere_aprobacion"), (nombre, r))
            self.assertEqual(r["vista_previa"]["chat_id"], "19:directo@unq.gbl.spaces")

    def test_nombre_que_no_existe(self):
        r = self._turno("t-1", "envía", chat_nombre="Club de fútbol", message=TEXTO)
        self.assertFalse(r["success"])
        self.assertIn("No encontré", r["error"])

    def test_approved_by_user_tambien_vale_pero_sigue_exigiendo_la_vista_previa_anterior(self):
        self._turno("t-1", "envía", chat_nombre="Club de trabajo", message=TEXTO)
        r = self._turno("t-2", "sí", chat_nombre="Club de trabajo", message=TEXTO, approved_by_user="true")
        self.assertTrue(r.get("success"), r)

    def test_sin_vista_previa_anterior_no_envia_aunque_diga_aprobado(self):
        r = self._turno("t-1", "sí", chat_nombre="Club de trabajo", message=TEXTO, aprobado_por_el_usuario=True)
        self.assertFalse(r.get("success"))
        self.assertTrue(r["requiere_aprobacion"])
        self.assertEqual(self._enviados(), [])


class TestAfirmacionDeEnvio(unittest.TestCase):
    def test_caso_real_presente_cuenta_como_afirmacion(self):
        respuesta = "Confirmado. Envío el mensaje al chat de Club de trabajo."
        self.assertTrue(rv.afirma_envio(respuesta))
        oraciones = [o for o in runner._ORACIONES.split(respuesta) if o]
        self.assertIn("correo", [runner._tipo_de_frase(o) for o in oraciones])

    def test_negado_o_propuesta_no_cuenta(self):
        self.assertFalse(rv.afirma_envio("No, aún no lo he enviado. Te muestro la vista previa para que confirmes:"))
        self.assertFalse(rv.afirma_envio("¿Confirmas que lo envíe?"))
        self.assertFalse(rv.afirma_envio("Envío el mensaje cuando me confirmes."))
        self.assertTrue(rv.afirma_envio("Listo, ya lo envié al grupo."))

    def test_el_aviso_no_se_agrega_a_una_negacion(self):
        texto = "No, aún no lo he enviado. ¿Confirmas que lo envíe?"
        self.assertEqual(rv.con_aviso_envio_no_confirmado(texto, [], "enviaste el mensaje??"), texto)
