# Context Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provider-native content (thinking, compaction) round-trips verbatim; prompt caching is on by default; thinking/effort/provider options are configurable; Anthropic compaction and tool-result clearing are opt-in; the message window is replaced by a cache-friendly token budget.

**Architecture:** `Message` carries `native_content`/`native_provider`; a `RequestOptions` value object in `types.py` travels from `AgentConfig` through `AgentLoop` to providers that declare `supports_request_options`. The Anthropic provider builds requests through one helper shared by `complete`/`stream`, switching to the beta client when betas are needed, and reports compaction/context edits as `Turn.context_events`. A new `agent_kit/memory/budget.py` plans trims that stores apply with `trim_oldest`.

**Tech Stack:** Python 3.11+, Pydantic v2, anthropic>=1.0 (beta client), openai>=1.40, sqlite3, pytest-asyncio, mypy strict, ruff.

**Spec:** `specs/14-context-management.md`

## Global Constraints

- `agent_kit/types.py` imports nothing from `agent_kit`.
- Native content is sent back byte-for-byte: never mutate `native_content` lists or dicts.
- `options=None` on a provider adds nothing to the request; `AgentLoop` always passes a `RequestOptions` to providers with `supports_request_options = True`.
- Beta headers: `compact-2026-01-12`, `context-management-2025-06-27`; `betas` sorted and unique.
- Defaults: `prompt_caching=True`, `context_budget_tokens=150_000`, `memory_window=None`, store `window=None`, `Compaction.trigger_tokens=150_000`, `ClearToolResults(trigger_tokens=100_000, keep=3)`, default `tokens_per_char=0.25`, trim target `budget // 2`.
- Audit payloads never contain message content.
- Gates per task: `python3 -m pytest -q`, `$V/python -m pytest -q` (V = scratchpad `venv-harness/bin`), `ruff check agent_kit tests`, `$V/python -m mypy agent_kit`.

---

### Task 1: Types and memory stores

**Files:**
- Modify: `agent_kit/types.py`, `agent_kit/memory/in_memory.py`, `agent_kit/memory/sqlite.py`, `agent_kit/__init__.py`
- Test: `tests/test_memory_window.py`, `tests/test_sqlite_memory.py`

**Interfaces:**
- Produces: `Message.native_content: list[dict[str, Any]] | None`, `Message.native_provider: str | None`, `Turn.context_events: list[dict[str, Any]]`, `Compaction`, `ClearToolResults`, `RequestOptions` (+ `.is_default()`), `InMemoryStore(window: int | None = None)`, `SQLiteMemory(path, window: int | None = None)`, `trim_oldest(count: int) -> int` on both.

- [ ] **Step 1: Failing tests** — append to `tests/test_memory_window.py`:

```python
def _history(n: int) -> list[Message]:
    msgs: list[Message] = []
    for i in range(n):
        msgs.append(Message(role="user", content=f"q{i}"))
        msgs.extend(_exchange(i))
        msgs.append(Message(role="assistant", content=f"a{i}"))
    return msgs


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_stores_default_to_no_window(make, tmp_path):
    mem = make(tmp_path)
    mem.add_many(_history(30))
    assert len(mem.history()) == 120


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_trim_oldest_cuts_at_a_user_turn(make, tmp_path):
    mem = make(tmp_path)
    mem.add(Message(role="system", content="sys"))
    mem.add_many(_history(3))  # q0 call0 tool0 a0 | q1 ... | q2 ...

    removed = mem.trim_oldest(2)  # would start mid-exchange; extends to the next user turn

    assert removed == 4
    history = mem.history()
    assert [m.content for m in history[:2]] == ["sys", "q1"]
    assert mem.trim_oldest(0) == 0


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_trim_oldest_keeps_latest_user_turn_and_tool_pairs(make, tmp_path):
    mem = make(tmp_path)
    mem.add(Message(role="user", content="go"))
    for i in range(4):
        mem.add_many(_exchange(i))

    removed = mem.trim_oldest(6)

    history = mem.history()
    assert history[0].content == "go"
    assert removed == 6
    assert [m.role for m in history[1:]] == ["assistant", "tool"]
    assert history[1].tool_calls[0].call_id == history[2].tool_call_id
```

(add `import pytest` at the top if missing). Append to `tests/test_sqlite_memory.py`:

```python
def test_sqlite_persists_native_content(tmp_path):
    native = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi"}]
    path = tmp_path / "n.db"
    SQLiteMemory(path).add(
        Message(role="assistant", content="hi", native_content=native, native_provider="anthropic")
    )
    restored = SQLiteMemory(path).history()[0]
    assert restored.native_content == native
    assert restored.native_provider == "anthropic"
    SQLiteMemory(path).add(Message(role="user", content="plain"))
    assert SQLiteMemory(path).history()[1].native_content is None


def test_sqlite_migrates_database_without_native_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
        "content TEXT NOT NULL, tool_call_id TEXT, metadata TEXT NOT NULL DEFAULT '{}', "
        "tool_calls TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO messages (role, content) VALUES ('user', 'hello')")
    conn.commit()
    conn.close()

    mem = SQLiteMemory(path)
    assert mem.history()[0].native_content is None
    mem.add(Message(role="assistant", content="x", native_content=[{"type": "text", "text": "x"}], native_provider="anthropic"))
    assert mem.history()[1].native_content == [{"type": "text", "text": "x"}]


def test_request_options_defaults():
    from agent_kit import ClearToolResults, Compaction
    from agent_kit.types import RequestOptions

    assert RequestOptions().is_default()
    assert RequestOptions().prompt_caching is True
    assert not RequestOptions(effort="high").is_default()
    assert Compaction().trigger_tokens == 150_000
    assert (ClearToolResults().trigger_tokens, ClearToolResults().keep) == (100_000, 3)
```

