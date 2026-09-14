# Hooks and Approval Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `before_tool` / `after_tool` / `before_llm` hooks with allow / deny / ask / replace decisions, an inline async approver with timeout, fail-closed semantics, and audit events for every decision.

**Architecture:** `agent_kit/hooks.py` defines decisions, contexts, `Hooks`, helpers, and `run_hook` (sync/async, fail-closed). `AgentLoop` gates each tool call (`_gate_tool` → `_request_approval`), filters outputs (`_filter_output`), gates provider calls (`_gate_llm`), and raises a pending `RunStoppedByHookError` after the turn's tool calls resolve.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2 types already in the SDK, pytest + pytest-asyncio (auto).

**Spec:** `specs/11-hooks-approval-gates.md`

## Global Constraints

- `agent_kit/types.py` stays import-free of other agent_kit modules; `hooks.py` imports nothing from `agent_kit` except exceptions.
- Fail closed: hook exceptions, invalid decisions, missing approver, approver errors, and timeouts all deny.
- Tool arguments and outputs never go into audit payloads.
- `allowed_tools` enforcement happens before any hook runs; budget checks happen before `before_llm`.
- `ruff check agent_kit tests`, `mypy agent_kit` (strict), `pytest` clean.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `agent_kit/hooks.py` | Decisions, contexts, `Hooks`, helpers, `run_hook` | Create |
| `agent_kit/exceptions.py` | `RunStoppedByHookError` | Modify |
| `agent_kit/agent/agent.py` | `hooks`, `approver`, `approval_timeout_s` on `AgentConfig`; pass to loop | Modify |
| `agent_kit/agent/loop.py` | Gates, approval, output filter, pending stop; tool message for `None` output | Modify |
| `tests/test_hooks.py` | Behaviour | Create |
| `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/11-hooks-approval-gates.md`, `PROJECT_INDEX.md`, `examples/approval_gate.py`, `examples/README.md` | Docs + example | Modify / Create |

---

### Task 1: Hooks module

**Files:**
- Create: `agent_kit/hooks.py`, `tests/test_hooks.py` (module-level unit tests)
- Modify: `agent_kit/exceptions.py`

**Interfaces:**
- Produces: `Decision` (`allow()`, `deny(reason, stop_run=False)`, `ask(reason=None)`, `replace(output, reason=None)`), `ToolCallContext`, `ToolResultContext`, `LLMCallContext`, `ApprovalRequest`, `Hooks`, `Approver`, `async run_hook(hook, ctx) -> Decision`, `require_approval`, `deny_tools`, `allow_only`; `RunStoppedByHookError(stage, reason, tool_name=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hooks.py
"""Hooks and approval gates."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_kit.exceptions import RunStoppedByHookError
from agent_kit.hooks import (
    Decision,
    ToolCallContext,
    allow_only,
    deny_tools,
    require_approval,
    run_hook,
)


def tool_ctx(name: str = "refund", **extra: Any) -> ToolCallContext:
    return ToolCallContext(run_id="r", turn=1, tool_name=name, arguments={"order_id": "1"}, call_id="c1",
                           context=extra)


async def test_run_hook_accepts_sync_async_and_none():
    async def async_deny(ctx):
        return Decision.deny("async no")

    assert (await run_hook(lambda ctx: None, tool_ctx())).kind == "allow"
    assert (await run_hook(lambda ctx: Decision.ask("why"), tool_ctx())).reason == "why"
    assert (await run_hook(async_deny, tool_ctx())).reason == "async no"


async def test_run_hook_fails_closed():
    def broken(ctx):
        raise ValueError("bad policy")

    decision = await run_hook(broken, tool_ctx())
    assert (decision.kind, decision.reason) == ("deny", "hook error: ValueError: bad policy")

    odd = await run_hook(lambda ctx: "yes", tool_ctx())
    assert (odd.kind, odd.reason) == ("deny", "hook error: returned str, expected Decision or None")


def test_decision_constructors():
    assert Decision.deny("x", stop_run=True).stop_run is True
    assert Decision.replace({"a": 1}, "redacted").output == {"a": 1}
    assert Decision.allow().kind == "allow"


async def test_helpers():
    approval = require_approval("refund", "wire")
    assert (await run_hook(approval, tool_ctx("refund"))).kind == "ask"
    assert (await run_hook(approval, tool_ctx("lookup"))).kind == "allow"

    blocked = deny_tools("delete_account", reason="never", stop_run=True)
    decision = await run_hook(blocked, tool_ctx("delete_account"))
    assert (decision.kind, decision.reason, decision.stop_run) == ("deny", "never", True)

    only = allow_only("lookup")
    assert (await run_hook(only, tool_ctx("lookup"))).kind == "allow"
    assert (await run_hook(only, tool_ctx("refund"))).reason == "tool not permitted by policy"


def test_run_stopped_error_message():
    err = RunStoppedByHookError("before_tool", "never", tool_name="delete_account")
    assert (err.stage, err.reason, err.tool_name) == ("before_tool", "never", "delete_account")
    assert "delete_account" in str(err) and "never" in str(err)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.hooks'`

