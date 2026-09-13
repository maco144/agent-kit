# Tier 1 Harness Fundamentals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make agent-kit's core agent loop correct — tool calls survive history, memory never orphans tool results, tools run in parallel, cost is accurate, and streaming runs the full loop.

**Architecture:** `Message` gains `tool_calls`; each provider adapter serialises them into its native wire shape and parses them back. Memory windowing moves to a shared index-selection helper that keeps tool exchanges intact. `AgentLoop` is refactored around one async generator (`_execute`) that both `run()` and `stream()` consume, so streaming inherits retry, circuit breaking, audit, and cloud reporting. Provider pricing moves to a shared longest-prefix lookup that warns once on unknown models.

**Tech Stack:** Python 3.11+, Pydantic v2, `anthropic` SDK (0.x and 1.x), `openai` SDK, pytest + pytest-asyncio (`asyncio_mode = "auto"`), stdlib `sqlite3`.

**Spec:** `specs/06-harness-roadmap.md` (Tier 1)

## Global Constraints

- `agent_kit/types.py` imports nothing from `agent_kit`.
- All public models are Pydantic v2 `BaseModel`; mutable defaults use `Field(default_factory=...)`.
- Every provider method, tool function, and loop entry point stays `async`.
- `CloudReporter` calls stay fire-and-forget; no new exception paths into the agent.
- Existing public signatures keep working: `Agent.run(prompt)`, `Agent.stream(prompt) -> AsyncIterator[str]`, providers yielding only `str` from `stream()`.
- CI gates: `ruff check agent_kit tests`, `mypy agent_kit` (strict), `pytest` — all clean on Python 3.11 and 3.12.
- Provider adapter tests inject a fake client that records request kwargs. Do not use respx for Anthropic (`anthropic>=1.0` uses `httpx2`, which respx does not intercept). No test may reach the network.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `agent_kit/types.py` | `ToolCall` moves above `Message`; `Message.tool_calls`; `CostSummary` cache token fields | Modify |
| `agent_kit/providers/pricing.py` | Longest-prefix rate lookup, warn-once on unknown models | Create |
| `agent_kit/providers/anthropic.py` | Tool-use/tool-result serialisation, shared response→Turn parse, pricing, streaming with tools | Modify |
| `agent_kit/providers/openai.py` | Assistant `tool_calls` serialisation, pricing lookup, streaming tool-call assembly | Modify |
| `agent_kit/providers/base.py` | `stream()` protocol: `tools` param, yields `str` chunks then optionally a final `Turn` | Modify |
| `agent_kit/memory/window.py` | `window_indices(roles, keep)` — which messages survive trimming | Create |
| `agent_kit/memory/in_memory.py` | Use `window_indices` | Modify |
| `agent_kit/memory/sqlite.py` | Persist `tool_calls` (with column migration); use `window_indices` | Modify |
| `agent_kit/agent/loop.py` | `_execute` generator, parallel tools, streaming call path, `is_error` metadata | Modify |
| `agent_kit/agent/agent.py` | `stream()` via loop; `last_result` | Modify |
| `tests/test_provider_requests.py` | Fake-client request capture for Anthropic/OpenAI | Create |
| `tests/test_memory_window.py` | Windowing + SQLite tool_calls persistence | Create |
| `tests/test_agent.py` | Parallel tools, streaming with tools | Modify |
| `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md` | Positioning, testing guidance, release notes, checkboxes | Modify |

---

### Task 1: Tool calls round-trip through Anthropic history

**Files:**
- Modify: `agent_kit/types.py` (Message section)
- Modify: `agent_kit/providers/anthropic.py` (`_messages_to_anthropic`, `complete`)
- Modify: `agent_kit/agent/loop.py` (tool message metadata)
- Create: `tests/test_provider_requests.py`

**Interfaces:**
- Produces: `Message.tool_calls: list[ToolCall]` (default `[]`); tool-result `Message.metadata["is_error"] = True` when the tool failed; `tests/test_provider_requests.py::FakeAnthropic` (records `create` kwargs in `.calls`, returns queued responses); helpers `anthropic_response(content, stop_reason, **usage)`, `text_block(text)`, `tool_use_block(id, name, input)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provider_requests.py
"""Provider adapters — assert the exact request payloads sent to each SDK client.

Adapters get a fake client that records call kwargs. No network, no respx
(anthropic>=1.0 uses httpx2, which respx does not intercept).
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

from agent_kit import Agent, tool
from agent_kit.providers.anthropic import AnthropicProvider


def text_block(text: str) -> NS:
    return NS(type="text", text=text)


def tool_use_block(id: str, name: str, input: dict[str, Any]) -> NS:
    return NS(type="tool_use", id=id, name=name, input=input)


def anthropic_response(content: list[NS], stop_reason: str = "end_turn", **usage: int) -> NS:
    fields = {"input_tokens": 10, "output_tokens": 5, **usage}
    return NS(content=content, stop_reason=stop_reason, usage=NS(**fields))


class FakeAnthropic:
    def __init__(self, responses: list[NS]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.messages = NS(create=self._create)

    async def _create(self, **kwargs: Any) -> NS:
        self.calls.append(kwargs)
        return self._responses.pop(0)


def anthropic_agent(responses: list[NS], tools: list[Any]) -> tuple[Agent, FakeAnthropic]:
    provider = AnthropicProvider(api_key="test")
    fake = FakeAnthropic(responses)
    provider._client = fake  # type: ignore[assignment]
    return Agent(provider, tools=tools), fake


@tool(description="Weather for a city")
async def get_weather(city: str) -> dict[str, Any]:
    return {"city": city, "temp_c": 21}


@tool(description="Always fails")
async def broken(city: str) -> dict[str, Any]:
    raise RuntimeError("upstream down")


async def test_anthropic_tool_use_round_trips_into_next_request():
    agent, fake = anthropic_agent(
        [
            anthropic_response(
                [text_block("Checking."), tool_use_block("toolu_1", "get_weather", {"city": "Paris"})],
                stop_reason="tool_use",
            ),
            anthropic_response([text_block("21C in Paris")]),
        ],
        tools=[get_weather],
    )

    result = await agent.run("weather in Paris?")

    assert result.output == "21C in Paris"
    assert fake.calls[1]["messages"] == [
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"city": "Paris", "temp_c": 21}'},
            ],
        },
    ]


async def test_anthropic_tool_only_turn_sends_no_empty_text_block():
    agent, fake = anthropic_agent(
        [
            anthropic_response([tool_use_block("toolu_1", "get_weather", {"city": "Oslo"})], "tool_use"),
            anthropic_response([text_block("done")]),
        ],
        tools=[get_weather],
    )

    await agent.run("weather in Oslo?")

    assistant = fake.calls[1]["messages"][1]
    assert assistant["content"] == [
        {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Oslo"}},
    ]


async def test_anthropic_parallel_tool_results_share_one_user_message():
    agent, fake = anthropic_agent(
        [
            anthropic_response(
                [
                    tool_use_block("toolu_a", "get_weather", {"city": "Paris"}),
                    tool_use_block("toolu_b", "broken", {"city": "Rome"}),
                ],
                "tool_use",
            ),
            anthropic_response([text_block("partial")]),
        ],
        tools=[get_weather, broken],
    )

    await agent.run("two cities")

    messages = fake.calls[1]["messages"]
    assert len(messages) == 3
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert "is_error" not in results[0]
    assert results[1]["is_error"] is True
    assert results[1]["content"] == "Error: upstream down"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_requests.py -v`
