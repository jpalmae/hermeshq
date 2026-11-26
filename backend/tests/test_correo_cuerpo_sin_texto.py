import json
from unittest.mock import patch

import pytest

from tests.test_ms365_mail_get import _load


@pytest.fixture
def plugin():
    modulo = _load()
    with (
        patch.object(modulo, "_task_user_id", return_value="u1"),
        patch.object(modulo, "_aprobacion_central_activa", return_value=False),
        patch.object(modulo, "_get_m365_token", return_value=("tok", None)),
        patch.object(modulo, "_reemplazar_yo", side_effect=lambda v, *_a, **_k: v),
    ):
        yield modulo


@pytest.mark.parametrize("cuerpo", ["HTML", "html", " Texto ", "<<body>>", "…", "contenido"])
def test_cuerpo_que_es_solo_un_nombre_de_formato_se_rechaza(plugin, cuerpo):
    for solo_borrador in (True, False):
        r = json.loads(
            plugin._enviar_o_guardar({"to": ["a@b.cl"], "subject": "Hola", "body": cuerpo}, solo_borrador=solo_borrador)
        )
        assert r["success"] is False and "nombre de un formato" in r["error"]


class _SinRed(Exception):
    pass


def _llamar_sin_red(plugin, cuerpo):
    with patch("urllib.request.urlopen", side_effect=_SinRed("sin red")):
        try:
            return json.loads(
                plugin._enviar_o_guardar({"to": ["a@b.cl"], "subject": "Hola", "body": cuerpo}, solo_borrador=True)
            )
        except _SinRed:
            return {"success": None, "error": "paso la validacion"}


def test_control_negativo_cuerpo_real_no_se_rechaza_por_esta_regla(plugin):
    r = _llamar_sin_red(plugin, "Muy buen día Lucía,\n\nTe confirmo la asistencia de Diego.")
    assert "nombre de un formato" not in str(r.get("error", ""))


def test_control_negativo_cuerpo_que_menciona_html_pero_tiene_texto(plugin):
    r = _llamar_sin_red(plugin, "Te envío la página HTML que pediste.")
    assert "nombre de un formato" not in str(r.get("error", ""))


def _enviar_re(plugin, destinos, propia="yo@u.cl"):
    with (
        patch.object(plugin, "_graph", return_value={"mail": propia}),
        patch.object(
            plugin,
            "_reemplazar_yo",
            side_effect=lambda v, *_a, **_k: [propia if str(x).lower() == "yo" else x for x in v],
        ),
        patch("urllib.request.urlopen", side_effect=_SinRed("sin red")),
    ):
        try:
            return json.loads(
                plugin._enviar_o_guardar(
                    {
                        "to": destinos,
                        "subject": "RE: Asistencia",
                        "body": "Muy buen día Lucía,\n\nTe confirmo la asistencia.",
                    }
                )
            )
        except _SinRed:
            return {"success": None, "error": "paso las validaciones"}


def test_caso_bruno_un_re_enviado_solo_al_propio_usuario_se_permite(plugin):
    assert "empieza con" not in str(_enviar_re(plugin, ["yo"]).get("error", ""))


def test_control_negativo_un_re_a_otra_persona_sigue_rechazado(plugin):
    assert "empieza con" in str(_enviar_re(plugin, ["lucia@u.cl"]).get("error", ""))


def test_la_descripcion_de_send_dice_que_envíamelo_a_mí_es_un_envio_no_un_borrador():
    from pathlib import Path

    ruta = (
        Path(__file__).resolve().parents[1]
        / "hermeshq"
        / "integration_packages"
        / "ms365-mail"
        / "plugin"
        / "__init__.py"
    )
    assert "Envíamelo solo a mí" in ruta.read_text(encoding="utf-8")
