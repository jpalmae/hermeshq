from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hermeshq.routers import public_chat
from hermeshq.services.public_chat_service import (
    PUBLIC_CHAT_USER_ID,
    PublicChatService,
    SimpleRateLimiter,
    ambito_de_bot,
    clave_de_cuota,
    consumir_cupos,
)


def _clave(**campos):
    base = {"id": "k1", "bound_user_id": None, "trust_sender_id": False, "bot_channel_id": None}
    return SimpleNamespace(**{**base, **campos})


def _servicio():
    return PublicChatService(SimpleNamespace(), SimpleNamespace(), None)


def _request(ip_socket, real_ip=None):
    headers = {"x-real-ip": real_ip} if real_ip else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=ip_socket) if ip_socket else None)


class TestSimpleRateLimiter:
    def test_excedido_no_consume(self):
        limiter = SimpleRateLimiter(max_requests=2, window_seconds=60)
        assert not limiter.excedido("a")
        assert not limiter.excedido("a")
        assert limiter.check("a") and limiter.check("a")
        assert limiter.excedido("a")
        assert not limiter.check("a")
        assert not limiter.excedido("b")

    def test_ventana_vencida_libera(self):
        limiter = SimpleRateLimiter(max_requests=1, window_seconds=60)
        with patch("hermeshq.services.public_chat_service.time.time", return_value=1000.0):
            assert limiter.check("a")
            assert limiter.excedido("a")
        with patch("hermeshq.services.public_chat_service.time.time", return_value=1061.0):
            assert not limiter.excedido("a")
            assert limiter.check("a")


class TestConsumirCupos:
    def test_todo_o_nada(self):
        usuario = SimpleRateLimiter(max_requests=5)
        bot = SimpleRateLimiter(max_requests=1)
        assert consumir_cupos([(bot, "canal:x"), (usuario, "u1")])
        assert not consumir_cupos([(bot, "canal:x"), (usuario, "u2")])
        assert "u2" not in usuario._requests
        assert len(usuario._requests["u1"]) == 1

    def test_rechazo_por_usuario_no_gasta_el_global(self):
        usuario = SimpleRateLimiter(max_requests=1)
        bot = SimpleRateLimiter(max_requests=10)
        assert consumir_cupos([(bot, "canal:x"), (usuario, "u1")])
        for _ in range(5):
            assert not consumir_cupos([(bot, "canal:x"), (usuario, "u1")])
        assert len(bot._requests["canal:x"]) == 1

    def test_acepta_dobles_con_solo_check(self):
        permisivo = SimpleNamespace(check=lambda _k: True)
        cerrado = SimpleNamespace(check=lambda _k: False)
        assert consumir_cupos([(permisivo, "a")])
        assert not consumir_cupos([(cerrado, "a")])


class TestClaveDeCuota:
    def test_clave_fija_usa_su_usuario_aunque_llegue_otro_remitente(self):
        clave = _clave(bound_user_id="ana")
        assert clave_de_cuota(clave, remitente="29:beto", usuario_resuelto="beto") == (
            "canal:predeterminado|usuario:ana"
        )

    def test_clave_de_confianza_usa_el_usuario_resuelto(self):
        clave = _clave(trust_sender_id=True, bot_channel_id="c2")
        assert clave_de_cuota(clave, remitente="29:x", usuario_resuelto="ana") == "canal:c2|usuario:ana"

    def test_remitente_sin_vincular_tiene_cuota_propia(self):
        clave = _clave(trust_sender_id=True)
        a = clave_de_cuota(clave, remitente="29:a", usuario_resuelto=PUBLIC_CHAT_USER_ID)
        b = clave_de_cuota(clave, remitente="29:b", usuario_resuelto=None)
        assert a == "canal:predeterminado|remitente:29:a"
        assert a != b

    def test_mismo_usuario_en_dos_bots_son_cuotas_distintas(self):
        bot1 = _clave(id="k1", bound_user_id="ana")
        bot2 = _clave(id="k2", bound_user_id="ana", bot_channel_id="c2")
        assert clave_de_cuota(bot1, remitente=None, usuario_resuelto="ana") != clave_de_cuota(
            bot2, remitente=None, usuario_resuelto="ana"
        )

    def test_mismo_usuario_dos_agentes_del_mismo_bot_comparten_cuota(self):
        a3 = _clave(id="k3", bound_user_id="u3", bot_channel_id="c2")
        a4 = _clave(id="k4", bound_user_id="u3", bot_channel_id="c2")
        assert clave_de_cuota(a3, remitente=None, usuario_resuelto="u3") == clave_de_cuota(
            a4, remitente=None, usuario_resuelto="u3"
        )

    def test_anonima_no_tiene_cuota_de_usuario(self):
        assert clave_de_cuota(_clave(), remitente="29:a", usuario_resuelto=None) is None
        assert clave_de_cuota(_clave(trust_sender_id=True), remitente="  ", usuario_resuelto=None) is None
        assert clave_de_cuota(None, remitente="29:a", usuario_resuelto="ana") is None

    def test_ambito_de_bot(self):
        assert ambito_de_bot(_clave(bound_user_id="ana")) == "canal:predeterminado"
        assert ambito_de_bot(_clave(trust_sender_id=True, bot_channel_id="c2")) == "canal:c2"
        assert ambito_de_bot(_clave(id="widget")) == "clave:widget"


