# agent-kit Cloud — Platform Overview

**Version:** 0.1 (pre-build spec)
**Date:** 2026-03-12
**Status:** Draft

---

## 1. What We're Building

Four commercial services layered on top of the open-source agent-kit library:

| # | Service | Core value | Buyer |
|---|---------|-----------|-------|
| 1 | **Hosted Audit Trail** | Durable, searchable, compliance-exportable audit chains | Compliance/legal |
| 2 | **Agent Fleet Dashboard** | Cost attribution, circuit breaker state, token usage across N agents | Engineering manager |
| 3 | **Alerting** | Paging/notifications on cost anomalies, circuit opens, integrity failures | SRE/on-call |
| 4 | **SLA-Backed Support** | Response-time guarantees for production incidents | VP Engineering |

These are not four separate products. They are four surfaces of a single platform: **agent-kit Cloud**. Services 1–3 share one backend, one data model, and one SDK integration point.

---

## 2. Design Principles

1. **Zero performance impact on the library.** All cloud reporting is fire-and-forget. If the cloud endpoint is unreachable, the agent runs normally. Errors are logged, never raised.
2. **Library stays self-contained.** `agent_kit.cloud` is a new optional module. The core library (`agent_kit.agent`, `agent_kit.types`, etc.) has zero knowledge of the cloud service.
3. **Single SDK integration point.** One `CloudReporter` class is passed to `AgentConfig`. It receives all events and handles batching, retry, and delivery.
4. **Data belongs to the customer.** Full export at any time. No lock-in on data format — exports are standard JSONL/CSV.
5. **Audit chain integrity is end-to-end.** The cloud service re-verifies the Merkle chain on ingest. A chain that fails verification is stored but flagged.

---

## 3. Multi-Tenancy Model

```
Organization
  └── Project (logical grouping of agents, e.g. "production", "staging")
        └── Agent (identified by name + optional tag)
              └── Run (one call to Agent.run())
                    ├── Turns
                    ├── Tool calls
                    ├── Cost events
                    ├── Circuit breaker events
                    └── Audit events
```

**Organization** maps to a billing account. **Project** is user-defined (free-form string, defaults to `"default"`). **Agent** is identified by the name set in `AgentConfig` — if not set, falls back to provider name + model.

---

## 4. Authentication

- API keys issued per organization, scoped optionally to project.
- Key format: `akt_live_<32-char-hex>` (production), `akt_test_<32-char-hex>` (sandbox).
- Keys are passed either via `CloudReporter(api_key="...")` or `AGENTKIT_API_KEY` env var.
- No OAuth in v1. Single-key-per-org to start. Key rotation supported via dashboard.

---

## 5. SDK Integration

### 5.1 New module: `agent_kit.cloud`

```python
from agent_kit.cloud import CloudReporter

reporter = CloudReporter(
    api_key="akt_live_...",          # or env AGENTKIT_API_KEY
    project="production",            # optional, default "default"
    agent_name="billing-assistant",  # optional label
    flush_interval_s=5.0,            # batch flush interval
    max_queue_size=1000,             # local buffer before dropping
)

agent = Agent(
    provider=AnthropicProvider(),
    config=AgentConfig(
        cloud=reporter,              # new AgentConfig field
    ),
)
```

### 5.2 What `CloudReporter` receives

The `AgentLoop` calls the reporter at these lifecycle points:

| Hook | Data sent | Triggers |
|------|-----------|---------|
| `on_run_start` | run_id, agent_name, project, model, prompt_hash | Every `Agent.run()` call |
| `on_turn_complete` | Turn (tokens, cost, tool_calls, duration_ms) | Every LLM round-trip |
| `on_run_complete` | AgentResult (output, total_cost, total_tokens, audit_root_hash) | Run finishes |
| `on_run_error` | exception type, message, turn count at failure | Run raises |
| `on_circuit_state_change` | resource, prev_state, new_state, failure_count | CircuitBreaker transitions |
| `on_audit_flush` | list[AuditEventRecord] | End of run (if audit_enabled) |

### 5.3 Transport

