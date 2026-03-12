"""LinearPipeline — sequential multi-agent pipeline."""

from __future__ import annotations

import time
from typing import Any

from agent_kit.agent.agent import Agent
from agent_kit.types import AgentResult, PipelineResult


class LinearPipeline:
    """
    Runs a sequence of agents in order, feeding each agent's output as the
    next agent's prompt (optionally with a prompt template).

    This covers the 80% case: research → summarize → format, etc.
    For parallel/DAG workflows, use DAGOrchestrator (v0.2.0).

    Usage::

        from agent_kit.orchestrator import LinearPipeline

        pipeline = LinearPipeline([
            (researcher_agent, "Research this topic: {input}"),
            (writer_agent,     "Write a blog post based on: {input}"),
            (editor_agent,     "Edit and improve this draft: {input}"),
        ])
        result = await pipeline.run("The future of AI agents")
        print(result.final_output)
        print(f"Total cost: ${result.total_cost_usd:.4f}")
    """

    def __init__(self, stages: list[tuple[Agent, str] | Agent]) -> None:
        """
        stages: list of (agent, prompt_template) pairs, or just agents.

        prompt_template supports one substitution: {input} → output of previous stage.
        If no template is given, the previous output is passed verbatim.
        """
        self._stages: list[tuple[Agent, str]] = []
        for stage in stages:
            if isinstance(stage, tuple):
                agent, template = stage
                self._stages.append((agent, template))
            else:
                self._stages.append((stage, "{input}"))

    async def run(self, initial_input: str, **context: Any) -> PipelineResult:
        """Execute all stages in sequence and return the aggregated result."""
        t0 = time.monotonic()
        current_input = initial_input
        stage_results: list[AgentResult] = []
        total_cost = 0.0
        total_tokens = 0

        for agent, template in self._stages:
            prompt = template.format(input=current_input)
            result = await agent.run(prompt, **context)
            stage_results.append(result)
            total_cost += result.total_cost_usd
            total_tokens += result.total_tokens
            current_input = result.output  # feed into next stage

        total_ms = int((time.monotonic() - t0) * 1000)
        return PipelineResult(
            stage_results=stage_results,
            final_output=current_input,
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            total_duration_ms=total_ms,
        )

    def __len__(self) -> int:
        return len(self._stages)

    def __repr__(self) -> str:
        agents = [a.__repr__() for a, _ in self._stages]
        return f"LinearPipeline(stages={agents})"
