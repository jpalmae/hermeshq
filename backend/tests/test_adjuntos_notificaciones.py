import unittest
from types import SimpleNamespace

from hermeshq.routers.public_chat import _adjuntos_publicos
from hermeshq.schemas.public_chat import NotificationRead


class AdjuntosDeNotificaciones(unittest.TestCase):
    def test_solo_se_exponen_los_campos_seguros(self):
        tarea = SimpleNamespace(
            metadata_json={
                "response_attachments": [
                    {
                        "file_id": "f1",
                        "filename": "informe.html",
                        "media_type": "text/html",
                        "size": 80000,
                        "path": "/app/workspaces/agent-x/uploads/f1.html",
                    }
                ]
            }
        )
        adjuntos = _adjuntos_publicos(tarea)
        self.assertEqual(
            adjuntos, [{"file_id": "f1", "filename": "informe.html", "media_type": "text/html", "size": 80000}]
        )
        self.assertNotIn("path", adjuntos[0])

    def test_una_tarea_sin_archivos_no_devuelve_nada(self):
        self.assertEqual(_adjuntos_publicos(SimpleNamespace(metadata_json={})), [])
        self.assertEqual(_adjuntos_publicos(None), [])

    def test_el_esquema_acepta_adjuntos_sin_romper_a_los_clientes_viejos(self):
        base = {"id": "n1", "title": "t", "body": "b", "created_at": "2026-10-30T17:00:00+00:00"}
        self.assertEqual(NotificationRead(**base).attachments, [])