- **Protocol:** HTTPS POST to `https://ingest.agentkit.io/v1/events`
- **Format:** NDJSON batch, gzip-compressed
- **Batching:** Events are held in an in-process queue. Flushed every `flush_interval_s` or when queue reaches 100 events.
- **Retry:** 3 attempts with exponential backoff (1s, 2s, 4s). Failures are dropped silently after retry exhaustion.
- **Timeout:** 5s connection, 10s total per request.
- **Process exit:** `CloudReporter` registers an `atexit` handler to flush the queue on clean shutdown.

### 5.4 Privacy / data minimisation

- Prompt content is **never sent** by default. Only `prompt_hash` (SHA256) is sent, allowing deduplication without exposing content.
- Tool call arguments are **never sent** by default. Only `tool_name` and `duration_ms`.
- Audit chain `payload_hash` values are sent (not the payloads themselves — the library only stores hashes).
- Opt-in `include_output=True` on `CloudReporter` sends `AgentResult.output` (for dashboard display).

---

## 6. Shared Backend Architecture

```
┌─────────────────────────────────────────────────┐
│  agent-kit library (customer's process)          │
│  CloudReporter → HTTPS → Ingest API              │
└───────────────────────┬─────────────────────────┘
                        │ NDJSON batches
                ┌───────▼────────┐
                │  Ingest API    │  FastAPI, stateless, horizontally scalable
                │  /v1/events    │  Auth, rate-limit, schema validation
                └───────┬────────┘
                        │
              ┌─────────▼──────────┐
              │   Event Queue       │  Redis Streams (or SQS)
              └─────────┬──────────┘
                        │
           ┌────────────▼───────────────┐
           │   Ingest Workers            │  Async consumers, fan-out
           │   - Audit chain verifier    │
           │   - Metrics aggregator      │
           │   - Alert evaluator         │
           └────┬──────────┬────────────┘
                │          │
        ┌───────▼──┐  ┌────▼──────────┐
        │ TimeSeries│  │  Audit Store  │
        │ (metrics) │  │  (PostgreSQL  │
        │ InfluxDB/ │  │  + S3 for     │
        │ Timescale │  │  long-term)   │
        └───────────┘  └───────────────┘
                │
        ┌───────▼──────────┐
        │  Dashboard API    │  FastAPI, REST + WebSocket for live data
        │  /v1/metrics      │
        │  /v1/audit        │
        │  /v1/alerts       │
        └───────┬───────────┘
                │
        ┌───────▼───────────┐
        │  Frontend          │  Next.js, deployed on Vercel/CF Pages
        └───────────────────┘
```

---

## 7. Data Retention

| Tier | Default | Configurable |
|------|---------|-------------|
| Free | 7 days | No |
| Pro | 90 days | No |
| Enterprise | 1 year | Yes (up to 7 years for compliance) |

Audit chain records are retained separately from metrics and can have independent retention periods (relevant for compliance requirements like SOC 2, HIPAA).

---

## 8. Build Phases

### Phase 1 — Foundation (prerequisite for everything)
- `agent_kit.cloud.CloudReporter` SDK module
- Ingest API (`/v1/events`)
- Event queue + ingest workers
- Organization + API key management
- Basic data storage (PostgreSQL)

### Phase 2 — Hosted Audit Trail
- Audit chain storage + verification on ingest
- Search/filter API
- Compliance export (JSONL, CSV)
- Audit log UI

### Phase 3 — Agent Fleet Dashboard
- Metrics aggregation pipeline
- Dashboard API (cost, tokens, circuit breaker, error rate)
- Frontend dashboard

### Phase 4 — Alerting
- Alert rule engine
- Notification delivery (email, Slack, PagerDuty, webhook)
- Alert management UI

### Phase 5 — SLA Support Infrastructure
- Support portal
- Ticket routing
- SLA tracking + escalation

---

## 9. Non-Goals (v1)

- Real-time streaming of agent output to the dashboard (content privacy)
- Replay / re-run agents from the dashboard
- Agent builder UI (different product)
- On-premises deployment (roadmap, not v1)
