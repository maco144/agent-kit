"""Parallel research DAG — 3 agents research concurrently, 1 synthesizes.

Demonstrates:
  - DAGOrchestrator with parallel execution
  - Upstream output injection ({upstream:node_id})
  - Aggregated cost + token tracking across all nodes
  - Wall-clock speedup from concurrency

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/research_dag.py
"""

import asyncio
import time

from agent_kit import Agent, AgentConfig
from agent_kit.orchestrator.dag import DAGOrchestrator, TaskNode
from agent_kit.providers import AnthropicProvider


async def main():
    provider = AnthropicProvider()

    # Three specialist researchers run in parallel
    market = Agent(provider, config=AgentConfig(
        system_prompt="You are a market analyst. Provide 5 concise data points with numbers.",
        max_tokens_per_turn=1024,
    ))
    tech = Agent(provider, config=AgentConfig(
        system_prompt="You are a technical researcher. Explain key technical developments concisely.",
        max_tokens_per_turn=1024,
    ))
    risk = Agent(provider, config=AgentConfig(
        system_prompt="You are a risk analyst. Identify the top 3 risks with likelihood and impact.",
        max_tokens_per_turn=1024,
    ))

    # Synthesis agent waits for all three, then combines
    synthesizer = Agent(provider, config=AgentConfig(
        system_prompt=(
            "You are a senior analyst. Synthesize the research below into a "
            "tight executive briefing (200 words max). Lead with the verdict."
        ),
        max_tokens_per_turn=2048,
    ))

    dag = DAGOrchestrator([
        TaskNode("market", market, "Market analysis of: {input}"),
        TaskNode("tech", tech, "Technical landscape for: {input}"),
        TaskNode("risk", risk, "Risk assessment for: {input}"),
        TaskNode("synthesis", synthesizer,
                 "Synthesize into an executive briefing:\n\n"
                 "## Market\n{upstream:market}\n\n"
                 "## Technical\n{upstream:tech}\n\n"
                 "## Risks\n{upstream:risk}",
                 depends_on=["market", "tech", "risk"]),
    ])

    topic = "autonomous AI agents in enterprise production systems"
    print(f"Researching: {topic}")
    print(f"DAG: market + tech + risk (parallel) → synthesis\n")

    t0 = time.monotonic()
    result = await dag.execute(topic)
    wall_time = time.monotonic() - t0

    print("=" * 60)
    print("EXECUTIVE BRIEFING")
    print("=" * 60)
    print(result.final_output)
    print("=" * 60)
    print(f"\nExecution order: {' → '.join(result.execution_order)}")
    print(f"Wall time: {wall_time:.1f}s")
    print(f"Total cost: ${result.total_cost_usd:.4f}")
    print(f"Total tokens: {result.total_tokens:,}")

    # Show per-node breakdown
    print("\nPer-node breakdown:")
    for node_id, node_result in result.node_results.items():
        print(f"  {node_id:12s}  ${node_result.total_cost_usd:.4f}  {node_result.total_tokens:5,} tokens")


if __name__ == "__main__":
    asyncio.run(main())
