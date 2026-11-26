import json
import unittest
from unittest.mock import patch

from tests.test_ms365_mail_get import _load

TOP = {
    "value": [
        {
            "id": "ID-INBOX",
            "displayName": "Bandeja de entrada",
            "childFolderCount": 2,
            "unreadItemCount": 3,
            "totalItemCount": 40,
        },
        {
            "id": "ID-SENT",
            "displayName": "Elementos enviados",
            "childFolderCount": 0,
            "unreadItemCount": 0,
            "totalItemCount": 9,
        },
    ]
}
CHILDREN = {
    "value": [
        {
            "id": "ID-INFO",
            "displayName": "Informaciones",
            "childFolderCount": 0,
            "unreadItemCount": 1,
            "totalItemCount": 7,
        },
        {"id": "ID-PROY", "displayName": "Proyectos", "childFolderCount": 0, "unreadItemCount": 0, "totalItemCount": 2},
    ]
}


def _fake_graph(method, path, token, **kwargs):
    if "/childFolders" in path:
        return CHILDREN
    if path.startswith("/me/mailFolders?"):
        return TOP
    return {"value": []}


class TestMailFolders(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _resolve(self, name):
        with patch.object(self.mod, "_graph", side_effect=_fake_graph):
            return self.mod._resolve_folder("tok", name)

    def test_well_known_and_aliases_skip_graph(self):
        with patch.object(self.mod, "_graph", side_effect=AssertionError("no debe llamar a Graph")):
            self.assertEqual(self.mod._resolve_folder("tok", "inbox"), ("inbox", None))
            self.assertEqual(self.mod._resolve_folder("tok", "Bandeja de entrada"), ("inbox", None))
            self.assertEqual(self.mod._resolve_folder("tok", "Enviados"), ("sentitems", None))

    def test_custom_subfolder_by_name_ignores_case_and_accents(self):
        self.assertEqual(self._resolve("informaciónes"), ("ID-INFO", None))
        self.assertEqual(self._resolve("INFORMACIONES"), ("ID-INFO", None))

    def test_custom_subfolder_by_path(self):
        self.assertEqual(self._resolve("Bandeja de entrada/Proyectos"), ("ID-PROY", None))

    def test_unknown_folder_lists_available(self):
        folder, error = self._resolve("Inexistente")
        self.assertIsNone(folder)
        self.assertIn("Bandeja de entrada/Informaciones", error)

    def test_long_id_passes_through(self):
        folder_id = "AAMkAGI2" + "x" * 60
        self.assertEqual(self._resolve(folder_id), (folder_id, None))

    def test_list_uses_resolved_folder_id(self):
        llamadas = []

        def broker(metodo, ruta, *a, **k):
            llamadas.append(ruta)
            return _fake_graph(metodo, ruta, None)

        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", side_effect=AssertionError("sin token directo")),
            patch.object(self.mod, "_graph", side_effect=AssertionError("sin Graph directo")),
            patch.object(self.mod, "_broker_graph", side_effect=broker),
        ):
            out = json.loads(self.mod._list_emails_tool({"folder": "Informaciones", "count": 5}))
            self.assertTrue(out["success"], out)
            self.assertTrue(any(c.startswith("/me/mailFolders/ID-INFO/messages") for c in llamadas), llamadas)
            out = json.loads(self.mod._list_emails_tool({"folder": "Bandeja de entrada", "count": 5}))
        self.assertTrue(out["success"])
        self.assertTrue(any("/me/mailFolders/inbox/messages" in c for c in llamadas))

    def test_folders_tool_returns_paths(self):
        with (
            patch.object(self.mod, "_task_user_id", return_value="u1"),
            patch.object(self.mod, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.mod, "_graph", side_effect=_fake_graph),
        ):
            out = json.loads(self.mod._list_folders_tool({}))
        paths = [f["path"] for f in out["folders"]]
        self.assertIn("Bandeja de entrada/Informaciones", paths)


class TestAvanceDelBarrido(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        mensaje = {"id": "M1", "subject": "Hola", "isRead": False, "receivedDateTime": "2026-11-23T10:00:00Z"}

        def broker(metodo, ruta, *a, **k):
            if "/messages" in ruta:
                return {"value": [dict(mensaje)]}
            return _fake_graph(metodo, ruta, None)

        for objetivo, valor in (
            ("_task_user_id", {"return_value": "u1"}),
            ("_get_m365_token", {"return_value": ("tok", "")}),
            ("_graph", {"side_effect": _fake_graph}),
            ("_broker_graph", {"side_effect": broker}),
            ("_remember_ids", {"return_value": None}),
        ):
            parche = patch.object(self.mod, objetivo, **valor)
            parche.start()
            self.addCleanup(parche.stop)

    def _tarea(self, nombre):
        parche = patch.dict("os.environ", {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task": nombre})})
        parche.start()
        self.addCleanup(parche.stop)

    def _listar(self, carpeta, since="2026-11-20T08:45:00-03:00"):
        return json.loads(self.mod._list_emails_tool({"folder": carpeta, "unread_only": True, "since": since}))

    def test_cada_lista_dice_que_carpetas_faltan_sin_quitar_campos(self):
        self._tarea("a")
        self.assertNotIn("barrido", self._listar("Informaciones"))
        self.mod._list_folders_tool({})
        out = self._listar("Informaciones")
        self.assertEqual(out["messages"][0]["subject"], "Hola")
        self.assertEqual(out["messages"][0]["id"], "M1")
        self.assertEqual(out["barrido"]["carpetas_con_no_leidos"], 2)
        self.assertEqual(out["barrido"]["pendientes"], ["Bandeja de entrada"])
        self.assertNotIn("aviso", out["barrido"])
        out = self._listar("Bandeja de entrada")
        self.assertEqual(out["barrido"]["pendientes"], [])
        self.assertIn("ms365_mail_get", out["barrido"]["siguiente"])
        out = self._listar("Informaciones")
        self.assertIn("ya se listó en esta tarea", out["barrido"]["aviso"])
        self.assertEqual(out["count"], 1)

    def test_otra_tarea_empieza_de_cero(self):
        self._tarea("a")
        self.mod._list_folders_tool({})
        self._listar("Informaciones")
        self._tarea("b")
        self.assertNotIn("barrido", self._listar("Informaciones"))
