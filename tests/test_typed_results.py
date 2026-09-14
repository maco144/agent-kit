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
