# Cloud Quickstart

Get your first agent reporting to the cloud in under 5 minutes.

## Prerequisites

- `pip install agent-kit` (v0.2.0+)
- An agent-kit Cloud API key (`akt_live_...`)

---

## Step 1 — Set your API key

```bash
export AGENTKIT_API_KEY=akt_live_your_key_here
```

Or pass it directly in code (useful for secrets managers):

```python
reporter = CloudReporter(api_key=get_secret("agentkit/api-key"), ...)
```

---

## Step 2 — Attach CloudReporter to your agent

```python
import asyncio
from agent_kit import Agent, AgentConfig
from agent_kit.cloud import CloudReporter
from agent_kit.providers import AnthropicProvider

reporter = CloudReporter(
    project="production",        # groups agents in the dashboard
    agent_name="billing-agent",  # identifies this agent in the fleet view
)

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(
        audit_enabled=True,   # enables tamper-evident audit trail storage
        cloud=reporter,
    ),
)

async def main():
    result = await agent.run("Summarise last month's invoices.")
    print(result.output)

asyncio.run(main())
```

That's it. Events are batched and shipped automatically every 5 seconds.

---

## Step 3 — Verify data is arriving

Open the fleet dashboard and check:

- **Summary** — total runs, active runs, cost
- **Agents** — your `billing-agent` should appear with run count and error rate
- **Audit** — if `audit_enabled=True`, the run's Merkle chain is stored and verified

---

## Step 4 — Set up an alert (optional)

Create an alert channel and rule via the API:

```bash
# Create a Slack channel
curl -X POST https://ingest.agentkit.io/v1/alerts/channels \
  -H "Authorization: Bearer $AGENTKIT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ops-slack",
    "type": "slack",
    "config": {"webhook_url": "https://hooks.slack.com/services/..."}
  }'

# Create a circuit-breaker-open alert rule
curl -X POST https://ingest.agentkit.io/v1/alerts/rules \
  -H "Authorization: Bearer $AGENTKIT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CB open",
    "type": "circuit_breaker_open",
    "config": {"agent_name": "*"},
    "channel_ids": ["<channel-id-from-above>"]
  }'
```

You'll receive a Slack message the next time any agent's circuit breaker opens.

---

## Common patterns

### Multiple agents, one reporter per agent

```python
billing_reporter = CloudReporter(project="prod", agent_name="billing-agent")
support_reporter = CloudReporter(project="prod", agent_name="support-agent")

billing_agent = Agent(provider, config=AgentConfig(cloud=billing_reporter))
support_agent = Agent(provider, config=AgentConfig(cloud=support_reporter))
```

### Graceful shutdown (long-running services)

```python
import signal

async def shutdown(reporter: CloudReporter):
    await reporter.close()  # flushes remaining events before exit

# Wire into your signal handler or FastAPI lifespan
```

### Self-hosted backend

Point `CloudReporter` at your own server:

```python
reporter = CloudReporter(
    api_key="akt_live_...",
    base_url="https://agentkit.internal.mycompany.com",
)
```

See [`self-hosting.md`](self-hosting.md) for deployment instructions.

---

## What is NOT sent to the cloud

- LLM prompt text or output content (only a SHA-256 hash of the prompt)
- Tool arguments or return values
- Any data you haven't explicitly included

Set `include_output=False` (the default) to ensure output hashes are also excluded.