Expected: 3 FAIL — assistant message is `{"role": "assistant", "content": ""}` and tool results are split / lack `is_error`.

- [ ] **Step 3: Implement**

`agent_kit/types.py` — move `ToolCall` above `Message` and add the field:

```python
class ToolCall(BaseModel):
    """A tool invocation requested by the LLM."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class Message(BaseModel):
    """A single message in a conversation."""

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant turns only
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`agent_kit/providers/anthropic.py` — replace `_messages_to_anthropic`:

```python
def _messages_to_anthropic(
    messages: list[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Split off the system message and convert the rest to Anthropic's format.

    Assistant tool calls become ``tool_use`` blocks. Consecutive tool results are
    merged into one user message, as the API expects for parallel tool use.

    Returns (system_text | None, anthropic_messages).
    """
    system_text: str | None = None
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            system_text = msg.content
        elif msg.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id,
                "content": msg.content,
            }
            if msg.metadata.get("is_error"):
                block["is_error"] = True
            prev = result[-1] if result else None
            if (
                prev is not None
                and prev["role"] == "user"
                and isinstance(prev["content"], list)
                and prev["content"][-1].get("type") == "tool_result"
            ):
                prev["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
        elif msg.role == "assistant" and msg.tool_calls:
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            blocks.extend(
                {"type": "tool_use", "id": tc.call_id, "name": tc.tool_name, "input": tc.arguments}
                for tc in msg.tool_calls
            )
            result.append({"role": "assistant", "content": blocks})
        elif msg.role == "assistant" and not msg.content:
            continue  # the API rejects empty assistant text
        else:
            result.append({"role": msg.role, "content": msg.content})

    return system_text, result
```

In `AnthropicProvider.complete`, attach the parsed calls to the assistant message:

```python
        assistant_msg = Message(role="assistant", content=" ".join(text_parts), tool_calls=tool_calls)
```

`agent_kit/agent/loop.py` — mark failed tool results:

```python
                        self._memory.add(
                            Message(
                                role="tool",
                                content=output_str,
                                tool_call_id=tc.call_id,
                                metadata={"is_error": True} if tool_result.error else {},
                            )
                        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_provider_requests.py tests/test_agent.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/types.py agent_kit/providers/anthropic.py agent_kit/agent/loop.py tests/test_provider_requests.py
git commit -m "fix: round-trip Anthropic tool calls through conversation history"
```

---

### Task 2: Tool calls round-trip through OpenAI/Ollama history

**Files:**
- Modify: `agent_kit/providers/openai.py` (`_messages_to_openai`, `complete`)
- Test: `tests/test_provider_requests.py`

**Interfaces:**
- Consumes: `Message.tool_calls`, `get_weather` tool from Task 1's test module.
- Produces: `FakeOpenAI` (records `chat.completions.create` kwargs, returns queued responses), helpers `openai_response(content, tool_calls, prompt_tokens=10, completion_tokens=5)`, `openai_tool_call(id, name, arguments_json)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_provider_requests.py
# (module imports gain: import importlib.util, import json, import pytest)
requires_openai = pytest.mark.skipif(
    importlib.util.find_spec("openai") is None, reason="openai extra not installed"
)


def openai_tool_call(id: str, name: str, arguments: str) -> NS:
    return NS(id=id, type="function", function=NS(name=name, arguments=arguments))


def openai_response(
    content: str | None, tool_calls: list[NS] | None = None, prompt_tokens: int = 10, completion_tokens: int = 5
) -> NS:
    message = NS(content=content, tool_calls=tool_calls)
    return NS(
        choices=[NS(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
        usage=NS(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class FakeOpenAI:
    def __init__(self, responses: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


def openai_agent(responses: list[Any], tools: list[Any], model: str = "gpt-4o") -> tuple[Agent, FakeOpenAI]:
    from agent_kit.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="test", default_model=model)
    fake = FakeOpenAI(responses)
    provider._client = fake  # type: ignore[assignment]
    return Agent(provider, tools=tools), fake


@requires_openai
async def test_openai_tool_calls_round_trip_into_next_request():
    agent, fake = openai_agent(
        [
            openai_response(None, [openai_tool_call("call_1", "get_weather", '{"city": "Paris"}')]),
            openai_response("21C in Paris"),
        ],
        tools=[get_weather],
    )

    result = await agent.run("weather in Paris?")

    assert result.output == "21C in Paris"
    assert fake.calls[1]["messages"] == [
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"city": "Paris", "temp_c": 21}'},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provider_requests.py::test_openai_tool_calls_round_trip_into_next_request -v`
Expected: FAIL — assistant message lacks `tool_calls`.

- [ ] **Step 3: Implement**

```python
def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                }
            )
        elif msg.role == "assistant" and msg.tool_calls:
            result.append(
                {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {"name": tc.tool_name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result
```

In `OpenAIProvider.complete`:

```python
        assistant_msg = Message(role="assistant", content=msg.content or "", tool_calls=tool_calls)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_provider_requests.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/providers/openai.py tests/test_provider_requests.py
git commit -m "fix: round-trip OpenAI tool calls through conversation history"
```

---

### Task 3: Memory windows keep tool exchanges intact; SQLite persists tool calls

**Files:**
- Create: `agent_kit/memory/window.py`
- Modify: `agent_kit/memory/in_memory.py` (`_trim`)
- Modify: `agent_kit/memory/sqlite.py` (schema, add, add_many, history, `_trim`)
- Create: `tests/test_memory_window.py`

**Interfaces:**
- Consumes: `Message.tool_calls`, `ToolCall`.
- Produces: `agent_kit.memory.window.window_indices(roles: list[str], keep: int) -> list[int]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_window.py
from __future__ import annotations

import sqlite3

from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.memory.sqlite import SQLiteMemory
from agent_kit.memory.window import window_indices
from agent_kit.types import Message, ToolCall


def test_window_under_limit_keeps_everything():
    assert window_indices(["user", "assistant"], keep=5) == [0, 1]


def test_window_starts_at_first_user_turn_inside_window():
    roles = ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert window_indices(roles, keep=3) == [4, 5]


def test_window_never_starts_on_tool_result_within_single_exchange():
    roles = ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    # Plain cut at 3 would start on an assistant; cut at 2 on a tool result.
    assert window_indices(roles, keep=4) == [0, 3, 4, 5]
    assert window_indices(roles, keep=5) == [0, 3, 4, 5]


def test_window_keeps_parallel_results_with_their_call():
    roles = ["user", "assistant", "tool", "tool", "tool"]
    assert window_indices(roles, keep=2) == [0, 1, 2, 3, 4]


def _exchange(i: int) -> list[Message]:
    return [
        Message(role="assistant", content="", tool_calls=[ToolCall(tool_name="t", arguments={}, call_id=f"c{i}")]),
        Message(role="tool", content="ok", tool_call_id=f"c{i}"),
    ]


def test_in_memory_store_trims_without_orphans():
    mem = InMemoryStore(window=4)
    mem.add(Message(role="user", content="go"))
    for i in range(5):
        mem.add_many(_exchange(i))
    history = mem.history()
    assert history[0].role == "user"
    assert history[1].role == "assistant"
    assert history[-1].tool_call_id == "c4"


def test_sqlite_persists_tool_calls_and_trims_without_orphans(tmp_path):
    mem = SQLiteMemory(tmp_path / "m.db", window=4)
    mem.add(Message(role="user", content="go"))
    for i in range(5):
        mem.add_many(_exchange(i))
    history = mem.history()
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[1].tool_calls[0].call_id == "c4"


def test_sqlite_migrates_database_without_tool_calls_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
        "content TEXT NOT NULL, tool_call_id TEXT, metadata TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute("INSERT INTO messages (role, content) VALUES ('user', 'hello')")
    conn.commit()
    conn.close()

    mem = SQLiteMemory(path)
    assert mem.history() == [Message(role="user", content="hello")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_window.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_kit.memory.window`

- [ ] **Step 3: Implement**

```python
# agent_kit/memory/window.py
"""Message windowing that never separates a tool call from its results."""

from __future__ import annotations


def window_indices(roles: list[str], keep: int) -> list[int]:
    """
    Return the indices of the messages to keep when trimming history to ``keep``.

    ``roles`` are the non-system message roles in order. Providers reject
    history that starts on an assistant turn or on a tool result whose call was
    trimmed, so the window is soft: it grows past ``keep`` rather than emit an
    invalid request.

    - If a user turn falls inside the window, keep from the first one.
    - Otherwise (one long tool-using exchange), keep the latest user turn as an
      anchor, then resume at the first assistant turn that still fits beside it,
      so every kept tool result follows its call. If none fits, resume at the
      latest assistant turn before that point and exceed the window.
    """
    n = len(roles)
    if n <= keep:
        return list(range(n))
    cut = n - keep

    for i in range(cut, n):
        if roles[i] == "user":
            return list(range(i, n))

    last_user = max((i for i in range(cut) if roles[i] == "user"), default=None)
    earliest = cut + (0 if last_user is None else 1)  # the anchor takes one slot
    resume = next((i for i in range(earliest, n) if roles[i] == "assistant"), None)
    if resume is None:
        resume = max((i for i in range(earliest) if roles[i] == "assistant"), default=cut)
    if last_user is None:
        return list(range(resume, n))
    if resume <= last_user:
        return list(range(last_user, n))
    return [last_user, *range(resume, n)]
```

`agent_kit/memory/in_memory.py`:

```python
from agent_kit.memory.window import window_indices
...
    def _trim(self) -> None:
        if len(self._messages) <= self._window:
            return
        # Keep all system messages; trim non-system without orphaning tool results
        system = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]
        keep = max(0, self._window - len(system))
        kept = window_indices([m.role for m in non_system], keep)
        self._messages = system + [non_system[i] for i in kept]
```

`agent_kit/memory/sqlite.py` — schema migration, persistence, trimming:

```python
from agent_kit.memory.window import window_indices
from agent_kit.types import Message, ToolCall

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    role    TEXT    NOT NULL,
                    content TEXT    NOT NULL,
                    tool_call_id TEXT,
                    metadata TEXT   NOT NULL DEFAULT '{}',
                    tool_calls TEXT NOT NULL DEFAULT '[]'
                )
            """)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
            if "tool_calls" not in columns:  # databases created before tool_calls existed
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN tool_calls TEXT NOT NULL DEFAULT '[]'"
                )

    @staticmethod
    def _row(m: Message) -> tuple[str, str, str | None, str, str]:
        return (
            m.role,
            m.content,
            m.tool_call_id,
            json.dumps(m.metadata),
            json.dumps([tc.model_dump() for tc in m.tool_calls]),
        )

    _INSERT = (
        "INSERT INTO messages (role, content, tool_call_id, metadata, tool_calls) VALUES (?,?,?,?,?)"
    )

    def add(self, message: Message) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(self._INSERT, self._row(message))
            self._trim()

    def add_many(self, messages: list[Message]) -> None:
        with self._lock:
            with self._conn:
                self._conn.executemany(self._INSERT, [self._row(m) for m in messages])
            self._trim()

    # history(): select tool_calls too and build
    #   tool_calls=[ToolCall(**tc) for tc in json.loads(row["tool_calls"])]

    def _trim(self) -> None:
        """Drop oldest non-system messages when window is exceeded, never orphaning tool results."""
        total = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total <= self._window:
            return

        system_count = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE role = 'system'"
        ).fetchone()[0]
        keep = max(0, self._window - system_count)

        rows = self._conn.execute(
            "SELECT id, role FROM messages WHERE role != 'system' ORDER BY id"
        ).fetchall()
        kept = set(window_indices([row[1] for row in rows], keep))
        drop_ids = [row[0] for i, row in enumerate(rows) if i not in kept]
        if drop_ids:
            placeholders = ",".join("?" * len(drop_ids))
            with self._conn:
                self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", drop_ids)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_memory_window.py tests/test_sqlite_memory.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/memory tests/test_memory_window.py
