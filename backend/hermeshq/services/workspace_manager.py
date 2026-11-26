import json
import os
import shutil
import uuid
from pathlib import Path

DIRECTORIO_PROPIETARIOS_ADJUNTOS = "_adjuntos_propietarios"


def _has_protected_part(parts: tuple[str, ...]) -> bool:
    return any(part.lower() == ".hermes" or part.lower().startswith(".env") for part in parts)


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def build_workspace_path(self, agent_id: str) -> Path:
        return self.root / f"agent-{agent_id}"

    def build_editable_workspace_path(self, agent_id: str) -> Path:
        return self.build_workspace_path(agent_id) / "work"

    def build_pi_config_path(self, agent_id: str) -> Path:
        return self.root / "_runtime_config" / "pi" / f"agent-{agent_id}"

    def create_workspace(
        self,
        agent_id: str,
        name: str,
        system_prompt: str | None,
        soul_md: str | None,
    ) -> str:
        workspace = self.build_workspace_path(agent_id)
        (workspace / ".hermes").mkdir(parents=True, exist_ok=True)
        (workspace / "work").mkdir(parents=True, exist_ok=True)
        (workspace / "shared").mkdir(parents=True, exist_ok=True)
        (workspace / "AGENTS.md").write_text(
            f"# {name}\n\n{system_prompt or 'HermesHQ managed agent.'}\n",
            encoding="utf-8",
        )
        (workspace / "SOUL.md").write_text(soul_md or "# Soul\n\nOperational.", encoding="utf-8")
        return str(workspace.resolve())

    def delete_workspace(self, agent_id: str) -> None:
        self._borrar_registro_propietarios(agent_id)
        workspace = self.build_workspace_path(agent_id)
        pi_config = self.build_pi_config_path(agent_id)
        if workspace.exists():
            shutil.rmtree(workspace)
        if pi_config.exists():
            shutil.rmtree(pi_config)

    def cleanup_orphan_pi_configs(self, live_agent_ids: set[str]) -> list[str]:
        """Remove _runtime_config/pi/agent-* dirs whose agent no longer exists.

        Returns the removed agent ids. Called once at startup so permanent
        deletes from older versions (which skipped cleanup) are reclaimed.
        """
        removed: list[str] = []
        pi_root = self.root / "_runtime_config" / "pi"
        if not pi_root.exists():
            return removed
        for entry in pi_root.iterdir():
            if not entry.is_dir() or not entry.name.startswith("agent-"):
                continue
            agent_id = entry.name[len("agent-") :]
            if agent_id in live_agent_ids:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(agent_id)
        return removed

    def sync_config(
        self,
        agent_id: str,
        name: str,
        system_prompt: str | None,
        soul_md: str | None,
    ) -> None:
        workspace = self.build_workspace_path(agent_id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "AGENTS.md").write_text(
            f"# {name}\n\n{system_prompt or 'HermesHQ managed agent.'}\n",
            encoding="utf-8",
        )
        (workspace / "SOUL.md").write_text(soul_md or "# Soul\n\nOperational.", encoding="utf-8")

    def is_protected_path(self, agent_id: str, relative_path: str) -> bool:
        workspace = self.build_workspace_path(agent_id).resolve()
        if _has_protected_part(Path(os.path.normpath(relative_path)).parts):
            return True
        try:
            resolved = (workspace / relative_path).resolve().relative_to(workspace)
        except ValueError:
            return False
        return _has_protected_part(resolved.parts)

    def list_workspace_files(
        self, agent_id: str, relative_path: str = ".", include_protected: bool = True
    ) -> list[dict]:
        workspace = self.build_editable_workspace_path(agent_id)
        workspace.mkdir(parents=True, exist_ok=True)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        if not target.exists():
            return []
        entries = []
        for path in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if not include_protected and self.is_protected_path(agent_id, str(path.relative_to(workspace))):
                continue
            entries.append(
                {
                    "name": path.name,
                    "path": str(path.relative_to(workspace)),
                    "is_dir": path.is_dir(),
                    "size": path.stat().st_size if path.is_file() else 0,
                }
            )
        return entries

    def read_workspace_file(self, agent_id: str, relative_path: str) -> str:
        workspace = self.build_editable_workspace_path(agent_id)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        if not target.exists():
            raise FileNotFoundError(f"Workspace file not found: {relative_path}")
        return target.read_text(encoding="utf-8")

    def write_workspace_file(self, agent_id: str, relative_path: str, content: str) -> None:
        workspace = self.build_editable_workspace_path(agent_id)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def get_workspace_size(self, agent_id: str) -> int:
        workspace = self.build_editable_workspace_path(agent_id)
        if not workspace.exists():
            return 0
        total = 0
        for path in workspace.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def _ruta_propietario_adjunto(self, agent_id: str, file_id: str) -> Path:
        normalizado = str(uuid.UUID(file_id))
        return self.root / DIRECTORIO_PROPIETARIOS_ADJUNTOS / f"agent-{agent_id}" / f"{normalizado}.json"

    def registrar_propietario_adjunto(self, agent_id: str, file_id: str, user_id: str) -> None:
        destino = self._ruta_propietario_adjunto(agent_id, file_id)
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(".tmp")
        temporal.write_text(json.dumps({"user_id": user_id}), encoding="utf-8")
        temporal.replace(destino)

    def propietario_adjunto(self, agent_id: str, file_id: str) -> str | None:
        try:
            datos = json.loads(self._ruta_propietario_adjunto(agent_id, file_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        valor = datos.get("user_id") if isinstance(datos, dict) else None
        return str(valor) if valor else None

    def borrar_propietario_adjunto(self, agent_id: str, file_id: str) -> None:
        try:
            self._ruta_propietario_adjunto(agent_id, file_id).unlink(missing_ok=True)
        except ValueError:
            return

    def _borrar_registro_propietarios(self, agent_id: str) -> None:
        directorio = self.root / DIRECTORIO_PROPIETARIOS_ADJUNTOS / f"agent-{agent_id}"
        if not directorio.is_dir():
            return
        for path in directorio.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink()
        directorio.rmdir()

    def _ensure_within_workspace(self, workspace: Path, target: Path) -> None:
        if workspace.resolve() not in [target, *target.parents]:
            raise ValueError("Path escapes workspace")