- [ ] **Step 2: Run** — `python3 -m pytest tests/test_memory_window.py tests/test_sqlite_memory.py -q` → FAIL (`AttributeError: trim_oldest`, unexpected `native_content`, import error).

- [ ] **Step 3: Implement**

`agent_kit/types.py` — `Message` gains after `metadata`:

```python
    native_content: list[dict[str, Any]] | None = None  # assistant blocks exactly as the provider returned them
    native_provider: str | None = None  # provider.name() that produced native_content
```

`Turn` gains `context_events: list[dict[str, Any]] = Field(default_factory=list)  # server-side context management`.

After the reliability configs:

```python
class Compaction(BaseModel):
    """Anthropic server-side compaction: summarise earlier context past a token threshold."""

    trigger_tokens: int = 150_000  # API minimum 50_000
    instructions: str | None = None  # replaces the default summarisation prompt


class ClearToolResults(BaseModel):
    """Anthropic context editing: clear old tool results past a token threshold."""

    trigger_tokens: int = 100_000
    keep: int = 3  # most recent tool uses kept
    exclude_tools: list[str] = Field(default_factory=list)
    clear_inputs: bool = False  # also clear tool_use inputs


class RequestOptions(BaseModel):
    """Per-request model settings AgentLoop passes to providers that support them."""

    thinking: Literal["adaptive", "disabled"] | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt_caching: bool = True
    compaction: Compaction | None = None
    clear_tool_results: ClearToolResults | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)

    def is_default(self) -> bool:
        return self == RequestOptions()
```

`agent_kit/__init__.py`: `from agent_kit.types import AgentResult, ClearToolResults, Compaction, Message, ToolResult, Turn` and add both names to `__all__` under core primitives.

`agent_kit/memory/in_memory.py`:

```python
    def __init__(self, window: int | None = None) -> None:
        self._window = window
        self._messages: list[Message] = []
    ...
    def trim_oldest(self, count: int) -> int:
        """Remove at least ``count`` oldest non-system messages at a tool-safe boundary; return how many."""
        if count <= 0:
            return 0
        system = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]
        kept = window_indices([m.role for m in non_system], max(0, len(non_system) - count))
        self._messages = system + [non_system[i] for i in kept]
        return len(non_system) - len(kept)

    def _trim(self) -> None:
        if self._window is None or len(self._messages) <= self._window:
            return
        ...  # unchanged
```

Update the class docstring usage (`InMemoryStore()  # no count cap; InMemoryStore(window=50) caps messages`).

`agent_kit/memory/sqlite.py`:
- `__init__(self, path=":memory:", window: int | None = None)`.
- Schema: `native TEXT` column in `CREATE TABLE`; after the `tool_calls` migration: `if "native" not in columns: ALTER TABLE messages ADD COLUMN native TEXT`.
- `_INSERT` adds `native`; `_row` returns a 6-tuple with `json.dumps({"provider": m.native_provider, "content": m.native_content}) if m.native_content is not None else None`.
- `history()` selects `native`; builds `native = json.loads(row["native"]) if row["native"] else None` then `native_content=native["content"] if native else None, native_provider=native["provider"] if native else None`.
- `_trim`: return early when `self._window is None`.
- `trim_oldest`:

```python
    def trim_oldest(self, count: int) -> int:
        """Remove at least ``count`` oldest non-system messages at a tool-safe boundary; return how many."""
        if count <= 0:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role FROM messages WHERE role != 'system' ORDER BY id"
            ).fetchall()
            kept = set(window_indices([row[1] for row in rows], max(0, len(rows) - count)))
            drop_ids = [row[0] for i, row in enumerate(rows) if i not in kept]
            if drop_ids:
                placeholders = ",".join("?" * len(drop_ids))
                with self._conn:
                    self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", drop_ids)
            return len(drop_ids)
```

- [ ] **Step 4: Gates** — all four commands pass.
- [ ] **Step 5: Commit** — `feat: native message content, request options, token-trim store API`.

---

### Task 2: Budget planning — `agent_kit/memory/budget.py`

**Files:**
- Create: `agent_kit/memory/budget.py`
- Test: `tests/test_context_budget.py`

**Interfaces:**
- Produces: `DEFAULT_TOKENS_PER_CHAR = 0.25`, `message_chars(m: Message) -> int`, `prompt_chars(system: str, tools: list[ToolSchema], messages: list[Message]) -> int`, `plan_trim(messages, chars, budget_tokens, tokens_per_char) -> tuple[int, int, int]`.

- [ ] **Step 1: Failing tests** — `tests/test_context_budget.py`:

