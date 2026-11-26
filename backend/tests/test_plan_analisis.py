import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import hermeshq.scripts.hermes_task_runner as runner
from hermeshq.routers import internal_agents
from hermeshq.services import plan_analisis as pa
from hermeshq.services.public_chat_service import PublicChatService, SimpleRateLimiter

PEDIDO_BRUNO = (
    "[Archivos adjuntos, ya disponibles en tu workspace:\n- Datos.xlsx (1360 KB) -> uploads/e0c2.xlsx]\n"
    "Analiza el archivo Datos.xlsx, que contiene dos hojas: Hoja OT y Hoja Solicitudes. Objetivo: determinar la existencia "
    "de reprocesos. Criterio temporal: define y justifica una ventana temporal razonable (ej. 30, 60 o 90 días)."
)
PLAN = (
    "📋 Plan de análisis\n- Datos: Hoja OT 3.315 OT, Hoja Solicitudes 51 solicitudes.\n- Ventana: 30 días desde Fecha fin real.\n"
    "Responde sí para ejecutar con estos criterios, o dime qué cambiar."
)


class TestReglas(unittest.TestCase):
    def test_caso_real_el_pedido_de_bruno_necesita_plan(self):
        self.assertTrue(pa.necesita_plan(PEDIDO_BRUNO))

    def test_pedidos_sin_analisis_o_que_piden_ir_directo_no(self):
        self.assertFalse(pa.necesita_plan("usa este archivo y haceme un dashboard interactivo en html"))
        self.assertFalse(pa.necesita_plan("analiza mis correos de hoy"))
        self.assertFalse(pa.necesita_plan(PEDIDO_BRUNO + " Hazlo directo, sin preguntar."))

    def test_respuestas_al_plan(self):
        for si in ("sí", "Sí, dale", "ok", "ejecuta", "sí pero usa 60 días", "usa 60 días", "excluye las OT03"):
            self.assertTrue(pa.responde_al_plan(si), si)
        for no in ("gracias", "hola", "¿qué es un candidato?", "no", "cancela", ""):
            self.assertFalse(pa.responde_al_plan(no), no)

    def test_pedido_confirmado_lleva_plan_y_correcciones(self):
        texto = pa.pedido_confirmado(PEDIDO_BRUNO, PLAN, "sí pero usa 60 días")
        self.assertIn("PLAN CONFIRMADO", texto)
        self.assertIn("Ventana: 30 días", texto)
        self.assertIn("CORRECCIONES DEL USUARIO AL PLAN", texto)
        self.assertIn("usa 60 días", texto)
        self.assertIn("hq_avance", texto)
        self.assertNotIn("CORRECCIONES", pa.pedido_confirmado(PEDIDO_BRUNO, PLAN, "sí"))


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: self._items)

    def scalar(self):
        return 0


def _servicio(tareas, workspace="/tmp"):
    service = PublicChatService.__new__(PublicChatService)
    service.rate_limiter = SimpleRateLimiter(max_requests=100)
    service.ip_rate_limiter = SimpleRateLimiter(max_requests=100)
    service.supervisor = SimpleNamespace(submit_task=AsyncMock())
    session = SimpleNamespace(id="s1", api_key_id="k1", agent_id="a1", last_activity=None, sender_id=None)
    service.validate_session = AsyncMock(return_value=session)
    service._check_monthly_quota = AsyncMock()
    service._resolve_identity = AsyncMock(return_value=("u1", None))
    service._running_interactive_task = AsyncMock(return_value=None)
    service._racha_del_chat = AsyncMock(return_value={})
    service.debe_reiniciar = MagicMock(return_value=False)
    agente = SimpleNamespace(id="a1", status="running", workspace_path=workspace)
    clave = SimpleNamespace(id="k1", bound_user_id=None, requests_per_month=0, tokens_per_month=0, status="running")

    async def obtener(modelo, ident):
        return agente if modelo.__name__ == "Agent" else clave

    db = MagicMock()
    db.get = AsyncMock(side_effect=obtener)
    db.execute = AsyncMock(return_value=_Result(tareas))
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return service, db


