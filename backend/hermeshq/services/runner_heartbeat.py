from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_LINE = 4096


def heartbeat_mac(key: str, execution_id: str, attempt_id: str, generation: int, seq: int, ts: str) -> str:
    message = f"{execution_id}|{attempt_id}|{generation}|{seq}|{ts}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


@dataclass
class HeartbeatChannel:
    execution_id: str
    attempt_id: str
    generation: int
    max_age_seconds: float
    key: str = field(default_factory=lambda: secrets.token_hex(32))
    last_seq: int = 0
    accepted: int = 0
    rejected: int = 0
    runner_fd: int = -1
    _read_fd: int = -1
    _write_fd: int = -1
    _reader: asyncio.Task | None = None
    _transport: asyncio.BaseTransport | None = None

    def open(self) -> int:
        self._read_fd, self._write_fd = os.pipe()
        os.set_inheritable(self._read_fd, False)
        self.runner_fd = self._write_fd
        return self._write_fd

    def adoptar(self, read_fd: int, write_fd: int) -> int:
        for fd in (self._read_fd, self._write_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._read_fd, self._write_fd = read_fd, write_fd
        os.set_inheritable(self._read_fd, False)
        self.runner_fd = self._write_fd
        return self._write_fd

    def runner_config(self, interval: int) -> dict:
        return {"heartbeat_fd": self._write_fd, "heartbeat_key": self.key, "heartbeat_interval": interval}

    def verify(self, line: bytes, now: float | None = None) -> int | None:
        try:
            data = json.loads(line)
            seq = int(data["seq"])
            ts = str(data["ts"])
            age = (now if now is not None else time.time()) - float(ts)
            mac = str(data["mac"])
        except (ValueError, KeyError, TypeError):
            return None
        if (
            data.get("execution_id") != self.execution_id
            or data.get("attempt_id") != self.attempt_id
            or data.get("lease_generation") != self.generation
        ):
            return None
        if age > self.max_age_seconds or age < -self.max_age_seconds:
            return None
        expected = heartbeat_mac(self.key, self.execution_id, self.attempt_id, self.generation, seq, ts)
        if not hmac.compare_digest(expected, mac):
            return None
        if seq <= self.last_seq:
            return None
        return seq

    def feed(self, line: bytes) -> None:
        seq = self.verify(line.strip())
        if seq is None:
            self.rejected += 1
            if self.rejected in (1, 10, 100):
                logger.warning("Intento %s: latido rechazado (%d rechazos)", self.attempt_id, self.rejected)
            return
        self.last_seq = seq
        self.accepted += 1

    async def start(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_LINE * 4)
        pipe = os.fdopen(self._read_fd, "rb", buffering=0)
        self._read_fd = -1
        self._transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)

        async def _consume() -> None:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    self.rejected += 1
                    with contextlib.suppress(Exception):
                        await reader.read(MAX_LINE * 4)
                    continue
                if not line:
                    return
                if len(line) > MAX_LINE:
                    self.rejected += 1
                    continue
                self.feed(line)

        self._reader = asyncio.create_task(_consume())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._transport is not None:
            self._transport.close()
        for fd in (self._read_fd, self._write_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._read_fd = self._write_fd = -1