```python
"""Token budget planning and context management through the loop."""

from __future__ import annotations

from agent_kit.memory.budget import message_chars, plan_trim, prompt_chars
from agent_kit.types import Message, ToolCall, ToolSchema


def test_message_chars_counts_content_native_and_arguments():
    m = Message(
        role="assistant",
        content="abc",
        tool_calls=[ToolCall(tool_name="t", arguments={"k": "v"}, call_id="1")],
        native_content=[{"type": "text", "text": "abc"}],
        native_provider="anthropic",
    )
    assert message_chars(m) == 3 + len('{"k": "v"}') + len('[{"type": "text", "text": "abc"}]')


def test_prompt_chars_includes_system_and_tools():
    tool = ToolSchema(name="get", description="desc", parameters={"type": "object"})
    msgs = [Message(role="user", content="hello")]
    assert prompt_chars("sys", [tool], msgs) == 3 + len("get") + len("desc") + len('{"type": "object"}') + 5


def test_plan_trim_under_budget():
    msgs = [Message(role="user", content="x" * 100)]
    assert plan_trim(msgs, 100, budget_tokens=50, tokens_per_char=0.25) == (0, 25, 25)


def test_plan_trim_cuts_to_half_budget():
    msgs = [Message(role="user", content="x" * 400) for _ in range(10)]  # 4000 chars = 1000 tokens
    dropped, before, after = plan_trim(msgs, 4000, budget_tokens=600, tokens_per_char=0.25)
    assert (dropped, before, after) == (7, 1000, 300)


def test_plan_trim_keeps_last_message():
    msgs = [Message(role="user", content="x" * 4000), Message(role="user", content="y" * 4000)]
    assert plan_trim(msgs, 8000, budget_tokens=10, tokens_per_char=0.25) == (1, 2000, 1000)
```

- [ ] **Step 2: Run** → `ModuleNotFoundError: agent_kit.memory.budget`.

- [ ] **Step 3: Implement**

```python
"""Token-budget trimming: estimate prompt size and plan how many oldest messages to drop."""

from __future__ import annotations

import json
import math

from agent_kit.types import Message, ToolSchema

DEFAULT_TOKENS_PER_CHAR = 0.25  # used until a provider has reported real prompt tokens


def message_chars(m: Message) -> int:
    chars = len(m.content)
    if m.native_content:
        chars += len(json.dumps(m.native_content, default=str))
    for tc in m.tool_calls:
        chars += len(json.dumps(tc.arguments, default=str))
    return chars


def prompt_chars(system: str, tools: list[ToolSchema], messages: list[Message]) -> int:
    tool_chars = sum(len(t.name) + len(t.description) + len(json.dumps(t.parameters)) for t in tools)
    return len(system) + tool_chars + sum(message_chars(m) for m in messages)


def plan_trim(
    messages: list[Message], chars: int, budget_tokens: int, tokens_per_char: float
) -> tuple[int, int, int]:
    """
    Return (messages_to_drop, estimated_tokens_before, estimated_tokens_after).

    Nothing is dropped under budget. Over budget, drop oldest messages until the estimate is at most
    half the budget — one cut, so the prompt prefix stays stable (and cached) until the next one.
    The last message is never dropped.
    """
    before = math.ceil(chars * tokens_per_char)
    if before <= budget_tokens:
        return 0, before, before
    target = budget_tokens // 2
    remaining, dropped = chars, 0
    while dropped < len(messages) - 1 and math.ceil(remaining * tokens_per_char) > target:
        remaining -= message_chars(messages[dropped])
        dropped += 1
    return dropped, before, math.ceil(remaining * tokens_per_char)
```

- [ ] **Step 4: Gates.** — [ ] **Step 5: Commit** — `feat: token budget trim planning`.

---

### Task 3: Anthropic provider — native content, request options, iterations, context events

**Files:**
- Modify: `agent_kit/providers/anthropic.py`, `agent_kit/providers/base.py`
- Test: `tests/test_provider_requests.py`

**Interfaces:**
- Consumes: `RequestOptions`, `Compaction`, `ClearToolResults`, `Message.native_*`, `Turn.context_events` (Task 1).
- Produces: `AnthropicProvider.supports_request_options = True`; `complete/stream(..., options: RequestOptions | None = None)`; `BaseProvider` protocol gains the `options` parameter.

- [ ] **Step 1: Failing tests** (append; add `from agent_kit.types import ClearToolResults, Compaction, RequestOptions`)