- [ ] **Step 3: Implement**

`agent_kit/exceptions.py` — append:

```python
class RunStoppedByHookError(AgentKitError):
    """A hook stopped the run (before_llm deny, or a tool deny with stop_run=True)."""

    def __init__(self, stage: str, reason: str, tool_name: str | None = None) -> None:
        where = f" on '{tool_name}'" if tool_name else ""
        super().__init__(f"Run stopped by {stage} hook{where}: {reason}")
        self.stage = stage
        self.reason = reason
        self.tool_name = tool_name
```

```python
# agent_kit/hooks.py
"""
Hooks and approval gates — policy around what an agent may do.

    from agent_kit.hooks import Decision, Hooks, require_approval, deny_tools

    config = AgentConfig(
        hooks=Hooks(
            before_tool=[deny_tools("delete_account", reason="never in prod"), require_approval("refund")],
            after_tool=[redact_card_numbers],
            before_llm=[stop_after_hours],
        ),
        approver=approve,            # async (ApprovalRequest) -> bool
        approval_timeout_s=300,
    )

Hooks may be sync or async and return a Decision or None (allow). Every path that
isn't a clear allow is a deny: a hook that raises, an invalid decision, an ask with no
approver, a denied or timed-out approval. Decisions are recorded in the audit chain.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

DecisionKind = Literal["allow", "deny", "ask", "replace"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str | None = None
    output: Any = None
    stop_run: bool = False

    @classmethod
    def allow(cls) -> Decision:
        return cls("allow")

    @classmethod
    def deny(cls, reason: str, stop_run: bool = False) -> Decision:
        return cls("deny", reason=reason, stop_run=stop_run)

    @classmethod
    def ask(cls, reason: str | None = None) -> Decision:
        return cls("ask", reason=reason)

    @classmethod
    def replace(cls, output: Any, reason: str | None = None) -> Decision:
        return cls("replace", reason=reason, output=output)


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultContext(ToolCallContext):
    output: Any = None
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class LLMCallContext:
    run_id: str
    turn: int
    model: str
    message_count: int
    run_cost_usd: float
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    reason: str | None
    context: dict[str, Any] = field(default_factory=dict)


HookResult = Decision | None
BeforeToolHook = Callable[[ToolCallContext], HookResult | Awaitable[HookResult]]
AfterToolHook = Callable[[ToolResultContext], HookResult | Awaitable[HookResult]]
BeforeLLMHook = Callable[[LLMCallContext], HookResult | Awaitable[HookResult]]
Approver = Callable[[ApprovalRequest], Awaitable[bool]]


@dataclass
class Hooks:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    before_llm: list[BeforeLLMHook] = field(default_factory=list)


async def run_hook(hook: Callable[[Any], Any], ctx: Any) -> Decision:
    """Call a sync or async hook; anything but a Decision or None fails closed."""
    try:
        result = hook(ctx)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return Decision.deny(f"hook error: {type(exc).__name__}: {exc}")
    if result is None:
        return Decision.allow()
    if not isinstance(result, Decision):
        return Decision.deny(f"hook error: returned {type(result).__name__}, expected Decision or None")
    return result


def require_approval(*tool_names: str, reason: str | None = None) -> BeforeToolHook:
    """Ask the approver before any of these tools run."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        if ctx.tool_name in names:
            return Decision.ask(reason or f"{ctx.tool_name} requires approval")
        return None

    return hook


def deny_tools(*tool_names: str, reason: str, stop_run: bool = False) -> BeforeToolHook:
    """Never run these tools."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return Decision.deny(reason, stop_run=stop_run) if ctx.tool_name in names else None

    return hook


def allow_only(*tool_names: str, reason: str = "tool not permitted by policy") -> BeforeToolHook:
    """Deny every tool not listed (the model still sees all tools, unlike allowed_tools)."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return None if ctx.tool_name in names else Decision.deny(reason)

    return hook
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_hooks.py -v && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit/hooks.py agent_kit/exceptions.py tests/test_hooks.py
git commit -m "feat: hook decisions, contexts, and policy helpers"
```

