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
