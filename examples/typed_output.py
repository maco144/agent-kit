"""Typed results: a validated Pydantic object back from an agent that uses tools.

    ANTHROPIC_API_KEY=... python examples/typed_output.py
"""

import asyncio

from pydantic import BaseModel, Field

from agent_kit import Agent, tool
from agent_kit.providers import AnthropicProvider

PRICES = {"WIDGET": 4.5, "GADGET": 12.0}


class LineItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1)
    unit_price_usd: float


class Quote(BaseModel):
    customer: str
    items: list[LineItem]
    total_usd: float


@tool(description="Look up the unit price in USD for a SKU", idempotent=True)
async def price_for(sku: str) -> dict:
    return {"sku": sku, "unit_price_usd": PRICES.get(sku.upper())}


async def main() -> None:
    agent = Agent(AnthropicProvider(), tools=[price_for])
    result = await agent.run(
        "Quote Acme Corp for 10 WIDGET and 2 GADGET. Look up each price.", output_type=Quote
    )
    quote = result.parsed  # a validated Quote
    assert quote is not None
    print(quote.customer)
    for item in quote.items:
        print(f"  {item.quantity:>3} x {item.sku:<8} ${item.unit_price_usd:.2f}")
    print(f"  total ${quote.total_usd:.2f}   (run cost ${result.total_cost_usd:.4f})")


if __name__ == "__main__":
    asyncio.run(main())
