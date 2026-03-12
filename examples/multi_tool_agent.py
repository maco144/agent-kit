"""Agent with multiple tools and console observability."""

import asyncio
import httpx

from agent_kit import Agent, AgentConfig, tool
from agent_kit.observability import AgentTracer
from agent_kit.providers import AnthropicProvider


@tool(description="Get the current price of a cryptocurrency in USD", idempotent=True)
async def get_crypto_price(symbol: str) -> dict:
    """Fetch current price from CoinGecko (free, no API key)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": symbol.lower(), "vs_currencies": "usd"},
        )
        resp.raise_for_status()
        return resp.json()


@tool(description="Convert an amount from one currency to another", idempotent=True)
async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Uses the Frankfurter API (free, no API key)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.frankfurter.app/latest",
            params={"amount": amount, "from": from_currency, "to": to_currency},
        )
        resp.raise_for_status()
        return resp.json()


async def main():
    agent = Agent(
        provider=AnthropicProvider(),
        tools=[get_crypto_price, convert_currency],
        config=AgentConfig(
            system_prompt="You are a financial assistant. Use the provided tools to answer questions.",
            tracer=AgentTracer(backend="console"),  # structured JSON to stderr
        ),
    )
    result = await agent.run(
        "What is the current Bitcoin price in USD? Also convert 1000 USD to EUR."
    )
    print("\n=== Final Answer ===")
    print(result.output)
    print(f"\nCost: ${result.total_cost_usd:.4f} | Tokens: {result.total_tokens}")
    print(f"Audit root hash: {result.audit_root_hash}")


if __name__ == "__main__":
    asyncio.run(main())