def _agregadas(db, clase="Task"):
    return [c.args[0] for c in db.add.call_args_list if c.args[0].__class__.__name__ == clase]


class TestFlujoDelChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for objetivo in (
            "hermeshq.services.ejecucion_aprobada.avisos_pendientes",
            "hermeshq.services.auto_background._recent_history",
        ):
            parche = patch(objetivo, AsyncMock(return_value=None if "avisos" in objetivo else ""))
            parche.start()
            self.addCleanup(parche.stop)
        parche = patch(
            "hermeshq.services.public_chat_service.con_archivos_generados", AsyncMock(side_effect=lambda *a, **k: a[-1])
        )
        parche.start()
        self.addCleanup(parche.stop)

    async def test_caso_real_el_pedido_largo_de_analisis_primero_propone_un_plan(self):
        service, db = _servicio([])
        await service.send_message("s1", "tok", PEDIDO_BRUNO, db)
        tareas = _agregadas(db)
        self.assertEqual(len(tareas), 1)
        self.assertFalse(tareas[0].metadata_json.get("background_job"))
        self.assertTrue(tareas[0].metadata_json["plan_analisis"])
        self.assertTrue(tareas[0].prompt.startswith("[ETAPA DE PLAN DEL ANÁLISIS"))
        self.assertIn("Analiza el archivo Datos.xlsx", tareas[0].prompt)

    async def test_la_confirmacion_con_correccion_lanza_un_solo_trabajo_con_el_plan(self):
        plan = SimpleNamespace(
            id="plan1",
            status="completed",
            response=PLAN,
            queued_at=datetime.now(UTC) - timedelta(minutes=2),
            metadata_json={"public_session_id": "s1", "plan_analisis": True, "pedido_original": PEDIDO_BRUNO},
        )
        service, db = _servicio([plan])
        with patch("hermeshq.services.auto_background._load_recent_messages", AsyncMock(return_value=[])):
            await service.send_message("s1", "tok", "sí, pero usa 60 días", db)
        trabajos = [t for t in _agregadas(db) if (t.metadata_json or {}).get("background_job")]
        self.assertEqual(len(trabajos), 1)
        trabajo = trabajos[0]
        self.assertEqual(trabajo.metadata_json["plan_task_id"], "plan1")
        self.assertTrue(trabajo.metadata_json["analisis_con_plan"])
        self.assertIn("Ventana: 30 días", trabajo.prompt)
        self.assertIn("usa 60 días", trabajo.prompt)
        self.assertTrue(trabajo.metadata_json["background_title"].startswith("Analiza el archivo"))

    async def test_un_saludo_tras_el_plan_no_lanza_el_trabajo(self):
        plan = SimpleNamespace(
            id="plan1",
            status="completed",
            response=PLAN,
            queued_at=datetime.now(UTC),
            metadata_json={"public_session_id": "s1", "plan_analisis": True, "pedido_original": PEDIDO_BRUNO},
        )
        service, db = _servicio([plan])
        await service.send_message("s1", "tok", "gracias", db)
        self.assertEqual([t for t in _agregadas(db) if (t.metadata_json or {}).get("background_job")], [])

    async def test_correccion_durante_el_trabajo_se_anota_para_el_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            en_curso = SimpleNamespace(
                id="job1",
                status="running",
                queued_at=datetime.now(UTC),
                metadata_json={
                    "background_job": True,
                    "analisis_con_plan": True,
                    "thread_user_id": "u1",
                    "origen_public_session_id": "s1",
                },
            )
            service, db = _servicio([en_curso], workspace=tmp)
            task_id, reply = await service.send_message("s1", "tok", "excluye las OT03", db)
            self.assertIsNone(task_id)
            self.assertIn("Anotado", reply)
            guardadas = json.loads((Path(tmp) / ".hermes" / "correcciones-job1.json").read_text(encoding="utf-8"))
            self.assertEqual([c["texto"] for c in guardadas], ["excluye las OT03"])
            with patch.dict(os.environ, {}), _en(tmp):
                self.assertEqual(runner.correcciones_del_usuario("job1"), ["excluye las OT03"])


