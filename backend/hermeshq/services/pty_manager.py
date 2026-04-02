import contextlib
import asyncio
import base64
import fcntl
import os
import pty
import shutil
import struct
import subprocess
import termios
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class PTYSession:
    agent_id: str
    master_fd: int
    slave_fd: int
    process: subprocess.Popen
    mode: str
    cols: int = 120
    rows: int = 40
    connections: set[WebSocket] = field(default_factory=set)
    reader_task: asyncio.Task | None = None


class PTYManager:
    def __init__(self, shell: str) -> None:
        self.shell = shell
        self.sessions: dict[str, PTYSession] = {}

    async def create_session(
        self,
        agent_id: str,
        mode: str,
        cwd: str,
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        cols: int = 120,
        rows: int = 40,
    ) -> PTYSession:
        if agent_id in self.sessions:
            return self.sessions[agent_id]
        master_fd, slave_fd = pty.openpty()
        self._resize_fd(slave_fd, cols, rows)
        shell = self._resolve_shell()
        launch_command = command or [shell]
        process = subprocess.Popen(
            launch_command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env={**os.environ, "TERM": "xterm-256color", **(env or {})},
            close_fds=True,
        )
        session = PTYSession(
            agent_id=agent_id,
            master_fd=master_fd,
            slave_fd=slave_fd,
            process=process,
            mode=mode,
            cols=cols,
            rows=rows,
        )
        session.reader_task = asyncio.create_task(self._reader_loop(session))
        self.sessions[agent_id] = session
        return session

    async def destroy_session(self, agent_id: str) -> None:
        session = self.sessions.pop(agent_id, None)
        if not session:
            return
        if session.reader_task:
            session.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.reader_task
        with contextlib.suppress(ProcessLookupError):
            session.process.terminate()
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        with contextlib.suppress(OSError):
            os.close(session.slave_fd)

    async def attach(self, session: PTYSession, websocket: WebSocket) -> None:
        await websocket.accept()
        session.connections.add(websocket)
        await websocket.send_json(
            {"type": "connected", "cols": session.cols, "rows": session.rows, "mode": session.mode}
        )

    async def detach(self, session: PTYSession, websocket: WebSocket) -> None:
        session.connections.discard(websocket)

    async def write_input(self, agent_id: str, data: bytes) -> None:
        session = self.sessions.get(agent_id)
        if not session:
            return
        os.write(session.master_fd, data)

    async def resize(self, agent_id: str, cols: int, rows: int) -> None:
        session = self.sessions.get(agent_id)
        if not session:
            return
        session.cols = cols
        session.rows = rows
        self._resize_fd(session.master_fd, cols, rows)

    async def _reader_loop(self, session: PTYSession) -> None:
        while True:
            output = await asyncio.to_thread(os.read, session.master_fd, 1024)
            if not output:
                break
            payload = base64.b64encode(output).decode("utf-8")
            stale: list[WebSocket] = []
            for connection in list(session.connections):
                try:
                    await connection.send_json({"type": "output", "data": payload})
                except Exception:
                    stale.append(connection)
            for connection in stale:
                session.connections.discard(connection)

    def _resize_fd(self, fd: int, cols: int, rows: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def _resolve_shell(self) -> str:
        candidates = [self.shell, "/bin/sh", "/bin/bash", "sh", "bash"]
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isabs(candidate) and os.path.exists(candidate):
                return candidate
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise FileNotFoundError("No interactive shell available for PTY session")