```python
# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------


def thinking_block(signature: str = "sig-1") -> NS:
    return NS(type="thinking", thinking="", signature=signature)


class FakeAnthropicWithBeta(FakeAnthropic):
    def __init__(self, responses: list[NS]) -> None:
        super().__init__(responses)
        self.beta_calls: list[dict[str, Any]] = []
        self.beta = NS(messages=NS(create=self._beta_create))

    async def _beta_create(self, **kwargs: Any) -> NS:
        self.beta_calls.append(kwargs)
        return self._responses.pop(0)


async def test_anthropic_thinking_blocks_round_trip_verbatim():
    agent, fake = anthropic_agent(
        [
            anthropic_response(
                [thinking_block(), tool_use_block("toolu_1", "get_weather", {"city": "Paris"})], "tool_use"
            ),
            anthropic_response([thinking_block("sig-2"), text_block("21C")]),
        ],
        tools=[get_weather],
    )

    await agent.run("weather?")

    assert fake.calls[1]["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
    }
    assert agent.memory.history()[-1].native_content == [
        {"type": "thinking", "thinking": "", "signature": "sig-2"},
        {"type": "text", "text": "21C"},
    ]


async def test_anthropic_agent_runs_cache_by_default():
    provider = AnthropicProvider(api_key="test")
    fake = FakeAnthropic([anthropic_response([text_block("hi")]), anthropic_response([text_block("hi")])])
    provider._client = fake  # type: ignore[assignment]
    from agent_kit import AgentConfig

    await Agent(provider, config=AgentConfig(system_prompt="Be brief.")).run("hi")
    await Agent(provider, config=AgentConfig(system_prompt="Be brief.", prompt_caching=False)).run("hi")

    assert fake.calls[0]["system"] == [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    assert fake.calls[0]["cache_control"] == {"type": "ephemeral"}
    assert fake.calls[1]["system"] == "Be brief."
    assert "cache_control" not in fake.calls[1]


async def test_anthropic_direct_call_without_options_adds_nothing():
    provider = AnthropicProvider(api_key="test")
    fake = FakeAnthropic([anthropic_response([text_block("hi")])])
    provider._client = fake  # type: ignore[assignment]
    await provider.complete(ASK, system="s")
    assert fake.calls[0]["system"] == "s" and "cache_control" not in fake.calls[0]


async def test_anthropic_thinking_effort_and_output_schema_share_output_config():
    provider = AnthropicProvider(api_key="test")
    fake = FakeAnthropic([anthropic_response([text_block("{}")])])
    provider._client = fake  # type: ignore[assignment]
    options = RequestOptions(
        thinking="adaptive",
        effort="high",
        prompt_caching=False,
        provider_options={"thinking": {"display": "summarized"}, "metadata": {"user_id": "u1"}},
    )

    await provider.complete(ASK, output_schema=WEATHER, options=options)

    call = fake.calls[0]
    assert provider.supports_request_options is True
    assert call["thinking"] == {"display": "summarized", "type": "adaptive"}
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": WEATHER.json_schema}}
    assert call["metadata"] == {"user_id": "u1"}


async def test_anthropic_context_management_uses_beta_client():
    provider = AnthropicProvider(api_key="test")
    response = anthropic_response([NS(type="compaction", content="summary"), text_block("ok")])
    response.usage.iterations = [
        NS(type="compaction", input_tokens=180_000, output_tokens=3_500),
        NS(type="message", input_tokens=23_000, output_tokens=1_000),
    ]
    response.context_management = NS(
        applied_edits=[NS(type="clear_tool_uses_20250919", cleared_tool_uses=4, cleared_input_tokens=51_000)]
    )
    fake = FakeAnthropicWithBeta([response])
    provider._client = fake  # type: ignore[assignment]
    options = RequestOptions(
        prompt_caching=False,
        compaction=Compaction(trigger_tokens=120_000, instructions="Keep decisions."),
        clear_tool_results=ClearToolResults(keep=2, exclude_tools=["search"], clear_inputs=True),
        provider_options={"betas": ["extra-beta"]},
    )

    turn = await provider.complete(ASK, model="claude-opus-5", options=options)

    assert fake.calls == []
    call = fake.beta_calls[0]
    assert call["betas"] == ["compact-2026-01-12", "context-management-2025-06-27", "extra-beta"]
    assert call["context_management"] == {
        "edits": [
            {
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": 100_000},
                "keep": {"type": "tool_uses", "value": 2},
                "exclude_tools": ["search"],
                "clear_tool_inputs": True,
            },
            {
                "type": "compact_20260112",
                "trigger": {"type": "input_tokens", "value": 120_000},
                "instructions": "Keep decisions.",
            },
        ]
    }
    assert (turn.cost.input_tokens, turn.cost.output_tokens) == (203_000, 4_500)
    assert turn.cost.cost_usd == pytest.approx((203_000 * 5 + 4_500 * 25) / 1_000_000)
    assert turn.context_events == [
        {"type": "compaction", "input_tokens": 180_000, "output_tokens": 3_500},
        {"type": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51_000},
    ]
    assert turn.message_out is not None
    assert turn.message_out.native_content == [{"type": "compaction", "content": "summary"}, {"type": "text", "text": "ok"}]


async def test_anthropic_stream_beta_and_native_content():
    provider = AnthropicProvider(api_key="test")
    calls: list[dict[str, Any]] = []

    def beta_stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return FakeAnthropicStream(["ok"], anthropic_response([thinking_block(), text_block("ok")]))

    provider._client = NS(beta=NS(messages=NS(stream=beta_stream)))  # type: ignore[assignment]
    items = [i async for i in provider.stream(ASK, options=RequestOptions(compaction=Compaction()))]

    assert calls[0]["betas"] == ["compact-2026-01-12"]
    assert items[-1].message_out.native_content[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}


@requires_openai
def test_anthropic_native_assistant_message_renders_portably_for_openai():
    from agent_kit.providers.openai import _messages_to_openai

    msg = Message(
        role="assistant",
        content="21C",
        native_content=[{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "21C"}],
        native_provider="anthropic",
    )
    assert _messages_to_openai([msg]) == [{"role": "assistant", "content": "21C"}]
```

- [ ] **Step 2: Run** (`$V/python -m pytest tests/test_provider_requests.py -q`) → new tests FAIL.

- [ ] **Step 3: Implement** in `agent_kit/providers/anthropic.py`:

```python
_COMPACTION_BETA = "compact-2026-01-12"
_CONTEXT_EDITING_BETA = "context-management-2025-06-27"


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _plain(value: Any) -> Any:
    """SDK content blocks (or test doubles) as JSON-ready dicts, dropping unset fields."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if v is not None}
    return value
```

In `_messages_to_anthropic`, insert before the `elif msg.role == "assistant" and msg.tool_calls:` branch:

```python
        elif msg.role == "assistant" and msg.native_provider == "anthropic" and msg.native_content:
            result.append({"role": "assistant", "content": list(msg.native_content)})  # verbatim
```

