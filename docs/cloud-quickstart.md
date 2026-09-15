# Cloud Quickstart

Get your first agent reporting to the cloud in under 5 minutes.

## Prerequisites

- `pip install agent-kit-ai` (v0.4.0+)
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

## Stop runaway spend

Cap each run locally and enforce fleet budgets from agent-kit Cloud:

```python
reporter = CloudReporter(project="support", agent_name="support-bot")

agent = Agent(
    AnthropicProvider(),
    config=AgentConfig(
        cloud=reporter,
        max_run_cost_usd=2.00,   # per run, enforced locally
        enforce_budgets=True,    # fleet budgets defined in agent-kit Cloud
    ),
)
```

Create the budget once:

```bash
curl -X POST https://ingest.agentkit.io/v1/budgets \
  -H "Authorization: Bearer $AGENTKIT_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "support daily", "period": "daily", "limit_usd": 200, "agent_name": "support-bot"}'
```

Before every model call the agent checks both. When a ceiling is reached it raises `BudgetExceededError` (`scope` is `"run"` or `"budget"`, with `limit_usd`, `spent_usd`, `budget_name`, `resets_at`) instead of calling the model, and records a `budget_exceeded` audit event. Add a `budget_exceeded` alert rule to hear about it.

- **Overshoot is bounded, not zero.** A per-run cap can be passed by the one call that crosses it. A fleet budget can be passed by one call per process, plus other processes' spend within the 30-second status refresh (`reporter.budget_guard(refresh_interval_s=...)`).
- **Fails open.** If budget status can't be fetched, agents keep running. Use `reporter.budget_guard(fail_closed=True)` to refuse instead.
- **Claude Agent SDK:** `ClaudeAgentObserver(reporter, max_run_cost_usd=2.0, enforce_budgets=True)` — the per-run cap becomes the SDK's own `max_budget_usd`; a tripped budget stops the agent at its next tool or subagent call.
- **OpenAI Agents SDK:** `Runner.run(agent, input, hooks=AgentKitRunHooks(reporter, max_run_cost_usd=2.0))` — stops before the next model call. Give the reporter an `agent_name` so budgets match the runs it reports.

---

## Evidence for auditors

```bash
# Export a signed evidence bundle for September
curl -o evidence.zip -H "Authorization: Bearer $AGENTKIT_API_KEY" \
  "https://ingest.agentkit.io/v1/compliance/export?from=2026-09-01T00:00:00&to=2026-10-01T00:00:00"

# Anyone can verify it offline against agent-kit's published keys
pip install "agent-kit-ai[compliance]"
agent-kit verify evidence.zip --keys-url https://ingest.agentkit.io/.well-known/agentkit-signing-keys
```

The bundle holds every audit chain link for the period, a re-verification report, retention policy and legal holds in force, and signed receipts for anything purged. `agent-kit verify` checks the signature, every file hash, every chain, and every receipt, and exits non-zero on any failure. It supports record-keeping obligations such as EU AI Act Article 12 and SOC 2 evidence requests; it is not a certification.

```bash
# Enterprise: keep audit data for 7 years
curl -X PUT -H "Authorization: Bearer $AGENTKIT_API_KEY" -H "Content-Type: application/json" \
  https://ingest.agentkit.io/v1/compliance/retention -d '{"audit_retention_days": 2555}'

# Freeze a project's audit trail during an investigation
curl -X POST -H "Authorization: Bearer $AGENTKIT_API_KEY" -H "Content-Type: application/json" \
  https://ingest.agentkit.io/v1/compliance/holds -d '{"project": "claims", "reason": "case #4471"}'
```

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

## Other harnesses

Already running the Claude Agent SDK or the OpenAI Agents SDK? Report those runs without
switching:

```python
# Claude Agent SDK — pip install agent-kit-ai[claude-agent-sdk]
from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver

observer = ClaudeAgentObserver(CloudReporter(project="support"))
options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Grep"]))
async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
    ...  # messages arrive unchanged

# OpenAI Agents SDK — pip install agent-kit-ai[openai-agents]
from agent_kit.integrations.openai_agents import AgentKitTraceProcessor

add_trace_processor(AgentKitTraceProcessor(CloudReporter(project="support")))
```

- Each Claude `observe()` call and each OpenAI trace is one run. Runs carry `harness` (`claude-agent-sdk` / `openai-agents`) in their `run_start` payload.
- Claude cost is the SDK's own `total_cost_usd`; OpenAI cost comes from agent-kit's pricing tables.
- Claude hooks without `observe()` record nothing — cost and completion come from the message stream.
- The OpenAI processor records nothing while Agents SDK tracing is disabled. OpenAI's own trace exporter keeps running alongside it.
- Adapters only observe: they never block tools, change outputs, or raise into your agent.

---

## Any OpenTelemetry-instrumented agent

If your framework already emits OpenTelemetry traces — in any language — point its OTLP/HTTP exporter at agent-kit. No agent-kit SDK needed:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.agentkit.io
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer akt_live_..."
export OTEL_RESOURCE_ATTRIBUTES="agentkit.project=support"
```

- Understood conventions: OpenTelemetry GenAI semantic conventions (`gen_ai.*`) and OpenInference (`openinference.span.kind`). Other spans in the same traces are ignored.
- Each trace becomes one run with turns, tool calls, tokens, cost, and failures in the fleet dashboard.
- Runs complete when the trace's root span arrives, or after 5 minutes without new spans (for exporters that drop non-GenAI spans).
- These runs show `chain_origin: "ingest"`: the audit chain is built when spans arrive, so it proves nothing changed after ingest — not what happened before export. Use the SDK or a harness adapter for source-side tamper evidence.
- Prompt, completion, and tool content attributes are never stored.

---

## What is NOT sent to the cloud

- LLM prompt text or output content (only a SHA-256 hash of the prompt)
- Tool arguments or return values
- Any data you haven't explicitly included

Set `include_output=False` (the default) to ensure output hashes are also excluded.
