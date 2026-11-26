import importlib.util
from pathlib import Path

import pytest
from conftest import requires_database

RUNNER = Path(__file__).resolve().parent.parent / "hermeshq" / "scripts" / "hermes_task_runner.py"


def _runner():
    spec = importlib.util.spec_from_file_location("runner_auditoria", RUNNER)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@requires_database
class TestAuditoriaEnBD:
    @pytest.mark.asyncio
    async def test_solo_el_exito_deja_registro_de_auditoria(self, db_engine):
        from sqlalchemy import text
        from test_execution_lease import _seed_task
        from test_execution_lease_e2e import _maker, _orphan

        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        store, lease = await _orphan(maker, task_id, "host-prueba", tool_events=0, ttl=60)
        aud = {"accion": "compartir", "skill": "informe-x", "destinatarios": ["ana"]}
        await store.registrar_fin_herramienta(lease, agent_id, "skill_manage", "ok", 5, auditoria=aud)
        await store.registrar_fin_herramienta(lease, agent_id, "skill_manage", "rechazo_guardia", 5, auditoria=aud)
        async with maker() as session:
            filas = (
                await session.execute(
                    text(
                        "select message, details->>'skill', details->>'accion' from activity_logs where task_id=:t and event_type='skill.audit'"
                    ),
                    {"t": task_id},
                )
            ).all()
        assert [tuple(f) for f in filas] == [("compartir informe-x", "informe-x", "compartir")]
