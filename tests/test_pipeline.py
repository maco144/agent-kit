"""Tests for LinearPipeline."""

from __future__ import annotations

import pytest

from agent_kit import Agent
from agent_kit.orchestrator import LinearPipeline


@pytest.mark.asyncio
async def test_pipeline_passes_output_to_next_stage(mock_provider_factory):
    provider = mock_provider_factory(["Stage 1 output.", "Stage 2 output."])
    agent1 = Agent(provider)
    agent2 = Agent(provider)

    pipeline = LinearPipeline([agent1, agent2])
    result = await pipeline.run("Initial input")

    assert result.final_output == "Stage 2 output."
    assert len(result.stage_results) == 2


@pytest.mark.asyncio
async def test_pipeline_template_interpolation(mock_provider_factory):
    captured = []

    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import Turn, Message, CostSummary

    class RecordingProvider:
        config = ProviderConfig(default_model="mock")
        def name(self): return "mock"
        async def complete(self, messages, **kw):
            captured.append(messages[-1].content)
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content="done"),
                cost=CostSummary(total_tokens=5),
            )
        async def stream(self, *a, **kw): yield "done"

    provider = RecordingProvider()
    agent = Agent(provider)
    pipeline = LinearPipeline([(agent, "Summarize: {input}")])
    await pipeline.run("some text")

    assert "Summarize: some text" in captured


@pytest.mark.asyncio
async def test_pipeline_accumulates_cost(mock_provider_factory):
    provider = mock_provider_factory(["r1", "r2", "r3"])
    agents = [Agent(provider) for _ in range(3)]
    pipeline = LinearPipeline(agents)
    result = await pipeline.run("start")

    # 3 turns × 0.0001 USD each (from MockProvider)
    assert result.total_cost_usd == pytest.approx(0.0003)
    assert result.total_tokens == 45  # 3 × 15


@pytest.mark.asyncio
async def test_pipeline_len(mock_provider_factory):
    provider = mock_provider_factory(["r"])
    agents = [Agent(provider) for _ in range(4)]
    pipeline = LinearPipeline(agents)
    assert len(pipeline) == 4
