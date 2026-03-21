# agent-kit

**Production-ready framework for building AI agents. Type-safe. Observable. Circuit-broken.**

```bash
pip install agent-kit
```

---

## Why agent-kit?

LangChain, CrewAI, and AutoGen solve agent *coordination*. agent-kit solves agent *infrastructure*:

| Feature | agent-kit | LangChain | CrewAI | AutoGen |
|---------|:---------:|:---------:|:------:|:-------:|
| Circuit breakers | ✅ | ❌ | ❌ | ❌ |
| Retry with idempotency | ✅ | partial | ❌ | ❌ |
| Tamper-evident audit trail | ✅ | ❌ | ❌ | ❌ |
| Type-safe throughout (Pydantic v2) | ✅ | partial | partial | ❌ |
| Built-in cost tracking | ✅ | ❌ | ❌ | ❌ |
| Zero mandatory deps beyond anthropic | ✅ | ❌ | ❌ | ❌ |
| OpenTelemetry-compatible tracing | ✅ | ❌ | ❌ | ❌ |

---

## Quick start

### 8-line minimal agent

```python
import asyncio
from agent_kit import Agent
from agent_kit.providers import AnthropicProvider

async def main():
    agent = Agent(AnthropicProvider())
    result = await agent.run("Explain the Monty Hall problem in two sentences.")
    print(result.output)
    print(f"Cost: ${result.total_cost_usd:.4f}")

asyncio.run(main())
```

### Add a tool (6 more lines)

```python
import httpx
from agent_kit import tool

@tool(description="Fetch the current price of a crypto asset", idempotent=True)
async def get_price(symbol: str) -> dict:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd")).json()

agent = Agent(AnthropicProvider(), tools=[get_price])
result = await agent.run("What is the current Bitcoin price?")
```

### Production hardening in config

```python
from agent_kit import Agent, AgentConfig
from agent_kit.types import RetryPolicyConfig, CircuitBreakerConfig

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(
        system_prompt="You are a helpful assistant.",
        retry_policy=RetryPolicyConfig(max_attempts=3),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5),
        audit_enabled=True,    # tamper-evident Merkle audit chain
    ),
)
result = await agent.run("Summarize the latest AI research.")
print(f"Audit root hash: {result.audit_root_hash}")  # verify integrity later
```

### Multi-stage pipeline

```python
from agent_kit.orchestrator import LinearPipeline

pipeline = LinearPipeline([
    (researcher, "Research this topic: {input}"),
    (writer,     "Write a blog post from these facts: {input}"),
    (editor,     "Polish and tighten this draft: {input}"),
])
result = await pipeline.run("The future of AI agent frameworks")
print(result.final_output)
print(f"Total cost: ${result.total_cost_usd:.4f} across {len(result.stage_results)} stages")
```

---

## Demos

### Parallel research DAG

Three specialist agents research concurrently, then a fourth synthesizes. The DAG handles dependency resolution and runs independent nodes in parallel.

```python
# examples/research_dag.py
dag = DAGOrchestrator([
    TaskNode("market", market_agent, "Market analysis of: {input}"),
    TaskNode("tech",   tech_agent,   "Technical landscape for: {input}"),
    TaskNode("risk",   risk_agent,   "Risk assessment for: {input}"),
    TaskNode("synthesis", synthesizer,
             "Synthesize:\n{upstream:market}\n{upstream:tech}\n{upstream:risk}",
             depends_on=["market", "tech", "risk"]),
])
result = await dag.execute("autonomous AI agents in enterprise production")
```

```
$ python examples/research_dag.py
Researching: autonomous AI agents in enterprise production systems
DAG: market + tech + risk (parallel) → synthesis

============================================================
EXECUTIVE BRIEFING
============================================================
Verdict: Enterprise AI agent adoption is accelerating but premature
for mission-critical workflows without proper guardrails.

The market is projected to reach $47B by 2028 (34% CAGR), with
75% of Fortune 500 companies running pilot programs. Technically,
the shift from chain-of-thought to tool-using agents with circuit
breakers and audit trails has made production deployment viable.
However, three risks dominate: regulatory uncertainty around
autonomous decision-making, cost blowouts from retry cascades,
and the "ghost agent" problem — orphaned processes consuming
resources with no human oversight.

Recommendation: Deploy with hard cost ceilings, tamper-evident
audit logging, and circuit breakers on all provider calls.
============================================================

Execution order: market → tech → risk → synthesis
Wall time: 4.2s
Total cost: $0.0183
Total tokens: 4,271

Per-node breakdown:
  market        $0.0041    982 tokens
  tech          $0.0044  1,053 tokens
  risk          $0.0038    891 tokens
  synthesis     $0.0060  1,345 tokens
```