---

### Task 2: Wire hooks and approvals into the agent loop

**Files:**
- Modify: `agent_kit/agent/agent.py`, `agent_kit/agent/loop.py`, `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `AgentConfig(hooks=None, approver=None, approval_timeout_s=300.0)`; loop methods `_gate_tool`, `_request_approval`, `_deny_tool`, `_filter_output`, `_gate_llm`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_hooks.py
from agent_kit import Agent, AgentConfig, tool
from agent_kit.hooks import ApprovalRequest, Hooks, LLMCallContext, ToolResultContext
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, Turn


class ScriptedProvider:
    """Returns scripted turns and records every request's messages."""

    config = ProviderConfig(default_model="mock")

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.requests: list[list[Message]] = []

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append(list(messages))
        return self.turns.pop(0)

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.requests.append(list(messages))
        yield self.turns.pop(0)


def calls(*specs: tuple[str, dict[str, Any]]) -> Turn:
    tool_calls = [ToolCall(tool_name=name, arguments=args, call_id=f"{name}-{i}") for i, (name, args) in enumerate(specs)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tool_calls), tool_calls=tool_calls,
                cost=CostSummary())


def final(text: str = "done") -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary())


executed: list[str] = []


@tool(description="refund an order")
async def refund(order_id: str) -> dict[str, Any]:
    executed.append(f"refund:{order_id}")
    return {"refunded": order_id}


@tool(description="look up an order")
async def lookup(order_id: str) -> dict[str, Any]:
    executed.append(f"lookup:{order_id}")
    return {"order_id": order_id, "card": "4111 1111 1111 1111"}


@tool(description="returns nothing")
async def noop() -> None:
    executed.append("noop")


@pytest.fixture(autouse=True)
def _reset_executed():
    executed.clear()


def build(provider: ScriptedProvider, **config: Any) -> tuple[Agent, list[tuple[str, str, dict[str, Any]]]]:
    agent = Agent(provider, tools=[refund, lookup, noop], config=AgentConfig(**config))
    recorded: list[tuple[str, str, dict[str, Any]]] = []
    assert agent.audit is not None
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, actor, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return agent, recorded


def last_tool_message(provider: ScriptedProvider) -> Message:
    return [m for m in provider.requests[-1] if m.role == "tool"][-1]


def audit_types(recorded) -> list[str]:
    return [event for event, _, _ in recorded]


async def test_denied_tool_feeds_reason_to_model():
    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, recorded = build(provider, hooks=Hooks(before_tool=[deny_tools("refund", reason="refunds frozen")]))

    result = await agent.run("refund order 7")

    assert result.output == "done"
    assert executed == []
    message = last_tool_message(provider)
    assert message.content == "Error: Tool call denied: refunds frozen"
    assert message.metadata.get("is_error") is True
    (denied,) = [r for r in recorded if r[0] == "tool_denied"]
    assert denied == ("tool_denied", "refund", {"call_id": "refund-0", "stage": "before_tool",
                                                 "reason": "refunds frozen", "stop_run": False})


@pytest.mark.parametrize(("approver_result", "ran", "message", "denied_payload"), [
    (True, True, None, None),
    (False, False, "Error: Tool call denied: approval denied", {"call_id": "refund-0", "timed_out": False, "error": None}),
])
async def test_approval_granted_or_denied(approver_result, ran, message, denied_payload):
    requests: list[ApprovalRequest] = []

    async def approver(req: ApprovalRequest) -> bool:
        requests.append(req)
        return approver_result

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, recorded = build(provider, hooks=Hooks(before_tool=[require_approval("refund", reason="money moves")]),
                            approver=approver)

    await agent.run("refund order 7", ticket="T-1")

    (req,) = requests
    assert (req.tool_name, req.arguments, req.reason, req.context) == ("refund", {"order_id": "7"}, "money moves", {"ticket": "T-1"})
    assert executed == (["refund:7"] if ran else [])
    types = audit_types(recorded)
    assert types.index("approval_requested") < types.index("tool_call")
    if ran:
        assert "approval_granted" in types and "tool_denied" not in types
    else:
        assert last_tool_message(provider).content == message
        assert next(p for e, _, p in recorded if e == "approval_denied") == denied_payload


async def test_approval_timeout_denies():
    async def slow(req: ApprovalRequest) -> bool:
        await asyncio.sleep(5)
        return True

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, recorded = build(provider, hooks=Hooks(before_tool=[require_approval("refund")]), approver=slow,
                            approval_timeout_s=0.05)

    await agent.run("refund")

    assert executed == []
    assert last_tool_message(provider).content == "Error: Tool call denied: approval timed out after 0.05s"
    assert next(p for e, _, p in recorded if e == "approval_denied")["timed_out"] is True


async def test_approver_error_and_missing_approver_deny():
    async def broken(req: ApprovalRequest) -> bool:
        raise RuntimeError("slack down")

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, _ = build(provider, hooks=Hooks(before_tool=[require_approval("refund")]), approver=broken)
    await agent.run("refund")
    assert last_tool_message(provider).content == "Error: Tool call denied: approver error: RuntimeError: slack down"

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, _ = build(provider, hooks=Hooks(before_tool=[require_approval("refund")]))
    await agent.run("refund")
    assert last_tool_message(provider).content == (
        "Error: Tool call denied: approval required but no approver is configured"
    )
    assert executed == []


async def test_first_non_allow_decision_wins_and_later_hooks_do_not_run():
    seen: list[str] = []

    def allow(ctx):
        seen.append("allow")

    async def deny(ctx):
        seen.append("deny")
        return Decision.deny("second hook says no")

    def ask(ctx):
        seen.append("ask")
        return Decision.ask()

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, _ = build(provider, hooks=Hooks(before_tool=[allow, deny, ask]))
    await agent.run("refund")

    assert seen == ["allow", "deny"]
    assert last_tool_message(provider).content == "Error: Tool call denied: second hook says no"


async def test_failing_or_invalid_before_tool_hook_denies():
    def broken(ctx):
        raise KeyError("policy table")

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"}), ("lookup", {"order_id": "8"})), final())
    agent, _ = build(provider, hooks=Hooks(before_tool=[
        lambda ctx: broken(ctx) if ctx.tool_name == "refund" else Decision.replace("nope"),
    ]))
    await agent.run("go")

    tool_messages = [m for m in provider.requests[-1] if m.role == "tool"]
    assert tool_messages[0].content == "Error: Tool call denied: hook error: KeyError: 'policy table'"
    assert tool_messages[1].content == "Error: Tool call denied: invalid decision 'replace' from before_tool hook"
    assert executed == []


async def test_concurrent_approvals_in_one_turn():
    arrived: list[str] = []
    both = asyncio.Event()

    async def approver(req: ApprovalRequest) -> bool:
        arrived.append(req.call_id)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)
        return True

    provider = ScriptedProvider(calls(("refund", {"order_id": "1"}), ("refund", {"order_id": "2"})), final())
    agent, _ = build(provider, hooks=Hooks(before_tool=[require_approval("refund")]), approver=approver)
    await agent.run("two refunds")

    assert sorted(executed) == ["refund:1", "refund:2"]


async def test_stop_run_raises_after_turn_resolves():
    provider = ScriptedProvider(calls(("refund", {"order_id": "7"}), ("lookup", {"order_id": "8"})), final())
    agent, recorded = build(provider, hooks=Hooks(before_tool=[deny_tools("refund", reason="fraud", stop_run=True)]))

    with pytest.raises(RunStoppedByHookError) as info:
        await agent.run("go")

    assert (info.value.stage, info.value.reason, info.value.tool_name) == ("before_tool", "fraud", "refund")
    assert executed == ["lookup:8"]
    assert [r for r in recorded if r[0] == "tool_call" and r[1] == "lookup"]
    assert len(provider.requests) == 1


async def test_after_tool_replace_chains_and_original_never_stored():
    def redact(ctx: ToolResultContext):
        return Decision.replace({**ctx.output, "card": "**** **** **** 1111"}, "pci")

    def tag(ctx: ToolResultContext):
        assert ctx.output["card"].startswith("****")
        return Decision.replace({**ctx.output, "redacted": True}, "tagged")

    provider = ScriptedProvider(calls(("lookup", {"order_id": "8"})), final())
    agent, recorded = build(provider, hooks=Hooks(after_tool=[redact, tag]))
    result = await agent.run("look it up")

    assert '"card": "**** **** **** 1111"' in last_tool_message(provider).content
    assert "4111" not in repr(agent.memory.history())
    assert "4111" not in repr(provider.requests)
    assert result.turns[0].tool_results[0].output == {"order_id": "8", "card": "**** **** **** 1111", "redacted": True}
    assert [p["reason"] for e, _, p in recorded if e == "tool_output_replaced"] == ["pci", "tagged"]


async def test_after_tool_deny_blocks_output():
    provider = ScriptedProvider(calls(("lookup", {"order_id": "8"})), final())
    agent, recorded = build(provider, hooks=Hooks(after_tool=[lambda ctx: Decision.deny("contains PII")]))
    await agent.run("look it up")

    assert last_tool_message(provider).content == "Error: Tool output blocked: contains PII"
    assert "4111" not in repr(agent.memory.history())
    assert next(p for e, _, p in recorded if e == "tool_denied")["stage"] == "after_tool"


async def test_before_llm_deny_stops_the_run():
    contexts: list[LLMCallContext] = []

    def after_hours(ctx: LLMCallContext):
        contexts.append(ctx)
        return Decision.deny("outside business hours")

    provider = ScriptedProvider(final())
    agent, recorded = build(provider, hooks=Hooks(before_llm=[after_hours]))

    with pytest.raises(RunStoppedByHookError) as info:
        await agent.run("hi", user="alice")

    assert info.value.stage == "before_llm"
    assert provider.requests == []
    (ctx,) = contexts
    assert (ctx.turn, ctx.model, ctx.message_count, ctx.run_cost_usd, ctx.context) == (1, "mock", 1, 0.0, {"user": "alice"})
    assert ("llm_call_denied", "agent", {"turn": 1, "reason": "outside business hours"}) in recorded


async def test_allowed_tools_is_enforced_before_hooks():
    seen: list[str] = []
    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final())
    agent, _ = build(provider, allowed_tools=["lookup"], hooks=Hooks(before_tool=[lambda ctx: seen.append(ctx.tool_name)]))
    await agent.run("refund")

    assert seen == []
    assert "not in this agent's allowed_tools" in last_tool_message(provider).content


async def test_stream_applies_the_same_gates():
    async def approver(req: ApprovalRequest) -> bool:
        return False

    provider = ScriptedProvider(calls(("refund", {"order_id": "7"})), final("streamed"))
    agent, _ = build(provider, hooks=Hooks(before_tool=[require_approval("refund")]), approver=approver)

    chunks = [c async for c in agent.stream("refund")]

    assert executed == []
    assert last_tool_message(provider).content == "Error: Tool call denied: approval denied"
    assert agent.last_result is not None and agent.last_result.output == "streamed"
    assert chunks == []


async def test_tool_returning_none_is_not_reported_as_an_error():
    provider = ScriptedProvider(calls(("noop", {})), final())
    agent, _ = build(provider)
    await agent.run("noop")

    message = last_tool_message(provider)
    assert (message.content, message.metadata.get("is_error")) == ("null", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hooks.py -v`
