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

## License

MIT
