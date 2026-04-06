from pathlib import Path


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def build_workspace_path(self, agent_id: str) -> Path:
        return self.root / f"agent-{agent_id}"

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
        workspace = self.build_workspace_path(agent_id)
        if not workspace.exists():
            return
        for path in sorted(workspace.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        workspace.rmdir()

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

    def list_workspace_files(self, agent_id: str, relative_path: str = ".") -> list[dict]:
        workspace = self.build_workspace_path(agent_id)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        if not target.exists():
            return []
        entries = []
        for path in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
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
        workspace = self.build_workspace_path(agent_id)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        if not target.exists():
            raise FileNotFoundError(f"Workspace file not found: {relative_path}")
        return target.read_text(encoding="utf-8")

    def write_workspace_file(self, agent_id: str, relative_path: str, content: str) -> None:
        workspace = self.build_workspace_path(agent_id)
        target = (workspace / relative_path).resolve()
        self._ensure_within_workspace(workspace, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def get_workspace_size(self, agent_id: str) -> int:
        workspace = self.build_workspace_path(agent_id)
        if not workspace.exists():
            return 0
        total = 0
        for path in workspace.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def _ensure_within_workspace(self, workspace: Path, target: Path) -> None:
        if workspace.resolve() not in [target, *target.parents]:
            raise ValueError("Path escapes workspace")
