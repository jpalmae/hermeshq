import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermeshq.routers import m365_broker
from hermeshq.services import graph_proxy as gp

CUERPO = {
    "requests": [{"entityTypes": ["driveItem"], "query": {"queryString": "Estructura Demo"}, "from": 0, "size": 10}]
}


def _respuesta_graph(*_args, **_kwargs):
    hit = {"resource": {"id": "1", "name": "Estructura Demo.html", "parentReference": {"driveId": "d1"}}}
    return 200, json.dumps({"value": [{"hitsContainers": [{"hits": [hit]}]}]}).encode()


@pytest.mark.asyncio
async def test_caso_real_la_busqueda_no_bloquea_el_bucle_esperando_el_token():
    loop = asyncio.get_running_loop()

    async def obtener():
        await asyncio.sleep(0)
        return "token-real"

    def token_de():
        return asyncio.run_coroutine_threadsafe(obtener(), loop).result(timeout=3)

    latidos = []

    async def latido():
        for _ in range(20):
            latidos.append(time.monotonic())
            await asyncio.sleep(0.05)

    vigia = asyncio.create_task(latido())
    inicio = time.monotonic()
    with patch.object(gp, "_llamar", side_effect=_respuesta_graph):
        resultado = await m365_broker._buscar_archivos_compatibilidad(
            SimpleNamespace(cuerpo_json=CUERPO), "compatibilidad", token_de
        )
    duracion = time.monotonic() - inicio
    await vigia
    assert duracion < 1.5
    assert resultado["status"] == 200
    nombres = [h["resource"]["name"] for h in resultado["json"]["value"][0]["hitsContainers"][0]["hits"]]
    assert nombres == ["Estructura Demo.html"]
    pausas = [b - a for a, b in zip(latidos, latidos[1:], strict=False)]
    assert max(pausas) < 1.0


def test_ninguna_ruta_async_del_broker_llama_token_de_en_el_bucle():
    fuente = Path(m365_broker.__file__).read_text(encoding="utf-8")
    directas = [
        linea.strip()
        for linea in fuente.splitlines()
        if re.search(r"\btoken_de\(\)", linea) and "to_thread" not in linea and "def token_de" not in linea
    ]
    assert directas == []
