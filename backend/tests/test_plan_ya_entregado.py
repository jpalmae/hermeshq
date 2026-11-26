import uuid

from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _seed_task

from hermeshq.models.task import Task
from hermeshq.services import plan_analisis as pa

pytestmark = [requires_database]


async def _plan(maker, agent_id, sesion, **extra):
    async with maker() as s:
        t = Task(
            agent_id=agent_id,
            prompt="Crea una presentación corporativa…",
            status="completed",
            response=extra.pop("response", "Propongo este plan: …"),
            metadata_json={"public_session_id": sesion, "plan_analisis": True, **extra},
        )
        s.add(t)
        await s.commit()


async def test_caso_real_la_presentacion_entregada_no_deja_un_plan_pendiente(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    agent_id, _ = await _seed_task(maker)
    sesion = str(uuid.uuid4())
    await _plan(
        maker,
        agent_id,
        sesion,
        response="Listo, Ana. Generé la presentación.\n\nMEDIA: outputs/Eficiencia-Operacional-2027_2.pptx",
        response_attachments=[{"filename": "Eficiencia-Operacional-2027_2.pptx"}],
    )
    async with maker() as s:
        assert await pa.plan_pendiente(s, agent_id, sesion) is None


async def test_un_plan_propuesto_sin_entrega_sigue_pendiente(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    agent_id, _ = await _seed_task(maker)
    sesion = str(uuid.uuid4())
    await _plan(maker, agent_id, sesion)
    async with maker() as s:
        assert await pa.plan_pendiente(s, agent_id, sesion) is not None
