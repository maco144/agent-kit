# Typed Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `await agent.run(prompt, output_type=Invoice)` returns `AgentResult[Invoice]` with a validated `.parsed`, using provider-native structured outputs where possible and a schema-in-prompt fallback otherwise, with repair turns on invalid answers.

**Architecture:** A new `agent_kit/output.py` turns any Pydantic-validatable type into an `OutputSpec` (strict provider schema + parser). Providers accept `output_schema: OutputSpec | None` and translate it to `output_config.format` (Anthropic) or `response_format` (OpenAI/Ollama). `AgentLoop` picks native or prompt mode per run, validates the tool-free final turn, feeds validation errors back up to `output_retries` times, and sets `AgentResult.parsed`.

**Tech Stack:** Python 3.11+, Pydantic v2 `TypeAdapter`, anthropic>=1.0, openai>=1.40, pytest-asyncio (auto mode), mypy strict, ruff.

**Spec:** `specs/13-typed-results.md`

## Global Constraints

- `agent_kit/types.py` imports nothing from `agent_kit` (import graph root).
- Dependency floors: `anthropic>=1.0`, `openai>=1.40`.
- Untyped runs are unchanged: no `output_schema` kwarg reaches providers, `parsed` is `None`.
- Provider detection is `getattr(provider, "supports_structured_output", False)`.
- Audit payloads never contain raw output; `errors` truncated to 500 chars.
- mypy runs `strict = true`: generic `AgentResult` must be parameterised (`AgentResult[Any]`) everywhere it is annotated.
- Gates for every task: `pytest`, `ruff check agent_kit tests`, `mypy agent_kit` — run with `python3` / the scratchpad venv (`venv-harness` has openai + anthropic 1.x installed).

---

### Task 1: `OutputSpec` — schemas and parsing

**Files:**
- Create: `agent_kit/output.py`
- Test: `tests/test_output.py`

**Interfaces:**
- Produces: `OutputSpec[T]` (frozen dataclass: `output_type: Any`, `name: str`, `json_schema: dict[str, Any]`, `wrapped: bool`, `native_compatible: bool`, `adapter: TypeAdapter[T]`), `OutputSpec.from_type(output_type: Any) -> OutputSpec[Any]`, `OutputSpec.parse(text: str) -> T`, `OutputSpec.instructions() -> str`, `OutputParseError(ValueError)` with `.errors: str`.

- [ ] **Step 1: Write the failing tests**

