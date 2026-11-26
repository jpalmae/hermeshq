from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from hermeshq.services import cita_teams

AGENTE = "agente-1"
CLAVE = "clave-asistentebot"
ANA = "user-ana"
BETO = "user-beto"
CITADO = datetime.fromtimestamp(1794342705193 / 1000, UTC)
MENSAJE = '<quoted messageId="1794342705193"/>dame el detalle de la 1'


def _notif(id_, user, clave=CLAVE, agente=AGENTE, delta=timedelta(seconds=2), status="delivered"):
    return SimpleNamespace(
        id=id_,
        task_id=f"task-{id_}",
        agent_id=agente,
        api_key_id=clave,
        recipient_user_id=user,
        status=status,
        delivered_at=CITADO + delta,
        title="Alertas de seguridad (últimas 5)",
        body=f"| 1 | alerta de {user} |",
    )


def _respuesta(id_, user, clave=CLAVE, delta=timedelta(seconds=3)):
    return SimpleNamespace(
        id=id_,
        agent_id=AGENTE,
        status="completed",
        completed_at=CITADO - delta,
        response=f"respuesta para {user}",
        metadata_json={"conversation": True, "thread_user_id": user, "public_chat_api_key_id": clave},
    )


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def scalars(self):
        return self

    def all(self):
        return self.filas


def _propio(id_, delta, contenido="Hace un informe interactivo con dos árboles y guárdalo en Proyectos"):
    return SimpleNamespace(id=id_, role="user", content=contenido, created_at=CITADO + delta)


class _Db:
    def __init__(self, notificaciones, respuestas, propios=None):
        self.lotes = [notificaciones, respuestas, propios or []]

    async def execute(self, _consulta):
        return _Resultado(self.lotes.pop(0))


def _resolver(notificaciones, respuestas, user=ANA, contenido=MENSAJE, propios=None):
    db = _Db(notificaciones, respuestas, propios)
    return asyncio.run(cita_teams.resolver_cita(db, AGENTE, CLAVE, user, contenido))


class TestExtraerCita:
    def test_sin_marca_no_hay_cita(self):
        assert cita_teams.extraer_cita("dame el detalle de la 1") == (False, None, "dame el detalle de la 1")

    def test_marca_da_instante_y_texto_limpio(self):
        hay, instante, resto = cita_teams.extraer_cita(MENSAJE)
        assert hay and instante == CITADO and resto == "dame el detalle de la 1"
        assert instante.strftime("%Y-%m-%d %H:%M") == "2026-11-10 20:31"


class TestResolverCita:
    def test_resuelve_al_cron_entregado_al_mismo_usuario(self):
        cita = _resolver([_notif("n-ana", ANA), _notif("n-beto", BETO, delta=timedelta(seconds=1))], [])
        assert cita.resuelta and cita.tipo == "programada"
        assert cita.notification_id == "n-ana" and cita.task_id == "task-n-ana"
        assert "alerta de user-ana" in cita.prefijo() and "user-beto" not in cita.prefijo()
        assert "2026-11-10 17:31" in cita.prefijo()
        assert cita.texto_usuario == "dame el detalle de la 1"

    def test_resuelve_a_la_respuesta_del_agente(self):
        cita = _resolver([], [_respuesta("t-ana", ANA)])
        assert cita.tipo == "respuesta" and cita.task_id == "t-ana" and "respuesta para user-ana" in cita.texto

    def test_elige_el_mas_cercano(self):
        cita = _resolver([_notif("lejos", ANA, delta=timedelta(minutes=4))], [_respuesta("cerca", ANA)])
        assert cita.task_id == "cerca"

    def test_control_negativo_otro_usuario(self):
        cita = _resolver([_notif("n-beto", BETO)], [_respuesta("t-beto", BETO)])
        assert not cita.resuelta and "no pude identificar" in cita.prefijo()
        assert "user-beto" not in cita.prefijo()

    def test_control_negativo_otro_bot_u_otro_agente(self):
        cita = _resolver(
            [_notif("otro-bot", ANA, clave="clave-bot-2"), _notif("otro-agente", ANA, agente="ag-2")],
            [_respuesta("t-otro-bot", ANA, clave="clave-bot-2")],
        )
        assert not cita.resuelta

    def test_control_negativo_fuera_de_ventana_o_no_entregada(self):
        cita = _resolver([_notif("vieja", ANA, delta=-timedelta(hours=1)), _notif("pend", ANA, status="pending")], [])
        assert not cita.resuelta

    def test_usuario_anonimo_no_consulta(self):
        cita = asyncio.run(cita_teams.resolver_cita(None, AGENTE, CLAVE, "__public_chat__", MENSAJE))
        assert not cita.resuelta and cita.texto_usuario == "dame el detalle de la 1"

    def test_sin_marca_no_toca_nada(self):
        assert asyncio.run(cita_teams.resolver_cita(None, AGENTE, CLAVE, ANA, "hola")) is None