Request options:

```python
def _merge(call_kwargs: dict[str, Any], key: str, values: dict[str, Any]) -> None:
    call_kwargs[key] = {**call_kwargs.get(key, {}), **values}


def _apply_options(call_kwargs: dict[str, Any], options: RequestOptions | None) -> list[str]:
    """Apply thinking, effort, caching, context management, and passthrough; return the betas needed."""
    if options is None:
        return []
    extra = dict(options.provider_options)
    betas = set(extra.pop("betas", None) or [])
    for key in ("output_config", "thinking"):
        if key in extra:
            _merge(call_kwargs, key, extra.pop(key))
    call_kwargs.update(extra)
    if options.thinking:
        _merge(call_kwargs, "thinking", {"type": options.thinking})
    if options.effort:
        _merge(call_kwargs, "output_config", {"effort": options.effort})
    if options.prompt_caching:
        system = call_kwargs.get("system")
        if isinstance(system, str) and system:
            call_kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        call_kwargs["cache_control"] = {"type": "ephemeral"}
    edits: list[dict[str, Any]] = []
    if options.clear_tool_results:
        ctr = options.clear_tool_results
        edit: dict[str, Any] = {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": ctr.trigger_tokens},
            "keep": {"type": "tool_uses", "value": ctr.keep},
        }
        if ctr.exclude_tools:
            edit["exclude_tools"] = list(ctr.exclude_tools)
        if ctr.clear_inputs:
            edit["clear_tool_inputs"] = True
        edits.append(edit)
        betas.add(_CONTEXT_EDITING_BETA)
    if options.compaction:
        compact: dict[str, Any] = {
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": options.compaction.trigger_tokens},
        }
        if options.compaction.instructions:
            compact["instructions"] = options.compaction.instructions
        edits.append(compact)
        betas.add(_COMPACTION_BETA)
    if edits:
        call_kwargs["context_management"] = {"edits": edits}
    return sorted(betas)
```

`_turn_from_response` — build native content, iteration-summed usage, context events:

```python
    iterations = _get(response.usage, "iterations") or []
    parts = iterations or [response.usage]

    def total(field: str) -> int:
        return sum(int(_get(p, field) or 0) for p in parts)

    input_tokens, output_tokens = total("input_tokens"), total("output_tokens")
    cache_read, cache_write = total("cache_read_input_tokens"), total("cache_creation_input_tokens")
    context_events: list[dict[str, Any]] = [
        {"type": "compaction", "input_tokens": int(_get(it, "input_tokens") or 0),
         "output_tokens": int(_get(it, "output_tokens") or 0)}
        for it in iterations
        if _get(it, "type") == "compaction"
    ]
    context_management = getattr(response, "context_management", None)
    context_events.extend(_plain(edit) for edit in (_get(context_management, "applied_edits") or []))
```

and `Message(..., native_content=[_plain(b) for b in response.content], native_provider="anthropic")`, `Turn(..., context_events=context_events)`, `CostSummary` from the totals.

One request builder shared by both methods:

```python
    def _request(
        self,
        messages: list[Message],
        model: str | None,
        tools: list[ToolSchema] | None,
        system: str | None,
        max_tokens: int,
        output_schema: OutputSpec[Any] | None,
        options: RequestOptions | None,
        kwargs: dict[str, Any],
    ) -> tuple[str, dict[str, Any], list[str]]:
        resolved_model = model or self.config.default_model
        sys_from_messages, converted = _messages_to_anthropic(messages)
        resolved_system = system or sys_from_messages
        call_kwargs: dict[str, Any] = {"model": resolved_model, "messages": converted, "max_tokens": max_tokens, **kwargs}
        if resolved_system:
            call_kwargs["system"] = resolved_system
        if tools:
            call_kwargs["tools"] = _to_anthropic_tools(tools)
        betas = _apply_options(call_kwargs, options)
        _apply_output_schema(call_kwargs, output_schema)
        return resolved_model, call_kwargs, betas
```

`complete`: `response = await (self._client.beta.messages.create(betas=betas, **call_kwargs) if betas else self._client.messages.create(**call_kwargs))`. `stream`: `manager = self._client.beta.messages.stream(betas=betas, **call_kwargs) if betas else self._client.messages.stream(**call_kwargs)` then `async with manager as stream:`. Class attribute `supports_request_options = True`. Both methods gain `options: RequestOptions | None = None` after `output_schema`.

`agent_kit/providers/base.py`: `TYPE_CHECKING` import `RequestOptions` from `agent_kit.types` (a runtime import is fine — types has no internal imports), add `options: RequestOptions | None = None` to both protocol methods, and docstring sentence: "Providers that accept ``RequestOptions`` set ``supports_request_options = True``; ``options=None`` must add nothing to the request."

