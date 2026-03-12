"""Tests for Tool, @tool decorator, and ToolRegistry."""

from __future__ import annotations

import pytest

from agent_kit.exceptions import ToolNotAllowedError, ToolNotFoundError
from agent_kit.tools import Tool, ToolRegistry, tool


def test_tool_decorator_sync():
    @tool(description="Adds two numbers", idempotent=True)
    def add(a: int, b: int) -> int:
        return a + b

    assert isinstance(add, Tool)
    assert add.schema.name == "add"
    assert add.schema.description == "Adds two numbers"
    assert add.schema.idempotent is True
    assert add.schema.parameters["properties"]["a"]["type"] == "integer"
    assert add.schema.parameters["properties"]["b"]["type"] == "integer"
    assert "a" in add.schema.parameters["required"]
    assert "b" in add.schema.parameters["required"]


def test_tool_decorator_uses_docstring():
    @tool()
    def multiply(x: float, y: float) -> float:
        """Multiplies two numbers together."""
        return x * y

    assert multiply.schema.description == "Multiplies two numbers together."


def test_tool_decorator_custom_name():
    @tool(name="my_custom_tool")
    def some_fn(q: str) -> str:
        return q

    assert some_fn.schema.name == "my_custom_tool"


@pytest.mark.asyncio
async def test_tool_sync_execution():
    @tool(description="returns greeting")
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    result = await greet(name="World")
    assert result.output == "Hello, World!"
    assert result.error is None
    assert result.tool_name == "greet"


@pytest.mark.asyncio
async def test_tool_async_execution():
    @tool(description="async tool")
    async def async_add(a: int, b: int) -> int:
        return a + b

    result = await async_add(a=3, b=4)
    assert result.output == 7
    assert result.error is None


@pytest.mark.asyncio
async def test_tool_captures_exception():
    @tool(description="always fails")
    def broken() -> str:
        raise ValueError("something went wrong")

    result = await broken()
    assert result.output is None
    assert "something went wrong" in result.error


def test_registry_get_existing():
    @tool(description="t")
    def my_tool() -> str:
        return "ok"

    registry = ToolRegistry(tools=[my_tool])
    assert registry.get("my_tool") is my_tool


def test_registry_not_found():
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nonexistent")


def test_registry_allowlist_blocks():
    @tool(description="t1")
    def tool_a() -> str: return "a"

    @tool(description="t2")
    def tool_b() -> str: return "b"

    registry = ToolRegistry(tools=[tool_a, tool_b], allowed_tools=["tool_a"])

    # tool_a is allowed
    assert registry.get("tool_a") is tool_a

    # tool_b is registered but not allowed
    with pytest.raises(ToolNotAllowedError):
        registry.get("tool_b")


def test_registry_schemas_filtered_by_allowlist():
    @tool(description="t1")
    def tool_a() -> str: return "a"

    @tool(description="t2")
    def tool_b() -> str: return "b"

    registry = ToolRegistry(tools=[tool_a, tool_b], allowed_tools=["tool_a"])
    schemas = registry.schemas()
    assert len(schemas) == 1
    assert schemas[0].name == "tool_a"


def test_registry_no_allowlist_returns_all_schemas():
    @tool(description="t1")
    def tool_a() -> str: return "a"

    @tool(description="t2")
    def tool_b() -> str: return "b"

    registry = ToolRegistry(tools=[tool_a, tool_b])
    schemas = registry.schemas()
    assert len(schemas) == 2
