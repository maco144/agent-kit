"""Cloud-monitored agent — full observability with agent-kit Cloud.

Demonstrates:
  - CloudReporter shipping events to the fleet dashboard
  - Console tracing for local debugging
  - Tool calls with real HTTP APIs (no API keys needed)
  - Circuit breaker configuration for resilience
  - Complete cost attribution per run

This is the production setup: your agents run anywhere, and the cloud
dashboard shows you what every agent is doing, what it costs, and whether
it's healthy — across your entire fleet.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...

    # Without cloud (local tracing only):
    python examples/cloud_monitored.py

    # With cloud dashboard:
    export AGENTKIT_API_KEY=akt_live_...
    python examples/cloud_monitored.py
"""

import asyncio
import os

import httpx

from agent_kit import Agent, AgentConfig, tool
from agent_kit.cloud import CloudReporter
from agent_kit.observability import AgentTracer
from agent_kit.providers import AnthropicProvider
from agent_kit.types import CircuitBreakerConfig, RetryPolicyConfig


# --- Tools that hit real APIs (free, no keys needed) ---

@tool(description="Get current weather for a city", idempotent=True)
async def get_weather(city: str) -> dict:
    """Uses wttr.in — free weather API, no key needed."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"https://wttr.in/{city}?format=j1")
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        return {
            "city": city,
            "temp_c": current["temp_C"],
            "temp_f": current["temp_F"],
            "condition": current["weatherDesc"][0]["value"],
            "humidity": current["humidity"],
            "wind_mph": current["windspeedMiles"],
        }


@tool(description="Get the top headline from Hacker News", idempotent=True)
async def top_hn_story() -> dict:
    """Uses the official HN API — free, no key needed."""
    async with httpx.AsyncClient(timeout=10) as client:
        ids = (await client.get("https://hacker-news.firebaseio.com/v0/topstories.json")).json()
        story = (await client.get(f"https://hacker-news.firebaseio.com/v0/item/{ids[0]}.json")).json()
        return {
            "title": story.get("title"),
            "url": story.get("url", ""),
            "score": story.get("score"),
            "by": story.get("by"),
        }


@tool(description="Look up a country's basic facts", idempotent=True)
async def country_facts(country: str) -> dict:
    """Uses restcountries.com — free, no key needed."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"https://restcountries.com/v3.1/name/{country}?fields=name,capital,population,region,languages")
        resp.raise_for_status()
        data = resp.json()[0]
        return {
            "name": data["name"]["common"],
            "capital": data.get("capital", ["N/A"])[0],
            "population": f"{data['population']:,}",
            "region": data["region"],
            "languages": list(data.get("languages", {}).values()),
        }


async def main():
    # Set up cloud reporting if API key is available
    cloud_key = os.environ.get("AGENTKIT_API_KEY")
    reporter = None
    if cloud_key:
        reporter = CloudReporter(
            api_key=cloud_key,
            project="demo",
            agent_name="travel-assistant",
        )
        print("Cloud reporting: ENABLED (events → fleet dashboard)")
    else:
        print("Cloud reporting: OFF (set AGENTKIT_API_KEY to enable)")

    agent = Agent(
        provider=AnthropicProvider(),
        tools=[get_weather, top_hn_story, country_facts],
        config=AgentConfig(
            system_prompt=(
                "You are a knowledgeable assistant. Use your tools to get "
                "real-time data. Be concise and cite your sources."
            ),
            # Production-grade resilience
            retry_policy=RetryPolicyConfig(max_attempts=3),
            circuit_breaker=CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout_s=30,
            ),
            # Console tracing for local debugging
            tracer=AgentTracer(backend="console"),
            cloud=reporter,
        ),
    )

    print(f"\nAgent: {agent}")
    print("=" * 60)

    result = await agent.run(
        "What's the weather in Tokyo right now? Also, what's the top story "
        "on Hacker News? And give me a quick fact about Japan."
    )

    print("\n" + "=" * 60)
    print("RESPONSE")
    print("=" * 60)
    print(result.output)

    print("\n" + "=" * 60)
    print("TELEMETRY")
    print("=" * 60)
    print(f"Turns: {len(result.turns)}")
    print(f"Cost:  ${result.total_cost_usd:.4f}")
    print(f"Tokens: {result.total_tokens:,}")
    print(f"Audit hash: {result.audit_root_hash}")
    print(f"Trace ID: {result.trace_id}")

    if reporter:
        print("\nEvents shipped to agent-kit Cloud. View at your fleet dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
