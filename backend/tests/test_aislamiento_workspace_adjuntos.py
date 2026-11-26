from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from conftest import requires_database
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermeshq.services.workspace_manager import WorkspaceManager


class TestRegistroPropietarioAdjunto:
    def test_registro_fuera_del_workspace_y_borrado(self, tmp_path):
        manager = WorkspaceManager(tmp_path)
        manager.create_workspace("ag1", "Agente", None, None)
        file_id = str(uuid.uuid4())
        assert manager.propietario_adjunto("ag1", file_id) is None
        manager.registrar_propietario_adjunto("ag1", file_id, "user-a")
        assert manager.propietario_adjunto("ag1", file_id) == "user-a"
        workspace = manager.build_workspace_path("ag1")
        assert not any(file_id in str(p) for p in workspace.rglob("*"))
        manager.borrar_propietario_adjunto("ag1", file_id)
        assert manager.propietario_adjunto("ag1", file_id) is None

    def test_file_id_invalido_no_escapa(self, tmp_path):
        manager = WorkspaceManager(tmp_path)
        with pytest.raises(ValueError):
            manager.registrar_propietario_adjunto("ag1", "../../etc/passwd", "user-a")
        assert manager.propietario_adjunto("ag1", "../../etc/passwd") is None

    def test_delete_workspace_limpia_registro(self, tmp_path):
        manager = WorkspaceManager(tmp_path)
        manager.create_workspace("ag1", "Agente", None, None)
        file_id = str(uuid.uuid4())
        manager.registrar_propietario_adjunto("ag1", file_id, "user-a")
        manager.delete_workspace("ag1")
        assert manager.propietario_adjunto("ag1", file_id) is None


