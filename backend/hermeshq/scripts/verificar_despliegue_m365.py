from __future__ import annotations

import asyncio
import json
import sys


async def _agentes():
    from sqlalchemy import select

    from hermeshq.database import AsyncSessionLocal
    from hermeshq.models.agent import Agent

    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(Agent).where(Agent.is_archived.is_(False)))).scalars().all())


def main(agentes=None, ajustes=None) -> int:
    from hermeshq.config import get_settings
    from hermeshq.services.compatibilidad_m365 import diagnostico

    ajustes = ajustes or get_settings()
    if agentes is None:
        try:
            agentes = asyncio.run(_agentes())
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"no se pudo leer la base de datos: {type(exc).__name__}"}))
            return 3
    resultado = diagnostico(ajustes, agentes)
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    if resultado["ambiguo"]:
        print(
            "NO DESPLEGAR: declara explícitamente "
            + ", ".join(campo.upper() for campo in resultado["no_declarados"])
            + " en el entorno del backend.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
