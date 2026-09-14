"""Provider adapters — assert the exact request payloads sent to each SDK client.

Adapters get a fake client that records call kwargs. No network, no respx
(anthropic>=1.0 uses httpx2, which respx does not intercept).
"""

from __future__ import annotations

import importlib.util
import json
import logging
from types import SimpleNamespace as NS
from typing import Any

import pytest
from pydantic import BaseModel

from agent_kit import Agent, tool
from agent_kit.output import OutputSpec
from agent_kit.providers import anthropic as anthropic_provider
from agent_kit.providers.anthropic import AnthropicProvider
from agent_kit.types import Message


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


requires_openai = pytest.mark.skipif(
    importlib.util.find_spec("openai") is None, reason="openai extra not installed"
)


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


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def openai_tool_call(id: str, name: str, arguments: str) -> NS:
    return NS(id=id, type="function", function=NS(name=name, arguments=arguments))


def openai_response(
    content: str | None,
    tool_calls: list[NS] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
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


def openai_agent(
    responses: list[Any], tools: list[Any], model: str = "gpt-4o"
) -> tuple[Agent, FakeOpenAI]:
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


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


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
        [
            anthropic_response(
                [text_block("hi")],
                input_tokens=100,
                output_tokens=10,
                cache_read_input_tokens=1000,
                cache_creation_input_tokens=200,
            )
        ],
        tools=[],
    )
    agent._provider.config.default_model = "claude-opus-5"  # type: ignore[attr-defined]

    result = await agent.run("hi")

    cost = result.turns[0].cost
    assert (cost.input_tokens, cost.cache_read_tokens, cost.cache_write_tokens) == (100, 1000, 200)
    assert cost.total_tokens == 1310
    assert cost.cost_usd == pytest.approx(
        (100 * 5 + 10 * 25 + 1000 * 5 * 0.1 + 200 * 5 * 1.25) / 1_000_000
    )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


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
        FakeAnthropicStream(
            ["Checking."],
            anthropic_response(
                [text_block("Checking."), tool_use_block("toolu_1", "get_weather", {"city": "Paris"})],
                "tool_use",
            ),
        ),
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
    assert agent.last_result is not None
    assert agent.last_result.output == "21C in Paris"
    assert agent.last_result.total_cost_usd > 0


def openai_chunk(
    content: str | None = None, tool_calls: list[NS] | None = None, usage: NS | None = None
) -> NS:
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
    first = FakeOpenAIStream(
        [
            openai_chunk(
                tool_calls=[NS(index=0, id="call_1", function=NS(name="get_weather", arguments='{"ci'))]
            ),
            openai_chunk(tool_calls=[NS(index=0, id=None, function=NS(name=None, arguments='ty": "Paris"}'))]),
            openai_chunk(usage=NS(prompt_tokens=10, completion_tokens=5)),
        ]
    )
    second = FakeOpenAIStream(
        [
            openai_chunk("21C "),
            openai_chunk("in Paris"),
            openai_chunk(usage=NS(prompt_tokens=20, completion_tokens=4)),
        ]
    )
    agent, fake = openai_agent([first, second], tools=[get_weather])

    chunks = [c async for c in agent.stream("weather in Paris?")]

    assert "".join(chunks) == "21C in Paris"
    assert fake.calls[0]["stream"] is True
    assert fake.calls[0]["stream_options"] == {"include_usage": True}
    assert fake.calls[1]["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Paris"}'
    assert agent.last_result is not None
    assert agent.last_result.total_tokens == 39


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
    # Ollama's format grammar covers the whole reply, so the model can't also call tools
    assert OllamaProvider().structured_output_with_tools is False