git commit -m "fix: trim memory without orphaning tool results; persist tool calls in SQLite"
```

---

### Task 4: Parallel tool execution

**Files:**
- Modify: `agent_kit/agent/loop.py` (tool execution block → `_run_tool`)
- Modify: `agent_kit/tools/base.py` (`Tool.__call__` runs sync functions via `asyncio.to_thread`)
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `AgentLoop._run_tool(tc: ToolCall) -> ToolResult` (never raises).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_agent.py
async def test_tool_calls_in_one_turn_run_concurrently():
    import asyncio

    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, ToolCall, Turn

    both_started = asyncio.Event()
    started: list[str] = []

    @tool(description="waits for its sibling")
    async def wait_for_sibling(name: str) -> str:
        started.append(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        return name

    class TwoCallProvider:
        config = ProviderConfig(default_model="mock")
        step = 0

        def name(self) -> str:
            return "mock"

        async def complete(self, messages, **kw):
            self.step += 1
            if self.step == 1:
                calls = [
                    ToolCall(tool_name="wait_for_sibling", arguments={"name": n}, call_id=n)
                    for n in ("a", "b")
                ]
                return Turn(message_out=Message(role="assistant", content="", tool_calls=calls),
                            tool_calls=calls, cost=CostSummary())
            return Turn(message_out=Message(role="assistant", content="done"), cost=CostSummary())

        async def stream(self, *a, **kw):
            yield ""

    result = await Agent(TwoCallProvider(), tools=[wait_for_sibling]).run("go")

    assert result.output == "done"
    assert [r.output for r in result.turns[0].tool_results] == ["a", "b"]
    assert all(r.error is None for r in result.turns[0].tool_results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py::test_tool_calls_in_one_turn_run_concurrently -v`
