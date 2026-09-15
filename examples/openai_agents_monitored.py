"""
OpenAI Agents SDK run reported to agent-kit Cloud.

Requires: pip install agent-kit-ai[openai-agents], OPENAI_API_KEY, and AGENTKIT_API_KEY.
"""

import asyncio

from agents import Agent, Runner, add_trace_processor, function_tool

from agent_kit.cloud import CloudReporter
from agent_kit.integrations.openai_agents import AgentKitTraceProcessor


@function_tool
def order_status(order_id: str) -> str:
    """Look up an order's shipping status."""
    return f"Order {order_id} shipped yesterday."


async def main() -> None:
    reporter = CloudReporter(project="demo")
    add_trace_processor(AgentKitTraceProcessor(reporter))

    agent = Agent(name="support", instructions="Answer order questions.", tools=[order_status])
    result = await Runner.run(agent, "Where is order 1042?")
    print(result.final_output)

    await reporter.close()


if __name__ == "__main__":
    asyncio.run(main())