Expected: FAIL — `TypeError: AgentConfig.__init__() got an unexpected keyword argument 'hooks'`

- [ ] **Step 3: Implement**

`agent_kit/agent/agent.py` — `AgentConfig.__init__` gains:

```python
        hooks: Hooks | None = None,
        approver: Approver | None = None,
        approval_timeout_s: float = 300.0,
```

stored as attributes (`from agent_kit.hooks import Approver, Hooks` under `TYPE_CHECKING`), and `_make_loop` passes `hooks=self._config.hooks, approver=self._config.approver, approval_timeout_s=self._config.approval_timeout_s`.

`agent_kit/agent/loop.py`:

Imports: `from agent_kit.exceptions import BudgetExceededError, MaxTurnsExceededError, RunStoppedByHookError`; `from agent_kit.hooks import ApprovalRequest, LLMCallContext, ToolCallContext, ToolResultContext, run_hook` and, under `TYPE_CHECKING`, `from agent_kit.hooks import Approver, Hooks`.

`__init__` gains `hooks: Hooks | None = None, approver: Approver | None = None, approval_timeout_s: float = 300.0`, storing them plus `self._run_id = ""`, `self._context: dict[str, Any] = {}`, `self._pending_stop: RunStoppedByHookError | None = None`.

In `_execute`, right after `run_id = str(uuid.uuid4())`:

```python
        self._run_id = run_id
        self._context = dict(context)
```

Replace the start of each turn:

```python
                    turn_count += 1
                    await self._enforce_budgets()
                    messages = self._memory.history(include_system=False)
                    await self._gate_llm(turn_count, len(messages))
```

Tool execution uses the turn number:

```python
                    tool_results = await asyncio.gather(
                        *(self._run_tool(tc, turn_count) for tc in turn.tool_calls)
                    )
```

Tool message content — report errors by the error field, not by `None` output:

```python
                        output_str = (
                            f"Error: {tool_result.error}"
                            if tool_result.error
                            else json.dumps(tool_result.output, default=str)
                        )
```

After `self._turns.append(turn)` and the reporter call for a tool-using turn:

```python
                    if self._pending_stop is not None:
                        raise self._pending_stop
```

`_run_tool` becomes:

```python
    async def _run_tool(self, tc: ToolCall, turn: int) -> ToolResult:
        """Gate, execute, and filter one tool call inside its span. Never raises."""
        with self._tracer.span(f"tool.{tc.tool_name}", kind=SpanKind.TOOL, tool=tc.tool_name) as tool_span:
            t0 = time.monotonic()

            def failed(error: str) -> ToolResult:
                return ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error=error,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )

            try:
                tool = self._registry.get(tc.tool_name)
            except Exception as exc:
                tool_result = failed(str(exc))
            else:
                denial = await self._gate_tool(tc, turn)
                if denial is not None:
                    tool_result = failed(f"Tool call denied: {denial}")
                else:
                    try:
                        tool_result = await tool(call_id=tc.call_id, **tc.arguments)
                    except Exception as exc:
                        tool_result = failed(str(exc))
                    tool_result = await self._filter_output(tc, turn, tool_result)

            tool_span.set_attribute("duration_ms", tool_result.duration_ms)
            tool_span.set_attribute("success", tool_result.error is None)

        self._tracer.record_tool_call(tc.tool_name, tool_result.duration_ms, tool_result.error is None)
        return tool_result
```