```python
"""OutputSpec: provider schemas from Python types, and answer parsing."""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import Annotated, Literal, TypedDict

import pytest
from pydantic import BaseModel, Field

from agent_kit.output import OutputParseError, OutputSpec


class Item(BaseModel):
    sku: str = Field(min_length=3, pattern="^A", description="stock unit")
    qty: int = Field(ge=1, default=1)


class Invoice(BaseModel):
    number: str
    note: str | None = None
    items: list[Item]
    main: Item = Field(description="primary line")


class Tagged(BaseModel):
    tags: dict[str, int] = {}


class Node(BaseModel):
    name: str
    children: list[Node] = []


class Cat(BaseModel):
    kind: Literal["cat"]


class Dog(BaseModel):
    kind: Literal["dog"]


class Pet(BaseModel):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]


class Color(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Point:
    x: int


class Movie(TypedDict):
    title: str


def objects(node: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(objects(value))
    return found


def keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in keys(v)}
    return set()


def test_model_schema_is_strict():
    spec = OutputSpec.from_type(Invoice)
    assert (spec.name, spec.wrapped, spec.native_compatible) == ("Invoice", False, True)
    for obj in objects(spec.json_schema):
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert spec.json_schema["required"] == ["number", "note", "items", "main"]
    assert not {"title", "default", "minLength", "pattern", "minimum"} & keys(spec.json_schema)


def test_constraints_move_to_description_and_are_still_enforced():
    spec = OutputSpec.from_type(Invoice)
    sku = spec.json_schema["$defs"]["Item"]["properties"]["sku"]
    assert sku["description"] == "stock unit (constraints: minLength=3, pattern=^A)"
    bad = {"number": "1", "note": None, "items": [{"sku": "b", "qty": 0}], "main": {"sku": "Axy", "qty": 1}}
    with pytest.raises(OutputParseError) as exc:
        spec.parse(json.dumps(bad))
    assert exc.value.errors.splitlines() == [
        "items.0.sku: String should have at least 3 characters",
        "items.0.qty: Input should be greater than or equal to 1",
    ]


def test_ref_with_siblings_is_inlined():
    main = OutputSpec.from_type(Invoice).json_schema["properties"]["main"]
    assert "$ref" not in main
    assert main["description"] == "primary line"
    assert main["additionalProperties"] is False


def test_discriminated_union_becomes_any_of():
    pet = OutputSpec.from_type(Pet).json_schema["properties"]["pet"]
    assert "oneOf" not in pet and "discriminator" not in pet
    assert len(pet["anyOf"]) == 2


@pytest.mark.parametrize(
    ("output_type", "answer", "value"),
    [
        (list[int], '{"result": [1, 2]}', [1, 2]),
        (Color, '{"result": "red"}', Color.RED),
        (int, '{"result": 7}', 7),
        (Literal["yes", "no"], '{"result": "no"}', "no"),
        (Cat | Dog, '{"result": {"kind": "dog"}}', Dog(kind="dog")),
    ],
)
def test_non_object_roots_are_wrapped(output_type, answer, value):
    spec = OutputSpec.from_type(output_type)
    assert spec.wrapped is True
    assert spec.json_schema["required"] == ["result"]
    assert spec.json_schema["additionalProperties"] is False
    assert spec.parse(answer) == value


def test_dataclass_and_typeddict():
    assert OutputSpec.from_type(Point).parse('{"x": 3}') == Point(x=3)
    assert OutputSpec.from_type(Movie).parse('{"title": "Heat"}') == {"title": "Heat"}
    assert OutputSpec.from_type(Point).wrapped is False


def test_open_dicts_and_recursion_are_not_native():
    assert OutputSpec.from_type(Tagged).native_compatible is False
    assert OutputSpec.from_type(dict[str, int]).native_compatible is False
    assert OutputSpec.from_type(Node).native_compatible is False
    assert OutputSpec.from_type(Node).parse('{"result": {"name": "a", "children": []}}') == Node(name="a")


def test_name_sanitised():
    assert OutputSpec.from_type(list[int]).name == "list"
    assert OutputSpec.from_type(Cat | Dog).name == "output"


def test_parse_accepts_fences():
    spec = OutputSpec.from_type(Point)
    fence = "`" * 3
    assert spec.parse(f'{fence}json\n{{"x": 1}}\n{fence}') == Point(x=1)
    assert spec.parse(f'  {fence}\n{{"x": 2}}\n{fence}  ') == Point(x=2)


def test_parse_errors():
    with pytest.raises(OutputParseError, match="response is not valid JSON"):
        OutputSpec.from_type(Point).parse("x is 1")
    with pytest.raises(OutputParseError, match='expected a JSON object with a "result" key'):
        OutputSpec.from_type(list[int]).parse("[1, 2]")


def test_errors_capped_at_20_lines():
    spec = OutputSpec.from_type(list[int])
    with pytest.raises(OutputParseError) as exc:
        spec.parse(json.dumps({"result": ["x"] * 30}))
    assert len(exc.value.errors.splitlines()) == 20


def test_instructions_embed_schema():
    spec = OutputSpec.from_type(Point)
    text = spec.instructions()
    assert text.startswith("Respond with only a JSON value that matches this JSON Schema")
    assert json.dumps(spec.json_schema, indent=2) in text
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_output.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'agent_kit.output'`.

- [ ] **Step 3: Implement `agent_kit/output.py`**

```python
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
```

- [ ] **Step 4: Run tests, lint, types**

Run: `python3 -m pytest tests/test_output.py -q && ruff check agent_kit tests && mypy agent_kit`
Expected: all pass, no findings.

- [ ] **Step 5: Commit**

```bash
git add agent_kit/output.py tests/test_output.py
git commit -m "feat: OutputSpec — strict provider schemas and validation for typed outputs"
```

---

### Task 2: Providers — native structured outputs

**Files:**
- Modify: `agent_kit/providers/base.py` (protocol signatures + docstring)
- Modify: `agent_kit/providers/anthropic.py` (`complete`, `stream`, `supports_structured_output`)
- Modify: `agent_kit/providers/openai.py` (`complete`, `stream`, `supports_structured_output`, refusal)
- Modify: `pyproject.toml` (`anthropic>=1.0`, `openai>=1.40`)
- Test: `tests/test_provider_requests.py`

**Interfaces:**
- Consumes: `OutputSpec` from Task 1 (`.name`, `.json_schema`).
- Produces: `complete(..., output_schema: OutputSpec[Any] | None = None, **kwargs)` and the same on `stream`; class attribute `supports_structured_output = True` on `AnthropicProvider` and `OpenAIProvider` (inherited by `OllamaProvider`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_provider_requests.py`; add `from pydantic import BaseModel`, `from agent_kit.output import OutputSpec`, `from agent_kit.types import Message` to the imports)

