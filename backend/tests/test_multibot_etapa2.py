from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from conftest import requires_database
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_crons_propietario import _escenario

from hermeshq.models.bot_channel import BotChannel, PublicChatDeliveryPref, UserChannelIdentity
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.routers import aprobaciones, bot_channels, internal_agents, public_chat, scheduled_tasks
from hermeshq.schemas.scheduled_task import ScheduledTaskDelivery
from hermeshq.services.channel_user_resolver import resolve_channel_user
from hermeshq.services.public_chat_service import PUBLIC_CHAT_USER_ID, PublicChatService, _hash_key
from hermeshq.services.schedule_notifications import enqueue_schedule_notification, resolver_claves_entrega

pytestmark = [pytest.mark.integration, requires_database]

AAD_ANA = "aad-ana"
VIVA = timedelta(minutes=1)


def _clave(e, usuario, etiqueta, canal=None, confianza=False):
    return PublicChatApiKey(
        key_hash=f"hash-{etiqueta}",
        key_prefix="pk_",
        label=f"bridge-teams-{etiqueta}",
        agent_id=e["agente"].id,
        allowed_domains=["*"],
        bound_user_id=None if confianza else e[usuario].id,
        trust_sender_id=confianza,
        bot_channel_id=canal.id if canal is not None else None,
        last_notification_poll_at=datetime.now(UTC) - VIVA,
    )


async def _mundo(db, identidades=True):
    e = await _escenario(db)
    for tarea in e["tareas"].values():
        tarea.status = "completed"
    e["ana"].teams_id = "29:ana-b1"
    e["c1"] = BotChannel(slug="teams-principal", display_name="Principal", is_default=True)
    e["c2"] = BotChannel(slug="teams-2", display_name="Segundo")
    db.add_all([e["c1"], e["c2"]])
    await db.flush()
    if identidades:
        db.add_all(
            [
                UserChannelIdentity(
                    user_id=e["ana"].id, bot_channel_id=e["c1"].id, external_user_id="29:ana-b1", aad_object_id=AAD_ANA
                ),
                UserChannelIdentity(
                    user_id=e["ana"].id, bot_channel_id=e["c2"].id, external_user_id="29:ana-b2", aad_object_id=AAD_ANA
                ),
            ]
        )
    e["k1"] = _clave(e, "ana", "ana-b1")
    e["k2"] = _clave(e, "ana", "ana-b2", e["c2"])
    db.add_all([e["k1"], e["k2"]])
    await db.commit()
    return e


def _corrida(e, cron):
    return Task(
        agent_id=e["agente"].id,
        prompt=cron.prompt,
        status="completed",
        created_by_user_id=e["ana"].id,
        metadata_json={"scheduled": True, "scheduled_task_id": cron.id, "thread_user_id": e["ana"].id},
    )


async def _cron_entregado(db, e):
    cron = e["crons"]["ana_aprobado"]
    cron.notify_public_chat = True
    corrida = _corrida(e, cron)
    db.add(corrida)
    await db.commit()
    total = await enqueue_schedule_notification(db, corrida, success=True, summary="ok")
    await db.commit()
    filas = await db.execute(select(PublicChatNotification).where(PublicChatNotification.task_id == corrida.id))
    return total, list(filas.scalars().all())


def _request(api_key, **cabeceras):
    service = MagicMock()
    service.validate_api_key = AsyncMock(return_value=api_key)
    service.notification_limiter = SimpleNamespace(check=lambda _k: True)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(public_chat_service=service)), headers=cabeceras)


class TestResolucionPorCanal:
    async def test_el_29_del_bot2_resuelve_a_ana_y_el_heredado_no_vale_en_el_bot2(self, db_session):
        e = await _mundo(db_session)
        assert (
            await resolve_channel_user(db_session, "microsoft_teams", "29:ana-b2", bot_channel_id=e["c2"].id)
        ).id == e["ana"].id
        assert await resolve_channel_user(db_session, "microsoft_teams", "29:ana-b1", bot_channel_id=e["c2"].id) is None

    async def test_aad_resuelve_si_el_29_no_esta_registrado(self, db_session):
        e = await _mundo(db_session)
        usuario = await resolve_channel_user(
            db_session, "microsoft_teams", "29:nuevo", bot_channel_id=e["c2"].id, aad_object_id=AAD_ANA
        )
        assert usuario.id == e["ana"].id

    async def test_canal_por_defecto_sin_identidades_usa_teams_id(self, db_session):
        e = await _mundo(db_session, identidades=False)
        assert (await resolve_channel_user(db_session, "microsoft_teams", "29:ana-b1")).id == e["ana"].id
        assert (
            await resolve_channel_user(db_session, "microsoft_teams", "29:ana-b1", bot_channel_id=e["c1"].id)
        ).id == e["ana"].id

    async def test_aad_compartido_entre_dos_usuarios_queda_anonimo(self, db_session):
        e = await _mundo(db_session)
        db_session.add(
            UserChannelIdentity(user_id=e["beto"].id, bot_channel_id=e["c2"].id, aad_object_id=AAD_ANA, tenant_id="t2")
        )
        await db_session.commit()
        assert (
            await resolve_channel_user(
                db_session, "microsoft_teams", "29:x", bot_channel_id=e["c2"].id, aad_object_id=AAD_ANA
            )
            is None
        )


