# Project Index: agent-kit

Generated: 2026-03-12

## 📁 Project Structure

```
agent-kit/
├── agent_kit/              # Main package
│   ├── agent/              # Core agent primitives
│   │   ├── agent.py        # Agent + AgentConfig
│   │   └── loop.py         # AgentLoop (turn execution engine)
│   ├── providers/          # LLM provider adapters
│   │   ├── anthropic.py    # AnthropicProvider (default)
│   │   ├── openai.py       # OpenAIProvider (optional dep)
│   │   ├── ollama.py       # OllamaProvider (local models)
│   │   └── base.py         # BaseProvider + ProviderConfig
│   ├── tools/              # Tool system
│   │   ├── base.py         # Tool class + @tool decorator
│   │   └── registry.py     # ToolRegistry (allowlist enforcement)
│   ├── orchestrator/       # Multi-agent coordination
│   │   ├── pipeline.py     # LinearPipeline (sequential)
│   │   └── dag.py          # DAGOrchestrator (parallel DAG)
│   ├── memory/             # Conversation memory backends
│   │   ├── in_memory.py    # InMemoryStore (default, windowed)
│   │   └── sqlite.py       # SQLiteMemory (persistent, thread-safe)
│   ├── reliability/        # Resilience primitives
│   │   ├── retry.py        # RetryPolicy (exponential backoff)
│   │   └── circuit_breaker.py  # CircuitBreaker (CLOSED/OPEN/HALF_OPEN)
│   ├── audit/              # Tamper-evident audit chain
│   │   └── chain.py        # AuditChain (Merkle hash chain)
│   ├── observability/      # Tracing
│   │   └── tracer.py       # AgentTracer (noop/console/OTLP)
│   ├── types.py            # All Pydantic models (no internal imports)
│   ├── exceptions.py       # Custom exceptions
│   └── __init__.py         # Public API surface
├── tests/                  # 9 test files
├── examples/               # 3 example scripts
└── pyproject.toml          # Build config + deps
```

## 🚀 Entry Points

- **Package**: `agent_kit/__init__.py` — exports `Agent`, `AgentConfig`, `Tool`, `tool`, result types
- **Examples**: `examples/hello_agent.py`, `examples/multi_tool_agent.py`, `examples/pipeline_example.py`
- **Tests**: `pytest tests/` (asyncio_mode=auto)

## 📦 Core Modules

### `agent_kit.agent.agent` — Agent
- **Exports**: `Agent`, `AgentConfig`
- Primary user-facing class. Wraps provider + tools + memory + tracer + audit chain.
- Key methods: `run(prompt) -> AgentResult`, `stream(prompt) -> AsyncIterator[str]`, `add_tool(t) -> Agent`

### `agent_kit.types` — Shared Pydantic Models
- **Exports**: `Message`, `ToolCall`, `ToolResult`, `Turn`, `AgentResult`, `PipelineResult`, `RetryPolicyConfig`, `BackoffConfig`, `CircuitBreakerConfig`, `SpanEvent`, `AuditEventRecord`
- No internal imports — foundation of the import graph.

### `agent_kit.providers` — LLM Adapters
- **Default**: `AnthropicProvider` (uses `ANTHROPIC_API_KEY`)
- **Optional**: `OpenAIProvider` (`pip install agent-kit[openai]`), `OllamaProvider` (local)
- All extend `BaseProvider` with `complete()` and `stream()` methods.

### `agent_kit.tools.base` — Tool System
- **Exports**: `Tool`, `@tool(description, idempotent, cost_estimate)`
- `@tool` decorator converts async functions to `Tool` instances with auto-generated JSON schema.

### `agent_kit.orchestrator` — Multi-Agent Coordination
- `LinearPipeline(stages)` — sequential pipeline with `{input}` template substitution
- `DAGOrchestrator(nodes)` — parallel DAG with dependency-based execution; `TaskNode` has `depends_on` + `{upstream:<node_id>}` template syntax

### `agent_kit.memory` — Conversation Memory
- `InMemoryStore(window=50)` — default, in-process sliding window
- `SQLiteMemory(path, window=100)` — persistent, thread-safe, survives restarts; shareable across agents

### `agent_kit.reliability` — Resilience
- `CircuitBreaker` — 3-state (CLOSED→OPEN→HALF_OPEN), raises `CircuitOpenError`
- `RetryPolicy` — exponential backoff with jitter, configurable retryable exception types

### `agent_kit.audit.chain` — Audit Chain
- `AuditChain` — Merkle-linked immutable event log; `verify()` checks integrity, `export_jsonl()` for compliance

### `agent_kit.observability.tracer` — Tracing
- `AgentTracer(backend=None|"console"|"otlp")` — no-op by default; OTLP requires `pip install agent-kit[otel]`

## 🔧 Configuration

- `pyproject.toml` — build (hatchling), deps, pytest (asyncio_mode=auto), ruff, mypy (strict), license: FSL-1.1-Apache-2.0

## 📚 Documentation

- `README.md` — quick start, all features with code examples, provider/observability/audit/circuit breaker docs

## 🧪 Test Coverage

| File | Subject |
|------|---------|
| `test_agent.py` | Agent.run(), AgentConfig |
| `test_tools.py` | @tool decorator, ToolRegistry |
| `test_circuit_breaker.py` | CircuitBreaker state machine |
| `test_audit.py` | AuditChain integrity |
| `test_retry.py` | RetryPolicy backoff |
| `test_pipeline.py` | LinearPipeline |
| `test_dag.py` | DAGOrchestrator + cycle detection |
| `test_sqlite_memory.py` | SQLiteMemory persistence |
| `conftest.py` | Shared fixtures |

## 🔗 Key Dependencies

| Dependency | Version | Purpose |
|-----------|---------|---------|
| `anthropic` | >=0.25 | Anthropic LLM provider (core) |
| `pydantic` | >=2.5 | Type-safe models throughout |
| `httpx` | >=0.27 | HTTP client for Ollama + OpenAI |
| `openai` | >=1.30 | Optional: OpenAI provider |
| `opentelemetry-api/sdk` | >=1.24 | Optional: OTLP tracing |

## 📝 Quick Start

```bash
pip install agent-kit
export ANTHROPIC_API_KEY=sk-ant-...
```

```python
from agent_kit import Agent
from agent_kit.providers import AnthropicProvider

agent = Agent(AnthropicProvider())
result = await agent.run("Hello!")
print(result.output)
```

## ⚠️ Key Exceptions

- `CircuitOpenError` — circuit breaker is OPEN, no LLM calls made
- `MaxTurnsExceededError` — agent hit `max_turns` limit
- `ProviderError` — LLM call failed (retries exhausted)
- `ToolNotAllowedError` — LLM tried to call a tool not in `allowed_tools`
- `DAGCycleError` / `DAGMissingDependencyError` — invalid DAG structure