```python
# ---------------------------------------------------------------------------
# Structured outputs
# ---------------------------------------------------------------------------


class Weather(BaseModel):
    city: str
    temp_c: int


WEATHER = OutputSpec.from_type(Weather)
ASK = [Message(role="user", content="weather?")]


async def test_anthropic_complete_sends_output_config():
    provider = AnthropicProvider(api_key="test")
    fake = FakeAnthropic([anthropic_response([text_block("{}")]), anthropic_response([text_block("{}")])])
    provider._client = fake  # type: ignore[assignment]

    await provider.complete(ASK, output_schema=WEATHER, output_config={"effort": "low"})
    await provider.complete(ASK)

    assert provider.supports_structured_output is True
    assert fake.calls[0]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": WEATHER.json_schema},
    }
    assert "output_config" not in fake.calls[1]


async def test_anthropic_stream_sends_output_config():
    provider = AnthropicProvider(api_key="test")
    calls: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return FakeAnthropicStream(["{}"], anthropic_response([text_block("{}")]))

    provider._client = NS(messages=NS(stream=fake_stream))  # type: ignore[assignment]
    [c async for c in provider.stream(ASK, output_schema=WEATHER)]

    assert calls[0]["output_config"] == {"format": {"type": "json_schema", "schema": WEATHER.json_schema}}


@requires_openai
async def test_openai_complete_sends_response_format():
    from agent_kit.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test")
    fake = FakeOpenAI([openai_response("{}"), openai_response("{}")])
    provider._client = fake  # type: ignore[assignment]

    await provider.complete(ASK, output_schema=WEATHER)
    await provider.complete(ASK)

    assert provider.supports_structured_output is True
    assert fake.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Weather", "schema": WEATHER.json_schema, "strict": True},
    }
    assert "response_format" not in fake.calls[1]


@requires_openai
async def test_openai_stream_sends_response_format():
    from agent_kit.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test")
    fake = FakeOpenAI([FakeOpenAIStream([openai_chunk("{}"), openai_chunk(usage=NS(prompt_tokens=1, completion_tokens=1))])])
    provider._client = fake  # type: ignore[assignment]

    [c async for c in provider.stream(ASK, output_schema=WEATHER)]

    assert fake.calls[0]["response_format"]["json_schema"]["strict"] is True


@requires_openai
async def test_openai_refusal_becomes_assistant_text():
    from agent_kit.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test")
    refusal = openai_response(None)
    refusal.choices[0].message.refusal = "I can't help with that."
    provider._client = FakeOpenAI([refusal])  # type: ignore[assignment]

    turn = await provider.complete(ASK, output_schema=WEATHER)

    assert turn.message_out is not None
    assert turn.message_out.content == "I can't help with that."


@requires_openai
def test_ollama_inherits_structured_output_support():
    from agent_kit.providers.ollama import OllamaProvider

    assert OllamaProvider().supports_structured_output is True
```

- [ ] **Step 2: Run to verify failure**

Run: `venv-harness/bin/python -m pytest tests/test_provider_requests.py -q -k "output_config or response_format or refusal or ollama_inherits"`
Expected: FAIL — `TypeError` from the fake clients receiving `output_schema`, and `AttributeError: supports_structured_output`.

- [ ] **Step 3: Implement**

`agent_kit/providers/base.py` — add under the imports:

```python
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable
...
if TYPE_CHECKING:
    from agent_kit.output import OutputSpec
```

Add `output_schema: OutputSpec[Any] | None = None,` after `max_tokens` in both `complete` and `stream`, and append to the class docstring:

```
    Providers that constrain answers natively set ``supports_structured_output = True`` and honour
    ``output_schema``. AgentLoop only passes ``output_schema`` to such providers; others receive the
    schema as system prompt instructions instead.
```

`agent_kit/providers/anthropic.py` — `TYPE_CHECKING` import of `OutputSpec`, a helper, and the parameter in both methods:

```python
def _apply_output_schema(call_kwargs: dict[str, Any], output_schema: OutputSpec[Any] | None) -> None:
    """Constrain the answer with output_config.format, keeping other output_config keys (effort)."""
    if output_schema is not None:
        call_kwargs["output_config"] = {
            **call_kwargs.get("output_config", {}),
            "format": {"type": "json_schema", "schema": output_schema.json_schema},
        }
```