Expected: FAIL — first tool times out waiting (sequential), result error `TimeoutError`.

- [ ] **Step 3: Implement**

`agent_kit/agent/loop.py` — replace the `for tc in turn.tool_calls:` block:

```python
                    # --- Execute tool calls concurrently; record results in call order ---
                    tool_results = await asyncio.gather(
                        *(self._run_tool(tc) for tc in turn.tool_calls)
                    )
                    for tc, tool_result in zip(turn.tool_calls, tool_results):
                        # Audit: tool execution
                        if self._audit:
                            self._audit.append(
                                "tool_call",
                                actor=tc.tool_name,
                                payload={
                                    "call_id": tc.call_id,
                                    "success": tool_result.error is None,
                                    "error": tool_result.error,
                                    "duration_ms": tool_result.duration_ms,
                                },
                            )

                        # Feed tool result back as a tool message
                        output_str = (
                            json.dumps(tool_result.output, default=str)
                            if tool_result.output is not None
                            else f"Error: {tool_result.error}"
                        )
                        self._memory.add(
                            Message(
                                role="tool",
                                content=output_str,
                                tool_call_id=tc.call_id,
                                metadata={"is_error": True} if tool_result.error else {},
                            )
                        )
                        turn.tool_results.append(tool_result)
```

New method on `AgentLoop`:

```python
    async def _run_tool(self, tc: ToolCall) -> ToolResult:
        """Execute one tool call inside its span. Never raises — failures become ToolResult.error."""
        with self._tracer.span(f"tool.{tc.tool_name}", kind=SpanKind.TOOL, tool=tc.tool_name) as tool_span:
            t0 = time.monotonic()
            try:
                tool = self._registry.get(tc.tool_name)
                tool_result = await tool(call_id=tc.call_id, **tc.arguments)
            except Exception as exc:
                tool_result = ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error=str(exc),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            tool_span.set_attribute("duration_ms", tool_result.duration_ms)
            tool_span.set_attribute("success", tool_result.error is None)
        self._tracer.record_tool_call(tc.tool_name, tool_result.duration_ms, tool_result.error is None)
        return tool_result
```

Add `import asyncio` and `ToolCall` to the `agent_kit.types` import.

`agent_kit/tools/base.py` — in `Tool.__call__`, keep sync tools off the event loop:

```python
            if inspect.iscoroutinefunction(self._fn):
                output = await self._fn(**kwargs)
            else:
                output = await asyncio.to_thread(self._fn, **kwargs)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_agent.py tests/test_tools.py tests/test_provider_requests.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/agent/loop.py agent_kit/tools/base.py tests/test_agent.py
git commit -m "feat: run a turn's tool calls concurrently"
```

---

### Task 5: Accurate pricing with cache tokens

**Files:**
- Create: `agent_kit/providers/pricing.py`
- Modify: `agent_kit/types.py` (`CostSummary`)
- Modify: `agent_kit/providers/anthropic.py` (pricing table, cost accounting)
- Modify: `agent_kit/providers/openai.py` (pricing lookup)
- Test: `tests/test_provider_requests.py`