class _en:
    def __init__(self, carpeta):
        self.carpeta = carpeta

    def __enter__(self):
        self.antes = os.getcwd()
        os.chdir(self.carpeta)

    def __exit__(self, *exc):
        os.chdir(self.antes)


class TestRunnerCorrecciones(unittest.TestCase):
    def test_mensaje_de_correcciones(self):
        mensaje = runner.mensaje_de_correcciones(["usa 60 días", "excluye las OT03"])
        self.assertEqual(mensaje["role"], "user")
        self.assertIn("«usa 60 días»", mensaje["content"])
        self.assertIn("vuelve a calcular", mensaje["content"])

    def test_sin_archivo_no_hay_correcciones(self):
        with tempfile.TemporaryDirectory() as tmp, _en(tmp):
            self.assertEqual(runner.correcciones_del_usuario("nada"), [])
            self.assertEqual(runner.correcciones_del_usuario(None), [])


def _plugin_schedule():
    ruta = Path(__file__).resolve().parents[1] / "hermeshq" / "plugin_templates" / "hermeshq_schedule" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermeshq_schedule_avance", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestAvance(unittest.IsolatedAsyncioTestCase):
    def test_la_herramienta_solo_sirve_en_segundo_plano(self):
        plugin = _plugin_schedule()
        with patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps({"task_id": "t1", "metadata": {}})}):
            self.assertFalse(json.loads(plugin.hq_avance_tool({"texto": "x"}))["success"])
        payload = {"task_id": "job1", "metadata": {"background_job": True}}
        with (
            patch.dict(os.environ, {"HERMESHQ_TASK_PAYLOAD": json.dumps(payload)}),
            patch.object(plugin, "_api_request", return_value=json.dumps({"success": True})) as api,
        ):
            plugin.hq_avance_tool({"texto": "Criterios: 60 días. 12 casos hasta ahora."})
        api.assert_called_once_with(
            "POST",
            "/agents/self/background-jobs/avance",
            {"task_id": "job1", "texto": "Criterios: 60 días. 12 casos hasta ahora."},
        )

    async def _avance(self, tarea, enviados=1):
        db = MagicMock()
        db.get = AsyncMock(return_value=tarea)
        db.commit = AsyncMock()
        with patch(
            "hermeshq.services.schedule_notifications.enqueue_avance_de_trabajo", AsyncMock(return_value=enviados)
        ):
            return await internal_agents.avance_de_trabajo(
                internal_agents.InternalAvance(task_id="job1", texto="12 casos"), agent=SimpleNamespace(id="a1"), db=db
            )

    async def test_el_avance_se_entrega_y_se_limita(self):
        tarea = SimpleNamespace(agent_id="a1", status="running", metadata_json={"background_job": True})
        self.assertTrue((await self._avance(tarea))["success"])
        self.assertEqual(len(tarea.metadata_json["avances"]), 1)
        with self.assertRaises(HTTPException) as seguido:
            await self._avance(tarea)
        self.assertEqual(seguido.exception.status_code, 429)
        tarea.metadata_json["avances"] = [(datetime.now(UTC) - timedelta(minutes=10)).isoformat()] * 5
        with self.assertRaises(HTTPException) as tope:
            await self._avance(tarea)
        self.assertEqual(tope.exception.status_code, 429)

    async def test_fuera_de_un_trabajo_en_segundo_plano_se_rechaza(self):
        for tarea in (
            SimpleNamespace(agent_id="a1", status="running", metadata_json={}),
            SimpleNamespace(agent_id="otro", status="running", metadata_json={"background_job": True}),
            SimpleNamespace(agent_id="a1", status="completed", metadata_json={"background_job": True}),
        ):
            with self.assertRaises(HTTPException) as contexto:
                await self._avance(tarea)
            self.assertEqual(contexto.exception.status_code, 403)


PEDIDO_ANA = "genera un informe interactivo por rol del procedimiento demo"