```python
class AnthropicProvider:
    ...
    supports_structured_output = True
```

In `complete` and `stream`, add the `output_schema: OutputSpec[Any] | None = None,` parameter after `max_tokens`, and call `_apply_output_schema(call_kwargs, output_schema)` right after the `if tools:` block.

`agent_kit/providers/openai.py` — `TYPE_CHECKING` import of `OutputSpec`, then:

```python
def _apply_response_format(call_kwargs: dict[str, Any], output_schema: OutputSpec[Any] | None) -> None:
    if output_schema is not None:
        call_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": output_schema.name, "schema": output_schema.json_schema, "strict": True},
        }
```

```python
class OpenAIProvider:
    ...
    supports_structured_output = True
```

Same parameter and `_apply_response_format(call_kwargs, output_schema)` after the `if tools:` block in `complete` and `stream`. In `complete`, replace the assistant message construction:

```python
        text = msg.content or getattr(msg, "refusal", None) or ""
        assistant_msg = Message(role="assistant", content=text, tool_calls=tool_calls)
```

In `stream`, after the `if delta.content:` block:

```python
                    refusal = getattr(delta, "refusal", None)
                    if refusal:
                        text_parts.append(refusal)
                        yield refusal
```

`pyproject.toml`: `"anthropic>=1.0",` and `openai = ["openai>=1.40"]`.

- [ ] **Step 4: Run full SDK tests, lint, types**

Run: `venv-harness/bin/python -m pytest -q && ruff check agent_kit tests && venv-harness/bin/python -m mypy agent_kit`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agent_kit/providers pyproject.toml tests/test_provider_requests.py
git commit -m "feat: providers accept output_schema — Anthropic output_config, OpenAI response_format"
```

---

### Task 3: Typed runs through the loop

**Files:**
- Modify: `agent_kit/types.py` (`AgentResult` generic, `PipelineResult.stage_results: list[AgentResult[Any]]`)
- Modify: `agent_kit/exceptions.py` (`OutputValidationError`)
- Modify: `agent_kit/agent/loop.py` (spec, mode, validation, repair, audit)
- Modify: `agent_kit/agent/agent.py` (`output_retries`, `run`/`stream` `output_type`, overloads)
- Modify: `agent_kit/cloud/reporter.py`, `agent_kit/orchestrator/pipeline.py`, `agent_kit/orchestrator/dag.py` (`AgentResult[Any]` annotations only, wherever mypy asks)
- Test: `tests/test_typed_results.py`

**Interfaces:**
- Consumes: `OutputSpec.from_type`, `.parse`, `.instructions`, `.native_compatible`, `.name`, `OutputParseError.errors` (Task 1); providers' `output_schema` kwarg and `supports_structured_output` (Task 2).
- Produces: `AgentResult[T].parsed`; `OutputValidationError(errors: str, raw_output: str, attempts: int)`; `AgentConfig(output_retries=2)`; `Agent.run(prompt, *, output_type=None, **context)`; `Agent.stream(prompt, *, output_type=None, **context)`; audit event `output_validation_failed`; `agent_complete.output_type`.

- [ ] **Step 1: Write the failing tests** — `tests/test_typed_results.py`

```python
"""Typed results: output_type through Agent.run / Agent.stream."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from agent_kit import Agent, AgentConfig, AgentResult, tool
from agent_kit.exceptions import MaxTurnsExceededError, OutputValidationError
from agent_kit.hooks import Hooks, ToolCallContext
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, Turn


class Weather(BaseModel):
    city: str
    temp_c: int


class Tagged(BaseModel):
    tags: dict[str, int]


class Scripted:
    """Scripted turns; records each request's messages and keyword arguments."""

    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn, native: bool | None = True) -> None:
        self.turns = list(turns)
        self.requests: list[tuple[list[Message], dict[str, Any]]] = []
        if native is not None:
            self.supports_structured_output = native

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append((list(messages), kw))
        return self.turns.pop(0)

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.requests.append((list(messages), kw))
        turn = self.turns.pop(0)
        if turn.message_out and turn.message_out.content:
            yield turn.message_out.content
        yield turn


def final(text: str) -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary())


def call(name: str, **args: Any) -> Turn:
    tc = [ToolCall(tool_name=name, arguments=args, call_id=f"{name}-1")]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tc), tool_calls=tc, cost=CostSummary())


