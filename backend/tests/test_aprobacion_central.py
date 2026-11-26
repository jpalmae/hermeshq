import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.aprobacion import EventoAprobacion
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.services import aprobacion_central as ac

pytestmark = [pytest.mark.integration, requires_database]
HUELLA_FALSA = "0" * 64
_TC = unittest.TestCase()


def correr(coro):
    return asyncio.run(coro)


class _Asserts:
    assertEqual = staticmethod(_TC.assertEqual)
    assertNotEqual = staticmethod(_TC.assertNotEqual)
    assertTrue = staticmethod(_TC.assertTrue)
    assertFalse = staticmethod(_TC.assertFalse)
    assertIn = staticmethod(_TC.assertIn)
    assertIsNone = staticmethod(_TC.assertIsNone)
    assertRaises = staticmethod(_TC.assertRaises)


@pytest.fixture
async def sesion(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        nodo = Node(id="nodo-aprob", name="nodo-aprob", hostname="localhost")
        s.add(nodo)
        await s.flush()
        for uid, nombre in (("u1", "bruno"), ("u2", "otra"), ("b1", "bridge-teams")):
            s.add(User(id=uid, username=nombre, display_name=nombre, password_hash="x"))
        for aid in ("ag1", "ag2"):
            s.add(Agent(id=aid, node_id=nodo.id, name=aid, slug=aid, workspace_path=f"/tmp/{aid}"))
        await s.commit()
    async with maker() as s:
        yield s


def _correo(**cambios):
    base = {
        "cuenta_emisora": "m365:u1",
        "para": ["ana@empresa.cl"],
        "cc": [],
        "cco": [],
        "asunto": "Informe mensual",
        "cuerpo_html": "<p>Hola Ana,</p><p>Adjunto el informe.</p><div>FIRMA PERSONAL</div>",
        "firma": "personal",
        "firma_sha256": "a" * 64,
        "adjuntos": [{"nombre": "informe.pdf", "sha256": "b" * 64, "bytes": 10, "content_id": ""}],
    }
    base.update(cambios)
    return base


CTX = ac.Contexto(agent_id="ag1", user_id="u1", conversacion_id="conv-1", task_id="t1", canal="public_chat")


def _ctx(**cambios):
    return ac.Contexto(**{**CTX.__dict__, **cambios})


class TestHuellaCanonica(unittest.TestCase):
    def test_orden_y_mayusculas_no_cambian_la_huella(self):
        a = ac.huella_canonica(ac.TIPO_CORREO, _correo(para=["Ana@Empresa.cl", "b@x.cl"]))
        b = ac.huella_canonica(ac.TIPO_CORREO, _correo(para=["b@x.cl", " ana@empresa.cl"]))
        self.assertEqual(a, b)

    def test_cada_campo_cambia_la_huella(self):
        base = ac.huella_canonica(ac.TIPO_CORREO, _correo())
        variantes = [
            _correo(para=["ana@empresa.co"]),
            _correo(cc=["jefe@empresa.cl"]),
            _correo(cco=["oculto@empresa.cl"]),
            _correo(asunto="Informe mensual."),
            _correo(cuerpo_html="<p>Hola Ana,</p><p>Adjunto el informe!</p><div>FIRMA PERSONAL</div>"),
            _correo(firma="corporativa"),
            _correo(firma_sha256="c" * 64),
            _correo(adjuntos=[{"nombre": "informe.pdf", "sha256": "d" * 64, "bytes": 10}]),
            _correo(cuenta_emisora="m365:u2"),
        ]
        for variante in variantes:
            self.assertNotEqual(base, ac.huella_canonica(ac.TIPO_CORREO, variante), variante)

    def test_direccion_texto_libre_rechazada(self):
        with self.assertRaises(ac.ErrorAprobacion) as err:
            ac.canonizar(ac.TIPO_CORREO, _correo(para=["Ana de finanzas"]))
        self.assertEqual(err.exception.codigo, "contenido_invalido")

    def test_vista_previa_muestra_lo_que_se_envia(self):
        canon = ac.canonizar(ac.TIPO_CORREO, _correo())
        vista = ac.vista_previa(ac.TIPO_CORREO, canon, ac.huella_canonica(ac.TIPO_CORREO, canon))
        for texto in (
            "ana@empresa.cl",
            "Informe mensual",
            "Adjunto el informe.",
            "FIRMA PERSONAL",
            "informe.pdf",
            "personal",
        ):
            self.assertIn(texto, vista)


class _ConBase(_Asserts):
    @pytest.fixture(autouse=True)
    def _bd(self, sesion):
        self.db = sesion


class TestTarjeta(_ConBase):
    async def test_tarjeta_lleva_id_y_huella_exacta(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        tarjeta = ac.tarjeta_adaptativa(aprob)
        datos = [a["data"] for a in tarjeta["actions"]]
        self.assertEqual({d["decision"] for d in datos}, {"aprobar", "rechazar"})
        self.assertTrue(all(d["aprobacion_id"] == aprob.id and d["huella"] == aprob.huella for d in datos))
        self.assertTrue(all(a["type"] == "Action.Submit" for a in tarjeta["actions"]))


class TestCicloAprobacion(_ConBase):
    async def _aprobada(self, contenido=None, ctx=CTX):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, contenido or _correo(), ctx)
        return await ac.decidir(self.db, aprob.id, True, ctx.user_id, "web", aprob.huella, ctx.conversacion_id)

    async def _eventos(self, aprob_id):
        filas = (
            await self.db.execute(
                select(EventoAprobacion)
                .where(EventoAprobacion.approval_id == aprob_id)
                .order_by(EventoAprobacion.creado_en)
            )
        ).scalars()
        return [e.evento for e in filas]

    async def test_ciclo_completo_con_auditoria(self):
        aprob = await self._aprobada()
        self.assertEqual(aprob.estado, "aprobada")
        reclamada = await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), _correo())
        self.assertEqual(reclamada.estado, "ejecutando")
        final = await ac.registrar_resultado(self.db, aprob.id, _ctx(task_id="t2"), reclamada.idempotency_key, True)
        self.assertEqual(final.estado, "ejecutada")
        self.assertEqual((await self._eventos(aprob.id)), ["creada", "aprobada", "ejecucion_iniciada", "ejecutada"])

    async def test_misma_accion_reutiliza_la_pendiente(self):
        a = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        b = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), _ctx(task_id="t9"))
        self.assertEqual(a.id, b.id)

    async def test_cambio_de_destinatario_tras_aprobar_exige_nueva_aprobacion(self):
        aprob = await self._aprobada()
        alterado = _correo(para=["ana@empresa-falsa.cl"])
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), alterado))
        self.assertEqual(err.exception.codigo, "huella_distinta")
        nueva = await ac.crear(self.db, ac.TIPO_CORREO, alterado, _ctx(task_id="t2"))
        self.assertEqual(nueva.estado, "pendiente")
        vieja = await ac.ver(self.db, aprob.id, "u1")
        self.assertEqual(vieja.estado, "expirada")
        self.assertEqual(vieja.reemplazada_por, nueva.id)
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, nueva.id, _ctx(task_id="t3"), alterado))
        self.assertEqual(err.exception.codigo, "pendiente")
        self.assertIn("denegado:huella_distinta", (await self._eventos(aprob.id)))

    async def test_cambio_de_firma_o_cuerpo_tras_aprobar_bloquea(self):
        aprob = await self._aprobada()
        for alterado in (_correo(firma="otra"), _correo(cuerpo_html="<p>Otro texto</p>")):
            with self.assertRaises(ac.ErrorAprobacion) as err:
                (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), alterado))
            self.assertEqual(err.exception.codigo, "huella_distinta")

    async def test_aprobacion_de_otro_usuario_rechazada(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.decidir(self.db, aprob.id, True, "u2", "teams", aprob.huella, "conv-1"))
        self.assertEqual(err.exception.codigo, "otro_usuario")
        self.assertEqual((await ac.ver(self.db, aprob.id, "u1")).estado, "pendiente")
        with self.assertRaises(ac.ErrorAprobacion):
            (await ac.ver(self.db, aprob.id, "u2"))

    async def test_ejecucion_por_otro_usuario_o_agente_rechazada(self):
        aprob = await self._aprobada()
        for ctx in (_ctx(user_id="u2", task_id="t2"), _ctx(agent_id="ag2", task_id="t2")):
            with self.assertRaises(ac.ErrorAprobacion) as err:
                (await ac.reclamar_ejecucion(self.db, aprob.id, ctx, _correo()))
            self.assertEqual(err.exception.codigo, "otro_usuario")
        self.assertEqual((await ac.ver(self.db, aprob.id, "u1")).estado, "aprobada")

    async def test_aprobacion_de_otra_conversacion_rechazada(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.decidir(self.db, aprob.id, True, "u1", "teams", aprob.huella, "conv-2"))
        self.assertEqual(err.exception.codigo, "otra_conversacion")
        aprobada = await ac.decidir(self.db, aprob.id, True, "u1", "web", aprob.huella, "conv-1")
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprobada.id, _ctx(conversacion_id="conv-2", task_id="t2"), _correo()))
        self.assertEqual(err.exception.codigo, "otra_conversacion")

    async def test_tarjeta_de_version_anterior_no_aprueba_la_nueva(self):
        vieja = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        nueva = await ac.crear(self.db, ac.TIPO_CORREO, _correo(asunto="Otro"), CTX)
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.decidir(self.db, nueva.id, True, "u1", "teams", vieja.huella, "conv-1"))
        self.assertEqual(err.exception.codigo, "huella_distinta")
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.decidir(self.db, vieja.id, True, "u1", "teams", vieja.huella, "conv-1"))
        self.assertEqual(err.exception.codigo, "expirada")

    async def test_doble_ejecucion_solo_una(self):
        aprob = await self._aprobada()
        primera = await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), _correo())
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t3"), _correo()))
        self.assertEqual(err.exception.codigo, "ya_ejecutada")
        (await ac.registrar_resultado(self.db, aprob.id, _ctx(task_id="t2"), primera.idempotency_key, True))
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t4"), _correo()))
        self.assertEqual(err.exception.codigo, "ya_ejecutada")
        self.assertEqual((await self._eventos(aprob.id)).count("ejecucion_iniciada"), 1)

    async def test_transicion_es_atomica(self):
        aprob = await self._aprobada()
        valores = {"ejecucion_iniciada_en": datetime.now(UTC)}
        self.assertTrue(await ac._transicion(self.db, aprob.id, ("aprobada",), "ejecutando", **valores))
        self.assertFalse(await ac._transicion(self.db, aprob.id, ("aprobada",), "ejecutando", **valores))

    async def test_resultado_exige_clave_de_idempotencia(self):
        aprob = await self._aprobada()
        (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), _correo()))
        with self.assertRaises(ac.ErrorAprobacion):
            (await ac.registrar_resultado(self.db, aprob.id, _ctx(task_id="t2"), "clave-falsa", True))

    async def test_cron_sin_aprobacion_bloqueado(self):
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.crear(self.db, ac.TIPO_CORREO, _correo(), _ctx(no_atendida=True)))
        self.assertEqual(err.exception.codigo, "no_atendida")
        aprob = await self._aprobada()
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(no_atendida=True, task_id="cron"), _correo()))
        self.assertEqual(err.exception.codigo, "no_atendida")
        self.assertEqual((await ac.aprobadas_para(self.db, _ctx(no_atendida=True))), [])
        self.assertEqual((await ac.ver(self.db, aprob.id, "u1")).estado, "aprobada")

    async def test_sin_conversacion_real_no_se_crea(self):
        for ctx in (_ctx(conversacion_id=""), _ctx(conversacion_id="u1"), _ctx(user_id="")):
            with self.assertRaises(ac.ErrorAprobacion):
                (await ac.crear(self.db, ac.TIPO_CORREO, _correo(), ctx))

    async def test_expiracion(self):
        ahora = datetime.now(UTC)
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX, ttl=timedelta(minutes=5), ahora=ahora)
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (
                await ac.decidir(
                    self.db, aprob.id, True, "u1", "web", aprob.huella, "conv-1", ahora=ahora + timedelta(minutes=6)
                )
            )
        self.assertEqual(err.exception.codigo, "expirada")

    async def test_aprobada_vencida_no_se_ejecuta(self):
        aprob = await self._aprobada()
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (
                await ac.reclamar_ejecucion(
                    self.db, aprob.id, _ctx(task_id="t2"), _correo(), ahora=datetime.now(UTC) + timedelta(hours=5)
                )
            )
        self.assertEqual(err.exception.codigo, "expirada")

    async def test_rechazada_no_se_ejecuta(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        (await ac.decidir(self.db, aprob.id, False, "u1", "web", aprob.huella, "conv-1"))
        with self.assertRaises(ac.ErrorAprobacion) as err:
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), _correo()))
        self.assertEqual(err.exception.codigo, "rechazada")

    async def test_ejecucion_colgada_queda_desconocida_y_no_se_reintenta(self):
        aprob = await self._aprobada()
        (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t2"), _correo()))
        cuenta = await ac.mantenimiento(self.db, ahora=datetime.now(UTC) + timedelta(minutes=20))
        self.assertEqual(cuenta["inciertas"], 1)
        actual = await ac.ver(self.db, aprob.id, "u1")
        self.assertEqual((actual.estado, actual.envio_estado), ("ejecutando", "desconocida"))
        with self.assertRaises(ac.ErrorAprobacion):
            (await ac.reclamar_ejecucion(self.db, aprob.id, _ctx(task_id="t3"), _correo()))


class TestAprobacionTextual(_ConBase):
    async def test_si_aprueba_la_unica_pendiente_de_la_conversacion(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        hecha = await ac.aprobar_por_texto(self.db, "ag1", "u1", "conv-1", "sí, envíalo", "public_chat")
        self.assertEqual(hecha.id, aprob.id)
        self.assertEqual(hecha.estado, "aprobada")
        self.assertEqual(hecha.canal_decision, "public_chat:texto")

    async def test_no_aprueba_desde_otra_conversacion_u_otro_usuario(self):
        (await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX))
        self.assertIsNone(await ac.aprobar_por_texto(self.db, "ag1", "u1", "conv-2", "sí", "teams"))
        self.assertIsNone(await ac.aprobar_por_texto(self.db, "ag1", "u2", "conv-1", "sí", "teams"))

    async def test_texto_con_cambios_no_aprueba(self):
        (await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX))
        for texto in ("sí pero cambia el asunto", "¿lo enviaste?", "no", ""):
            self.assertIsNone((await ac.aprobar_por_texto(self.db, "ag1", "u1", "conv-1", texto, "teams")), texto)


