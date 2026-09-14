"""Hooks and approval gates."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.exceptions import RunStoppedByHookError
from agent_kit.hooks import (
    ApprovalRequest,
    Decision,
    Hooks,
    LLMCallContext,
    ToolCallContext,
    ToolResultContext,
    allow_only,
    deny_tools,
    require_approval,
    run_hook,
)
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, Turn


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