@tool(description="Weather for a city")
async def get_weather(city: str) -> dict[str, Any]:
    return {"city": city, "temp_c": 21}


GOOD = '{"city": "Paris", "temp_c": 21}'


def audited(agent: Agent) -> list[tuple[str, dict[str, Any]]]:
    """Record (event_type, payload) for every audit append — records only keep payload hashes."""
    assert agent.audit is not None
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return recorded


def payloads(recorded: list[tuple[str, dict[str, Any]]], event_type: str) -> list[dict[str, Any]]:
    return [p for e, p in recorded if e == event_type]


async def test_valid_answer_is_parsed():
    provider = Scripted(final(GOOD))
    agent = Agent(provider)
    recorded = audited(agent)

    result = await agent.run("weather?", output_type=Weather)

    assert result.parsed == Weather(city="Paris", temp_c=21)
    assert result.output == GOOD
    assert provider.requests[0][1]["output_schema"].name == "Weather"
    assert payloads(recorded, "agent_complete")[0]["output_type"] == "Weather"


async def test_untyped_run_passes_no_output_schema():
    provider = Scripted(final("hello"))
    agent = Agent(provider)
    recorded = audited(agent)

    result = await agent.run("hi")

    assert result.parsed is None
    assert "output_schema" not in provider.requests[0][1]
    assert payloads(recorded, "agent_complete")[0]["output_type"] is None


async def test_invalid_answer_is_repaired():
    provider = Scripted(final('{"city": "Paris"}'), final(GOOD))
    agent = Agent(provider)
    recorded = audited(agent)

    result = await agent.run("weather?", output_type=Weather)

    assert result.parsed == Weather(city="Paris", temp_c=21)
    repair = provider.requests[1][0][-1]
    assert repair.role == "user"
    assert repair.content == (
        "Your response did not match the required output schema:\n"
        "temp_c: Field required\n"
        "Respond again with only the corrected JSON."
    )
    assert payloads(recorded, "output_validation_failed") == [
        {"turn": 1, "attempt": 1, "native": True, "errors": "temp_c: Field required"}
    ]
    assert len(result.turns) == 2


async def test_retries_exhausted_raises():
    provider = Scripted(final("nope"), final("still no"), final("never"))
    agent = Agent(provider, config=AgentConfig(output_retries=2))
    recorded = audited(agent)

    with pytest.raises(OutputValidationError) as exc:
        await agent.run("weather?", output_type=Weather)

    assert exc.value.attempts == 3
    assert exc.value.raw_output == "never"
    assert exc.value.errors.startswith("response is not valid JSON")
    assert [p["attempt"] for p in payloads(recorded, "output_validation_failed")] == [1, 2, 3]


async def test_zero_retries_raises_on_first_invalid_answer():
    agent = Agent(Scripted(final("nope")), config=AgentConfig(output_retries=0))
    with pytest.raises(OutputValidationError) as exc:
        await agent.run("weather?", output_type=Weather)
    assert exc.value.attempts == 1


async def test_repair_turns_count_toward_max_turns():
    agent = Agent(Scripted(final("nope"), final("no")), config=AgentConfig(max_turns=2, output_retries=5))
    with pytest.raises(MaxTurnsExceededError):
        await agent.run("weather?", output_type=Weather)


async def test_tools_then_typed_answer():
    provider = Scripted(call("get_weather", city="Paris"), final(GOOD))
    agent = Agent(provider, tools=[get_weather])

    result = await agent.run("weather?", output_type=Weather)

    assert result.parsed == Weather(city="Paris", temp_c=21)
    assert all(kw["output_schema"].name == "Weather" for _, kw in provider.requests)
    assert provider.requests[1][0][-1].role == "tool"


async def test_provider_without_native_support_gets_prompt_mode():
    provider = Scripted(final(GOOD), native=None)
    agent = Agent(provider, config=AgentConfig(system_prompt="Be terse."))

    result = await agent.run("weather?", output_type=Weather)

    kw = provider.requests[0][1]
    assert "output_schema" not in kw
    assert kw["system"].startswith("Be terse.\n\nRespond with only a JSON value")
    assert result.parsed == Weather(city="Paris", temp_c=21)


async def test_non_native_schema_forces_prompt_mode():
    provider = Scripted(final('{"tags": {"a": 1}}'))
    agent = Agent(provider)

    result = await agent.run("tags?", output_type=Tagged)

    kw = provider.requests[0][1]
    assert "output_schema" not in kw
    assert kw["system"].startswith("Respond with only a JSON value")
    assert result.parsed == Tagged(tags={"a": 1})


