from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import requires_database
from fastapi import HTTPException
from sqlalchemy import text
from test_cron_recuperacion import _maker, _scheduled_tasks
from test_execution_lease import _seed_task

from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.routers import scheduled_tasks as router
from hermeshq.schemas.scheduled_task import ScheduledTaskApprove, ScheduledTaskUpdate
from hermeshq.services import cron_schedule as cs
from hermeshq.services import scheduler as scheduler_mod
from hermeshq.services.scheduler import SchedulerService

FORMATO = (
    "## Resumen de correos no leídos — <día> <fecha>\n"
    "| Remitente | Asunto | Prioridad |\n|---|---|---|\n"
    "Si no hay correos: «Sin correos no leídos en la ventana.» y nada más."
)


def _sin_hora(prompt: str) -> str:
    return prompt.split("\n\n[Hora actual:", 1)[0]


class TestBloqueDeFormato:
    def test_mismo_bloque_todos_los_dias_y_solo_cambia_la_hora(self):
        cron = SimpleNamespace(
            prompt="Resume mis correos no leídos.",
            timezone="America/Santiago",
            output_format=FORMATO,
            definition_version=3,
        )
        dias = [datetime(2026, 11, d, 11, 30, tzinfo=UTC) for d in (9, 10, 11)]
        prompts = [cs.build_scheduled_prompt(cron, d) for d in dias]
        assert len({_sin_hora(p) for p in prompts}) == 1
        assert "versión 3" in prompts[0] and FORMATO in prompts[0]
        assert [p.rsplit("[Hora actual: ", 1)[1][:10] for p in prompts] == ["lunes 2026", "martes 202", "miércoles "]

    def test_sin_formato_el_prompt_queda_como_antes(self):
        cron = SimpleNamespace(prompt="Ejecuta", timezone="America/Santiago", output_format=None, definition_version=1)
        assert cs.build_scheduled_prompt(cron, datetime(2026, 11, 9, tzinfo=UTC)).startswith("Ejecuta\n\n[Hora actual:")

    def test_que_cambia_la_definicion(self):
        item = SimpleNamespace(
            prompt="A", output_format="F", cron_expression="0 8 * * 1-5", timezone="America/Santiago"
        )
        assert not cs.cambia_la_definicion(item, {"cron_expression": "0  8 * * 1-5", "prompt": " A ", "enabled": False})
        assert cs.cambia_la_definicion(item, {"output_format": "otro"})
        assert cs.cambia_la_definicion(item, {"timezone": "UTC"})


async def _dueno(maker) -> str:
    from hermeshq.core.security import hash_password
    from hermeshq.models.user import User

    async with maker() as s:
        usuario = User(
            username=f"duena-{datetime.now(UTC).timestamp()}",
            display_name="Dueña",
            password_hash=hash_password("clave-de-prueba-larga"),
        )
        s.add(usuario)
        await s.commit()
        return usuario.id


def _fijar_hora(monkeypatch, momento: datetime) -> None:
    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return momento if tz is None else momento.astimezone(tz)

    monkeypatch.setattr(scheduler_mod, "datetime", _Reloj)