**Interfaces:**
- Produces: `pricing.lookup_rates(table: dict[str, tuple[float, float]], model: str) -> tuple[float, float] | None` (longest prefix; logs one warning per unknown model on logger `agent_kit.providers`); `CostSummary.cache_read_tokens: int = 0`, `CostSummary.cache_write_tokens: int = 0`; `anthropic._estimate_cost(model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0) -> float`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_provider_requests.py
import logging

from agent_kit.providers import anthropic as anthropic_provider


@pytest.mark.parametrize(
    ("model", "input_tokens", "output_tokens", "usd"),
    [
        ("claude-opus-5", 1_000_000, 1_000_000, 30.0),
        ("claude-opus-4-8", 1_000_000, 0, 5.0),
        ("claude-opus-4-1", 1_000_000, 0, 15.0),
        ("claude-sonnet-5", 0, 1_000_000, 10.0),
        ("claude-sonnet-4-6", 1_000_000, 0, 3.0),
        ("claude-haiku-4-5", 1_000_000, 0, 1.0),
        ("claude-fable-5-1", 0, 1_000_000, 50.0),
    ],
)
def test_anthropic_pricing_current_models(model, input_tokens, output_tokens, usd):
    assert anthropic_provider._estimate_cost(model, input_tokens, output_tokens) == pytest.approx(usd)


def test_anthropic_pricing_cache_tokens():
    # Opus 5: $5 input. Reads bill 0.1x, 5-minute writes 1.25x.
    assert anthropic_provider._estimate_cost(
        "claude-opus-5", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
    ) == pytest.approx(0.5 + 6.25)
    # Fable 5.1 cache reads are $0.25/MTok (0.025x).
    assert anthropic_provider._estimate_cost(
        "claude-fable-5-1", 0, 0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.25)


