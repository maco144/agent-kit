"""Long-running agent: prompt caching, effort, compaction, and tool-result clearing.

    ANTHROPIC_API_KEY=... python examples/long_running_agent.py
"""

import asyncio

from agent_kit import Agent, AgentConfig, ClearToolResults, Compaction, tool
from agent_kit.providers import AnthropicProvider


@tool(description="Fetch one section of the (simulated) employee handbook by number", idempotent=True)
async def handbook(section: int) -> dict:
    return {"section": section, "text": f"Section {section}: " + "policy detail " * 400}


async def main() -> None:
    agent = Agent(
        AnthropicProvider(default_model="claude-opus-5"),
        tools=[handbook],
        config=AgentConfig(
            system_prompt="You audit handbooks. Read every section you are asked about before answering.",
            max_turns=40,
            effort="medium",
            # Server-side: summarise past 100K input tokens, clear stale tool results past 60K
            compaction=Compaction(trigger_tokens=100_000, instructions="Keep every policy conflict found."),
            clear_tool_results=ClearToolResults(trigger_tokens=60_000, keep=4),
        ),
    )
    result = await agent.run("Read sections 1 through 25 and list every conflicting policy.")
    print(result.output)

    cached = sum(t.cost.cache_read_tokens for t in result.turns)
    print(f"\n{len(result.turns)} turns, ${result.total_cost_usd:.4f}, {cached:,} input tokens read from cache")
    assert agent.audit is not None
    for event in agent.audit.events():
        if event.event_type.startswith("context_"):
            print(" ", event.event_type)


if __name__ == "__main__":
    asyncio.run(main())