class TestCuposDelServicio:
    def test_mensaje_identificado_usa_usuario_y_bot_no_ip(self):
        servicio = _servicio()
        cupos = servicio.cupos_de_mensaje(
            _clave(bound_user_id="ana"), SimpleNamespace(sender_id=None), "s1", "ana", "10.0.0.1"
        )
        assert cupos == [
            (servicio.bot_rate_limiter, "canal:predeterminado"),
            (servicio.user_rate_limiter, "canal:predeterminado|usuario:ana"),
        ]

    def test_mensaje_anonimo_conserva_sesion_e_ip(self):
        servicio = _servicio()
        cupos = servicio.cupos_de_mensaje(
            _clave(id="w"), SimpleNamespace(sender_id=None), "s1", PUBLIC_CHAT_USER_ID, "10.0.0.1"
        )
        assert cupos == [
            (servicio.bot_rate_limiter, "clave:w"),
            (servicio.rate_limiter, "s1"),
            (servicio.ip_rate_limiter, "10.0.0.1"),
        ]

    def test_sesion_identificada_y_anonima(self):
        servicio = _servicio()
        assert servicio.cupos_de_sesion(_clave(trust_sender_id=True), "29:a", "ana", "10.0.0.1") == [
            (servicio.bot_session_limiter, "canal:predeterminado"),
            (servicio.user_session_limiter, "canal:predeterminado|usuario:ana"),
        ]
        assert servicio.cupos_de_sesion(_clave(id="w"), None, None, "10.0.0.1") == [
            (servicio.bot_session_limiter, "clave:w"),
            (servicio.session_create_limiter, "10.0.0.1"),
        ]

    def test_limites_salen_de_settings(self):
        ajustes = SimpleNamespace(
            public_chat_session_messages_per_minute=1,
            public_chat_ip_messages_per_minute=2,
            public_chat_ip_sessions_per_minute=3,
            public_chat_user_messages_per_minute=4,
            public_chat_user_sessions_per_minute=5,
            public_chat_bot_messages_per_minute=6,
            public_chat_bot_sessions_per_minute=7,
            public_chat_ip_auth_failures_per_minute=8,
        )
        with patch("hermeshq.services.public_chat_service.get_settings", return_value=ajustes):
            servicio = _servicio()
        assert [
            servicio.rate_limiter.max_requests,
            servicio.ip_rate_limiter.max_requests,
            servicio.session_create_limiter.max_requests,
            servicio.user_rate_limiter.max_requests,
            servicio.user_session_limiter.max_requests,
            servicio.bot_rate_limiter.max_requests,
            servicio.bot_session_limiter.max_requests,
            servicio.auth_failure_limiter.max_requests,
        ] == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_fallos_de_autenticacion_bloquean_la_ip_antes_de_autenticar(self):
        servicio = _servicio()
        servicio.auth_failure_limiter = SimpleRateLimiter(max_requests=2)
        servicio.anotar_fallo_de_autenticacion("1.1.1.1")
        servicio.frenar_ip_con_fallos("1.1.1.1")
        servicio.anotar_fallo_de_autenticacion("1.1.1.1")
        try:
            servicio.frenar_ip_con_fallos("1.1.1.1")
        except ValueError as e:
            assert str(e) == "Rate limit exceeded"
        else:
            raise AssertionError("la IP con fallos debía quedar frenada")
        servicio.frenar_ip_con_fallos("2.2.2.2")
        servicio.frenar_ip_con_fallos("")


class TestIpDelCliente:
    def test_x_real_ip_solo_desde_proxy_de_confianza(self):
        assert public_chat._get_client_ip(_request("127.0.0.1", "203.0.113.7")) == "203.0.113.7"
        assert public_chat._get_client_ip(_request("172.18.0.3", "203.0.113.7")) == "203.0.113.7"
        assert public_chat._get_client_ip(_request("198.51.100.4", "203.0.113.7")) == "198.51.100.4"
        assert public_chat._get_client_ip(_request("testclient", "203.0.113.7")) == "testclient"
        assert public_chat._get_client_ip(_request("198.51.100.4")) == "198.51.100.4"
        assert public_chat._get_client_ip(_request(None, "203.0.113.7")) == ""

    def test_lista_de_proxies_configurable(self):
        ajustes = SimpleNamespace(public_chat_trusted_proxies=["198.51.100.0/24", "no-es-una-red"])
        with patch.object(public_chat, "get_settings", return_value=ajustes):
            assert public_chat._get_client_ip(_request("198.51.100.4", "203.0.113.7")) == "203.0.113.7"
            assert public_chat._get_client_ip(_request("127.0.0.1", "203.0.113.7")) == "127.0.0.1"
