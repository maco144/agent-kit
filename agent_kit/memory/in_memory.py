"""InMemoryStore — default zero-dependency conversation memory."""

from __future__ import annotations

from agent_kit.types import Message


class InMemoryStore:
    """
    Simple in-process conversation memory with a configurable window.

    When the window is exceeded, the oldest non-system messages are dropped.
    System messages are always preserved.

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
        # Keep all system messages; drop oldest non-system
        system = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]
        # Keep most recent (window - len(system)) non-system messages
        keep = max(0, self._window - len(system))
        trimmed = system + non_system[-keep:] if keep > 0 else system
        self._messages = trimmed

    def __len__(self) -> int:
        return len(self._messages)

    def __bool__(self) -> bool:
        # Always truthy — an empty store is still a valid store object.
        return True

    def __repr__(self) -> str:
        return f"InMemoryStore(messages={len(self._messages)}, window={self._window})"
