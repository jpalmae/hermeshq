import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-teams" / "plugin" / "__init__.py"
)

YO = {"id": "d976a076", "displayName": "Ana Soto (Sede Central)", "email": "asoto@contoso.cl"}
PERSONA_A = {"id": "f353b7cf", "displayName": "Julia Mora (Sede Central)", "email": "jmora@contoso.cl"}
PERSONA_B = {"id": "c578f611", "displayName": "Nicolás Parra (Sede Central)", "email": "nparra@contoso.cl"}


def _miembro(p):
    return {"userId": p["id"], "displayName": p["displayName"], "email": p["email"]}


def _mensaje(id_, quien, texto, cuando, tipo="message"):
    return {
        "id": id_,
        "messageType": tipo,
        "from": {"user": {"id": quien["id"], "displayName": quien["displayName"]}} if quien else None,
        "body": {"contentType": "html", "content": texto},
        "createdDateTime": cuando,
    }


CHATS = [
    {"id": "19:chat-a", "chatType": "oneOnOne", "topic": None, "members": [_miembro(YO), _miembro(PERSONA_A)]},
    {"id": "19:chat-b", "chatType": "oneOnOne", "topic": None, "members": [_miembro(YO), _miembro(PERSONA_B)]},
    {"id": "19:bot", "chatType": "oneOnOne", "topic": "Ana Soto (Sede Central)", "members": [_miembro(YO)]},
    {"id": "19:meeting_x", "chatType": "meeting", "topic": "Comité", "members": [_miembro(YO), _miembro(PERSONA_A)]},
]
PREVIAS = {
    "19:chat-a": _mensaje(
        "1", PERSONA_A, '<attachment id="9"></attachment>\n<p>ya gracias!!</p>', "2026-11-12T13:23:25.077Z"
    ),
    "19:chat-b": _mensaje("2", YO, "<p>¿le aviso al Pedro?</p>", "2026-11-11T15:56:00Z"),
    "19:bot": {
        "id": "3",
        "messageType": "message",
        "from": {"application": {"displayName": "HermesHQ-Agent-Bot"}, "user": None},
        "body": {"content": "<p>Procesando…</p>"},
        "createdDateTime": "2026-11-12T14:04:44Z",
    },
    "19:meeting_x": _mensaje("4", None, "<systemEventMessage/>", "2026-11-10T12:00:00Z", tipo="systemEventMessage"),
}
MENSAJES_PERSONA_A = [
    _mensaje("1", PERSONA_A, "<p>ya gracias!!</p>", "2026-11-12T13:23:25.077Z"),
    _mensaje("0", YO, "<p>lamentablemente si por ahora&nbsp;</p>", "2026-11-12T13:18:22.969Z"),
]


def _cargar():
    spec = importlib.util.spec_from_file_location("teams_chats_sin_responder", PLUGIN)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _Base(unittest.TestCase):
    def setUp(self):
        self.p = _cargar()
        self.llamadas = []
        self.previas_disponibles = True

    def _graph(self, metodo, ruta, token, payload=None):
        self.llamadas.append(ruta)
        if ruta.startswith("/me?"):
            return {"displayName": YO["displayName"], "mail": YO["email"], "userPrincipalName": YO["email"]}
        if ruta.startswith("/me/chats?") and "lastMessagePreview" in ruta:
            if not self.previas_disponibles:
                return {"error": "Error 400: expand no soportado"}
            orden = sorted(PREVIAS, key=lambda i: PREVIAS[i]["createdDateTime"], reverse=True)
            vistas = getattr(self, "vistas", {})
            return {
                "value": [
                    {
                        "id": i,
                        "chatType": "x",
                        "lastMessagePreview": PREVIAS[i],
                        **({"viewpoint": vistas[i]} if i in vistas else {}),
                    }
                    for i in orden
                ]
            }
        if ruta.startswith("/me/chats?"):
            return {"value": CHATS}
        if ruta.startswith("/me/chats/19:chat-a/messages"):
            return {"value": MENSAJES_PERSONA_A}
        if ruta.startswith("/me/chats/19:chat-a?"):
            return CHATS[0]
        if ruta.endswith("/messages") and payload:
            return {"id": "nuevo"}
        return {"error": "Chat o elemento no encontrado (404)."}

    def _llamar(self, herramienta, **args):
        with (
            patch.object(self.p, "_task_user_id", return_value="u1"),
            patch.object(self.p, "_central_activa", return_value=False),
            patch.object(self.p, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.p, "_graph", side_effect=self._graph),
        ):
            return json.loads(getattr(self.p, herramienta)(args))


