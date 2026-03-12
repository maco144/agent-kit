"""Minimal example — 8 lines."""

import asyncio
from agent_kit import Agent
from agent_kit.providers import AnthropicProvider


async def main():
    agent = Agent(AnthropicProvider())
    result = await agent.run("Explain the Monty Hall problem in two sentences.")
    print(result.output)
    print(f"Cost: ${result.total_cost_usd:.4f} | Tokens: {result.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