New methods:

```python
    async def _gate_tool(self, tc: ToolCall, turn: int) -> str | None:
        """Run before_tool hooks. Returns a denial reason, or None to execute."""
        if self._hooks is None or not self._hooks.before_tool:
            return None
        ctx = ToolCallContext(
            run_id=self._run_id,
            turn=turn,
            tool_name=tc.tool_name,
            arguments=dict(tc.arguments),
            call_id=tc.call_id,
            context=self._context,
        )
        for hook in self._hooks.before_tool:
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            if decision.kind == "ask":
                return await self._request_approval(ctx, decision.reason)
            if decision.kind == "deny":
                return self._deny_tool(ctx, "before_tool", decision.reason or "denied", decision.stop_run)
            return self._deny_tool(ctx, "before_tool", f"invalid decision '{decision.kind}' from before_tool hook")
        return None

    async def _request_approval(self, ctx: ToolCallContext, reason: str | None) -> str | None:
        self._audit_event("approval_requested", ctx.tool_name, {"call_id": ctx.call_id, "reason": reason})
        if self._approver is None:
            return self._deny_tool(ctx, "before_tool", "approval required but no approver is configured")
        request = ApprovalRequest(
            run_id=ctx.run_id,
            turn=ctx.turn,
            tool_name=ctx.tool_name,
            arguments=dict(ctx.arguments),
            call_id=ctx.call_id,
            reason=reason,
            context=ctx.context,
        )
        try:
            approved = await asyncio.wait_for(self._approver(request), timeout=self._approval_timeout_s)
        except asyncio.TimeoutError:
            self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": True, "error": None})
            return self._deny_tool(ctx, "before_tool", f"approval timed out after {self._approval_timeout_s:g}s")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": False, "error": error})
            return self._deny_tool(ctx, "before_tool", f"approver error: {error}")
        if approved:
            self._audit_event("approval_granted", ctx.tool_name, {"call_id": ctx.call_id})
            return None
        self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": False, "error": None})
        return self._deny_tool(ctx, "before_tool", "approval denied")

    def _deny_tool(self, ctx: ToolCallContext, stage: str, reason: str, stop_run: bool = False) -> str:
        self._audit_event(
            "tool_denied", ctx.tool_name, {"call_id": ctx.call_id, "stage": stage, "reason": reason, "stop_run": stop_run}
        )
        if stop_run and self._pending_stop is None:
            self._pending_stop = RunStoppedByHookError(stage, reason, tool_name=ctx.tool_name)
        return reason

    async def _filter_output(self, tc: ToolCall, turn: int, result: ToolResult) -> ToolResult:
        """Run after_tool hooks; replacements chain, a deny withholds the output."""
        if self._hooks is None or not self._hooks.after_tool:
            return result
        output = result.output
        for hook in self._hooks.after_tool:
            ctx = ToolResultContext(
                run_id=self._run_id,
                turn=turn,
                tool_name=tc.tool_name,
                arguments=dict(tc.arguments),
                call_id=tc.call_id,
                context=self._context,
                output=output,
                error=result.error,
                duration_ms=result.duration_ms,
            )
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            if decision.kind == "replace":
                output = decision.output
                self._audit_event("tool_output_replaced", tc.tool_name, {"call_id": tc.call_id, "reason": decision.reason})
                continue
            if decision.kind == "deny":
                reason = self._deny_tool(ctx, "after_tool", decision.reason or "denied", decision.stop_run)
            else:
                reason = self._deny_tool(ctx, "after_tool", f"invalid decision '{decision.kind}' from after_tool hook")
            return result.model_copy(update={"output": None, "error": f"Tool output blocked: {reason}"})
        return result.model_copy(update={"output": output})

    async def _gate_llm(self, turn: int, message_count: int) -> None:
        if self._hooks is None or not self._hooks.before_llm:
            return
        ctx = LLMCallContext(
            run_id=self._run_id,
            turn=turn,
            model=self._model or self._provider.config.default_model,
            message_count=message_count,
            run_cost_usd=self._run_cost_usd,
            context=self._context,
        )
        for hook in self._hooks.before_llm:
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            reason = (
                decision.reason or "denied"
                if decision.kind == "deny"
                else f"invalid decision '{decision.kind}' from before_llm hook"
            )
            self._audit_event("llm_call_denied", "agent", {"turn": turn, "reason": reason})
            raise RunStoppedByHookError("before_llm", reason)

    def _audit_event(self, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        if self._audit:
            self._audit.append(event_type, actor=actor, payload=payload)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_hooks.py -v && pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit tests/test_hooks.py
git commit -m "feat: hooks and approval gates in the agent loop"
```

