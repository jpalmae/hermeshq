from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_execution_lease import _StubInstallation  # noqa: E402

from hermeshq.services import hermes_installation, hermes_runtime  # noqa: E402

WORKSPACE = Path(os.environ["HQ_E2E_WORKSPACE"])
LLM_URL = os.environ["HQ_E2E_LLM_URL"]
_stub = _StubInstallation(WORKSPACE, LLM_URL)


async def _noop(self, *_a, **_k):
    return None


async def _prompt(self, *_a, **_k):
    return "Agente de prueba."


async def _runtime(self, *_a, **_k):
    return SimpleNamespace(
        python_bin=sys.executable,
        source="sistema",
        detected_version=None,
        effective_version="aislado",
        release_tag=None,
        requested_version=None,
        hermes_bin="hermes",
    )


async def _env(self, agent):
    env = await _stub.build_process_env(agent)
    if os.environ.get("HQ_E2E_INSTALACION_REAL") == "1" and len(sys.argv) > 1:
        from hermeshq.core.security import create_agent_service_token
        from hermeshq.services.aprobacion_central import central_activa_para

        env.update(
            {
                "HERMESHQ_INTERNAL_API_URL": f"http://127.0.0.1:{sys.argv[1]}/api/internal",
                "HERMESHQ_AGENT_ID": str(agent.id),
                "HERMESHQ_AGENT_TOKEN": create_agent_service_token(agent.id),
                "HERMESHQ_APROBACION_CENTRAL": "1" if central_activa_para(agent) else "0",
            }
        )
    return env


async def _api_key(self, *_a, **_k):
    return "clave-simulada"


M = hermes_installation.HermesInstallationManager
if os.environ.get("HQ_E2E_INSTALACION_REAL") != "1":
    M.sync_agent_installation = _noop
    M.get_runtime_system_prompt = _prompt
M.resolve_hermes_runtime = _runtime
if os.environ.get("HQ_E2E_WORKSPACE_POR_AGENTE") == "1":

    def _hogar_propio(self, ruta):
        hogar = Path(ruta) / ".hermes"
        hogar.mkdir(parents=True, exist_ok=True)
        return hogar

    async def _env_propio(self, agent):
        return {**(await _env(self, agent)), "HOME": str(agent.workspace_path)}

    M.resolve_workspace_path = lambda self, ruta: Path(ruta)
    M.build_hermes_home = _hogar_propio
    M.build_process_env = _env_propio
else:
    M.resolve_workspace_path = lambda self, _p: _stub.resolve_workspace_path(_p)
    M.build_hermes_home = lambda self, _p: _stub.build_hermes_home(_p)
    M.build_process_env = _env
M._model_provider_for_agent = lambda self, _a: "custom"
M._effective_provider_base_url = lambda self, _a: LLM_URL
hermes_runtime.HermesRuntime._has_credentials = lambda self, _a: True
hermes_runtime.HermesRuntime._resolve_api_key = _api_key
if os.environ.get("HQ_E2E_DIFUSION_ANTIGUA") == "1":
    from hermeshq.services import schedule_notifications as _avisos

    async def _difusion_antigua(session, user, agent_id, origen, *, task=None, schedule=None):
        claves = await _avisos._delivery_keys(session, user, agent_id)
        if not claves and origen in ("cron", "segundo_plano"):
            claves = await _avisos._todas_las_claves(session, user, agent_id)
        return claves

    _avisos.resolver_claves_entrega = _difusion_antigua

if __name__ == "__main__":
    import uvicorn

    from hermeshq.main import app

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