class TestInmutabilidadEnBaseDeDatos(_ConBase):
    async def test_contenido_aprobado_no_se_puede_modificar(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        await ac.decidir(self.db, aprob.id, True, "u1", "web", aprob.huella, "conv-1")
        for columna, valor in (
            ("huella", "'" + "e" * 64 + "'"),
            ("contenido_json", "'{}'::jsonb"),
            ("propietario_user_id", "'u2'"),
            ("decidido_por_user_id", "NULL"),
        ):
            with self.assertRaises(Exception):
                await self.db.execute(text(f"UPDATE approvals SET {columna} = {valor} WHERE id = :i"), {"i": aprob.id})
            await self.db.rollback()

    async def test_estado_final_no_se_reabre_y_eventos_solo_insercion(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        await ac.decidir(self.db, aprob.id, False, "u1", "web", aprob.huella, "conv-1")
        with self.assertRaises(Exception):
            await self.db.execute(text("UPDATE approvals SET estado = 'aprobada' WHERE id = :i"), {"i": aprob.id})
        await self.db.rollback()
        with self.assertRaises(Exception):
            await self.db.execute(text("DELETE FROM approval_events WHERE approval_id = :i"), {"i": aprob.id})
        await self.db.rollback()

    async def test_el_agente_no_puede_aprobar_como_usuario(self):
        aprob = await ac.crear(self.db, ac.TIPO_CORREO, _correo(), CTX)
        with self.assertRaises(Exception):
            await self.db.execute(
                text("UPDATE approvals SET estado = 'aprobada', decidido_por_user_id = 'u2' WHERE id = :i"),
                {"i": aprob.id},
            )
        await self.db.rollback()


class TestContextoDesdeTarea(unittest.TestCase):
    def _db(self, tarea, usuario):
        db = MagicMock()

        async def get(modelo, ident, **_kw):
            return tarea if modelo.__name__ == "Task" else usuario

        db.get = get
        return db

    def _tarea(self, **meta):
        return SimpleNamespace(id="t1", agent_id="ag1", status="running", created_by_user_id=None, metadata_json=meta)

    def test_conversacion_de_public_chat(self):
        usuario = SimpleNamespace(id="u1", is_active=True, username="bruno")
        ctx = correr(
            ac.contexto_desde_tarea(
                self._db(self._tarea(thread_user_id="u1", public_session_id="s1", source="public_chat"), usuario),
                "ag1",
                "t1",
            )
        )
        self.assertEqual((ctx.user_id, ctx.conversacion_id, ctx.no_atendida), ("u1", "s1", False))

    def test_cuenta_del_bridge_no_es_solicitante(self):
        usuario = SimpleNamespace(id="b1", is_active=True, username="bridge-teams")
        with self.assertRaises(ac.ErrorAprobacion) as err:
            correr(
                ac.contexto_desde_tarea(
                    self._db(self._tarea(thread_user_id="b1", public_session_id="s1"), usuario), "ag1", "t1"
                )
            )
        self.assertEqual(err.exception.codigo, "usuario_no_humano")

    def test_tarea_programada_es_no_atendida_y_sin_conversacion(self):
        usuario = SimpleNamespace(id="u1", is_active=True, username="bruno")
        ctx = correr(
            ac.contexto_desde_tarea(self._db(self._tarea(thread_user_id="u1", scheduled=True), usuario), "ag1", "t1")
        )
        self.assertTrue(ctx.no_atendida)
        self.assertEqual(ctx.conversacion_id, "")

    def test_tarea_de_otro_agente_o_terminada(self):
        usuario = SimpleNamespace(id="u1", is_active=True, username="bruno")
        for tarea in (
            SimpleNamespace(id="t1", agent_id="ag2", status="running", created_by_user_id=None, metadata_json={}),
            SimpleNamespace(id="t1", agent_id="ag1", status="completed", created_by_user_id=None, metadata_json={}),
        ):
            with self.assertRaises(ac.ErrorAprobacion) as err:
                correr(ac.contexto_desde_tarea(self._db(tarea, usuario), "ag1", "t1"))
            self.assertEqual(err.exception.codigo, "tarea_invalida")