async def test_wrapped_root():
    agent = Agent(Scripted(final('{"result": ["a", "b"]}')))
    result = await agent.run("letters?", output_type=list[str])
    assert result.parsed == ["a", "b"]


async def test_stream_parity():
    provider = Scripted(call("get_weather", city="Paris"), final('{"city": 1}'), final(GOOD))
    agent = Agent(provider, tools=[get_weather])
    recorded = audited(agent)

    chunks = [c async for c in agent.stream("weather?", output_type=Weather)]

    assert chunks == ['{"city": 1}', GOOD]
    assert agent.last_result is not None
    assert agent.last_result.parsed == Weather(city="Paris", temp_c=21)
    assert all(kw["output_schema"].name == "Weather" for _, kw in provider.requests)
    assert len(payloads(recorded, "output_validation_failed")) == 1


async def test_output_type_not_in_hook_context():
    seen: list[dict[str, Any]] = []

    def record(ctx: ToolCallContext) -> None:
        seen.append(ctx.context)

    provider = Scripted(call("get_weather", city="Paris"), final(GOOD))
    agent = Agent(provider, tools=[get_weather], config=AgentConfig(hooks=Hooks(before_tool=[record])))

    await agent.run("weather?", output_type=Weather, tenant="acme")

    assert seen == [{"tenant": "acme"}]


def test_agent_result_is_generic():
    result = AgentResult[Weather](output=GOOD, parsed=Weather(city="Paris", temp_c=21))
    assert result.parsed is not None and result.parsed.temp_c == 21
    assert AgentResult(output="x").parsed is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_typed_results.py -q`
Expected: FAIL — `ImportError: cannot import name 'OutputValidationError'`.

- [ ] **Step 3: Implement**

`agent_kit/exceptions.py` (append):

```python
class OutputValidationError(AgentKitError):
    """The final answer never validated against the run's output_type."""

    def __init__(self, errors: str, raw_output: str, attempts: int) -> None:
        super().__init__(f"Output failed validation after {attempts} attempt(s):\n{errors}")
        self.errors = errors
        self.raw_output = raw_output
        self.attempts = attempts
```

`agent_kit/types.py` — imports `from typing import Any, Generic, Literal, TypeVar`, then:

```python
T = TypeVar("T")


class AgentResult(BaseModel, Generic[T]):
    """Final result returned by Agent.run(). ``parsed`` holds the validated output_type value."""

    output: str
    parsed: T | None = None
    turns: list[Turn] = Field(default_factory=list)
    ...  # remaining fields unchanged
```

and `stage_results: list[AgentResult[Any]] = Field(default_factory=list)`.

`agent_kit/agent/loop.py`:

1. Imports: `from agent_kit.exceptions import BudgetExceededError, MaxTurnsExceededError, OutputValidationError, RunStoppedByHookError` and `from agent_kit.output import OutputParseError, OutputSpec`.
2. Module constant:

```python
_REPAIR_PROMPT = (
    "Your response did not match the required output schema:\n{errors}\n"
    "Respond again with only the corrected JSON."
)
```

3. `__init__` gains `output_retries: int = 2` → `self._output_retries = output_retries`; `self.result: AgentResult[Any] | None = None`.
4. `run(self, prompt: str, output_type: Any = None, **context: Any) -> AgentResult[Any]` and `stream(self, prompt: str, output_type: Any = None, **context: Any)` pass `output_type` to `_execute(prompt, streaming, context, output_type)`.
5. At the top of `_execute`, after `self._context = dict(context)`:

```python
        spec = OutputSpec.from_type(output_type) if output_type is not None else None
        native = (
            spec is not None
            and spec.native_compatible
            and bool(getattr(self._provider, "supports_structured_output", False))
        )
        system = self._system_prompt
        if spec is not None and not native:
            system = f"{system}\n\n{spec.instructions()}" if system else spec.instructions()
        output_kwargs: dict[str, Any] = {"output_schema": spec} if native else {}
        parsed: Any = None
        invalid_answers = 0