class TestListaConPendientes(_Base):
    def test_caso_real_una_sola_llamada_dice_a_quien_le_toca_y_la_hora_de_chile(self):
        r = self._llamar("_list_chats_tool", count=50)
        self.assertTrue(r["success"], r)
        por_id = {c["id"]: c for c in r["chats"]}
        persona_a = por_id["19:chat-a"]
        self.assertEqual(persona_a["pendiente"], "te_toca_responder")
        self.assertEqual(persona_a["ultimo_mensaje"]["hora_chile"], "jue 12-11 10:23", "13:23Z es 10:23 en Chile")
        self.assertEqual(persona_a["ultimo_mensaje"]["texto"], "ya gracias!!")
        self.assertEqual(persona_a["ultimo_mensaje"]["de"], "Julia Mora (Sede Central)")
        ricardo = por_id["19:chat-b"]
        self.assertEqual(ricardo["pendiente"], "esperas_respuesta", "el último mensaje es de ella")
        self.assertEqual(ricardo["ultimo_mensaje"]["de"], "tú")
        self.assertEqual(ricardo["ultimo_mensaje"]["hora_chile"], "mié 11-11 12:56")
        self.assertEqual(por_id["19:bot"]["pendiente"], "bot")
        self.assertEqual(por_id["19:meeting_x"]["pendiente"], "sin_mensajes")
        self.assertEqual(r["resumen"], {"te_toca_responder": 1, "esperas_respuesta": 1, "no_leidos": 0})
        self.assertIn("no las conviertas", r["nota"])
        self.assertEqual([c["id"] for c in r["chats"]][:2], ["19:bot", "19:chat-a"], "del más reciente al más antiguo")
        self.assertNotIn("created", json.dumps(r), "no se entrega la hora UTC")
        self.assertLessEqual(len(self.llamadas), 3)

    def test_no_leidos_distinto_de_sin_responder(self):
        self.vistas = {
            "19:chat-a": {"isHidden": False, "lastMessageReadDateTime": "2026-11-12T13:00:00Z"},
            "19:chat-b": {"isHidden": False, "lastMessageReadDateTime": "2026-11-11T15:56:00Z"},
        }
        r = self._llamar("_list_chats_tool", count=50)
        por_id = {c["id"]: c for c in r["chats"]}
        self.assertIs(por_id["19:chat-a"]["leido"], False, "Rosa escribió 13:23Z y se leyó hasta 13:00Z")
        self.assertIs(por_id["19:chat-b"]["leido"], True, "el último es de ella")
        self.assertIsNone(por_id["19:bot"]["leido"], "sin viewpoint no se sabe")
        self.assertEqual(r["resumen"]["no_leidos"], 1)
        self.assertIn("no leído", r["nota"])
        self.vistas = {"19:chat-a": {"lastMessageReadDateTime": "2026-11-12T13:30:00Z"}}
        self.p._LECTURAS_DE_LA_TAREA.clear()
        r = self._llamar("_list_chats_tool", count=50)
        self.assertIs({c["id"]: c for c in r["chats"]}["19:chat-a"]["leido"], True)

    def test_sin_previas_sigue_funcionando_como_antes(self):
        self.previas_disponibles = False
        r = self._llamar("_list_chats_tool", count=50)
        self.assertTrue(r["success"], r)
        self.assertEqual(len(r["chats"]), 4)
        self.assertNotIn("pendiente", r["chats"][0])
        self.assertIn("No se pudo leer el último mensaje", r["nota"])

    def test_sin_correo_propio_se_reconoce_por_los_chats_directos(self):
        p = self.p
        identidad = {"nombre": "", "correos": set(), "ids": set()}
        sin_correo = [{**c, "members": [{**m, "email": ""} for m in c["members"]]} for c in CHATS]
        self.assertEqual(p._ids_propios_de_los_miembros(sin_correo, identidad), {"d976a076"})
        self.assertTrue(p._es_propio(PREVIAS["19:chat-b"], identidad))
        self.assertFalse(p._es_propio(PREVIAS["19:chat-a"], identidad))


class TestMensajesDeUnChat(_Base):
    def test_cada_mensaje_dice_si_es_tuyo_y_su_hora_de_chile(self):
        r = self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=10)
        self.assertTrue(r["success"], r)
        self.assertEqual([m["tuyo"] for m in r["messages"]], [False, True])
        self.assertEqual([m["hora_chile"] for m in r["messages"]], ["jue 12-11 10:23", "jue 12-11 10:18"])
        self.assertEqual(r["messages"][1]["body"], "lamentablemente si por ahora")
        self.assertNotIn("created", json.dumps(r))


class TestLecturasRepetidas(_Base):
    def test_caso_real_el_mismo_chat_pedido_una_y_otra_vez(self):
        primera = self._llamar("_get_chat_messages_tool", chat_id="19:meeting_x", count=10)
        self.assertFalse(primera["success"])
        segunda = self._llamar("_get_chat_messages_tool", chat_id="19:meeting_x", count=10)
        self.assertFalse(segunda["success"])
        llamadas_a_graph = len([r for r in self.llamadas if "meeting_x" in r])
        tercera = self._llamar("_get_chat_messages_tool", chat_id="19:meeting_x", count=10)
        self.assertFalse(tercera["success"])
        self.assertIn("misma lectura", tercera["error"])
        self.assertEqual(len([r for r in self.llamadas if "meeting_x" in r]), llamadas_a_graph, "no vuelve a Graph")

    def test_la_segunda_lectura_avisa_y_otro_chat_no_se_ve_afectado(self):
        self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=10)
        segunda = self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=10)
        self.assertTrue(segunda["success"])
        self.assertIn("misma lectura", segunda["aviso"])
        otro = self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=20)
        self.assertTrue(otro["success"])
        self.assertNotIn("aviso", otro)

    def test_despues_de_enviar_se_puede_volver_a_leer(self):
        for _ in range(2):
            self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=10)
        with patch.object(self.p, "_modo_automatico", return_value=False):
            self._llamar("_send_chat_message_tool", chat_id="19:chat-a", message="ok")
        despues = self._llamar("_get_chat_messages_tool", chat_id="19:chat-a", count=10)
        self.assertTrue(despues["success"], despues)
        self.assertNotIn("aviso", despues)


class TestHoraChile(unittest.TestCase):
    def test_horario_de_verano_y_valores_raros(self):
        p = _cargar()
        self.assertEqual(p._hora_chile("2026-11-12T13:23:25.077Z"), "jue 12-11 10:23")
        self.assertEqual(p._hora_chile("2026-07-01T13:00:00Z"), "mié 01-07 09:00", "invierno: UTC-4")
        self.assertIsNone(p._hora_chile(None))
        self.assertIsNone(p._hora_chile("no es fecha"))
