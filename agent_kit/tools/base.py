"""Tool primitive and @tool decorator."""

from __future__ import annotations

import inspect
import json
import time
import typing
import uuid
from typing import Any, Callable

from agent_kit.types import ToolResult, ToolSchema


def _extract_json_schema(fn: Callable) -> dict[str, Any]:
    """
    Build a JSON Schema 'object' description from a function's type hints.

    Supports: str, int, float, bool, list, dict, None/Optional.
    Falls back to {"type": "string"} for unknown types.
    """
    hints: dict[str, Any] = {}
    try:
        # get_type_hints() resolves 'from __future__ import annotations' string annotations
        # back to actual type objects. Falls back to __annotations__ on failure.
        hints = typing.get_type_hints(fn)
        hints.pop("return", None)
    except Exception:
        try:
            hints = fn.__annotations__.copy()
            hints.pop("return", None)
        except Exception:
            pass

    _PY_TO_JSON: dict[Any, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    properties: dict[str, Any] = {}
    required: list[str] = []

    sig = inspect.signature(fn)
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = hints.get(param_name)
        json_type = _PY_TO_JSON.get(annotation, "string")
        properties[param_name] = {"type": json_type}

        # Required if no default value
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


class Tool:
    """
    A callable agent tool with a typed JSON Schema description.

    Use the @tool decorator to create tools — don't instantiate directly.
    """

    def __init__(self, fn: Callable, schema: ToolSchema) -> None:
        self._fn = fn
        self.schema = schema
        self.__name__ = schema.name
        self.__doc__ = schema.description

    async def __call__(self, call_id: str | None = None, **kwargs: Any) -> ToolResult:
        resolved_call_id = call_id or str(uuid.uuid4())
        t0 = time.monotonic()
        try:
            if inspect.iscoroutinefunction(self._fn):
                output = await self._fn(**kwargs)
            else:
                output = self._fn(**kwargs)

            # Ensure output is JSON-serialisable for safe embedding in messages
            try:
                json.dumps(output)
            except (TypeError, ValueError):
                output = str(output)

            return ToolResult(
                call_id=resolved_call_id,
                tool_name=self.schema.name,
                output=output,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            return ToolResult(
                call_id=resolved_call_id,
                tool_name=self.schema.name,
                output=None,
                error=str(exc),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

    def __repr__(self) -> str:
        return f"Tool(name={self.schema.name!r})"


def tool(
    name: str | None = None,
    description: str = "",
    cost_estimate: float = 0.0,
    idempotent: bool = False,
) -> Callable[[Callable], Tool]:
    """
    Decorator that wraps a sync or async function as an agent Tool.

    The function's type hints are used to generate a JSON Schema for the LLM.
    The docstring is used as the description if none is provided.

    Usage::

        @tool(description="Search the web for current information")
        async def web_search(query: str) -> str:
            ...

        @tool(idempotent=True, cost_estimate=0.001)
        async def get_stock_price(ticker: str) -> dict:
            ...
    """
    def decorator(fn: Callable) -> Tool:
        resolved_name = name or fn.__name__
        resolved_desc = description or fn.__doc__ or ""
        schema = ToolSchema(
            name=resolved_name,
            description=resolved_desc.strip(),
            parameters=_extract_json_schema(fn),
            cost_estimate=cost_estimate,
            idempotent=idempotent,
        )
        return Tool(fn, schema)

    return decorator