### Production-safe agent with audit trail

Tool allowlisting, Merkle audit chain, and compliance export. The agent has `delete_employee` registered but it's blocked — only `check_pto` and `submit_pto` are allowed.

```python
# examples/safe_agent.py
agent = Agent(
    provider=AnthropicProvider(),
    tools=[check_pto, submit_pto, delete_employee],
    config=AgentConfig(
        system_prompt="You are an HR assistant.",
        allowed_tools=["check_pto", "submit_pto"],  # delete_employee → blocked
    ),
)
result = await agent.run("I'm employee E003. Can I take 5 days off for vacation?")
```

```
$ python examples/safe_agent.py
Running agent with tool allowlist: [check_pto, submit_pto]
(delete_employee is registered but BLOCKED)

============================================================
RESPONSE
============================================================
I checked your PTO balance, Carol. You have 17 days remaining
(20 allocated, 3 used). I've submitted your request for 5 days
for your family vacation.

Your confirmation number is PTO-2026-0042.

============================================================
AUDIT TRAIL
============================================================
Events recorded: 6
Root hash: a1c9f3e7d204b85610ef38c7...
Chain integrity: VERIFIED

Cost: $0.0052 | Tokens: 1,203

Event log:
  [0] agent_start                     actor=run-8f3a2c...
  [1] llm_complete                    actor=anthropic
  [2] tool_call                       actor=check_pto
  [3] llm_complete                    actor=anthropic
  [4] tool_call                       actor=submit_pto
  [5] agent_complete                  actor=run-8f3a2c...

JSONL export: 6 records, 1,847 bytes
First record: agent_start
Last record:  agent_complete
```

### Cloud-monitored agent with live tools

Real HTTP tools (no API keys needed), circuit breaker config, console tracing, and optional cloud reporting for fleet-wide visibility.

```python
# examples/cloud_monitored.py
agent = Agent(
    provider=AnthropicProvider(),
    tools=[get_weather, top_hn_story, country_facts],
    config=AgentConfig(
        retry_policy=RetryPolicyConfig(max_attempts=3),
        circuit_breaker=CircuitBreakerConfig(failure_threshold=5, recovery_timeout_s=30),
        tracer=AgentTracer(backend="console"),
        cloud=reporter,  # optional — ships events to fleet dashboard
    ),
)
```

```
$ python examples/cloud_monitored.py
Cloud reporting: ENABLED (events → fleet dashboard)

Agent: Agent(provider='anthropic', tools=['get_weather', 'top_hn_story', 'country_facts'], model='claude-sonnet-4-20250514')
============================================================
{"span":"agent.run","kind":"agent","duration_ms":3841,"attributes":{"run_id":"e9f1..."}}
{"span":"llm.complete","kind":"llm","duration_ms":1203,"attributes":{"input_tokens":847,"output_tokens":52,"cost_usd":0.0031}}
{"tool_call":"get_weather","duration_ms":340,"success":true}
{"tool_call":"top_hn_story","duration_ms":289,"success":true}
{"tool_call":"country_facts","duration_ms":195,"success":true}
{"cost_event":true,"tokens":1847,"model":"claude-sonnet-4-20250514","usd":0.0058,"cumulative_usd":0.0089}
{"span":"llm.complete","kind":"llm","duration_ms":1814,"attributes":{"input_tokens":1203,"output_tokens":644,"cost_usd":0.0058}}

============================================================
RESPONSE
============================================================
Here's what I found:

🌤 Tokyo: 18°C (64°F), partly cloudy, 62% humidity, wind 8 mph

📰 Top HN story: "Show HN: I built a self-healing Kubernetes operator"
   by tobiaswright — 847 points

🇯🇵 Japan: Capital Tokyo, population 125,681,593, East Asia
   Languages: Japanese

============================================================
TELEMETRY
============================================================
Turns: 2
Cost:  $0.0089
Tokens: 1,847
Audit hash: f7a2e19c830d...
Trace ID: 4c91b7a3-8e2f-4d1a-b3c9-7f2e1a3d5b8c

Events shipped to agent-kit Cloud. View at your fleet dashboard.
```

---

## Providers

```python
from agent_kit.providers import AnthropicProvider

# Anthropic (default — uses ANTHROPIC_API_KEY env var)
provider = AnthropicProvider()
provider = AnthropicProvider(api_key="sk-ant-...", default_model="claude-3-haiku-20240307")

# OpenAI (requires pip install agent-kit[openai])
from agent_kit.providers.openai import OpenAIProvider
provider = OpenAIProvider()

# Ollama (local models)
from agent_kit.providers.ollama import OllamaProvider
provider = OllamaProvider(default_model="llama3.2")

# Any OpenAI-compatible endpoint
from agent_kit.providers.openai import OpenAIProvider
provider = OpenAIProvider(base_url="http://localhost:11434/v1", api_key="none", default_model="mistral")
```

