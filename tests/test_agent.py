"""Tests for Agent and AgentLoop."""

from __future__ import annotations

import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.exceptions import MaxTurnsExceededError
from agent_kit.types import CircuitBreakerConfig, RetryPolicyConfig


@pytest.mark.asyncio
async def test_agent_basic_run(mock_provider):
    agent = Agent(mock_provider)
    result = await agent.run("Hello, world!")

    assert result.output == "Mock response."
    assert result.total_tokens == 15
    assert result.total_cost_usd == pytest.approx(0.0001)
    assert len(result.turns) == 1


@pytest.mark.asyncio
async def test_agent_audit_chain(mock_provider):
    agent = Agent(mock_provider, config=AgentConfig(audit_enabled=True))
    result = await agent.run("Test audit")

    assert result.audit_root_hash is not None
    assert agent.audit is not None
    assert len(agent.audit) >= 2  # agent_start + llm_complete + agent_complete
    assert agent.audit.verify()


@pytest.mark.asyncio
async def test_agent_no_audit(mock_provider):
    agent = Agent(mock_provider, config=AgentConfig(audit_enabled=False))
    result = await agent.run("No audit")
    assert result.audit_root_hash is None


@pytest.mark.asyncio
async def test_agent_with_tool(mock_provider_factory):
    call_log = []

    @tool(description="A test tool")
    def greet(name: str) -> str:
        call_log.append(name)
        return f"Hello, {name}!"

    from agent_kit.types import ToolCall
    from agent_kit.types import Turn, Message, CostSummary
    from agent_kit.providers.base import ProviderConfig

    class ToolCallingProvider:
        """Provider that requests a tool call on first turn, then returns final answer."""
        config = ProviderConfig(default_model="mock")
        _step = 0

        def name(self): return "mock"

        async def complete(self, messages, model=None, tools=None, system=None, max_tokens=4096, **kw):
            self._step += 1
            if self._step == 1:
                # First call: request the tool
                return Turn(
                    messages_in=messages,
                    message_out=Message(role="assistant", content=""),
                    tool_calls=[ToolCall(tool_name="greet", arguments={"name": "World"}, call_id="tc1")],
                    cost=CostSummary(total_tokens=10, cost_usd=0.0),
                )
            else:
                # Second call: final answer after seeing tool result
                return Turn(
                    messages_in=messages,
                    message_out=Message(role="assistant", content="Done!"),
                    cost=CostSummary(total_tokens=5, cost_usd=0.0),
                )

        async def stream(self, *a, **kw):
            yield "Done!"

    provider = ToolCallingProvider()
    agent = Agent(provider, tools=[greet])
    result = await agent.run("Greet the world")

    assert result.output == "Done!"
    assert call_log == ["World"]


@pytest.mark.asyncio
async def test_agent_max_turns_exceeded(mock_provider_factory):
    """Agent should raise MaxTurnsExceededError if it keeps calling tools forever."""
    from agent_kit.types import ToolCall, Turn, Message, CostSummary
    from agent_kit.providers.base import ProviderConfig

    class InfiniteToolProvider:
        config = ProviderConfig(default_model="mock")
        def name(self): return "mock"
        async def complete(self, messages, **kw):
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content=""),
                tool_calls=[ToolCall(tool_name="loop_tool", arguments={}, call_id="x")],
                cost=CostSummary(total_tokens=1),
            )
        async def stream(self, *a, **kw): yield ""

    @tool(description="loops forever")
    def loop_tool() -> str:
        return "looping"

    agent = Agent(
        InfiniteToolProvider(),
        tools=[loop_tool],
        config=AgentConfig(max_turns=3),
    )
    with pytest.raises(MaxTurnsExceededError):
        await agent.run("loop")


@pytest.mark.asyncio
async def test_agent_memory_persists_across_runs(mock_provider_factory):
    provider = mock_provider_factory(["First.", "Second."])
    from agent_kit.memory.in_memory import InMemoryStore
    shared_mem = InMemoryStore()

    agent1 = Agent(provider, memory=shared_mem)
    await agent1.run("Turn 1")

    agent2 = Agent(provider, memory=shared_mem)
    await agent2.run("Turn 2")

    # Memory should have both turns
    history = shared_mem.history()
    contents = [m.content for m in history]
    assert "Turn 1" in contents
    assert "Turn 2" in contents