- [ ] **Step 4: Gates** (existing Anthropic tests still pass: their responses' native content has the same shape they assert).
- [ ] **Step 5: Commit** — `feat: Anthropic native content round-trip, caching, reasoning, compaction and context editing`.

---

### Task 4: OpenAI provider — request options

**Files:**
- Modify: `agent_kit/providers/openai.py`
- Test: `tests/test_provider_requests.py`

- [ ] **Step 1: Failing test**

```python
@requires_openai
async def test_openai_request_options():
    from agent_kit.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test")
    fake = FakeOpenAI([openai_response("a"), openai_response("b"), openai_response("c")])
    provider._client = fake  # type: ignore[assignment]

    await provider.complete(ASK, options=RequestOptions(effort="max", compaction=Compaction(), provider_options={"seed": 7}))
    await provider.complete(ASK, options=RequestOptions(effort="medium", thinking="adaptive"))
    await provider.complete(ASK)

    assert provider.supports_request_options is True
    assert (fake.calls[0]["reasoning_effort"], fake.calls[0]["seed"]) == ("high", 7)
    assert "context_management" not in fake.calls[0] and "cache_control" not in fake.calls[0]
    assert fake.calls[1]["reasoning_effort"] == "medium" and "thinking" not in fake.calls[1]
    assert "reasoning_effort" not in fake.calls[2]
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**

```python
_REASONING_EFFORT = {"xhigh": "high", "max": "high"}


def _apply_options(call_kwargs: dict[str, Any], options: RequestOptions | None) -> None:
    """Passthrough and effort; thinking, caching, and context management don't apply to this API."""
    if options is None:
        return
    call_kwargs.update(options.provider_options)
    if options.effort:
        call_kwargs["reasoning_effort"] = _REASONING_EFFORT.get(options.effort, options.effort)
```

`supports_request_options = True`; `options: RequestOptions | None = None` on `complete`/`stream`; call `_apply_options(call_kwargs, options)` before `_apply_response_format`.

- [ ] **Step 4: Gates.** — [ ] **Step 5: Commit** — `feat: OpenAI request options (reasoning_effort, passthrough)`.

---

### Task 5: Loop and AgentConfig

**Files:**
- Modify: `agent_kit/agent/loop.py`, `agent_kit/agent/agent.py`
- Test: `tests/test_context_budget.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `AgentConfig(thinking, effort, prompt_caching, compaction, clear_tool_results, provider_options, context_budget_tokens=150_000, memory_window=None)`; `AgentLoop(..., request_options: RequestOptions | None = None, context_budget_tokens: int | None = None)`; audit `context_trimmed`, `context_compacted`, `context_edited`.

- [ ] **Step 1: Failing tests** (append to `tests/test_context_budget.py`)

```python
import logging
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, Compaction, tool
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, RequestOptions, Turn


class Scripted:
    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn, options: bool = True) -> None:
        self.turns = list(turns)
        self.requests: list[tuple[list[Message], dict[str, Any]]] = []
        if options:
            self.supports_request_options = True

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append((list(messages), kw))
        return self.turns.pop(0)

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.requests.append((list(messages), kw))
        yield self.turns.pop(0)


def final(text: str = "done", input_tokens: int = 0, events: list[dict[str, Any]] | None = None) -> Turn:
    return Turn(
        message_out=Message(role="assistant", content=text),
        cost=CostSummary(input_tokens=input_tokens),
        context_events=events or [],
    )


def call(input_tokens: int = 0) -> Turn:
    tc = [ToolCall(tool_name="lookup", arguments={}, call_id="c1")]
    return Turn(
        message_out=Message(role="assistant", content="", tool_calls=tc), tool_calls=tc,
        cost=CostSummary(input_tokens=input_tokens),
    )


@tool(description="look something up")
async def lookup() -> dict[str, Any]:
    return {"ok": True}


def audited(agent: Agent) -> list[tuple[str, dict[str, Any]]]:
    assert agent.audit is not None
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return recorded


def seeded(agent: Agent, exchanges: int) -> None:
    for i in range(exchanges):
        agent.memory.add(Message(role="user", content=f"q{i}" + "x" * 398))
        agent.memory.add(Message(role="assistant", content=f"a{i}" + "y" * 398))


async def test_options_reach_providers_that_support_them():
    provider = Scripted(final())
    config = AgentConfig(effort="high", thinking="adaptive", provider_options={"seed": 1})
    await Agent(provider, config=config).run("hi")
    assert provider.requests[0][1]["options"] == RequestOptions(
        effort="high", thinking="adaptive", provider_options={"seed": 1}
    )


async def test_provider_without_options_gets_none_and_one_warning(caplog):
    provider = Scripted(final(), final(), options=False)
    agent = Agent(provider, config=AgentConfig(effort="high"))
    with caplog.at_level(logging.WARNING):
        await agent.run("hi")
    assert "options" not in provider.requests[0][1]
    assert sum("does not accept request options" in r.message for r in caplog.records) == 1

    quiet = Scripted(final(), options=False)
    caplog.clear()
    await Agent(quiet).run("hi")
    assert not any("does not accept request options" in r.message for r in caplog.records)


async def test_history_over_budget_is_cut_once_to_half():
    provider = Scripted(call(input_tokens=700), final(input_tokens=720))
    agent = Agent(provider, tools=[lookup], config=AgentConfig(context_budget_tokens=2_000))
    seeded(agent, 10)  # 20 messages × 400 chars ≈ 2_000 tokens at 0.25
    recorded = audited(agent)

    await agent.run("latest question")

    first, second = provider.requests[0][0], provider.requests[1][0]
    trims = [p for e, p in recorded if e == "context_trimmed"]
    assert len(trims) == 1
    assert trims[0]["turn"] == 1 and trims[0]["budget_tokens"] == 2_000
    assert trims[0]["estimated_tokens_before"] > 2_000 >= 2 * trims[0]["estimated_tokens_after"] - 100
    assert first[0].role == "user" and first[-1].content == "latest question"
    assert second[: len(first)] == first  # prefix unchanged between cuts


async def test_trim_never_splits_tool_pairs_and_keeps_latest_user():
    provider = Scripted(final())
    agent = Agent(provider, config=AgentConfig(context_budget_tokens=50))
    agent.memory.add(Message(role="user", content="u" * 400))
    for i in range(5):
        tc = [ToolCall(tool_name="lookup", arguments={}, call_id=f"c{i}")]
        agent.memory.add(Message(role="assistant", content="", tool_calls=tc))
        agent.memory.add(Message(role="tool", content="r" * 400, tool_call_id=f"c{i}"))

    await agent.run("now")

    sent = provider.requests[0][0]
    assert sent[-1].content == "now"
    for i, m in enumerate(sent):
        if m.role == "tool":
            assert sent[i - 1].tool_calls and sent[i - 1].tool_calls[0].call_id == m.tool_call_id


@pytest.mark.parametrize("config", [AgentConfig(context_budget_tokens=None), AgentConfig(context_budget_tokens=100, compaction=Compaction())])
async def test_no_client_trimming_when_disabled_or_compacting(config):
    provider = Scripted(final())
    agent = Agent(provider, config=config)
    seeded(agent, 10)
    await agent.run("q")
    assert len(provider.requests[0][0]) == 21


async def test_context_events_are_audited():
    events = [
        {"type": "compaction", "input_tokens": 180_000, "output_tokens": 3_500},
        {"type": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51_000},
    ]
    agent = Agent(Scripted(final(events=events)))
    recorded = audited(agent)

    await agent.run("hi")

    assert [p for e, p in recorded if e == "context_compacted"] == [
        {"turn": 1, "input_tokens": 180_000, "output_tokens": 3_500}
    ]
    assert [p for e, p in recorded if e == "context_edited"] == [
        {"turn": 1, "edit": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51_000}
    ]


def test_agent_config_defaults():
    config = AgentConfig()
    assert (config.context_budget_tokens, config.memory_window, config.prompt_caching) == (150_000, None, True)
```

- [ ] **Step 2: Run** → FAIL (`AgentConfig` unexpected kwargs).

- [ ] **Step 3: Implement**

`agent_kit/agent/agent.py` — `AgentConfig.__init__` gains the parameters and attributes:

```python
        memory_window: int | None = None,
        ...
        thinking: Literal["adaptive", "disabled"] | None = None,
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
        prompt_caching: bool = True,
        compaction: Compaction | None = None,
        clear_tool_results: ClearToolResults | None = None,
        provider_options: dict[str, Any] | None = None,
        context_budget_tokens: int | None = 150_000,
```

```python
        self.memory_window = memory_window  # message cap applied on append; None = token budget only
        self.thinking = thinking  # Anthropic thinking type
        self.effort = effort  # Anthropic output_config.effort / OpenAI reasoning_effort
        self.prompt_caching = prompt_caching  # Anthropic cache breakpoints (system + conversation)
        self.compaction = compaction  # Anthropic server-side compaction; disables client trimming
        self.clear_tool_results = clear_tool_results  # Anthropic server-side tool-result clearing
        self.provider_options = provider_options or {}  # merged into every provider request
        self.context_budget_tokens = context_budget_tokens  # trim history in one cut to half when exceeded
```

`_make_loop` passes:

```python
            request_options=RequestOptions(
                thinking=self._config.thinking,
                effort=self._config.effort,
                prompt_caching=self._config.prompt_caching,
                compaction=self._config.compaction,
                clear_tool_results=self._config.clear_tool_results,
                provider_options=dict(self._config.provider_options),
            ),
            context_budget_tokens=self._config.context_budget_tokens,
```

`agent_kit/agent/loop.py`:

1. Imports: `import logging`, `import math`; `from agent_kit.memory.budget import DEFAULT_TOKENS_PER_CHAR, plan_trim, prompt_chars`; add `RequestOptions` to the `agent_kit.types` import; `logger = logging.getLogger(__name__)`.
2. `__init__` gains `request_options: RequestOptions | None = None, context_budget_tokens: int | None = None` → `self._request_options = request_options or RequestOptions()`, `self._context_budget_tokens = context_budget_tokens`, `self._last_prompt_tokens = 0`, `self._last_prompt_chars = 0`.
3. In `_execute`, after `output_kwargs` is built:

```python
        options_kwargs: dict[str, Any] = {}
        if getattr(self._provider, "supports_request_options", False):
            options_kwargs["options"] = self._request_options
        elif not self._request_options.is_default():
            logger.warning(
                "%s does not accept request options; thinking/effort/caching/context settings are ignored",
                self._provider.name(),
            )
```

4. Loop head becomes:

```python
                    turn_count += 1
                    await self._enforce_budgets()
                    tool_schemas = self._registry.schemas()
                    self._trim_context(turn_count, system, tool_schemas)
                    messages = self._memory.history(include_system=False)
                    self._last_prompt_chars = prompt_chars(system, tool_schemas, messages)
                    await self._gate_llm(turn_count, len(messages))
```

   (remove the later `tool_schemas = self._registry.schemas()` line).
5. Both provider calls pass `**output_kwargs, **options_kwargs` (stream: `_open_stream(..., system, {**output_kwargs, **options_kwargs})`).
6. After the `llm_complete` audit block:

```python
                    self._last_prompt_tokens = (
                        turn.cost.input_tokens + turn.cost.cache_read_tokens + turn.cost.cache_write_tokens
                    )
                    self._audit_context_events(turn_count, turn)
```

7. New methods:

```python
    def _trim_context(self, turn: int, system: str, tools: list[ToolSchema]) -> None:
        """Cut history once to half the token budget when the next request would exceed it."""
        budget = self._context_budget_tokens
        if budget is None or self._request_options.compaction is not None:
            return
        messages = self._memory.history(include_system=False)
        ratio = (
            self._last_prompt_tokens / self._last_prompt_chars
            if self._last_prompt_tokens and self._last_prompt_chars
            else DEFAULT_TOKENS_PER_CHAR
        )
        drop, before, _ = plan_trim(messages, prompt_chars(system, tools, messages), budget, ratio)
        if drop == 0:
            return
        removed = self._memory.trim_oldest(drop)
        after = math.ceil(prompt_chars(system, tools, self._memory.history(include_system=False)) * ratio)
        self._audit_event(
            "context_trimmed",
            "agent",
            {
                "turn": turn,
                "removed_messages": removed,
                "estimated_tokens_before": before,
                "estimated_tokens_after": after,
                "budget_tokens": budget,
            },
        )

    def _audit_context_events(self, turn_number: int, turn: Turn) -> None:
        for event in turn.context_events:
            if event.get("type") == "compaction":
                payload = {
                    "turn": turn_number,
                    "input_tokens": event.get("input_tokens", 0),
                    "output_tokens": event.get("output_tokens", 0),
                }
                self._audit_event("context_compacted", self._provider.name(), payload)
            else:
                payload = {"turn": turn_number, "edit": event.get("type")}
                for key in ("cleared_tool_uses", "cleared_thinking_turns", "cleared_input_tokens"):
                    if key in event:
                        payload[key] = event[key]
                self._audit_event("context_edited", self._provider.name(), payload)
```

- [ ] **Step 4: Gates**, including existing suites (`test_agent.py`, `test_hooks.py`, `test_typed_results.py`, provider tests).
- [ ] **Step 5: Commit** — `feat: token budget trimming and request options through the agent loop`.

---

### Task 6: Live verification (scratchpad, not committed)

- [ ] `e2e-context/live_ollama.py`: `OllamaProvider("llama3.2")`, a `lookup` tool returning ~2 KB of text, `AgentConfig(context_budget_tokens=3_000, max_turns=12, effort="low")`, prompt asking it to look up five different items then summarise. Print turn count, audit event counts (`context_trimmed` payloads), request message counts per call (spy on `provider._client.chat.completions.create`), and confirm `reasoning_effort` in requests and the run completes.
- [ ] Anthropic live checks only when `ANTHROPIC_API_KEY` is set (thinking round-trip on `claude-opus-5` with a tool; `cache_read_tokens > 0` on turn 2).

---

### Task 7: Docs

**Files:** `README.md` (new "Context management" section after "Typed results"), `CHANGELOG.md` (`[Unreleased]` Added + Changed + Fixed), `examples/long_running_agent.py` + `examples/README.md` row, `specs/06-harness-roadmap.md` (2.4 ticked, table row "Context management | ✅ (2.4)"), `specs/14-context-management.md` (status implemented; note clear-tool-uses edit precedes compaction in `edits`), `PROJECT_INDEX.md` (`memory/budget.py`, `tests/test_context_budget.py`, spec 14).

- [ ] README section content: defaults (caching on, 150K budget with half cut, `memory_window` opt-in), reasoning (`thinking`, `effort`, `provider_options`), server-side (`Compaction`, `ClearToolResults`), audit events, and the thinking-block round-trip.
- [ ] CHANGELOG — Fixed: thinking blocks dropped from Anthropic history (broke tool use on Claude Opus 5 / Sonnet 5, which think by default); message window rewriting history every turn (cache misses, Fable 5.1 signature 400s). Changed: `memory_window` / store `window` default `None`, `context_budget_tokens=150_000` default; Anthropic agent runs send cache breakpoints by default.
- [ ] Example `examples/long_running_agent.py`:

```python
"""Long-running research agent: caching, adaptive thinking, compaction, and tool-result clearing.

    ANTHROPIC_API_KEY=... python examples/long_running_agent.py
"""

import asyncio

from agent_kit import Agent, AgentConfig, ClearToolResults, Compaction, tool
from agent_kit.providers import AnthropicProvider


@tool(description="Fetch a section of the (simulated) handbook by number", idempotent=True)
async def handbook(section: int) -> dict:
    return {"section": section, "text": f"Section {section}: " + "policy detail " * 400}


async def main() -> None:
    agent = Agent(
        AnthropicProvider(default_model="claude-opus-5"),
        tools=[handbook],
        config=AgentConfig(
            system_prompt="You audit handbooks. Read every section you are asked about before answering.",
            max_turns=40,
            effort="medium",
            compaction=Compaction(trigger_tokens=100_000, instructions="Keep every policy conflict found."),
            clear_tool_results=ClearToolResults(trigger_tokens=60_000, keep=4),
        ),
    )
    result = await agent.run("Read sections 1 through 25 and list every conflicting policy.")
    print(result.output)
    cached = sum(t.cost.cache_read_tokens for t in result.turns)
    print(f"{len(result.turns)} turns, ${result.total_cost_usd:.4f}, {cached} cached input tokens")
    assert agent.audit is not None
    for event in agent.audit.events():
        if event.event_type.startswith("context_"):
            print(event.event_type)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] Gates + `python3 -m py_compile examples/*.py`; commit `docs: context management guide and example`; fast-forward `main`, push, watch CI.
