from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class RuntimeExecutionResult:
    final_response: str
    messages: list[dict]
    tool_calls: list[dict]
    tokens_used: int
    iterations: int
    engine: str
    response_attachments: list[dict]


class RuntimeExecutionError(RuntimeError):
    pass


class RuntimeBase(ABC):
    """Interface that both HermesRuntime and PiRuntime implement."""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def execute(
        self,
        agent,
        task,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> RuntimeExecutionResult: ...