class TestClaveDeConfianzaEnElBot2:
    async def test_sesion_de_confianza_resuelve_y_el_aviso_sale_con_el_29_del_bot2(self, db_session):
        e = await _mundo(db_session)
        e["k2"].bound_user_id = None
        e["k2"].trust_sender_id = True
        await db_session.commit()
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), MagicMock(), None)
        creada = await servicio.create_session(e["k2"], db_session, sender_id="29:ana-b2", aad_object_id=AAD_ANA)
        sesion = await db_session.get(PublicChatSession, creada["session_id"])
        assert sesion.user_id == e["ana"].id and sesion.aad_object_id == AAD_ANA
        total, avisos = await _cron_entregado(db_session, e)
        por_clave = {a.api_key_id: a for a in avisos}
        assert total == 2
        aviso = por_clave[e["k2"].id]
        assert aviso.sender_id == "29:ana-b2" and aviso.recipient_external_id == "29:ana-b2"
        assert aviso.recipient_aad_object_id == AAD_ANA and aviso.bot_channel_id == e["c2"].id
        assert aviso.origin == "cron"
        assert por_clave[e["k1"].id].sender_id is None and por_clave[e["k1"].id].recipient_external_id == "29:ana-b1"


class TestDestinoDelCron:
    async def test_delivery_key_id_entrega_solo_en_esa_clave(self, db_session):
        e = await _mundo(db_session)
        e["crons"]["ana_aprobado"].delivery_key_id = e["k2"].id
        await db_session.commit()
        total, avisos = await _cron_entregado(db_session, e)
        assert total == 1 and [a.api_key_id for a in avisos] == [e["k2"].id]

    async def test_preferencia_del_usuario_entrega_solo_en_esa_clave(self, db_session):
        e = await _mundo(db_session)
        db_session.add(PublicChatDeliveryPref(user_id=e["ana"].id, agent_id=e["agente"].id, cron_key_id=e["k2"].id))
        await db_session.commit()
        total, avisos = await _cron_entregado(db_session, e)
        assert total == 1 and [a.api_key_id for a in avisos] == [e["k2"].id]

    async def test_delivery_key_de_otro_usuario_cae_a_la_regla_de_respaldo(self, db_session):
        e = await _mundo(db_session)
        ajena = _clave(e, "beto", "beto-b1")
        db_session.add(ajena)
        await db_session.flush()
        e["crons"]["ana_aprobado"].delivery_key_id = ajena.id
        await db_session.commit()
        total, avisos = await _cron_entregado(db_session, e)
        assert total == 2 and ajena.id not in {a.api_key_id for a in avisos}

    async def test_clave_desactivada_cae_a_la_regla_de_respaldo(self, db_session):
        e = await _mundo(db_session)
        e["crons"]["ana_aprobado"].delivery_key_id = e["k2"].id
        e["k2"].is_active = False
        await db_session.commit()
        total, avisos = await _cron_entregado(db_session, e)
        assert total == 1 and [a.api_key_id for a in avisos] == [e["k1"].id]

    async def test_aviso_de_delegacion_por_la_clave_preferida(self, db_session):
        e = await _mundo(db_session)
        db_session.add(PublicChatDeliveryPref(user_id=e["ana"].id, agent_id=e["agente"].id, notice_key_id=e["k2"].id))
        await db_session.commit()
        claves = await resolver_claves_entrega(db_session, e["ana"], e["agente"].id, "delegacion")
        assert [k.id for k in claves] == [e["k2"].id]

    async def test_cron_creado_desde_el_chat_toma_la_clave_de_la_sesion(self, db_session):
        e = await _mundo(db_session)
        origen = Task(
            agent_id=e["agente"].id,
            prompt="programa",
            status="running",
            execution_lane=uuid4().hex[:16],
            metadata_json={
                "source": "public_chat",
                "thread_user_id": e["ana"].id,
                "public_chat_api_key_id": e["k2"].id,
            },
        )
        db_session.add(origen)
        await db_session.commit()
        res = await internal_agents.create_own_schedule(
            internal_agents.InternalScheduleCreate(
                name="Nuevo cron", cron_expression="0 9 * * 1-5", prompt="haz algo", task_id=origen.id
            ),
            e["agente"],
            db_session,
        )
        assert (await db_session.get(ScheduledTask, res["schedule_id"])).delivery_key_id == e["k2"].id