---

### Task 3: Example and docs

**Files:**
- Create: `examples/approval_gate.py`
- Modify: `examples/README.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/11-hooks-approval-gates.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: Example** — a runnable script with a terminal approver:

```python
# examples/approval_gate.py
"""
Human approval for risky tools, redaction of tool output, and a hard stop.

The approver here asks on the terminal; in production it's a Slack button, an
internal approvals API, or anything else you can await.
"""

import asyncio
import re

from agent_kit import Agent, AgentConfig, tool
from agent_kit.hooks import ApprovalRequest, Decision, Hooks, ToolResultContext, deny_tools, require_approval
from agent_kit.providers import AnthropicProvider


@tool(description="Look up an order, including the card on file")
async def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "total": 129.00, "card": "4111 1111 1111 1111"}


@tool(description="Refund an order in full")
async def refund_order(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


@tool(description="Close the customer's account permanently")
async def close_account(customer_id: str) -> dict:
    return {"customer_id": customer_id, "closed": True}


def redact_cards(ctx: ToolResultContext) -> Decision | None:
    text = str(ctx.output)
    if re.search(r"\b(?:\d[ -]?){13,19}\b", text):
        return Decision.replace(re.sub(r"\b(?:\d[ -]?){12}(\d{4})\b", r"**** \1", text), reason="card number")
    return None


async def terminal_approver(req: ApprovalRequest) -> bool:
    answer = await asyncio.to_thread(input, f"\nApprove {req.tool_name}({req.arguments})? [y/N] ")
    return answer.strip().lower() == "y"


async def main() -> None:
    agent = Agent(
        AnthropicProvider(),
        tools=[lookup_order, refund_order, close_account],
        config=AgentConfig(
            system_prompt="You are a support agent. Use tools to resolve the request.",
            hooks=Hooks(
                before_tool=[
                    deny_tools("close_account", reason="account closure needs a human", stop_run=True),
                    require_approval("refund_order", reason="refunds move money"),
                ],
                after_tool=[redact_cards],
            ),
            approver=terminal_approver,
            approval_timeout_s=120,
        ),
    )
    result = await agent.run("Order 1042 arrived broken. Look it up and refund it.")
    print("\n" + result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Docs**
  - `examples/README.md`: row for `approval_gate.py`.
  - `README.md`: "Built in" row `Hooks and approval gates | Block tools, require human approval, redact tool output, or stop runs — fail-closed, every decision audited`; a short `## Hooks and approval gates` section after `## Tool allowlist` with the spec's config example, the decision table, and the fail-closed rules.
  - `CHANGELOG.md` `[Unreleased]` → `### Added`: hooks entry; `### Fixed`: tools returning `None` were reported to the model as `Error: None`.
  - Spec 11 status `implemented`; roadmap 2.3 ticked; `PROJECT_INDEX.md` (`hooks.py`, `test_hooks.py`, spec 11, example count).

- [ ] **Step 3: Gates and commit**

Run: `pytest && ruff check agent_kit tests && mypy agent_kit && python -m compileall -q examples/`

```bash
git add examples README.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: hooks and approval gates example and guide"
```