```

6. Streaming call: `self._open_stream, messages, tool_schemas or None, system, output_kwargs`; `_open_stream` signature becomes `(self, messages, tools, system: str, output_kwargs: dict[str, Any])` and passes `system=system or None, **output_kwargs` to `self._provider.stream(...)`.
7. Non-streaming call: `system=system or None,` and `**output_kwargs,` in the `with_retry(... self._provider.complete ...)` call.
8. Replace the final-answer block:

```python
                    # No tool calls → a final answer (validated when the run is typed)
                    if not turn.tool_calls:
                        final_output = turn.message_out.content if turn.message_out else ""
                        self._turns.append(turn)
                        if self._reporter:
                            await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                        if spec is None:
                            break
                        try:
                            parsed = spec.parse(final_output)
                            break
                        except OutputParseError as exc:
                            invalid_answers += 1
                            self._audit_event(
                                "output_validation_failed",
                                "agent",
                                {
                                    "turn": turn_count,
                                    "attempt": invalid_answers,
                                    "native": native,
                                    "errors": exc.errors[:500],
                                },
                            )
                            if invalid_answers > self._output_retries:
                                raise OutputValidationError(exc.errors, final_output, invalid_answers) from None
                            self._memory.add(
                                Message(role="user", content=_REPAIR_PROMPT.format(errors=exc.errors))
                            )
                            continue
```

9. `agent_complete` payload gains `"output_type": spec.name if spec else None`.
10. `AgentResult(output=final_output, parsed=parsed, ...)`.

`agent_kit/agent/agent.py`:

```python
from typing import TYPE_CHECKING, Any, AsyncIterator, TypeVar, overload
...
T = TypeVar("T")
```

`AgentConfig.__init__` gains `output_retries: int = 2,` → `self.output_retries = output_retries  # repair turns after an invalid typed answer`.
`self.last_result: AgentResult[Any] | None = None`.

```python
    @overload
    async def run(self, prompt: str, *, output_type: type[T], **context: Any) -> AgentResult[T]: ...

    @overload
    async def run(self, prompt: str, *, output_type: None = None, **context: Any) -> AgentResult[Any]: ...

    async def run(self, prompt: str, *, output_type: Any = None, **context: Any) -> AgentResult[Any]:
        """
        Run the agent on a prompt and return the final result.

        With ``output_type`` (any type Pydantic can validate), the final answer is constrained to its
        JSON Schema — natively when the provider supports structured outputs, otherwise via the system
        prompt — and validated into ``result.parsed``. Invalid answers are sent back to the model up to
        ``AgentConfig.output_retries`` times. Context kwargs reach hooks as ``ctx.context``.

        Raises:
            MaxTurnsExceededError: if the agent runs out of turns
            CircuitOpenError: if the provider circuit breaker is OPEN
            ProviderError: if the LLM call fails and retries are exhausted
            OutputValidationError: if a typed answer never validates
        """
        self.last_result = await self._make_loop().run(prompt, output_type=output_type, **context)
        return self.last_result

    async def stream(self, prompt: str, *, output_type: Any = None, **context: Any) -> AsyncIterator[str]:
        """
        ...existing docstring...

        With ``output_type``, chunks are the raw JSON of each answer (including invalid attempts before
        a repair); ``agent.last_result.parsed`` holds the validated value.
        """
        loop = self._make_loop()
        async for chunk in loop.stream(prompt, output_type=output_type, **context):
            yield chunk
        self.last_result = loop.result
```

`_make_loop` passes `output_retries=self._config.output_retries`.

Annotate remaining `AgentResult` uses as `AgentResult[Any]` in `agent_kit/cloud/reporter.py`, `agent_kit/orchestrator/pipeline.py`, `agent_kit/orchestrator/dag.py` as mypy reports them.

- [ ] **Step 4: Run everything**

Run: `python3 -m pytest -q && venv-harness/bin/python -m pytest -q && ruff check agent_kit tests && venv-harness/bin/python -m mypy agent_kit`
Expected: all pass. Also add a mypy reveal check once, not committed: `reveal_type(await Agent(p).run("x", output_type=Weather))` → `AgentResult[Weather]`.

- [ ] **Step 5: Commit**

```bash
git add agent_kit tests/test_typed_results.py
git commit -m "feat: typed results — agent.run(prompt, output_type=Model) returns AgentResult[Model]"
```

---

### Task 4: Live verification against Ollama

**Files:**
- Create (scratchpad, not committed): `e2e-typed/live_ollama.py`

- [ ] **Step 1: Write the live script**