class TestPatchDestinoDelCron:
    async def test_valida_dueno_y_agente(self, db_session):
        e = await _mundo(db_session)
        cron = e["crons"]["ana_aprobado"]
        ajena = _clave(e, "beto", "beto-b1")
        db_session.add(ajena)
        await db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await scheduled_tasks.update_scheduled_task_delivery(
                cron.id, ScheduledTaskDelivery(delivery_key_id=ajena.id), e["admin"], db_session
            )
        assert exc.value.status_code == 422
        leido = await scheduled_tasks.update_scheduled_task_delivery(
            cron.id, ScheduledTaskDelivery(delivery_key_id=e["k2"].id), e["ana"], db_session
        )
        assert leido.delivery_key_id == e["k2"].id
        limpio = await scheduled_tasks.update_scheduled_task_delivery(
            cron.id, ScheduledTaskDelivery(delivery_key_id=None), e["ana"], db_session
        )
        assert limpio.delivery_key_id is None

    async def test_otro_usuario_no_puede_moverlo(self, db_session):
        e = await _mundo(db_session)
        with pytest.raises(HTTPException) as exc:
            await scheduled_tasks.update_scheduled_task_delivery(
                e["crons"]["ana_aprobado"].id, ScheduledTaskDelivery(delivery_key_id=e["k2"].id), e["beto"], db_session
            )
        assert exc.value.status_code == 403


class TestCabeceras:
    async def test_x_bot_channel_que_no_coincide_da_403(self, db_session):
        e = await _mundo(db_session)
        with pytest.raises(HTTPException) as exc:
            await public_chat.list_notifications(
                _request(e["k2"], **{"x-bot-channel": "teams-principal"}), 20, "k", db_session
            )
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "canal_incorrecto"
        with pytest.raises(HTTPException):
            await public_chat.list_notifications(_request(e["k1"], **{"x-bot-channel": "teams-2"}), 20, "k", db_session)

    async def test_cabecera_correcta_o_ausente_se_acepta(self, db_session):
        e = await _mundo(db_session)
        assert (
            await public_chat.list_notifications(_request(e["k2"], **{"x-bot-channel": "teams-2"}), 20, "k", db_session)
            == []
        )
        assert (
            await public_chat.list_notifications(
                _request(e["k1"], **{"x-bot-channel": "teams-principal"}), 20, "k", db_session
            )
            == []
        )
        assert await public_chat.list_notifications(_request(e["k2"]), 20, "k", db_session) == []

    async def test_canal_que_exige_cabecera(self, db_session):
        e = await _mundo(db_session)
        e["c2"].require_channel_header = True
        await db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await public_chat.list_notifications(_request(e["k2"]), 20, "k", db_session)
        assert exc.value.status_code == 403

    async def test_dos_bridges_con_la_misma_clave(self, db_session):
        e = await _mundo(db_session)
        await public_chat.list_notifications(_request(e["k2"], **{"x-bridge-instance": "uno"}), 20, "k", db_session)
        assert e["k2"].last_poll_instance == "uno"
        with patch.object(public_chat.logger, "error") as error:
            await public_chat.list_notifications(_request(e["k2"], **{"x-bridge-instance": "dos"}), 20, "k", db_session)
        assert any("clave_compartida" in llamada.args[0] for llamada in error.call_args_list)
        e["c2"].require_channel_header = True
        await db_session.commit()
        with pytest.raises(HTTPException) as exc:
            await public_chat.list_notifications(
                _request(e["k2"], **{"x-bot-channel": "teams-2", "x-bridge-instance": "uno"}), 20, "k", db_session
            )
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "clave_compartida"


