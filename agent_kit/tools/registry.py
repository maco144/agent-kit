"""ToolRegistry — manages available tools for an agent."""

from __future__ import annotations

from agent_kit.exceptions import ToolNotAllowedError, ToolNotFoundError
from agent_kit.tools.base import Tool
from agent_kit.types import ToolSchema


class ToolRegistry:
    """
    Holds the set of tools registered on an agent and enforces the allowlist.

    The allowlist (allowed_tools) is kernel-enforced: any tool call not in the
    list raises ToolNotAllowedError before the tool is even invoked.
    """

    def __init__(
        self,
        tools: list[Tool] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._allowed: set[str] | None = (
            set(allowed_tools) if allowed_tools is not None else None
        )
        for t in tools or []:
            self.register(t)

    def register(self, t: Tool) -> None:
        self._tools[t.schema.name] = t

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(name)
        if self._allowed is not None and name not in self._allowed:
            raise ToolNotAllowedError(name)
        return self._tools[name]

    def schemas(self) -> list[ToolSchema]:
        """Return schemas for all tools that pass the allowlist filter."""
        tools = list(self._tools.values())
        if self._allowed is not None:
            tools = [t for t in tools if t.schema.name in self._allowed]
        return [t.schema for t in tools]

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        names = list(self._tools.keys())
        return f"ToolRegistry(tools={names}, allowed={self._allowed})"