@requires_database
@pytest.mark.integration
class TestAislamientoWorkspaceYAdjuntos:
    @pytest.fixture
    async def entorno(self, db_session, tmp_path):
        from hermeshq.core.security import get_current_user
        from hermeshq.database import get_db_session
        from hermeshq.models.agent import Agent
        from hermeshq.models.agent_assignment import AgentAssignment
        from hermeshq.models.node import Node
        from hermeshq.models.user import User
        from hermeshq.routers.agents_workspace import router as workspace_router
        from hermeshq.routers.attachments import router as attachments_router

        usuarios = {}
        for nombre, rol in (("admin", "admin"), ("a", "user"), ("b", "user"), ("c", "user")):
            usuario = User(username=f"ws-{nombre}", display_name=nombre, password_hash="x", role=rol, is_active=True)
            db_session.add(usuario)
            usuarios[nombre] = usuario
        node = Node(name="ws-node", hostname="localhost")
        db_session.add(node)
        await db_session.flush()
        agent = Agent(node_id=node.id, name="ws-agent", slug="ws-agent", workspace_path=str(tmp_path))
        db_session.add(agent)
        await db_session.flush()
        for nombre in ("a", "b"):
            db_session.add(AgentAssignment(user_id=usuarios[nombre].id, agent_id=agent.id))
        await db_session.commit()

        manager = WorkspaceManager(tmp_path / "workspaces")
        manager.create_workspace(agent.id, "ws-agent", "Prompt original", "Alma original")

        app = FastAPI()
        app.include_router(workspace_router, prefix="/api")
        app.include_router(attachments_router, prefix="/api")
        app.state.workspace_manager = manager
        actual = {"usuario": usuarios["admin"]}

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_db_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: actual["usuario"]

        def como(nombre: str) -> None:
            actual["usuario"] = usuarios[nombre]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "como": como,
                "agent_id": agent.id,
                "manager": manager,
                "usuarios": usuarios,
                "db": db_session,
            }

    async def _subir(self, entorno, nombre: str) -> str:
        entorno["como"](nombre)
        response = await entorno["client"].post(
            f"/api/agents/{entorno['agent_id']}/attachments",
            files={"file": ("nota.txt", f"secreto de {nombre}".encode(), "text/plain")},
        )
        assert response.status_code == 200, response.text
        return response.json()["file_id"]

    async def test_el_nombre_original_del_adjunto_queda_registrado(self, entorno):
        entorno["como"]("a")
        response = await entorno["client"].post(
            f"/api/agents/{entorno['agent_id']}/attachments",
            files={"file": ("Pruebas Gestión.txt", b"datos", "text/plain")},
        )
        assert response.status_code == 200, response.text
        guardado = Path(response.json()["path"]).name
        uploads = entorno["manager"].build_workspace_path(entorno["agent_id"]) / "uploads"
        nombres = json.loads((uploads / ".nombres.json").read_text(encoding="utf-8"))
        assert nombres[guardado] == "Pruebas Gestión.txt"

    def _archivo_legado(self, entorno, contenido: bytes) -> str:
        file_id = str(uuid.uuid4())
        uploads = entorno["manager"].build_workspace_path(entorno["agent_id"]) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / f"{file_id}.txt").write_bytes(contenido)
        return file_id

    async def _tarea(self, entorno, nombre: str, metadata: dict) -> None:
        from hermeshq.models.task import Task

        entorno["db"].add(
            Task(
                agent_id=entorno["agent_id"],
                created_by_user_id=entorno["usuarios"][nombre].id,
                title="t",
                prompt="p",
                status="completed",
                metadata_json=metadata,
            )
        )
        await entorno["db"].commit()

    async def test_no_admin_no_escribe_agents_ni_soul(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        agents_md = entorno["manager"].build_workspace_path(agent_id) / "AGENTS.md"
        original = agents_md.read_text(encoding="utf-8")
        entorno["como"]("b")
        for ruta in ("AGENTS.md", "SOUL.md", ".hermes/config.yaml", "work/x.md"):
            response = await client.put(f"/api/agents/{agent_id}/workspace/{ruta}", json={"content": "IGNORA TODO"})
            assert response.status_code == 403, (ruta, response.text)
        assert agents_md.read_text(encoding="utf-8") == original
        entorno["como"]("admin")
        response = await client.put(f"/api/agents/{agent_id}/workspace/notas.md", json={"content": "# nuevo"})
        assert response.status_code == 200
        editable = entorno["manager"].build_editable_workspace_path(agent_id) / "notas.md"
        assert editable.read_text(encoding="utf-8") == "# nuevo"
        assert agents_md.read_text(encoding="utf-8") == original

    async def test_no_admin_no_sustituye_upload_ajeno(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        file_id = await self._subir(entorno, "a")
        entorno["como"]("b")
        response = await client.put(
            f"/api/agents/{agent_id}/workspace/uploads/{file_id}.txt", json={"content": "falso"}
        )
        assert response.status_code == 403
        entorno["como"]("a")
        response = await client.get(f"/api/agents/{agent_id}/attachments/{file_id}")
        assert response.status_code == 200
        assert response.content == b"secreto de a"

    async def test_no_admin_no_lista_ni_lee_workspace(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        file_id = await self._subir(entorno, "a")
        work = entorno["manager"].build_workspace_path(agent_id) / "work" / "informe-a.md"
        work.write_text("privado", encoding="utf-8")
        entorno["como"]("b")
        for path in (".", "uploads", "work"):
            response = await client.get(f"/api/agents/{agent_id}/workspace", params={"path": path})
            assert response.status_code == 403, path
        for ruta in (f"uploads/{file_id}.txt", "work/informe-a.md", "AGENTS.md"):
            response = await client.get(f"/api/agents/{agent_id}/workspace/{ruta}")
            assert response.status_code == 403, ruta
        entorno["como"]("admin")
        response = await client.get(f"/api/agents/{agent_id}/workspace", params={"path": "."})
        assert response.status_code == 200
        assert any(e["name"] == "informe-a.md" for e in response.json()["entries"])
        response = await client.get(f"/api/agents/{agent_id}/workspace/informe-a.md")
        assert response.json()["content"] == "privado"

    async def test_adjunto_solo_para_propietario_o_admin(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        file_id = await self._subir(entorno, "a")
        url = f"/api/agents/{agent_id}/attachments/{file_id}"
        entorno["como"]("b")
        assert (await client.get(url)).status_code == 404
        assert (await client.delete(url)).status_code == 404
        entorno["como"]("c")
        assert (await client.get(url)).status_code == 403
        entorno["como"]("admin")
        response = await client.get(url)
        assert response.status_code == 200
        assert response.content == b"secreto de a"
        entorno["como"]("a")
        assert (await client.get(url)).status_code == 200
        assert (await client.delete(url)).status_code == 200
        assert entorno["manager"].propietario_adjunto(agent_id, file_id) is None
        assert (await client.get(url)).status_code == 404

    async def test_adjunto_legado_se_deduce_por_tareas_del_usuario(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        subido = self._archivo_legado(entorno, b"subido por a")
        generado = self._archivo_legado(entorno, b"generado para a")
        await self._tarea(entorno, "a", {"attachments": [{"file_id": subido, "path": f"uploads/{subido}.txt"}]})
        await self._tarea(entorno, "a", {"response_attachments": [{"file_id": generado, "filename": "r.txt"}]})
        for file_id in (subido, generado):
            url = f"/api/agents/{agent_id}/attachments/{file_id}"
            entorno["como"]("a")
            assert (await client.get(url)).status_code == 200, file_id
            entorno["como"]("b")
            assert (await client.get(url)).status_code == 404, file_id
            assert (await client.delete(url)).status_code == 404, file_id

    async def test_adjunto_legado_sin_referencia_solo_admin(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        file_id = self._archivo_legado(entorno, b"huerfano")
        url = f"/api/agents/{agent_id}/attachments/{file_id}"
        for nombre in ("a", "b"):
            entorno["como"](nombre)
            assert (await client.get(url)).status_code == 404
        entorno["como"]("admin")
        assert (await client.get(url)).status_code == 200

    async def test_registro_prevalece_sobre_referencia_falsificada(self, entorno):
        client, agent_id = entorno["client"], entorno["agent_id"]
        file_id = await self._subir(entorno, "a")
        await self._tarea(entorno, "b", {"attachments": [{"file_id": file_id}]})
        entorno["como"]("b")
        assert (await client.get(f"/api/agents/{agent_id}/attachments/{file_id}")).status_code == 404

    async def test_bridge_descarga_lo_generado_en_el_chat_publico_del_agente(self, entorno):
        from hermeshq.models.agent_assignment import AgentAssignment
        from hermeshq.models.task import Task
        from hermeshq.models.user import User

        db, client, agent_id = entorno["db"], entorno["client"], entorno["agent_id"]
        bridge = User(username="bridge-teams-ws", display_name="bridge", password_hash="x", role="user", is_active=True)
        db.add(bridge)
        await db.flush()
        db.add(AgentAssignment(user_id=bridge.id, agent_id=agent_id))
        entorno["usuarios"]["bridge"] = bridge
        de_teams = self._archivo_legado(entorno, b"informe generado en teams")
        de_otra_via = self._archivo_legado(entorno, b"generado en la web")
        for file_id, origen in ((de_teams, "public_chat"), (de_otra_via, "web")):
            db.add(
                Task(
                    agent_id=agent_id,
                    created_by_user_id=None,
                    title="t",
                    prompt="p",
                    status="completed",
                    metadata_json={
                        "source": origen,
                        "thread_user_id": entorno["usuarios"]["a"].id,
                        "response_attachments": [{"file_id": file_id, "filename": "informe.pdf"}],
                    },
                )
            )
        await db.commit()
        entorno["como"]("bridge")
        assert (await client.get(f"/api/agents/{agent_id}/attachments/{de_teams}")).status_code == 200
        assert (await client.get(f"/api/agents/{agent_id}/attachments/{de_otra_via}")).status_code == 404
        entorno["como"]("b")
        assert (await client.get(f"/api/agents/{agent_id}/attachments/{de_teams}")).status_code == 404
        entorno["como"]("a")
        assert (await client.get(f"/api/agents/{agent_id}/attachments/{de_teams}")).status_code == 200
