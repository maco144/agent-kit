# Project Index: agent-kit

Generated: 2026-09-08

## 📁 Project Structure

```
agent-kit/
├── agent_kit/              # SDK package (pip install agent-kit)
│   ├── agent/              # Core agent primitives
│   │   ├── agent.py        # Agent + AgentConfig
│   │   └── loop.py         # AgentLoop (turn execution engine)
│   ├── cloud/              # Cloud reporting SDK module
│   │   ├── models.py       # CloudEvent, EventType
│   │   └── reporter.py     # CloudReporter (batched, fire-and-forget)
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
├── server/                 # agent-kit Cloud backend (FastAPI)
│   ├── app/
│   │   ├── main.py         # FastAPI app + lifespan (alert worker)
│   │   ├── auth.py         # Bearer token auth → Organization
│   │   ├── database.py     # SQLAlchemy async engine + SessionLocal
│   │   ├── models.py       # ORM models (all tables)
│   │   ├── schemas.py      # Pydantic request/response schemas
│   │   ├── audit_chain.py  # Server-side Merkle chain verifier
│   │   ├── alerting/
│   │   │   ├── evaluator.py  # Alert rule evaluation + firing
│   │   │   └── dispatch.py   # Notification dispatch (email/Slack/PD/webhook)
│   │   └── routers/
│   │       ├── ingest.py   # POST /v1/events
│   │       ├── metrics.py  # GET /v1/metrics/*
│   │       ├── alerts.py   # CRUD /v1/alerts/*
│   │       ├── audit.py    # GET /v1/audit/*
│   │       └── support.py  # GET /v1/support/*
│   ├── migrations/         # Alembic versions 001–004
│   ├── tests/              # 4 server test files
│   └── pyproject.toml      # agentkit-cloud-server v0.1.0
├── tests/                  # SDK tests (10 test files)
├── examples/               # 3 example scripts
├── docs/                   # 4 cloud documentation files
├── specs/                  # 5 platform spec files
└── pyproject.toml          # SDK build config + deps
```

## 🚀 Entry Points

- **SDK Package**: `agent_kit/__init__.py` — exports `Agent`, `AgentConfig`, `Tool`, `tool`, result types
- **Cloud Server**: `server/app/main.py` — FastAPI app (`uvicorn app.main:app`)
- **Examples**: `examples/hello_agent.py`, `examples/multi_tool_agent.py`, `examples/pipeline_example.py`
- **SDK Tests**: `pytest tests/` (asyncio_mode=auto)
- **Server Tests**: `cd server && pytest tests/` (asyncio_mode=auto)

## 📦 Core Modules — SDK

### `agent_kit.agent.agent` — Agent
- **Exports**: `Agent`, `AgentConfig`
- Primary user-facing class. Wraps provider + tools + memory + tracer + audit chain + cloud reporter.
- Key methods: `run(prompt) -> AgentResult`, `stream(prompt) -> AsyncIterator[str]`, `add_tool(t) -> Agent`

### `agent_kit.types` — Shared Pydantic Models
- **Exports**: `Message`, `ToolCall`, `ToolResult`, `Turn`, `AgentResult`, `PipelineResult`, `RetryPolicyConfig`, `BackoffConfig`, `CircuitBreakerConfig`, `SpanEvent`, `AuditEventRecord`
- No internal imports — foundation of the import graph.

### `agent_kit.cloud.reporter` — CloudReporter
- **Exports**: `CloudReporter`
- Batches and ships lifecycle events to agent-kit Cloud over gzip-compressed NDJSON.
- Fire-and-forget: errors are logged, never raised. Agent performance is never blocked.
- Hooks: `on_run_start`, `on_turn_complete`, `on_run_complete`, `on_run_error`, `on_circuit_state_change`, `on_audit_flush`
- Config: `api_key` (or `AGENTKIT_API_KEY` env), `project`, `agent_name`, `flush_interval_s=5.0`, `max_queue_size=1000`

