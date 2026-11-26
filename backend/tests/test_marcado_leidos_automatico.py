import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.services.cron_schedule import scheduled_task_metadata
from hermeshq.services.marcado_leidos import CLAVE_METADATA, declara_marcar_leidos, marcado_autorizado

PLUGIN = (
    Path(__file__).resolve().parents[1] / "hermeshq" / "integration_packages" / "ms365-mail" / "plugin" / "__init__.py"
)
AHORA = "2026-10-26T18:00:00Z"
MID = "N" * 60

DECLARAN = (
    "Revisa los no leídos y márcalos como leídos",
    "Marca como leídos los correos mostrados en el informe",
    "Haz el informe; no leídos, márcalos como leídos",
    "Mark the reported emails as read",
)
NO_DECLARAN = (
    "Resumen de correos no leídos de hoy",
    "Informe de no leídos. No marques nada como leído.",
    "No los marques como leídos",
    "sin marcar como leído",
    "marca como no leídos los urgentes",
    "Marca los correos que no son urgentes como leídos",
    "do not mark them as read",
    "",
)


def _cargar():
    spec = importlib.util.spec_from_file_location("ms365_mail_marcado_auto", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(prompt, **metadata):
    return json.dumps({"task_id": "t1", "prompt": prompt, "metadata": {"thread_user_id": "u1", **metadata}})


class TestDeclaracion(unittest.TestCase):
    def test_reconoce_solo_instrucciones_explicitas_y_no_negadas(self):
        for texto in DECLARAN:
            with self.subTest(texto=texto):
                self.assertTrue(declara_marcar_leidos(texto))
        for texto in NO_DECLARAN:
            with self.subTest(texto=texto):
                self.assertFalse(declara_marcar_leidos(texto))

    def test_la_copia_del_plugin_coincide_con_el_backend(self):
        plugin = _cargar()
        for texto in DECLARAN + NO_DECLARAN:
            with self.subTest(texto=texto):
                self.assertEqual(plugin._declara_marcar_leidos(texto), declara_marcar_leidos(texto))

    def test_marcado_autorizado(self):
        self.assertTrue(marcado_autorizado({"conversation": True}, "Resumen de no leídos"))
        self.assertFalse(marcado_autorizado({"scheduled": True}, "Resumen de no leídos"))
        self.assertTrue(marcado_autorizado({"scheduled": True}, "Resumen y márcalos como leídos"))
        self.assertFalse(marcado_autorizado({"scheduled": True, CLAVE_METADATA: False}, "márcalos como leídos"))
        self.assertTrue(marcado_autorizado({"scheduled": True, CLAVE_METADATA: True}, ""))
        self.assertFalse(marcado_autorizado({"background_job": True}, "revisa la bandeja"))


class TestMetadataDelCron(unittest.TestCase):
    def _schedule(self, prompt, output_format=None):
        return SimpleNamespace(
            id="s1", owner_user_id="u1", created_by_agent_id=None, prompt=prompt, output_format=output_format
        )

    def test_resumen_de_no_leidos_no_autoriza(self):
        meta = scheduled_task_metadata(self._schedule("Envíame un resumen de los correos no leídos"))
        self.assertIs(meta[CLAVE_METADATA], False)

    def test_definicion_que_lo_pide_autoriza(self):
        meta = scheduled_task_metadata(self._schedule("Resume la bandeja y marca como leídos los correos del informe"))
        self.assertIs(meta[CLAVE_METADATA], True)
        meta = scheduled_task_metadata(self._schedule("Resume", output_format="Al final marca todo como leído."))
        self.assertIs(meta[CLAVE_METADATA], True)

    def test_schedule_sin_prompt_no_autoriza(self):
        meta = scheduled_task_metadata(SimpleNamespace(id="s1", owner_user_id=None, created_by_agent_id=None))
        self.assertIs(meta[CLAVE_METADATA], False)


class _ConPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = _cargar()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = []
        self.estado_al_marcar = []

    def _entorno(self, payload=None):
        env = {"HERMES_SESSION_CWD": self.tmp.name}
        if payload is not None:
            env["HERMESHQ_TASK_PAYLOAD"] = payload
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        if payload is None:
            os.environ.pop("HERMESHQ_TASK_PAYLOAD", None)

    def _graph(self, method, path, token, *a, **k):
        self.patches.append((method, path))
        ruta = Path(self.tmp.name) / "work" / "correo_unificado_estado.json"
        self.estado_al_marcar.append(ruta.read_text(encoding="utf-8") if ruta.is_file() else "")
        return {}

    def _con_graph(self, fn, *args):
        with (
            patch.object(self.plugin, "_task_user_id", return_value="u1"),
            patch.object(self.plugin, "_get_m365_token", return_value=("tok", "")),
            patch.object(self.plugin, "_graph", side_effect=self._graph),
        ):
            return json.loads(fn(*args))


class TestMarkReadEnTareaAutomatica(_ConPlugin):
    def _marcar(self, payload):
        self._entorno(payload)
        self.plugin._remember_ids([{"id": MID}])
        return self._con_graph(self.plugin._mark_read_tool, {"message_ids": [MID]})

    def test_cron_sin_declaracion_no_hace_patch(self):
        out = self._marcar(_payload("Resumen de no leídos", scheduled=True, **{CLAVE_METADATA: False}))
        self.assertFalse(out["success"])
        self.assertEqual(self.patches, [])

    def test_cron_antiguo_sin_bandera_usa_el_prompt(self):
        out = self._marcar(_payload("Resumen de no leídos", scheduled=True))
        self.assertFalse(out["success"])
        self.assertEqual(self.patches, [])

    def test_la_bandera_manda_sobre_el_prompt(self):
        self._marcar(_payload("márcalos como leídos", scheduled=True, **{CLAVE_METADATA: False}))
        self.assertEqual(self.patches, [])

    def test_cron_con_declaracion_hace_patch(self):
        out = self._marcar(_payload("Resumen y marca como leídos", scheduled=True, **{CLAVE_METADATA: True}))
        self.assertTrue(out["success"], out)
        self.assertEqual(self.patches, [("PATCH", f"/me/messages/{MID}")])

    def test_trabajo_en_segundo_plano_sin_declaracion_no_hace_patch(self):
        self._marcar(_payload("Revisa mi bandeja y resume", background_job=True))
        self.assertEqual(self.patches, [])

    def test_trabajo_en_segundo_plano_que_lo_pide_hace_patch(self):
        self._marcar(_payload("Revisa mi bandeja y márcalos como leídos", background_job=True))
        self.assertEqual(self.patches, [("PATCH", f"/me/messages/{MID}")])

    def test_chat_con_humano_sigue_marcando(self):
        self._marcar(_payload("márcalo", conversation=True))
        self.assertEqual(self.patches, [("PATCH", f"/me/messages/{MID}")])


class TestCorreoSyncEnTareaAutomatica(_ConPlugin):
    def _commit(self, payload, **extra):
        self._entorno(payload)
        sync = self.plugin._correo_sync_tool
        nuevo = {"id": MID, "subject": "Firma pendiente", "sender": "Gino", "received": "2026-10-26T17:50:00Z"}
        plan = json.loads(sync({"accion": "plan", "mensajes": [nuevo], "ahora": AHORA}))
        tok = plan["nuevos_sin_clasificar"][0]["token"]
        clasif = {
            "display_category": "CRÍTICO",
            "base_temperature": "CRÍTICO",
            "relationship": "PARA_MÍ",
            "operational_status": "PENDIENTE_DE_MÍ",
            "action_required": "Firmar",
            "requires_response": True,
        }
        args = {"accion": "commit", "clasificaciones": {tok: clasif}, "ahora": AHORA, "verificar_leidos": False}
        args.update(extra)
        return self._con_graph(sync, args)

    def test_cron_sin_declaracion_no_marca_aunque_el_modelo_lo_pida(self):
        digest = self._commit(
            _payload("Resumen de no leídos", scheduled=True, **{CLAVE_METADATA: False}), marcar_leidos=True
        )
        self.assertEqual(self.patches, [])
        self.assertNotIn("marcados_leidos", digest)
        self.assertEqual(digest["categorias_que_se_marcan_leidas"], [])
        self.assertIn("desactivado", digest["marcado_leidos"])

    def test_cron_con_declaracion_marca_despues_de_registrar_el_estado(self):
        digest = self._commit(_payload("Informe y marca como leídos", scheduled=True, **{CLAVE_METADATA: True}))
        self.assertEqual(self.patches, [("PATCH", f"/me/messages/{MID}")])
        self.assertEqual(digest["marcados_leidos"], {"marcados": 1, "fallos": 0})
        estado = json.loads(self.estado_al_marcar[0])
        self.assertIn(MID, [r.get("message_id") for r in estado["resolved"]])

    def test_cron_con_declaracion_y_marcar_leidos_false_no_marca(self):
        self._commit(_payload("marca como leídos", scheduled=True, **{CLAVE_METADATA: True}), marcar_leidos=False)
        self.assertEqual(self.patches, [])

    def test_chat_sin_pedirlo_no_marca_por_defecto(self):
        digest = self._commit(None)
        self.assertEqual(self.patches, [])
        self.assertNotIn("marcados_leidos", digest)

    def test_chat_con_preferencia_guardada_marca_esas_categorias(self):
        self._entorno(None)
        pref = {"accion": "preferencias", "marcar_leidos_categorias": ["CRÍTICO"]}
        r = json.loads(self.plugin._correo_sync_tool(pref))
        self.assertTrue(r["success"], r)
        self._commit(None)
        self.assertEqual(self.patches, [("PATCH", f"/me/messages/{MID}")])

    def test_preferencia_guardada_no_autoriza_un_cron(self):
        self._entorno(None)
        self.plugin._correo_sync_tool({"accion": "preferencias", "marcar_leidos_categorias": ["CRÍTICO"]})
        self._commit(_payload("Resumen de no leídos", scheduled=True, **{CLAVE_METADATA: False}))
        self.assertEqual(self.patches, [])

    def test_cron_no_puede_guardar_preferencias(self):
        self._entorno(_payload("Resumen", scheduled=True, **{CLAVE_METADATA: False}))
        r = json.loads(self.plugin._correo_sync_tool({"accion": "preferencias", "marcar_leidos_categorias": "todas"}))
        self.assertFalse(r["success"])

    def test_cron_sin_declaracion_no_marca_ahora(self):
        self._commit(_payload("Resumen", scheduled=True, **{CLAVE_METADATA: False}))
        r = self._con_graph(self.plugin._correo_sync_tool, {"accion": "marcar_leidos", "categorias": ["CRÍTICO"]})
        self.assertFalse(r["success"])
        self.assertEqual(self.patches, [])
