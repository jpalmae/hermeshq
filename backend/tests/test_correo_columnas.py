import importlib.util
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
spec = importlib.util.spec_from_file_location("ms365_mail_columnas_under_test", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

AHORA = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)


def _item(**extra):
    base = {
        "display_id": "#412",
        "subject": "Licencias Copilot",
        "sender_name": "Juan Pérez",
        "relationship": "Para mí",
        "action_required": "Aprobar compra",
        "requires_response": True,
        "due_status": "SIN_PLAZO",
        "first_detected": "2026-11-05T18:25:00Z",
        "display_category": "CALIENTE",
        "operational_status": "PENDIENTE_DE_MÍ",
    }
    base.update(extra)
    return base


class TestColumnasDelInforme(unittest.TestCase):
    def test_fila_trae_todas_las_columnas(self):
        fila = plugin._cu_fila(_item(), AHORA)
        self.assertEqual(fila["id"], "#412")
        self.assertEqual(fila["edad"], "1 h 35 min")
        self.assertEqual(fila["asunto"], "Licencias Copilot")
        self.assertEqual(fila["remitente"], "Juan Pérez")
        self.assertEqual(fila["relacion"], "Para mí")
        self.assertEqual(fila["accion"], "Aprobar compra")
        self.assertEqual(fila["plazo"], "Sin plazo")
        self.assertEqual(fila["respuesta"], "Sí")

    def test_edad(self):
        self.assertEqual(plugin._cu_edad({"first_detected": "2026-11-05T19:40:00Z"}, AHORA), "20 min")
        self.assertEqual(plugin._cu_edad({"first_detected": "2026-11-05T18:00:00Z"}, AHORA), "2 h")
        self.assertEqual(plugin._cu_edad({"first_detected": "2026-11-04T17:00:00Z"}, AHORA), "1 d 3 h")

    def test_relacion_normalizada(self):
        for valor, esperado in (
            ("Para mí", "Para mí"),
            ("PARA_MÍ", "Para mí"),
            ("En copia", "En copia"),
            ("CC", "En copia"),
            ("No dirigido", "No dirigido"),
            ("interno", "—"),
            ("", "—"),
        ):
            self.assertEqual(plugin._cu_relacion_fila({"relationship": valor}), esperado, valor)

    def test_plazo_normalizado(self):
        casos = (
            ({"due_status": "HOY"}, "Hoy"),
            ({"due_status": "hoy-mañana"}, "Hoy"),
            ({"due_status": "MAÑANA"}, "Mañana"),
            ({"due_status": "FECHA", "due_date": "2026-11-05"}, "Hoy"),
            ({"due_status": "FECHA", "due_date": "2026-11-06"}, "Mañana"),
            ({"due_status": "FECHA", "due_date": "2026-11-20"}, "20-11"),
            ({"due_status": "sin_plazo"}, "Sin plazo"),
            ({"due_status": "NO_IDENTIFICADO"}, "Sin plazo"),
        )
        for item, esperado in casos:
            self.assertEqual(plugin._cu_plazo_fila(item, AHORA), esperado, item)

    def test_relacion_desde_destinatarios(self):
        propias = {"mcastro@empresa.cl"}
        para = {"toRecipients": [{"emailAddress": {"address": "mcastro@empresa.cl"}}], "ccRecipients": []}
        cc = {
            "toRecipients": [{"emailAddress": {"address": "x@y.cl"}}],
            "ccRecipients": [{"emailAddress": {"address": "mcastro@empresa.cl"}}],
        }
        otro = {"toRecipients": [{"emailAddress": {"address": "lista@empresa.cl"}}]}
        self.assertEqual(plugin._cu_relacion_destinatarios(para, propias), "Para mí")
        self.assertEqual(plugin._cu_relacion_destinatarios(cc, propias), "En copia")
        self.assertEqual(plugin._cu_relacion_destinatarios(otro, propias), "No dirigido")

    def test_la_relacion_del_servidor_manda_sobre_la_del_modelo(self):
        m = {"id": "AAMk1", "subject": "x", "sender": "y", "received": "2026-11-05T19:00:00Z", "relacion": "En copia"}
        item = plugin._cu_item_nuevo(m, {"relationship": "Para mí", "display_category": "TIBIO"}, AHORA, "#500")
        self.assertEqual(item["relationship"], "En copia")