class TestCitaDelPropioMensaje:
    def test_caso_real_ana_su_instruccion_no_la_respuesta_posterior(self):
        respuesta_posterior = _respuesta("aviso", ANA, delta=-timedelta(seconds=6.55))
        cita = _resolver([], [respuesta_posterior], propios=[_propio("m-ana", timedelta(seconds=6.46))])
        assert cita.resuelta and cita.tipo == "propio"
        assert "informe interactivo con dos árboles" in cita.texto
        prefijo = cita.prefijo()
        assert "SU PROPIO mensaje" in prefijo and "no la busques en el correo" in prefijo

    def test_control_negativo_cita_la_respuesta_del_bot_aunque_el_usuario_escriba_despues(self):
        cita = _resolver([], [_respuesta("t-ana", ANA)], propios=[_propio("siguiente", timedelta(seconds=20))])
        assert cita.tipo == "respuesta" and cita.task_id == "t-ana"

    def test_notificacion_cercana_gana_al_mensaje_propio(self):
        cita = _resolver([_notif("n-ana", ANA)], [], propios=[_propio("m", timedelta(seconds=30))])
        assert cita.tipo == "programada"

    def test_respuesta_lejana_pierde_contra_el_mensaje_propio(self):
        cita = _resolver(
            [], [_respuesta("vieja", ANA, delta=timedelta(minutes=1))], propios=[_propio("m", timedelta(seconds=4))]
        )
        assert cita.tipo == "propio"

    def test_sin_mensaje_propio_sigue_resolviendo_al_bot(self):
        cita = _resolver([], [_respuesta("vieja", ANA, delta=timedelta(minutes=1))])
        assert cita.tipo == "respuesta"

    def test_el_mensaje_que_trae_la_cita_no_se_cita_a_si_mismo(self):
        actual = _propio("actual", timedelta(seconds=40), contenido=MENSAJE)
        cita = _resolver([], [], propios=[actual])
        assert not cita.resuelta

    def test_quita_marcas_de_cita_del_texto_propio(self):
        contenido = '<quoted messageId="1794340000000"/> y esto también'
        cita = _resolver([], [], propios=[_propio("m", timedelta(seconds=2), contenido=contenido)])
        assert cita.texto == "y esto también"


class TestRespuestaAlMensajeCitado:
    def test_caso_real_bateria_el_aviso_inmediato_no_es_el_mensaje_citado(self):
        aviso = _respuesta("aviso", ANA, delta=-timedelta(seconds=1.74))
        propio = _propio("m-instruccion", timedelta(seconds=1.71))
        cita = _resolver([], [aviso], propios=[propio])
        assert cita.tipo == "propio" and "informe interactivo" in cita.texto

    def test_respuesta_del_bot_anterior_al_mensaje_sigue_siendo_citable(self):
        cita = _resolver(
            [], [_respuesta("t-ana", ANA, delta=timedelta(seconds=2))], propios=[_propio("m", timedelta(seconds=1))]
        )
        assert cita.tipo == "respuesta" and cita.task_id == "t-ana"
