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

from agent_kit import Agent, AgentConfig, tool
from agent_kit.exceptions import ProviderError, ResponseTruncatedError, UnpricedModelError
from agent_kit.providers.pricing import clear_prices, set_price
from agent_kit.output import OutputSpec
from agent_kit.providers import anthropic as anthropic_provider
from agent_kit.providers.anthropic import AnthropicProvider
from agent_kit.types import ClearToolResults, Compaction, Message, RequestOptions


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


def anthropic_agent(
    responses: list[NS], tools: list[Any], model: str | None = None, **config: Any
) -> tuple[Agent, FakeAnthropic]:
    provider = AnthropicProvider(api_key="test", **({"default_model": model} if model else {}))
    fake = FakeAnthropic(responses)
    provider._client = fake  # type: ignore[assignment]
    return Agent(provider, tools=tools, config=AgentConfig(**config)), fake


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
    content: str | None = None,
    tool_calls: list[NS] | None = None,
    usage: NS | None = None,
    finish_reason: str | None = None,
) -> NS:
    choices = [] if usage else [NS(delta=NS(content=content, tool_calls=tool_calls), finish_reason=finish_reason)]
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
            openai_chunk(finish_reason="tool_calls"),
            openai_chunk(usage=NS(prompt_tokens=10, completion_tokens=5)),
        ]
    )
    second = FakeOpenAIStream(
        [
            openai_chunk("21C "),
            openai_chunk("in Paris", finish_reason="stop"),
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
    fake = FakeOpenAI([FakeOpenAIStream([openai_chunk("{}", finish_reason="stop"), openai_chunk(usage=NS(prompt_tokens=1, completion_tokens=1))])])
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


def test_anthropic_provider_defaults_to_the_current_model():
    assert AnthropicProvider(api_key="test").config.default_model == "claude-opus-5"


# ---------------------------------------------------------------------------
# Truncated and cut responses
# ---------------------------------------------------------------------------

weather_calls: list[str] = []


@tool(description="Weather for a city, recording each call")
async def recorded_weather(city: str) -> dict[str, Any]:
    weather_calls.append(city)
    return {"city": city}


async def test_anthropic_tool_call_cut_at_max_tokens_is_not_executed():
    weather_calls.clear()
    agent, _ = anthropic_agent(
        [anthropic_response([tool_use_block("toolu_1", "recorded_weather", {"ci": "Par"})], "max_tokens")],
        tools=[recorded_weather],
    )
    with pytest.raises(ResponseTruncatedError, match="max_tokens"):
        await agent.run("weather?")
    assert weather_calls == []


async def test_anthropic_answer_cut_at_max_tokens_is_not_a_completed_run():
    agent, _ = anthropic_agent([anthropic_response([text_block("The answer is")], "max_tokens")], tools=[])
    with pytest.raises(ResponseTruncatedError):
        await agent.run("question")


async def test_anthropic_stream_that_ends_without_a_stop_reason_fails():
    provider = AnthropicProvider(api_key="test")
    cut = FakeAnthropicStream(["The answer"], anthropic_response([text_block("The answer")], None))  # type: ignore[arg-type]
    provider._client = NS(messages=NS(stream=lambda **kw: cut))  # type: ignore[assignment]
    with pytest.raises(ProviderError, match="ended before"):
        [c async for c in Agent(provider).stream("question")]


@requires_openai
async def test_openai_tool_call_cut_at_length_is_not_executed():
    weather_calls.clear()
    response = openai_response(None, [openai_tool_call("call_1", "recorded_weather", '{"city": "Pa')])
    response.choices[0].finish_reason = "length"
    agent, _ = openai_agent([response], tools=[recorded_weather])
    with pytest.raises(ResponseTruncatedError, match="max_tokens"):
        await agent.run("weather?")
    assert weather_calls == []


@requires_openai
async def test_openai_stream_cut_mid_tool_call_is_retried_not_executed():
    weather_calls.clear()

    def tool_stream(arguments: str, finish_reason: str | None) -> FakeOpenAIStream:
        delta = NS(index=0, id="call_1", function=NS(name="recorded_weather", arguments=arguments))
        return FakeOpenAIStream([openai_chunk(tool_calls=[delta], finish_reason=finish_reason)])

    agent, fake = openai_agent(
        [tool_stream("", None), tool_stream('{"city": "Paris"}', "tool_calls"),
         FakeOpenAIStream([openai_chunk("done", finish_reason="stop")])],
        tools=[recorded_weather],
    )
    assert "".join([c async for c in agent.stream("weather?")]) == "done"
    assert weather_calls == ["Paris"] and len(fake.calls) == 3


@requires_openai
async def test_openai_stream_cut_after_text_fails():
    agent, _ = openai_agent([FakeOpenAIStream([openai_chunk("The answer")])], tools=[])
    with pytest.raises(ProviderError, match="ended before"):
        [c async for c in agent.stream("question")]


@requires_openai
async def test_openai_stream_cut_at_length_fails():
    agent, _ = openai_agent(
        [FakeOpenAIStream([openai_chunk("The answer is"), openai_chunk(finish_reason="length")])], tools=[]
    )
    with pytest.raises(ResponseTruncatedError):
        [c async for c in agent.stream("question")]


# ---------------------------------------------------------------------------
# Unpriced models under a cost cap
# ---------------------------------------------------------------------------


async def test_unpriced_model_under_a_run_cost_cap_fails_closed():
    agent, _ = anthropic_agent(
        [anthropic_response([text_block("hi")])], tools=[], model="claude-future-9", max_run_cost_usd=1.0
    )
    with pytest.raises(UnpricedModelError, match="claude-future-9"):
        await agent.run("hello")


async def test_unpriced_model_without_a_cap_still_runs_at_zero_cost():
    agent, _ = anthropic_agent([anthropic_response([text_block("hi")])], tools=[], model="claude-future-9")
    result = await agent.run("hello")
    assert (result.output, result.total_cost_usd) == ("hi", 0.0)


async def test_set_price_prices_a_new_model():
    set_price("claude-future-9", 1.0, 2.0)
    try:
        agent, _ = anthropic_agent(
            [anthropic_response([text_block("hi")], input_tokens=1_000_000)],
            tools=[], model="claude-future-9", max_run_cost_usd=5.0,
        )
        result = await agent.run("hello")
    finally:
        clear_prices()
    assert result.total_cost_usd == pytest.approx(1.0 + 5 * 2.0 / 1_000_000)


@requires_openai
async def test_ollama_models_are_free_not_unpriced(caplog):
    from agent_kit.providers.ollama import OllamaProvider

    provider = OllamaProvider(default_model="llama-unlisted")
    fake = FakeOpenAI([openai_response("hi")])
    provider._client = fake  # type: ignore[assignment]
    agent = Agent(provider, config=AgentConfig(max_run_cost_usd=1.0))
    with caplog.at_level(logging.WARNING, logger="agent_kit.providers"):
        result = await agent.run("hello")
    assert (result.output, result.total_cost_usd) == ("hi", 0.0)
    assert "llama-unlisted" not in caplog.text


# ---------------------------------------------------------------------------
# OpenAI cached prompt tokens
# ---------------------------------------------------------------------------


def cached_usage(prompt_tokens: int, cached: int, completion_tokens: int = 0) -> NS:
    return NS(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
              prompt_tokens_details=NS(cached_tokens=cached))


@requires_openai
async def test_openai_cached_prompt_tokens_bill_at_the_cached_rate():
    response = openai_response("hi")
    response.usage = cached_usage(1_000_000, 800_000)
    agent, _ = openai_agent([response], tools=[])

    result = await agent.run("hello")

    (turn,) = result.turns
    assert (turn.cost.input_tokens, turn.cost.cache_read_tokens) == (200_000, 800_000)
    assert turn.cost.total_tokens == 1_000_000
    assert result.total_cost_usd == pytest.approx(0.2 * 2.50 + 0.8 * 1.25)  # gpt-4o cached input is $1.25


@requires_openai
async def test_openai_stream_cached_prompt_tokens_bill_at_the_cached_rate():
    stream = FakeOpenAIStream([openai_chunk("hi", finish_reason="stop"), openai_chunk(usage=cached_usage(1_000_000, 1_000_000))])
    agent, _ = openai_agent([stream], tools=[])

    [c async for c in agent.stream("hello")]

    assert agent.last_result is not None
    assert agent.last_result.total_cost_usd == pytest.approx(1.25)


@requires_openai
async def test_set_price_cached_input_rate():
    set_price("gpt-next", 1.0, 8.0, cached_input_usd_per_mtok=0.1)
    try:
        response = openai_response("hi")
        response.usage = cached_usage(1_000_000, 1_000_000)
        agent, _ = openai_agent([response], tools=[], model="gpt-next")
        result = await agent.run("hello")
    finally:
        clear_prices()
    assert result.total_cost_usd == pytest.approx(0.1)


# OpenAI standard-tier prices, developers.openai.com/api/docs/pricing (2026-09-21): input, cached input, output
OPENAI_PRICES = [
    ("gpt-6-astra", 10.00, 1.00, 50.00),
    ("gpt-5.6-sol", 4.00, 0.40, 20.00),
    ("gpt-5.6-luna", 0.20, 0.02, 1.20),
    ("gpt-5.5", 5.00, 0.50, 30.00),
    ("gpt-5.4-mini", 0.75, 0.075, 4.50),
    ("gpt-5.2", 1.75, 0.175, 14.00),
    ("gpt-5", 1.25, 0.125, 10.00),
    ("gpt-5-mini-2025-08-07", 0.25, 0.025, 2.00),
    ("gpt-4.1", 2.00, 0.50, 8.00),
    ("gpt-4.1-nano", 0.10, 0.025, 0.40),
    ("gpt-4o", 2.50, 1.25, 10.00),
    ("o3", 2.00, 0.50, 8.00),
    ("o3-mini", 1.10, 0.55, 4.40),
    ("o4-mini", 1.10, 0.275, 4.40),
]


@requires_openai
@pytest.mark.parametrize(("model", "input_usd", "cached_usd", "output_usd"), OPENAI_PRICES)
def test_openai_current_prices(model, input_usd, cached_usd, output_usd):
    from agent_kit.providers import openai as openai_provider

    m = 1_000_000
    assert openai_provider._estimate_cost(model, m, 0) == pytest.approx(input_usd)
    assert openai_provider._estimate_cost(model, 0, 0, cached_tokens=m) == pytest.approx(cached_usd)
    assert openai_provider._estimate_cost(model, 0, m) == pytest.approx(output_usd)


@requires_openai
@pytest.mark.parametrize(("model", "input_usd", "output_usd"), [
    ("gpt-5-pro", 15.00, 120.00), ("gpt-5.4-pro", 30.00, 180.00), ("o1-pro", 150.00, 600.00), ("o3-pro", 20.00, 80.00),
])
def test_openai_pro_models_are_not_priced_as_their_base_model(model, input_usd, output_usd):
    from agent_kit.providers import openai as openai_provider

    assert openai_provider._estimate_cost(model, 1_000_000, 1_000_000) == pytest.approx(input_usd + output_usd)
