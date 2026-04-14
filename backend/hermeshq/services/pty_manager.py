import contextlib
import asyncio
import base64
import fcntl
import os
import pty
import re
import shutil
import struct
import subprocess
import termios
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import WebSocket

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
REDRAW_NOISE_RE = re.compile(r"^\d{1,3}s?$")
BORDER_STRIP_RE = re.compile(r"^[\\s─│╭╮╰╯═║╔╗╚╝┌┐└┘━┃]+|[\\s─│╭╮╰╯═║╔╗╚╝┌┐└┘━┃]+$")
MULTISPACE_RE = re.compile(r"\s+")
BORDER_CHARS = set("─│╭╮╰╯═║╔╗╚╝┌┐└┘━┃")
BRAILLE_BLOCK_START = 0x2800
BRAILLE_BLOCK_END = 0x28FF


@dataclass
class PTYSession:
    session_id: str
    agent_id: str
    master_fd: int
    slave_fd: int
    process: subprocess.Popen
    mode: str
    cwd: str
    command: list[str]
    cols: int = 120
    rows: int = 40
    connections: set[WebSocket] = field(default_factory=set)
    reader_task: asyncio.Task | None = None
    input_buffer: str = ""
    output_buffer: str = ""


class PTYManager:
    def __init__(self, shell: str, audit_callback=None) -> None:
        self.shell = shell
        self.sessions: dict[str, PTYSession] = {}
        self.audit_callback = audit_callback

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
            session_id=str(uuid4()),
            agent_id=agent_id,
            master_fd=master_fd,
            slave_fd=slave_fd,
            process=process,
            mode=mode,
            cwd=cwd,
            command=launch_command,
            cols=cols,
            rows=rows,
        )
        session.reader_task = asyncio.create_task(self._reader_loop(session))
        self.sessions[agent_id] = session
        await self._audit(
            session,
            "terminal.session.started",
            f"Started terminal session in {cwd}",
            details={
                "mode": mode,
                "cwd": cwd,
                "command": launch_command,
                "cols": cols,
                "rows": rows,
                "pid": process.pid,
            },
        )
        return session

    async def destroy_session(self, agent_id: str) -> None:
        session = self.sessions.pop(agent_id, None)
        if not session:
            return
        await self._flush_input_buffer(session)
        await self._flush_output_buffer(session)
        exit_code = session.process.poll()
        with contextlib.suppress(ProcessLookupError):
            session.process.terminate()
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        with contextlib.suppress(OSError):
            os.close(session.slave_fd)
        if session.reader_task:
            session.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, RuntimeError):
                await asyncio.wait_for(session.reader_task, timeout=1)
        if exit_code is None:
            with contextlib.suppress(Exception):
                exit_code = session.process.wait(timeout=1)
        await self._audit(
            session,
            "terminal.session.closed",
            "Closed terminal session",
            details={"exit_code": exit_code},
        )

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
        await self._capture_input(session, data)

    async def resize(self, agent_id: str, cols: int, rows: int) -> None:
        session = self.sessions.get(agent_id)
        if not session:
            return
        session.cols = cols
        session.rows = rows
        self._resize_fd(session.master_fd, cols, rows)

    async def broadcast_notice(self, agent_id: str, text: str) -> None:
        session = self.sessions.get(agent_id)
        if not session or not session.connections:
            return
        payload = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        stale: list[WebSocket] = []
        for connection in list(session.connections):
            try:
                await connection.send_json({"type": "output", "data": payload})
            except Exception:
                stale.append(connection)
        for connection in stale:
            session.connections.discard(connection)

    async def _reader_loop(self, session: PTYSession) -> None:
        try:
            while True:
                try:
                    output = await asyncio.to_thread(os.read, session.master_fd, 1024)
                except OSError:
                    break
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
                await self._capture_output(session, output)
        except asyncio.CancelledError:
            return

    async def _capture_input(self, session: PTYSession, data: bytes) -> None:
        text = ANSI_ESCAPE_RE.sub("", data.decode("utf-8", errors="ignore"))
        for char in text:
            if char in ("\r", "\n"):
                await self._flush_input_buffer(session)
                continue
            if char in ("\x7f", "\b"):
                session.input_buffer = session.input_buffer[:-1]
                continue
            if char == "\x03":
                await self._audit(session, "terminal.input", "^C", details={"source": "tui"})
                session.input_buffer = ""
                continue
            if char == "\x04":
                await self._audit(session, "terminal.input", "^D", details={"source": "tui"})
                session.input_buffer = ""
                continue
            if char == "\x1b":
                continue
            if char.isprintable() or char == "\t":
                session.input_buffer += char
            if len(session.input_buffer) >= 400:
                await self._flush_input_buffer(session)

    async def _capture_output(self, session: PTYSession, data: bytes) -> None:
        cleaned = ANSI_ESCAPE_RE.sub("", data.decode("utf-8", errors="ignore")).replace("\r\n", "\n").replace("\r", "\n")
        if not cleaned:
            return
        session.output_buffer += cleaned
        while "\n" in session.output_buffer:
            line, session.output_buffer = session.output_buffer.split("\n", 1)
            await self._emit_output_line(session, line)
        if len(session.output_buffer) >= 700:
            await self._flush_output_buffer(session)

    async def _flush_input_buffer(self, session: PTYSession) -> None:
        line = session.input_buffer.strip()
        session.input_buffer = ""
        if not line:
            return
        await self._audit(session, "terminal.input", line[:2000], details={"source": "tui"})

    async def _emit_output_line(self, session: PTYSession, line: str) -> None:
        message = self._normalize_output_line(line)
        if not message:
            return
        if self._is_output_noise(message):
            return
        await self._audit(session, "terminal.output", message[:4000], details={"source": "tui"})

    async def _flush_output_buffer(self, session: PTYSession) -> None:
        pending = self._normalize_output_line(session.output_buffer)
        session.output_buffer = ""
        if not pending:
            return
        if self._is_output_noise(pending):
            return
        await self._audit(session, "terminal.output", pending[:4000], details={"source": "tui", "partial": True})

    async def _audit(self, session: PTYSession, event_type: str, message: str, details: dict | None = None) -> None:
        if not self.audit_callback:
            return
        payload = {
            "session_id": session.session_id,
            "mode": session.mode,
            **(details or {}),
        }
        await self.audit_callback(session.agent_id, event_type, message, payload)

    def _is_output_noise(self, message: str) -> bool:
        compact = message.strip()
        if not compact:
            return True
        if REDRAW_NOISE_RE.fullmatch(compact):
            return True
        if compact and all(char in BORDER_CHARS for char in compact):
            return True
        if len(compact) <= 2 and not any(char.isalpha() for char in compact):
            return True
        if all(self._is_braille_or_space(char) for char in compact):
            return True
        return False

    def _normalize_output_line(self, text: str) -> str:
        without_braille = "".join("" if self._is_braille_or_space(char) and not char.isspace() else char for char in text)
        stripped = BORDER_STRIP_RE.sub("", without_braille)
        return MULTISPACE_RE.sub(" ", stripped).strip()

    def _is_braille_or_space(self, char: str) -> bool:
        if char.isspace():
            return True
        codepoint = ord(char)
        return BRAILLE_BLOCK_START <= codepoint <= BRAILLE_BLOCK_END

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