class TestSesionYTarjetas:
    async def test_aad_distinto_al_registrado_en_clave_fija_da_403(self, db_session):
        e = await _mundo(db_session)
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), MagicMock(), None)
        with pytest.raises(ValueError, match="Sender is not the bound user"):
            await servicio.create_session(e["k2"], db_session, aad_object_id="aad-intruso")
        creada = await servicio.create_session(e["k2"], db_session, aad_object_id=AAD_ANA)
        sesion = await db_session.get(PublicChatSession, creada["session_id"])
        assert sesion.user_id == e["ana"].id

    async def test_tarjeta_con_identidad_registrada(self, db_session):
        e = await _mundo(db_session)
        sesion = SimpleNamespace(id="s", sender_id=None, aad_object_id=None)
        assert await aprobaciones.verificar_quien_pulsa(db_session, e["k2"], sesion, e["ana"].id, AAD_ANA, None) is None
        assert await aprobaciones.verificar_quien_pulsa(db_session, e["k2"], sesion, e["ana"].id, "aad-beto", None)

    async def test_r9_clave_de_confianza_con_29_en_la_sesion_acepta_el_aad(self, db_session):
        e = await _mundo(db_session, identidades=False)
        e["k2"].bound_user_id = None
        e["k2"].trust_sender_id = True
        sesion = SimpleNamespace(id="s", sender_id="29:ana-b2", aad_object_id=AAD_ANA)
        assert await aprobaciones.verificar_quien_pulsa(db_session, e["k2"], sesion, e["ana"].id, AAD_ANA, None) is None
        assert await aprobaciones.verificar_quien_pulsa(db_session, e["k2"], sesion, e["ana"].id, "aad-otro", None)


class TestAdministracion:
    async def test_canales_e_identidades(self, db_session):
        e = await _mundo(db_session, identidades=False)
        admin = e["admin"]
        nuevo = await bot_channels.crear_canal(
            bot_channels.BotChannelCreate(slug="teams-3", display_name="Tercero", is_default=True), admin, db_session
        )
        canales = {c.slug: c for c in await bot_channels.listar_canales(admin, db_session)}
        assert canales["teams-3"].is_default and not canales["teams-principal"].is_default
        with pytest.raises(HTTPException) as exc:
            await bot_channels.crear_canal(
                bot_channels.BotChannelCreate(slug="teams-3", display_name="Otra vez"), admin, db_session
            )
        assert exc.value.status_code == 409
        for objeto in (admin, e["ana"], e["beto"], e["c2"]):
            await db_session.refresh(objeto)
        editado = await bot_channels.editar_canal(
            nuevo.id, bot_channels.BotChannelUpdate(require_channel_header=True), admin, db_session
        )
        assert editado.require_channel_header
        creada = await bot_channels.crear_identidad(
            e["ana"].id,
            bot_channels.IdentidadCreate(
                bot_channel_id=e["c2"].id, external_user_id="29:ana-b2", aad_object_id=AAD_ANA
            ),
            admin,
            db_session,
        )
        with pytest.raises(HTTPException) as exc:
            await bot_channels.crear_identidad(
                e["beto"].id,
                bot_channels.IdentidadCreate(bot_channel_id=e["c2"].id, external_user_id="29:ana-b2"),
                admin,
                db_session,
            )
        assert exc.value.status_code == 409
        assert [i.id for i in await bot_channels.listar_identidades(e["ana"].id, admin, db_session)] == [creada.id]
        await bot_channels.borrar_identidad(e["ana"].id, creada.id, admin, db_session)
        assert await bot_channels.listar_identidades(e["ana"].id, admin, db_session) == []


class TestCompatibilidadUnSoloBot:
    async def test_sin_filas_nuevas_la_entrega_y_la_resolucion_son_las_de_siempre(self, db_session):
        e = await _escenario(db_session)
        e["ana"].teams_id = "29:ana-b1"
        fija = _clave(e, "ana", "fija")
        confianza = _clave(e, "ana", "confianza", confianza=True)
        db_session.add_all([fija, confianza])
        await db_session.commit()
        assert (await resolve_channel_user(db_session, "microsoft_teams", "29:ana-b1")).id == e["ana"].id
        servicio = PublicChatService(async_sessionmaker(db_session.bind, class_=AsyncSession), MagicMock(), None)
        sesion = PublicChatSession(
            api_key_id=confianza.id, agent_id=e["agente"].id, session_token=_hash_key("t"), sender_id="29:ana-b1"
        )
        identidad, _ = await servicio._resolve_identity(confianza, sesion, db_session)
        assert identidad == e["ana"].id
        anonima = PublicChatSession(
            api_key_id=confianza.id, agent_id=e["agente"].id, session_token=_hash_key("u"), sender_id="29:otro"
        )
        assert (await servicio._resolve_identity(confianza, anonima, db_session))[0] == PUBLIC_CHAT_USER_ID
        total, avisos = await _cron_entregado(db_session, e)
        por_clave = {a.api_key_id: a for a in avisos}
        assert total == 2
        assert por_clave[confianza.id].sender_id == "29:ana-b1" and por_clave[fija.id].sender_id is None
        for aviso in avisos:
            assert aviso.bot_channel_id is None and aviso.recipient_external_id is None
            assert aviso.recipient_aad_object_id is None
        items = await public_chat.list_notifications(_request(fija), 20, "k", db_session)
        assert [i.title for i in items] == [e["crons"]["ana_aprobado"].name]
        assert fija.last_poll_instance is None
