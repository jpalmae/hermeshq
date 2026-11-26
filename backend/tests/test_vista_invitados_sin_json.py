import hermeshq.scripts.hermes_task_runner as runner

BASE = {"titulo": "Reunión X", "inicio": "2026-11-17T13:00:00", "fin": "2026-11-17T13:30:00"}


class TestAvisoDeInvitados:
    def test_invitados_verificados_no_muestran_aviso_ni_json(self):
        vista = {
            **BASE,
            "verificacion_directorio": {
                "a@contoso.cl": "✓ en directorio: Ana",
                "s@contoso.cl": "✓ en directorio: Sala",
            },
        }
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        assert "Revisa" not in texto
        assert "{" not in texto

    def test_solo_los_dudosos_se_listan_en_texto_plano(self):
        vista = {
            **BASE,
            "verificacion_directorio": {
                "a@contoso.cl": "✓ en directorio: Ana",
                "x@gmail.com": "⚠ NO está en el directorio",
            },
        }
        texto = runner.texto_vista_calendario("ms365_calendar_create_event", vista)
        assert "Revisa estos invitados: x@gmail.com: ⚠ NO está en el directorio" in texto
        assert "a@contoso.cl" not in texto
        assert "{" not in texto