```python
import asyncio
from pydantic import BaseModel
from agent_kit import Agent, AgentConfig, tool
from agent_kit.providers import OllamaProvider


class Weather(BaseModel):
    city: str
    temp_c: int
    summary: str


@tool(description="Current weather for a city")
async def get_weather(city: str) -> dict:
    return {"city": city, "temp_c": 21, "conditions": "sunny"}


async def main() -> None:
    for native in (True, False):
        provider = OllamaProvider(default_model="llama3.2")
        provider.supports_structured_output = native  # type: ignore[misc]
        agent = Agent(provider, tools=[get_weather], config=AgentConfig(max_turns=6))
        result = await agent.run("Use the tool to get the weather in Paris, then report it.", output_type=Weather)
        print("native" if native else "prompt", result.parsed, "turns:", len(result.turns))


asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `venv-harness/bin/python e2e-typed/live_ollama.py`
Expected: two lines, each with a `Weather(city='Paris', temp_c=21, ...)` value; the native run's first provider request carried `response_format`. If a small model fails validation, the repair path is exercised — report the counts either way.

- [ ] **Step 3: Anthropic / OpenAI** — only when `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are set: same script with `AnthropicProvider()` / `OpenAIProvider()`.

---

### Task 5: Docs, example, roadmap

**Files:**
- Create: `examples/typed_output.py`
- Modify: `examples/README.md`, `README.md`, `CHANGELOG.md` (`[Unreleased]`), `specs/06-harness-roadmap.md` (tick 2.2, table row), `specs/13-typed-results.md` (status → implemented), `PROJECT_INDEX.md`

- [ ] **Step 1: Example** — `examples/typed_output.py`

```python
"""Typed results: get a validated Pydantic object back from an agent that uses tools.

    pip install agent-kit
    ANTHROPIC_API_KEY=... python examples/typed_output.py
"""

import asyncio

from pydantic import BaseModel, Field

from agent_kit import Agent, tool
from agent_kit.providers import AnthropicProvider


class LineItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1)
    unit_price_usd: float


class Quote(BaseModel):
    customer: str
    items: list[LineItem]
    total_usd: float


@tool(description="Look up the unit price for a SKU")
async def price_for(sku: str) -> dict:
    return {"sku": sku, "unit_price_usd": {"WIDGET": 4.5, "GADGET": 12.0}.get(sku, 1.0)}


async def main() -> None:
    agent = Agent(AnthropicProvider(), tools=[price_for])
    result = await agent.run(
        "Quote Acme Corp for 10 WIDGET and 2 GADGET. Look up prices.", output_type=Quote
    )
    quote = result.parsed  # Quote, validated
    assert quote is not None
    for item in quote.items:
        print(f"{item.quantity:>3} × {item.sku:<8} ${item.unit_price_usd:.2f}")
    print(f"total ${quote.total_usd:.2f}  (run cost ${result.total_cost_usd:.4f})")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: README** — add a "Typed results" section after the tools section:

```markdown
### Typed results

Pass any type Pydantic can validate — a model, dataclass, `TypedDict`, `list[...]`, enum — and get a
validated value back. Tools, hooks, budgets, and audit all still apply.

```python
class Quote(BaseModel):
    customer: str
    total_usd: float

result = await agent.run("Quote Acme for 10 widgets", output_type=Quote)
result.parsed.total_usd     # validated Quote
```

Anthropic and OpenAI/Ollama constrain the answer with native structured outputs; other providers get
the schema in the system prompt. Invalid answers are sent back to the model with the validation errors
(`AgentConfig(output_retries=2)`), then `OutputValidationError` is raised.
```

- [ ] **Step 3: Remaining docs** — `examples/README.md` row for `typed_output.py`; CHANGELOG `[Unreleased]` → Added: typed results (`output_type`, `AgentResult[T].parsed`, `OutputValidationError`, `output_retries`, provider `output_schema`); Changed: `anthropic>=1.0`, `openai>=1.40` floors; roadmap 2.2 `[x]` with `design: specs/13-typed-results.md` and table row `Typed / structured output | ✅ (2.2)`; spec status `implemented`; PROJECT_INDEX entries for `agent_kit/output.py`, `tests/test_output.py`, `tests/test_typed_results.py`, `examples/typed_output.py`.

- [ ] **Step 4: Gates** — `python3 -m pytest -q`, `venv-harness/bin/python -m pytest -q`, `ruff check agent_kit tests examples`, `mypy agent_kit`, `python3 -m py_compile examples/*.py`.

- [ ] **Step 5: Commit, fast-forward, push**

```bash
git add examples README.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: typed results guide and example"
git checkout main && git merge --ff-only typed-results && git push origin main
git branch -d typed-results
```

Then watch CI (`gh run watch`) until all jobs are green.
