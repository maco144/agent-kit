"""Typed run outputs: provider-ready JSON Schemas from Python types, and answer validation."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Generic, Iterator, TypeVar

from pydantic import TypeAdapter, ValidationError

T = TypeVar("T")

_CONSTRAINT_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "maxItems",
        "uniqueItems",
    }
)
_SUPPORTED_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
_TICKS = "`" * 3
_FENCE = re.compile(rf"^{_TICKS}[A-Za-z0-9_-]*\s*\n(.*)\n{_TICKS}$", re.DOTALL)
_MAX_ERROR_LINES = 20


class OutputParseError(ValueError):
    """A final answer that is not valid JSON or does not validate against the output type."""

    def __init__(self, errors: str) -> None:
        super().__init__(errors)
        self.errors = errors


@dataclass(frozen=True)
class OutputSpec(Generic[T]):
    """A run's output type, its provider-facing JSON Schema, and its parser."""

    output_type: Any
    name: str
    json_schema: dict[str, Any]
    wrapped: bool  # root wrapped as {"result": ...}: providers require an object root
    native_compatible: bool  # False when strict provider schemas can't express it
    adapter: TypeAdapter[T]

    @classmethod
    def from_type(cls, output_type: Any) -> OutputSpec[Any]:
        adapter: TypeAdapter[Any] = TypeAdapter(output_type)
        schema = adapter.json_schema()
        defs: dict[str, Any] = schema.pop("$defs", {})
        wrapped = not _is_plain_object(schema)
        if wrapped:
            schema = {"type": "object", "properties": {"result": schema}, "required": ["result"]}
        native = not _has_open_object([schema, defs]) and not _is_recursive(defs)
        if native:
            schema = _inline_ref_siblings(schema, defs)
        schema = _strict(schema)
        if defs:
            schema["$defs"] = {name: _strict(body) for name, body in defs.items()}
        name = re.sub(r"[^A-Za-z0-9_-]", "_", getattr(output_type, "__name__", None) or "output")[:64]
        return cls(output_type, name, schema, wrapped, native, adapter)

    def parse(self, text: str) -> T:
        body = text.strip()
        fenced = _FENCE.match(body)
        if fenced:
            body = fenced.group(1).strip()
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OutputParseError(f"response is not valid JSON: {exc}") from None
        if self.wrapped:
            if not isinstance(value, dict) or "result" not in value:
                raise OutputParseError('expected a JSON object with a "result" key')
            value = value["result"]
        try:
            return self.adapter.validate_python(value)
        except ValidationError as exc:
            lines = [
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            ]
            raise OutputParseError("\n".join(lines[:_MAX_ERROR_LINES])) from None

    def instructions(self) -> str:
        """System prompt addition for providers without native structured outputs."""
        return (
            "Respond with only a JSON value that matches this JSON Schema — no prose, no code fences.\n"
            + json.dumps(self.json_schema, indent=2)
        )


def _is_plain_object(schema: dict[str, Any]) -> bool:
    return (
        schema.get("type") == "object"
        and "properties" in schema
        and schema.get("additionalProperties", False) is False
    )


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _has_open_object(node: Any) -> bool:
    return any(n.get("additionalProperties") not in (None, False) for n in _walk(node))


def _is_recursive(defs: dict[str, Any]) -> bool:
    graph = {
        name: {n["$ref"].rsplit("/", 1)[-1] for n in _walk(body) if isinstance(n.get("$ref"), str)}
        for name, body in defs.items()
    }

    def cycles_back(start: str) -> bool:
        stack, seen = list(graph.get(start, ())), set()
        while stack:
            current = stack.pop()
            if current == start:
                return True
            if current not in seen:
                seen.add(current)
                stack.extend(graph.get(current, ()))
        return False

    return any(cycles_back(name) for name in graph)


def _inline_ref_siblings(node: Any, defs: dict[str, Any]) -> Any:
    """Replace {"$ref": ..., <other keys>} with the referenced schema merged with those keys."""
    if isinstance(node, list):
        return [_inline_ref_siblings(v, defs) for v in node]
    if not isinstance(node, dict):
        return node
    node = {k: _inline_ref_siblings(v, defs) for k, v in node.items()}
    if "$ref" in node and len(node) > 1:
        target = copy.deepcopy(defs[node.pop("$ref").rsplit("/", 1)[-1]])
        node = {**_inline_ref_siblings(target, defs), **node}
    return node


def _strict(node: Any) -> Any:
    """Close objects, require every property, and move unsupported keywords into descriptions."""
    if isinstance(node, list):
        return [_strict(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    moved: list[str] = []
    for key, value in node.items():
        if key in ("title", "default", "discriminator"):
            continue
        if key in _CONSTRAINT_KEYS or (key == "minItems" and value not in (0, 1)):
            moved.append(f"{key}={value}")
        elif key == "format" and value not in _SUPPORTED_FORMATS:
            moved.append(f"format={value}")
        elif key == "properties":
            out[key] = {name: _strict(prop) for name, prop in value.items()}
        else:
            out["anyOf" if key == "oneOf" else key] = _strict(value)
    if moved:
        note = f"(constraints: {', '.join(moved)})"
        out["description"] = f"{out['description']} {note}" if out.get("description") else note
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out