### `agent_kit.cloud.models` — Wire Types
- **Exports**: `CloudEvent`, `EventType`
- `EventType`: `run_start`, `turn_complete`, `run_complete`, `run_error`, `circuit_state_change`, `audit_flush`

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
- `SQLiteMemory(path, window=100)` — persistent, thread-safe, survives restarts

### `agent_kit.reliability` — Resilience
- `CircuitBreaker` — 3-state (CLOSED→OPEN→HALF_OPEN), raises `CircuitOpenError`
- `RetryPolicy` — exponential backoff with jitter, configurable retryable exception types

### `agent_kit.audit.chain` — Audit Chain
- `AuditChain` — Merkle-linked immutable event log; `verify()` checks integrity, `export_jsonl()` for compliance

### `agent_kit.observability.tracer` — Tracing
- `AgentTracer(backend=None|"console"|"otlp")` — no-op by default; OTLP requires `pip install agent-kit[otel]`

## 📦 Core Modules — Cloud Server

### `server/app/routers/ingest.py` — Event Ingest
- `POST /v1/events` — receives gzip-compressed NDJSON batches from the SDK
- Processes all 6 event types; populates `AuditRun`, `AuditEvent`, `ActiveRunCache`, `AgentMetricSnapshot`, `CircuitBreakerEvent`
- Triggers background Merkle chain verification after each `audit_flush`
- Triggers alert evaluation on `circuit_state_change` events

### `server/app/routers/metrics.py` — Fleet Dashboard API
- `GET /v1/metrics/summary` — aggregate KPIs (runs, errors, cost, tokens, active count)
- `GET /v1/metrics/cost` — cost time-series, grouped by `agent_name|model|project`, resolutions `1m|1h|1d`
- `GET /v1/metrics/runs` — runs time-series (total, success, error, avg_turns, avg_duration)
- `GET /v1/metrics/agents` — per-agent summary with circuit breaker state
- `GET /v1/metrics/circuit-breaker` — circuit breaker state history with open-duration tracking
- `GET /v1/metrics/active` — live active runs (excludes stale >1h)

### `server/app/routers/alerts.py` — Alerting CRUD
- `GET|POST|DELETE /v1/alerts/channels` — notification channels (email, Slack, PagerDuty, webhook)
- `POST /v1/alerts/channels/{id}/test` — send test notification
- `GET|POST|PATCH|DELETE /v1/alerts/rules` — alert rules (circuit_breaker_open, cost_anomaly, error_rate, audit_integrity_failure)
- `GET /v1/alerts/firing` — firing history with state filter
- `POST /v1/alerts/firing/{id}/ack` — acknowledge a firing with optional comment

### `server/app/routers/support.py` — Support Context + SLA
- `GET /v1/support/sla` — SLA definition for org's current tier (free/pro/enterprise)
- `GET /v1/support/context` — rich operational snapshot: metrics, CB status, alert status, audit status, agent table
- `PATCH /v1/support/tier` — update org tier + plan metadata

### `server/app/alerting/evaluator.py` — Alert Evaluation
- `evaluate_all_rules(db)` — periodic evaluation of all enabled rules (called by background worker every 60s)
- `fire_circuit_breaker_open(...)` — immediate fire on CB state change
- `fire_audit_integrity_failure(...)` — immediate fire on chain verification failure

### `server/app/alerting/dispatch.py` — Notification Dispatch
- Routes firings to channel-type handlers: email, Slack, PagerDuty, webhook
- `send_test_notification(channel)` — validates channel config at creation time

## 🗄️ Database Schema

Managed by Alembic (`server/migrations/versions/`):

| Migration | Tables Added |
|-----------|-------------|
| `001_initial_schema` | `organizations`, `cloud_event_log`, `audit_runs`, `audit_events` |
| `002_metrics_schema` | `agent_metric_snapshots`, `active_run_cache`, `circuit_breaker_events` |
| `003_alerting` | `alert_channels`, `alert_rules`, `alert_firings` |
| `004_org_tier` | `org.tier`, `org.plan_metadata` columns |

## 🔧 Configuration