class TestFuenteSinIdentificar(unittest.TestCase):
    def test_caso_real_el_procedimiento_nombrado_sin_adjuntar_pide_ubicar_la_fuente(self):
        self.assertTrue(pa.necesita_fuente(PEDIDO_ANA))
        self.assertFalse(pa.necesita_plan(PEDIDO_ANA))
        self.assertIn("📄 Fuente para tu pedido", pa.con_directiva_plan(PEDIDO_ANA, "fuente"))

    def test_con_adjunto_ruta_o_nombre_de_archivo_no_hace_falta(self):
        self.assertFalse(
            pa.necesita_fuente("[Archivos adjuntos, ya disponibles:\n- p.docx -> uploads/x.docx]\n" + PEDIDO_ANA)
        )
        self.assertFalse(pa.necesita_fuente(PEDIDO_ANA + " usando Procedimiento_Demo_v3.docx"))
        self.assertFalse(pa.necesita_fuente(PEDIDO_ANA + " que está en C:\\Users\\d\\OneDrive - U\\Proc"))
        self.assertFalse(pa.necesita_fuente("hazme un informe interactivo de ventas"))
        self.assertFalse(
            pa.necesita_fuente("hazme un informe interactivo con las ventas del Excel que acabas de hacer")
        )
        self.assertFalse(pa.necesita_fuente("haz una presentación del informe que te mandé"))

    def test_elegir_un_candidato_responde_a_la_etapa(self):
        for eleccion in ("el primero", "1", "usa el segundo", "ese", "el más reciente", "[Archivos adjuntos, ya ...]"):
            self.assertTrue(pa.responde_al_plan(eleccion), eleccion)

    def test_el_trabajo_usa_solo_la_fuente_elegida(self):
        texto = pa.pedido_confirmado(
            PEDIDO_ANA, "📄 Fuente para tu pedido\n1. Procedimiento Demo.docx", "el 1", "fuente"
        )
        self.assertIn("usa SOLO la fuente que eligió", texto)
        self.assertIn("Procedimiento Demo.docx", texto)
        self.assertNotIn("PLAN CONFIRMADO", texto)


class TestFuenteEnElChat(TestFlujoDelChat):
    async def test_caso_real_ana_primero_se_ubica_la_fuente(self):
        service, db = _servicio([])
        with patch("hermeshq.services.public_chat_service.es_pedido_largo", return_value=True):
            await service.send_message("s1", "tok", PEDIDO_ANA, db)
        tareas = _agregadas(db)
        self.assertEqual(len(tareas), 1)
        self.assertFalse(tareas[0].metadata_json.get("background_job"))
        self.assertEqual(tareas[0].metadata_json["etapa_plan"], "fuente")
        self.assertTrue(tareas[0].prompt.startswith("[ETAPA DE PREPARACIÓN"))

    async def test_la_eleccion_lanza_el_trabajo_con_la_fuente(self):
        etapa = SimpleNamespace(
            id="fuente1",
            status="completed",
            response="📄 Fuente para tu pedido\n1. Procedimiento Demo v3.docx (Calidad/Procedimientos, 2026-10-02)",
            queued_at=datetime.now(UTC),
            metadata_json={
                "public_session_id": "s1",
                "plan_analisis": True,
                "etapa_plan": "fuente",
                "pedido_original": PEDIDO_ANA,
            },
        )
        service, db = _servicio([etapa])
        with patch("hermeshq.services.auto_background._load_recent_messages", AsyncMock(return_value=[])):
            await service.send_message("s1", "tok", "el primero", db)
        trabajos = [t for t in _agregadas(db) if (t.metadata_json or {}).get("background_job")]
        self.assertEqual(len(trabajos), 1)
        self.assertIn("Procedimiento Demo v3.docx", trabajos[0].prompt)
        self.assertIn("usa SOLO la fuente", trabajos[0].prompt)


class TestAvisoDeCorte(unittest.TestCase):
    def test_caso_real_el_corte_de_la_guardia_no_lleva_terminado(self):
        from hermeshq.services.schedule_notifications import admite_que_no_pudo

        self.assertTrue(
            admite_que_no_pudo(
                "⚠️ Detuve esta respuesta: el modelo entró en un bucle (el contexto se compactó una y otra vez sin "
                "avanzar) y no iba a terminar."
            )
        )
        self.assertFalse(admite_que_no_pudo("Listo. El informe quedó en la carpeta."))