---

## Observability

```python
from agent_kit.observability import AgentTracer

# Zero dependencies — no-op (default)
tracer = AgentTracer()

# Structured JSON to stderr — no extra deps
tracer = AgentTracer(backend="console")

# Full OpenTelemetry (requires pip install agent-kit[otel])
tracer = AgentTracer(backend="otlp", service_name="my-agent", endpoint="http://localhost:4317")

agent = Agent(provider, config=AgentConfig(tracer=tracer))
```

---

## Audit chain

Every agent run produces a tamper-evident Merkle audit chain:

```python
agent = Agent(provider, config=AgentConfig(audit_enabled=True))
result = await agent.run("Do something important.")

# Verify chain integrity
assert agent.audit.verify()
print(f"Root hash: {result.audit_root_hash}")

# Export to JSONL for compliance storage
with open("audit.jsonl", "w") as f:
    f.write(agent.audit.export_jsonl())
```

---

## Circuit breaker

The circuit breaker wraps every LLM provider call. It transitions:
- **CLOSED** → normal operation
- **OPEN** → failing fast (no LLM calls; raises `CircuitOpenError`)
- **HALF_OPEN** → probing recovery after `recovery_timeout_s`

```python
from agent_kit.types import CircuitBreakerConfig

config = AgentConfig(
    circuit_breaker=CircuitBreakerConfig(
        failure_threshold=5,      # open after 5 consecutive failures
        recovery_timeout_s=60.0,  # attempt recovery after 60s
        success_threshold=2,      # 2 successes in half-open → closed
    )
)
```

---

## Reliability

```python
from agent_kit.types import RetryPolicyConfig, BackoffConfig

policy = RetryPolicyConfig(
    max_attempts=3,
    backoff=BackoffConfig(
        initial_delay_s=1.0,
        multiplier=2.0,
        max_delay_s=30.0,
        jitter=True,
    ),
    retryable_on=["ProviderError", "httpx.TimeoutException"],
)
```

---

## Tool allowlist

Lock down which tools an agent can call — enforced at the registry level, not just advisory:

```python
agent = Agent(
    provider,
    tools=[web_search, read_file, delete_file, send_email],
    config=AgentConfig(allowed_tools=["web_search", "read_file"]),
    # delete_file and send_email raise ToolNotAllowedError if the LLM tries to call them
)
```

---

## Installation

```bash
# Core (Anthropic only)
pip install agent-kit

# With OpenAI support
pip install agent-kit[openai]

# With OpenTelemetry
pip install agent-kit[otel]

# Everything
pip install agent-kit[all]
```

---

## agent-kit Cloud

Connect any agent to the **agent-kit Cloud** backend — a hosted service that gives you a fleet dashboard, audit trail storage, alerting, and SLA-backed support without running any infrastructure yourself.

```python
from agent_kit.cloud import CloudReporter

reporter = CloudReporter(
    api_key="akt_live_...",      # or set AGENTKIT_API_KEY env var
    project="production",
    agent_name="billing-assistant",
)

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(cloud=reporter),
)
result = await agent.run("Process this invoice.")
# Events are batched and shipped automatically — no await needed.
```

`CloudReporter` is **fire-and-forget**: network errors are logged at `DEBUG` level and never propagate to your agent. Performance is completely unaffected by cloud connectivity.

### What gets reported

| Event | When |
|---|---|
| `run_start` | Agent loop begins |
| `turn_complete` | Each LLM response + tool calls |
| `run_complete` | Successful finish (includes audit root hash) |
| `run_error` | Unhandled exception |
| `circuit_state_change` | CB opens / half-opens / closes |
| `audit_flush` | Full Merkle chain (if `audit_enabled=True`) |

### CloudReporter options

```python
CloudReporter(
    api_key="akt_live_...",
    project="production",
    agent_name="billing-assistant",
    flush_interval_s=5.0,    # how often to batch-send (default 5s)
    max_queue_size=1000,     # drop events if queue exceeds this
    include_output=False,    # never ships LLM output text to cloud
)
```

See [`docs/cloud-quickstart.md`](docs/cloud-quickstart.md) to get started, or [`docs/self-hosting.md`](docs/self-hosting.md) to run the backend yourself.

---

## License

[FSL-1.1-Apache-2.0](https://fsl.software). Source-available for use in non-competing products. Converts to Apache 2.0 two years after each release.