- `pyproject.toml` — SDK build (hatchling), deps, pytest, ruff, mypy strict; license: Rising Sun License v1.0
- `server/pyproject.toml` — server build (hatchling), FastAPI/SQLAlchemy/Alembic deps; `agentkit-cloud-server v0.1.0`
- `server/alembic.ini` — Alembic config; reads `DATABASE_URL` env var
- Env vars: `ANTHROPIC_API_KEY`, `AGENTKIT_API_KEY`, `DATABASE_URL`, `ENABLE_ALERT_WORKER`

## 📚 Documentation

| File | Topic |
|------|-------|
| `README.md` | SDK quick start, all features with code examples |
| `docs/cloud-quickstart.md` | Connecting the SDK to agent-kit Cloud |
| `docs/self-hosting.md` | Running the server yourself (Docker, Postgres, Alembic) |
| `docs/api-reference.md` | Full REST API reference |
| `docs/troubleshooting.md` | Common issues and fixes |

## 📐 Specs

| File | Topic |
|------|-------|
| `specs/00-platform-overview.md` | Cloud platform architecture overview |
| `specs/01-audit-trail.md` | Hosted audit trail (spec implemented) |
| `specs/02-fleet-dashboard.md` | Agent fleet dashboard metrics (spec implemented) |
| `specs/03-alerting.md` | Alerting rules, channels, evaluator (spec implemented) |
| `specs/04-sla-support.md` | SLA-backed support context API (spec implemented) |
| `specs/05-dashboard-ui.md` | Dashboard UI design spec |

## 🧪 Test Coverage

### SDK Tests (`tests/`)

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
| `test_cloud_reporter.py` | CloudReporter batching + HTTP shipping |
| `conftest.py` | Shared fixtures |

### Server Tests (`server/tests/`)

| File | Subject |
|------|---------|
| `test_ingest.py` | POST /v1/events — all event types, idempotency, chain verification |
| `test_metrics.py` | GET /v1/metrics/* — all endpoints |
| `test_alerts.py` | Alert CRUD, firings, ack workflow |
| `test_support.py` | Support context, SLA endpoints, tier management |

## 🔗 Key Dependencies

### SDK

| Dependency | Version | Purpose |
|-----------|---------|---------|
| `anthropic` | >=0.25 | Anthropic LLM provider (core) |
| `pydantic` | >=2.5 | Type-safe models throughout |
| `httpx` | >=0.27 | HTTP client for Ollama + OpenAI + CloudReporter |
| `openai` | >=1.30 | Optional: OpenAI provider |
| `opentelemetry-api/sdk` | >=1.24 | Optional: OTLP tracing |

### Server

| Dependency | Version | Purpose |
|-----------|---------|---------|
| `fastapi` | >=0.111 | Web framework |
| `uvicorn[standard]` | >=0.30 | ASGI server |
| `sqlalchemy[asyncio]` | >=2.0 | Async ORM |
| `asyncpg` | >=0.29 | PostgreSQL async driver (production) |
| `aiosqlite` | >=0.20 | SQLite async driver (dev/test) |
| `alembic` | >=1.13 | Database migrations |

## 📝 Quick Start

### SDK
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

### SDK + Cloud Reporting
```python
from agent_kit.cloud import CloudReporter

reporter = CloudReporter(api_key="akt_live_...", project="production", agent_name="my-agent")
agent = Agent(AnthropicProvider(), config=AgentConfig(cloud=reporter))
```

### Cloud Server
```bash
cd server
pip install -e ".[dev]"
DATABASE_URL=sqlite+aiosqlite:///./dev.db uvicorn app.main:app --reload
```

## ⚠️ Key Exceptions

- `CircuitOpenError` — circuit breaker is OPEN, no LLM calls made
- `MaxTurnsExceededError` — agent hit `max_turns` limit
- `ProviderError` — LLM call failed (retries exhausted)
- `ToolNotAllowedError` — LLM tried to call a tool not in `allowed_tools`
- `DAGCycleError` / `DAGMissingDependencyError` — invalid DAG structure