class TestTablasArmadasPorElServidor(unittest.TestCase):
    def test_cada_categoria_trae_su_tabla_con_las_ocho_columnas(self):
        filas = [
            plugin._cu_fila(_item(), AHORA),
            plugin._cu_fila(_item(display_id="#413", display_category="FRÍO", subject="A | B"), AHORA),
        ]
        tablas = plugin._cu_tablas_md(filas)
        self.assertTrue(
            tablas["CALIENTE"].startswith(
                "| ID | Edad | Asunto | Remitente | Relación | Acción requerida | Plazo | Respuesta |"
            )
        )
        self.assertIn(
            "| #412 | 1 h 35 min | Licencias Copilot | Juan Pérez | Para mí | Aprobar compra | Sin plazo | Sí |",
            tablas["CALIENTE"],
        )
        self.assertIn("A / B", tablas["FRÍO"])
        self.assertEqual(tablas["CRÍTICO"], "_(sin correos nuevos en esta ventana)_")


class TestLeidosPorCategoria(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch.dict(os.environ, {"HERMES_SESSION_CWD": self._tmp.name}).start()
        self.estado = {"pending": [], "resolved": [], "discarded": [], "frios_consolidados": {"count": 0}}
        patch.object(plugin, "_cu_load", side_effect=lambda: self.estado).start()
        patch.object(plugin, "_cu_escribir", side_effect=lambda d: 1.0).start()
        self.addCleanup(patch.stopall)

    def test_normaliza_categorias(self):
        self.assertEqual(plugin._cu_normalizar_categorias(["frio"]), ["FRÍO"])
        self.assertEqual(plugin._cu_normalizar_categorias("fríos y tibios"), ["FRÍO", "TIBIO"])
        self.assertEqual(plugin._cu_normalizar_categorias("todas"), plugin._CU_CATEGORIAS)
        self.assertEqual(plugin._cu_normalizar_categorias("ninguna"), [])
        self.assertIsNone(plugin._cu_normalizar_categorias(["templado"]))

    def test_por_defecto_se_marcan_todas(self):
        self.assertEqual(plugin._cu_categorias_a_leer({}), set(plugin._CU_CATEGORIAS))

    def test_preferencia_solo_frio(self):
        r = plugin._cu_preferencias({"marcar_leidos_categorias": ["FRÍO"]})
        self.assertEqual(r["marcar_leidos_categorias"], ["FRÍO"])
        self.assertIn("CRÍTICO", r["quedan_sin_leer"])
        self.assertEqual(plugin._cu_categorias_a_leer(self.estado), {"FRÍO"})

    def test_preferencia_invalida_no_cambia_nada(self):
        r = plugin._cu_preferencias({"marcar_leidos_categorias": ["templado"]})
        self.assertFalse(r["success"])
        self.assertNotIn("marcar_leidos_categorias", self.estado)

    def test_marcar_ahora_los_frios_del_ultimo_informe(self):
        from unittest.mock import patch

        self.estado["resolved"] = [
            {
                "display_id": "#1",
                "message_id": "AAMk1",
                "display_category": "FRÍO",
                "resolution_type": "mostrado",
                "resolved_at": "2026-11-05T20:00:00Z",
            },
            {
                "display_id": "#2",
                "message_id": "AAMk2",
                "display_category": "CRÍTICO",
                "resolution_type": "mostrado",
                "resolved_at": "2026-11-05T20:00:00Z",
            },
            {
                "display_id": "#0",
                "message_id": "AAMk0",
                "display_category": "FRÍO",
                "resolution_type": "mostrado",
                "resolved_at": "2026-11-05T19:00:00Z",
            },
        ]
        with patch.object(plugin, "_cu_marcar_leidos", return_value={"marcados": 1, "fallos": 0}) as marcar:
            r = plugin._cu_marcar_leidos_ahora({"categorias": ["frío"]})
        marcar.assert_called_once_with(["AAMk1"])
        self.assertEqual(r["display_ids"], ["#1"])
