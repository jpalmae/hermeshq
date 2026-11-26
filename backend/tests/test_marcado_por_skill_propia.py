import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermeshq.services.cron_schedule import scheduled_task_metadata
from hermeshq.services.marcado_leidos import (
    CLAVE_METADATA,
    definicion_declara_marcar_leidos,
    skills_nombradas,
)

SKILL_ANA = (
    '---\nname: resumen-correos-equipo\ndescription: "Resumen de correos no leídos… Importantes arriba; marca automático '
    'como leído los no importantes."\n---\n8. Marcar AUTOMÁTICAMENTE como leído los 📬 otros con ms365_mail_mark_read, '
    "TODOS los IDs en una sola llamada, sin preguntar\n- NUNCA marcar como leído los importantes sin confirmación explícita\n"
)
SKILL_SEMANAL = (
    "---\nname: resumen-semanal-equipo\n---\nGenera el informe HTML semanal. No marques correos como leídos.\n"
)
PROMPT_ANA = "Ejecuta la skill resumen-correos-equipo: genera el resumen de correos no leídos de todas las carpetas."


def _cron(prompt, output_format=None):
    return SimpleNamespace(
        id="s1",
        prompt=prompt,
        output_format=output_format,
        definition_version=1,
        owner_user_id="u1",
        created_by_agent_id="a1",
    )


@pytest.fixture
def espacio():
    with tempfile.TemporaryDirectory() as raiz:
        skills = Path(raiz) / ".hermes" / "skills"
        for categoria, nombre, texto in (
            ("ms365", "resumen-correos-equipo", SKILL_ANA),
            ("ms365", "resumen-semanal-equipo", SKILL_SEMANAL),
            ("hermeshq-managed", "ms365-mail", "Para marcar como leídos usa ms365_mail_mark_read."),
        ):
            (skills / categoria / nombre).mkdir(parents=True)
            (skills / categoria / nombre / "SKILL.md").write_text(texto, encoding="utf-8")
        yield raiz


class TestSkillPropiaDelCron:
    def test_caso_real_ana_su_skill_pide_marcar_los_otros(self, espacio):
        assert definicion_declara_marcar_leidos(_cron(PROMPT_ANA), espacio) is True
        meta = scheduled_task_metadata(_cron(PROMPT_ANA), workspace_path=espacio)
        assert meta[CLAVE_METADATA] is True

    def test_sin_el_espacio_de_trabajo_sigue_como_antes(self):
        assert definicion_declara_marcar_leidos(_cron(PROMPT_ANA)) is False

    def test_una_skill_que_no_lo_pide_no_autoriza(self, espacio):
        prompt = "Ejecuta la skill resumen-semanal-equipo: genera el informe HTML interactivo"
        assert definicion_declara_marcar_leidos(_cron(prompt), espacio) is False

    def test_una_skill_administrada_no_autoriza(self, espacio):
        assert definicion_declara_marcar_leidos(_cron("Ejecuta la skill ms365-mail y resume"), espacio) is False

    def test_un_cron_sin_skill_ni_declaracion_no_marca(self, espacio):
        prompt = 'Revisa la carpeta "Bajas por hacer" (ms365_mail_list con unread_only=true) y resume.'
        assert definicion_declara_marcar_leidos(_cron(prompt), espacio) is False

    def test_una_skill_inexistente_no_autoriza(self, espacio):
        assert definicion_declara_marcar_leidos(_cron("Ejecuta la skill no-existe-aqui"), espacio) is False

    def test_el_nombre_no_permite_salir_del_espacio(self, espacio):
        assert skills_nombradas("Ejecuta la skill ../../etc/passwd") == []
        assert (
            definicion_declara_marcar_leidos(_cron("Ejecuta la skill ../ms365/resumen-correos-equipo"), espacio)
            is False
        )

    def test_la_declaracion_en_el_texto_del_cron_sigue_valiendo(self):
        prompt = "Resume la bandeja y marca como leídos los correos del informe"
        assert definicion_declara_marcar_leidos(_cron(prompt), None) is True

    def test_nombres_de_skill_en_el_prompt(self):
        assert skills_nombradas(
            "Ejecuta la skill correo-unificado-bruno con mode=hourly. Luego la skill «otra-skill»."
        ) == [
            "correo-unificado-bruno",
            "otra-skill",
        ]
