"""InMemoryStore — default zero-dependency conversation memory."""

from __future__ import annotations

from agent_kit.memory.window import window_indices
from agent_kit.types import Message


class InMemoryStore:
    """
    Simple in-process conversation memory with a configurable window.

    When the window is exceeded, the oldest non-system messages are dropped,
    never separating a tool call from its results. System messages are always
    preserved.

    Usage::

        mem = InMemoryStore(window=50)
        mem.add(Message(role="user", content="Hello"))
        history = mem.history()
    """

    def __init__(self, window: int = 50) -> None:
        self._window = window
        self._messages: list[Message] = []

    def add(self, message: Message) -> None:
        self._messages.append(message)
        self._trim()

    def add_many(self, messages: list[Message]) -> None:
        self._messages.extend(messages)
        self._trim()

    def history(self, include_system: bool = True) -> list[Message]:
        if include_system:
            return list(self._messages)
        return [m for m in self._messages if m.role != "system"]

    def clear(self) -> None:
        self._messages = []

    def _trim(self) -> None:
        if len(self._messages) <= self._window:
            return
        # Keep all system messages; trim non-system without orphaning tool results
        system = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]
        keep = max(0, self._window - len(system))
        kept = window_indices([m.role for m in non_system], keep)
        self._messages = system + [non_system[i] for i in kept]

    def __len__(self) -> int:
        return len(self._messages)

    def __bool__(self) -> bool:
        # Always truthy — an empty store is still a valid store object.
        return True

    def __repr__(self) -> str:
        return f"InMemoryStore(messages={len(self._messages)}, window={self._window})"