def test_unknown_model_warns_once_and_costs_zero(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_kit.providers"):
        assert anthropic_provider._estimate_cost("claude-future-9", 1000, 1000) == 0.0
        assert anthropic_provider._estimate_cost("claude-future-9", 1000, 1000) == 0.0
    assert caplog.text.count("claude-future-9") == 1


@requires_openai
def test_openai_longest_prefix_pricing():
    from agent_kit.providers import openai as openai_provider

    assert openai_provider._estimate_cost("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert openai_provider._estimate_cost("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.5)


async def test_anthropic_turn_cost_includes_cache_usage():
    agent, _ = anthropic_agent(
        [anthropic_response([text_block("hi")], input_tokens=100, output_tokens=10,
                            cache_read_input_tokens=1000, cache_creation_input_tokens=200)],
        tools=[],
    )
    agent._provider.config.default_model = "claude-opus-5"  # type: ignore[attr-defined]

    result = await agent.run("hi")

    cost = result.turns[0].cost
    assert (cost.input_tokens, cost.cache_read_tokens, cost.cache_write_tokens) == (100, 1000, 200)
    assert cost.total_tokens == 1310
    assert cost.cost_usd == pytest.approx((100 * 5 + 10 * 25 + 1000 * 5 * 0.1 + 200 * 5 * 1.25) / 1_000_000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_requests.py -k "pricing or cost or unknown" -v`
Expected: FAIL — `claude-opus-5` costs 0.0; `_estimate_cost` has no cache kwargs.

- [ ] **Step 3: Implement**

```python
# agent_kit/providers/pricing.py
"""Shared model price lookup for provider adapters."""

from __future__ import annotations

import logging

logger = logging.getLogger("agent_kit.providers")

_warned: set[str] = set()


def lookup_rates(table: dict[str, tuple[float, float]], model: str) -> tuple[float, float] | None:
    """
    Return (input, output) USD per million tokens for the longest table prefix matching ``model``.

    Unknown models return None and log one warning per model, so a missing price
    shows up in logs instead of as a silent $0.00.
    """
    matches = [prefix for prefix in table if model.startswith(prefix)]
    if matches:
        return table[max(matches, key=len)]
    if model not in _warned:
        _warned.add(model)
        logger.warning("No pricing for model %r; cost_usd will be reported as 0.0", model)
    return None
```

`agent_kit/types.py` — `CostSummary`:

```python
class CostSummary(BaseModel):
    """Token and USD cost for a single LLM call."""

    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
```

`agent_kit/providers/anthropic.py` — table and cost function:

```python
from agent_kit.providers.pricing import lookup_rates

# USD per million tokens (input, output). Longest matching prefix wins.
# Advisory — actual billing comes from the Anthropic console.
_COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-fable-5":    (10.00, 50.00),
    "claude-mythos-5":   (10.00, 50.00),
    "claude-opus-5":     (5.00,  25.00),
    "claude-opus-4-8":   (5.00,  25.00),
    "claude-opus-4-7":   (5.00,  25.00),
    "claude-opus-4-6":   (5.00,  25.00),
    "claude-opus-4-5":   (5.00,  25.00),
    "claude-opus-4":     (15.00, 75.00),  # Opus 4 / 4.1
    "claude-sonnet-5":   (2.00,  10.00),
    "claude-sonnet-4":   (3.00,  15.00),  # Sonnet 4 / 4.5 / 4.6
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-3-7-sonnet": (3.00,  15.00),
    "claude-3-5-sonnet": (3.00,  15.00),
    "claude-3-5-haiku":  (0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-haiku":    (0.25,  1.25),
}

# Cache reads bill at 0.1x input except where listed; 5-minute cache writes at 1.25x.
_CACHE_READ_MULTIPLIER: dict[str, float] = {"claude-fable-5-1": 0.025}
_CACHE_WRITE_MULTIPLIER = 1.25


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    rates = lookup_rates(_COST_TABLE, model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    read_multiplier = next(
        (m for prefix, m in _CACHE_READ_MULTIPLIER.items() if model.startswith(prefix)), 0.1
    )
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * in_rate * read_multiplier
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
```

Response → Turn parsing moves to a function shared with streaming (Task 6):

```python
def _turn_from_response(
    response: Any, messages: list[Message], model: str, duration_ms: int
) -> Turn:
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(tool_name=block.name, arguments=block.input, call_id=block.id))

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    cost = CostSummary(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=usage.input_tokens + usage.output_tokens + cache_read + cache_write,
        cost_usd=_estimate_cost(model, usage.input_tokens, usage.output_tokens, cache_read, cache_write),
        model=model,
    )
    return Turn(
        messages_in=messages,
        message_out=Message(role="assistant", content=" ".join(text_parts), tool_calls=tool_calls),
        tool_calls=tool_calls,
        cost=cost,
        duration_ms=duration_ms,
    )
```

`complete()` ends with `return _turn_from_response(response, messages, resolved_model, int((time.monotonic() - t0) * 1000))`.

`agent_kit/providers/openai.py`:

```python
from agent_kit.providers.pricing import lookup_rates

_COST_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o":        (2.50, 10.00),
    "gpt-4o-mini":   (0.15, 0.60),
    "gpt-4-turbo":   (10.00, 30.00),
    "gpt-4":         (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1":            (15.00, 60.00),
    "o1-mini":       (3.00, 12.00),
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = lookup_rates(_COST_TABLE, model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
```

- [ ] **Step 4: Run tests**

Run: `pytest tests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/providers agent_kit/types.py tests/test_provider_requests.py
git commit -m "fix: accurate model pricing with cache tokens; warn on unpriced models"
```

---

### Task 6: `Agent.stream()` runs the full loop

**Files:**
- Modify: `agent_kit/providers/base.py` (`stream` protocol)
- Modify: `agent_kit/providers/anthropic.py` (`stream` with tools → final `Turn`)
- Modify: `agent_kit/providers/openai.py` (`stream` with tools → final `Turn`)
- Modify: `agent_kit/agent/loop.py` (`_execute`, `run`, `stream`, `_open_stream`)
- Modify: `agent_kit/agent/agent.py` (`stream`, `run`, `last_result`, `_make_loop`)
- Test: `tests/test_agent.py`, `tests/test_provider_requests.py`

**Interfaces:**
- Consumes: `_turn_from_response` (Task 5), `Message.tool_calls` (Task 1), `_run_tool` (Task 4).
- Produces: `BaseProvider.stream(messages, model=None, tools=None, system=None, max_tokens=4096, **kwargs) -> AsyncIterator[str | Turn]` — text chunks, then optionally one final `Turn`; `AgentLoop.stream(prompt, **context) -> AsyncIterator[str]`; `AgentLoop.result: AgentResult | None`; `Agent.last_result: AgentResult | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_agent.py
async def test_stream_executes_tools_and_records_result():
    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, ToolCall, Turn

    calls_made: list[str] = []

    @tool(description="records a call")
    def lookup(key: str) -> str:
        calls_made.append(key)
        return f"value-{key}"

    class StreamingToolProvider:
        config = ProviderConfig(default_model="mock")
        step = 0

        def name(self) -> str:
            return "mock"

        async def complete(self, messages, **kw):
            raise AssertionError("stream() must not fall back to complete()")

        async def stream(self, messages, model=None, tools=None, system=None, max_tokens=4096, **kw):
            self.step += 1
            if self.step == 1:
                assert tools, "tools must be offered while streaming"
                yield "Looking up. "
                call = ToolCall(tool_name="lookup", arguments={"key": "k1"}, call_id="c1")
                yield Turn(message_out=Message(role="assistant", content="Looking up. ", tool_calls=[call]),
                           tool_calls=[call], cost=CostSummary(total_tokens=3, cost_usd=0.001))
            else:
                assert messages[-1].role == "tool" and messages[-1].content == '"value-k1"'
                yield "Found "
                yield "value-k1."
                yield Turn(message_out=Message(role="assistant", content="Found value-k1."),
                           cost=CostSummary(total_tokens=2, cost_usd=0.001))

    agent = Agent(StreamingToolProvider(), tools=[lookup])
    chunks = [c async for c in agent.stream("find k1")]

    assert "".join(chunks) == "Looking up. Found value-k1."
    assert calls_made == ["k1"]
    assert agent.last_result is not None
    assert agent.last_result.output == "Found value-k1."
    assert len(agent.last_result.turns) == 2
    assert agent.audit is not None
    assert {e.event_type for e in agent.audit.events()} >= {"agent_start", "tool_call", "agent_complete"}
    assert agent.memory.history()[-1].content == "Found value-k1."


async def test_stream_accepts_text_only_providers(mock_provider_factory):
    agent = Agent(mock_provider_factory(["hello streaming world"]))
    chunks = [c async for c in agent.stream("hi")]
    assert "".join(chunks) == "hello streaming world "
    assert agent.last_result is not None
    assert agent.last_result.output == "hello streaming world "
```

```python
# append to tests/test_provider_requests.py
class FakeAnthropicStream:
    def __init__(self, chunks: list[str], final: NS) -> None:
        self._chunks = chunks
        self._final = final

    async def __aenter__(self) -> FakeAnthropicStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> Any:
        async def gen() -> Any:
            for c in self._chunks:
                yield c
        return gen()

    async def get_final_message(self) -> NS:
        return self._final


async def test_anthropic_stream_runs_tools_through_loop():
    provider = AnthropicProvider(api_key="test", default_model="claude-opus-5")
    streams = [
        FakeAnthropicStream(["Checking."], anthropic_response(
            [text_block("Checking."), tool_use_block("toolu_1", "get_weather", {"city": "Paris"})], "tool_use")),
        FakeAnthropicStream(["21C ", "in Paris"], anthropic_response([text_block("21C in Paris")])),
    ]
    stream_calls: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> FakeAnthropicStream:
        stream_calls.append(kwargs)
        return streams.pop(0)

    provider._client = NS(messages=NS(stream=fake_stream))  # type: ignore[assignment]
    agent = Agent(provider, tools=[get_weather])

    chunks = [c async for c in agent.stream("weather in Paris?")]

    assert "".join(chunks) == "Checking.21C in Paris"
    assert stream_calls[0]["tools"][0]["name"] == "get_weather"
    assert stream_calls[1]["messages"][1]["content"][1]["type"] == "tool_use"
    assert agent.last_result is not None and agent.last_result.output == "21C in Paris"
    assert agent.last_result.total_cost_usd > 0


def openai_chunk(content: str | None = None, tool_calls: list[NS] | None = None, usage: NS | None = None) -> NS:
    choices = [] if usage else [NS(delta=NS(content=content, tool_calls=tool_calls))]
    return NS(choices=choices, usage=usage)


class FakeOpenAIStream:
    def __init__(self, chunks: list[NS]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> FakeOpenAIStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for c in self._chunks:
                yield c
        return gen()


@requires_openai
async def test_openai_stream_assembles_tool_call_deltas():
    first = FakeOpenAIStream([
        openai_chunk(tool_calls=[NS(index=0, id="call_1", function=NS(name="get_weather", arguments='{"ci'))]),
        openai_chunk(tool_calls=[NS(index=0, id=None, function=NS(name=None, arguments='ty": "Paris"}'))]),
        openai_chunk(usage=NS(prompt_tokens=10, completion_tokens=5)),
    ])
    second = FakeOpenAIStream([
        openai_chunk("21C "), openai_chunk("in Paris"),
        openai_chunk(usage=NS(prompt_tokens=20, completion_tokens=4)),
    ])
    agent, fake = openai_agent([first, second], tools=[get_weather])

    chunks = [c async for c in agent.stream("weather in Paris?")]

    assert "".join(chunks) == "21C in Paris"
    assert fake.calls[0]["stream"] is True
    assert fake.calls[0]["stream_options"] == {"include_usage": True}
    assert fake.calls[1]["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Paris"}'
    assert agent.last_result is not None
    assert agent.last_result.total_tokens == 39
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent.py -k stream tests/test_provider_requests.py -k stream -v`
Expected: FAIL — `Agent` has no `last_result`; streaming never executes tools.

- [ ] **Step 3: Implement**

`agent_kit/providers/base.py`:

```python
    def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str | Turn]:
        """
        Yield text chunks as they arrive, then optionally one final Turn.

        The final Turn carries tool calls and cost, exactly as complete() would
        return. Providers that yield only text still work; AgentLoop builds a
        text-only Turn from the chunks.
        """
        ...
```

`agent_kit/providers/anthropic.py` — `stream`:

```python
    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str | Turn]:
        resolved_model = model or self.config.default_model
        sys_from_messages, converted = _messages_to_anthropic(messages)
        resolved_system = system or sys_from_messages

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if resolved_system:
            call_kwargs["system"] = resolved_system
        if tools:
            call_kwargs["tools"] = _to_anthropic_tools(tools)

        t0 = time.monotonic()
        try:
            async with self._client.messages.stream(**call_kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic stream error: {exc}") from exc

        yield _turn_from_response(final, messages, resolved_model, int((time.monotonic() - t0) * 1000))
```

`agent_kit/providers/openai.py` — `stream`:

```python
    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str | Turn]:
        resolved_model = model or self.config.default_model
        converted = _messages_to_openai(messages)

        if system and not any(m["role"] == "system" for m in converted):
            converted = [{"role": "system", "content": system}] + converted

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = _to_openai_tools(tools)
            call_kwargs["tool_choice"] = "auto"

        text_parts: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        input_tokens = output_tokens = 0
        t0 = time.monotonic()
        try:
            stream: openai.AsyncStream[ChatCompletionChunk] = (
                await self._client.chat.completions.create(stream=True, **call_kwargs)
            )
            async with stream:
                async for chunk in stream:
                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens
                        output_tokens = chunk.usage.completion_tokens
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text_parts.append(delta.content)
                        yield delta.content
                    for tc in delta.tool_calls or []:
                        slot = pending.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI stream error: {exc}") from exc

        tool_calls = [
            ToolCall(tool_name=s["name"], arguments=json.loads(s["arguments"] or "{}"), call_id=s["id"])
            for _, s in sorted(pending.items())
        ]
        content = "".join(text_parts)
        yield Turn(
            messages_in=messages,
            message_out=Message(role="assistant", content=content, tool_calls=tool_calls),
            tool_calls=tool_calls,
            cost=CostSummary(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost_usd=_estimate_cost(resolved_model, input_tokens, output_tokens),
                model=resolved_model,
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
```

`agent_kit/agent/loop.py` — one generator drives both entry points. `run()` and `stream()`:

```python
    async def run(self, prompt: str, **context: Any) -> AgentResult:
        """Execute the agent loop and return the final result."""
        async for _ in self._execute(prompt, streaming=False, context=context):
            pass
        assert self.result is not None
        return self.result

    async def stream(self, prompt: str, **context: Any) -> AsyncIterator[str]:
        """Execute the agent loop, yielding text as it streams. ``self.result`` is set at the end."""
        async for chunk in self._execute(prompt, streaming=True, context=context):
            yield chunk
```

`_execute(self, prompt, streaming, context) -> AsyncIterator[str]` is the former body of `run()` with three changes:
1. It ends with `self.result = result` instead of `return result`.
2. The LLM call inside the `llm.complete` span becomes:

```python
                        if streaming:
                            turn = None
                            it, first = await with_retry(
                                self._cb_call,
                                self._retry_policy,
                                run_id,
                                self._open_stream,
                                messages,
                                tool_schemas or None,
                            )
                            chunks: list[str] = []
                            item = first
                            while item is not None:
                                if isinstance(item, Turn):
                                    turn = item
                                else:
                                    chunks.append(item)
                                    yield item
                                item = await anext(it, None)
                            if turn is None:  # provider yields text only
                                turn = Turn(
                                    messages_in=messages,
                                    message_out=Message(role="assistant", content="".join(chunks)),
                                    cost=CostSummary(model=self._model or self._provider.config.default_model),
                                )
                        else:
                            turn = await with_retry(
                                self._cb_call,
                                self._retry_policy,
                                run_id,
                                self._provider.complete,
                                messages,
                                model=self._model,
                                tools=tool_schemas if tool_schemas else None,
                                system=self._system_prompt or None,
                                max_tokens=self._max_tokens_per_turn,
                            )
```

3. `self.result: AgentResult | None = None` is initialised in `__init__`.

New method — retry and circuit breaking cover opening the stream; a failure after text has been yielded propagates, because replaying it would duplicate output:

```python
    async def _open_stream(
        self, messages: list[Message], tools: list[ToolSchema] | None
    ) -> tuple[AsyncIterator[str | Turn], str | Turn | None]:
        """Start a provider stream and pull its first item, so connection failures are retryable."""
        it = self._provider.stream(
            messages,
            model=self._model,
            tools=tools,
            system=self._system_prompt or None,
            max_tokens=self._max_tokens_per_turn,
        ).__aiter__()
        return it, await anext(it, None)
```

Imports: `AsyncIterator` from `typing`; `CostSummary`, `ToolCall`, `ToolSchema` from `agent_kit.types`.

`agent_kit/agent/agent.py`:

```python
        self.last_result: AgentResult | None = None

    def _make_loop(self) -> AgentLoop:
        return AgentLoop(
            provider=self._provider,
            registry=self._registry,
            memory=self._memory,
            tracer=self._tracer,
            audit=self._audit,
            model=self._config.model,
            system_prompt=self._config.system_prompt,
            max_turns=self._config.max_turns,
            max_tokens_per_turn=self._config.max_tokens_per_turn,
            retry_policy=self._config.retry_policy,
            circuit_breaker_config=self._config.circuit_breaker,
            reporter=self._config.cloud,
        )

    async def run(self, prompt: str, **context: Any) -> AgentResult:
        """(docstring unchanged)"""
        self.last_result = await self._make_loop().run(prompt, **context)
        return self.last_result

    async def stream(self, prompt: str, **context: Any) -> AsyncIterator[str]:
        """
        Stream the agent's response as text chunks.

        Runs the same loop as run(): tools execute between turns, and retry,
        circuit breaking, audit, and cloud reporting all apply. Retry covers
        opening each provider stream; a failure mid-stream propagates. The
        completed AgentResult is available as ``agent.last_result`` once the
        iterator is exhausted.
        """
        loop = self._make_loop()
        async for chunk in loop.stream(prompt, **context):
            yield chunk
        self.last_result = loop.result
```

- [ ] **Step 4: Run the full suite + gates**

Run: `pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: all PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit tests
git commit -m "feat: stream through the full agent loop with tools, retry, and audit"
```

---

### Task 7: Positioning, testing guidance, release notes

**Files:**
- Modify: `README.md` (`## Why agent-kit?` section)
- Modify: `CONTRIBUTING.md` (Testing section)
- Modify: `CHANGELOG.md` (`[Unreleased]`)
- Modify: `specs/06-harness-roadmap.md` (Tier 1 checkboxes)

- [ ] **Step 1: Replace the README comparison table**

```markdown
## Why agent-kit?

First-party SDKs and agent frameworks give you a capable loop. agent-kit is for what comes after the
demo — running agents you can trust, afford, and prove things about:

| Built in | What you get |
|---|---|
| Per-provider circuit breaker | Stops hammering a failing provider; every state change lands in the audit chain |
| Retry with backoff | Transient provider failures retried under a configurable policy |
| Tamper-evident audit chain | Hash-linked record of every LLM call and tool call; verify locally, re-verified server-side, JSONL/CSV export |
| Cost per turn | Token- and cache-aware USD for current Claude and OpenAI models; unpriced models are logged, not silently $0 |
| Self-hostable ops backend | Fleet metrics, alerting (Slack, PagerDuty, webhook, SMTP), and SLA context — see [agent-kit Cloud](#agent-kit-cloud) |
| Provider-neutral | Anthropic, OpenAI, Ollama, and any OpenAI-compatible endpoint behind one interface |
| OpenTelemetry | No-op by default; console JSON or OTLP export when you want it |

Not yet: MCP tools, typed outputs, approval hooks, and resumable runs — tracked in
[specs/06-harness-roadmap.md](specs/06-harness-roadmap.md).
```

- [ ] **Step 2: Update CONTRIBUTING testing guidance**

Replace the SDK mocking bullet with:

```markdown
- SDK: test provider adapters by injecting a fake client that records request kwargs — see `tests/test_provider_requests.py`. Assert the exact payload the SDK would send. Don't use respx for Anthropic: `anthropic>=1.0` uses `httpx2`, which respx doesn't intercept, so requests silently reach the network. Agent-loop behaviour can use `MockProvider` from `tests/conftest.py`.
```

- [ ] **Step 3: CHANGELOG `[Unreleased]` entries**

Under `### Added`:

```markdown
- `Agent.stream()` now runs the full agent loop — tools execute between turns, and retry, circuit breaking, audit, and cloud reporting apply. The finished `AgentResult` is available as `agent.last_result`. Providers' `stream()` accepts `tools` and may yield a final `Turn` after text chunks; text-only providers keep working.
- Tool calls within one turn run concurrently; synchronous tools run in a worker thread instead of blocking the event loop.
- `CostSummary.cache_read_tokens` / `cache_write_tokens`; Anthropic cost includes cache reads and writes.
```

Under `### Fixed`:

```markdown
- **Multi-turn tool use failed on Anthropic and OpenAI.** Assistant tool calls were not stored in history, so the request after a tool call carried an empty assistant turn and orphaned tool results, which both APIs reject. `Message.tool_calls` now round-trips through both adapters and `SQLiteMemory` (existing databases migrate automatically); parallel tool results share one message and failed tools set `is_error`.
- Memory windows could split a tool call from its results; trimming now keeps tool exchanges intact.
- Cost tracking reported $0 for Claude Opus 5, Sonnet 5, and Fable; billed Opus 4.5–4.8 at 3× actual; understated Haiku 4.5; priced `gpt-4o-mini` as `gpt-4o`. Prices now use longest-prefix matching and unknown models log a warning.
```

- [ ] **Step 4: Tick Tier 1 in the roadmap, run the gates**

Run: `pytest && ruff check agent_kit tests && mypy agent_kit && python -m compileall -q examples/`
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add README.md CONTRIBUTING.md CHANGELOG.md specs/06-harness-roadmap.md
git commit -m "docs: honest positioning, adapter testing guidance, Tier 1 changelog"
```