@pytest.mark.integration
@requires_database
class TestEjecucionesEnDiasDistintos:
    async def _cron(self, maker, agent_id, dueno):
        async with maker() as s:
            item = ScheduledTask(
                agent_id=agent_id,
                name="resumen diario",
                cron_expression="30 8 * * 1-5",
                prompt="Resume mis correos no leídos.",
                output_format=FORMATO,
                enabled=True,
                approval_status="approved",
                timezone="America/Santiago",
                owner_user_id=dueno,
                next_run=cs.compute_next_run(
                    "30 8 * * 1-5", "America/Santiago", datetime(2026, 11, 9, 11, 0, tzinfo=UTC)
                ),
            )
            s.add(item)
            await s.commit()
            return item.id

    async def _correr_dia(self, maker, monkeypatch, schedule_id):
        async with maker() as s:
            disparo = (await s.get(ScheduledTask, schedule_id)).next_run
        _fijar_hora(monkeypatch, disparo + timedelta(seconds=20))
        await SchedulerService(maker, AsyncMock()).tick()
        tareas = sorted(await _scheduled_tasks(maker, schedule_id), key=lambda t: t.metadata_json["scheduled_fire_at"])
        async with maker() as s:
            await s.execute(text("UPDATE tasks SET status = 'completed' WHERE id = :t"), {"t": tareas[-1].id})
            await s.commit()
        return tareas[-1]

    async def test_tres_dias_mismas_instrucciones_mismo_formato_mismo_dueno(self, db_engine, monkeypatch):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        dueno = await _dueno(maker)
        schedule_id = await self._cron(maker, agent_id, dueno)
        tareas = [await self._correr_dia(maker, monkeypatch, schedule_id) for _ in range(3)]
        assert len({t.metadata_json["scheduled_fire_key"] for t in tareas}) == 3, "un disparo por día"
        assert [
            datetime.fromisoformat(t.metadata_json["scheduled_fire_at"])
            .astimezone(cs.resolve_timezone("America/Santiago"))
            .strftime("%a %H:%M")
            for t in tareas
        ] == ["Mon 08:30", "Tue 08:30", "Wed 08:30"]
        assert len({_sin_hora(t.prompt) for t in tareas}) == 1 and FORMATO in tareas[0].prompt
        for t in tareas:
            assert t.metadata_json["scheduled_definition_version"] == 1
            assert t.metadata_json["thread_user_id"] == dueno and t.created_by_user_id == dueno
            assert "conversation" not in t.metadata_json and "thread_id" not in t.metadata_json, "sin historial"

    async def test_cambiar_el_formato_sube_la_version_y_la_ejecucion_siguiente_la_usa(self, db_engine, monkeypatch):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        dueno = await _dueno(maker)
        schedule_id = await self._cron(maker, agent_id, dueno)
        primera = await self._correr_dia(maker, monkeypatch, schedule_id)
        admin = SimpleNamespace(id=dueno, role="admin", is_admin=True)
        monkeypatch.setattr(router, "is_admin", lambda u: True)
        monkeypatch.setattr(router, "ensure_agent_access", AsyncMock())
        async with maker() as s:
            leido = await router.update_scheduled_task(
                schedule_id, ScheduledTaskUpdate(output_format=FORMATO + "\nAgrega al final: Total: <n>."), admin, s
            )
        assert leido.definition_version == 2
        async with maker() as s:
            await router.update_scheduled_task(schedule_id, ScheduledTaskUpdate(enabled=True), admin, s)
            assert (await s.get(ScheduledTask, schedule_id)).definition_version == 2, "activar no es otra definición"
        segunda = await self._correr_dia(maker, monkeypatch, schedule_id)
        assert primera.metadata_json["scheduled_definition_version"] == 1
        assert segunda.metadata_json["scheduled_definition_version"] == 2
        assert "Total: <n>." in segunda.prompt and "versión 2" in segunda.prompt

    async def test_un_dueno_que_cambia_el_formato_debe_reaprobar(self, db_engine, monkeypatch):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        dueno = await _dueno(maker)
        schedule_id = await self._cron(maker, agent_id, dueno)
        usuario = SimpleNamespace(id=dueno, role="user")
        monkeypatch.setattr(router, "is_admin", lambda u: False)
        monkeypatch.setattr(router, "ensure_agent_access", AsyncMock())
        async with maker() as s:
            await router.update_scheduled_task(
                schedule_id, ScheduledTaskUpdate(output_format="Otro formato"), usuario, s
            )
        async with maker() as s:
            item = await s.get(ScheduledTask, schedule_id)
            assert (item.approval_status, item.enabled) == ("pending", False)
        monkeypatch.setattr(router, "_compute_next_run", lambda *_a: datetime(2026, 11, 20, tzinfo=UTC))
        async with maker() as s:
            with pytest.raises(HTTPException) as e:
                await router.approve_scheduled_task(
                    schedule_id,
                    ScheduledTaskApprove(
                        cron_expression="30 8 * * 1-5", prompt="Resume mis correos no leídos.", output_format=FORMATO
                    ),
                    usuario,
                    s,
                )
            assert e.value.status_code == 409, "no se aprueba un formato distinto del que se revisó"

    async def test_la_web_sin_formato_no_aprueba_a_ciegas_y_dice_como_hacerlo(self, db_engine, monkeypatch):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        dueno = await _dueno(maker)
        schedule_id = await self._cron(maker, agent_id, dueno)
        async with maker() as s:
            await s.execute(
                text("UPDATE scheduled_tasks SET approval_status = 'pending' WHERE id = :i"), {"i": schedule_id}
            )
            await s.commit()
        monkeypatch.setattr(router, "ensure_agent_access", AsyncMock())
        async with maker() as s:
            with pytest.raises(HTTPException) as e:
                await router.approve_scheduled_task(
                    schedule_id,
                    ScheduledTaskApprove(cron_expression="30 8 * * 1-5", prompt="Resume mis correos no leídos."),
                    SimpleNamespace(id=dueno, role="user"),
                    s,
                )
        assert e.value.status_code == 409 and "aprobar CÓDIGO" in e.value.detail
        async with maker() as s:
            leido = await router.approve_scheduled_task(
                schedule_id,
                ScheduledTaskApprove(
                    cron_expression="30 8 * * 1-5", prompt="Resume mis correos no leídos.", output_format=FORMATO
                ),
                SimpleNamespace(id=dueno, role="user"),
                s,
            )
        assert leido.approval_status == "approved" and leido.output_format == FORMATO
