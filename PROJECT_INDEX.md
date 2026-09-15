# Project Index: agent-kit

Generated: 2026-09-15 (updated for tool output scanning) · SDK `agent-kit` **v0.3.0** (unreleased changes in CHANGELOG) · server `agentkit-cloud-server` v0.1.0 · Rising Sun License v1.0 · Python ≥3.11

## 📁 Project Structure

```
agent-kit/
├── agent_kit/                  # SDK package
│   ├── __init__.py             # Agent, AgentConfig, SUSPEND, Compaction, ClearToolResults, Tool, tool, AgentResult, Message, Turn, ToolResult
│   ├── types.py                # All shared Pydantic models — import graph root, NO internal imports
│   ├── exceptions.py           # AgentKitError hierarchy (see ⚠️ below)
│   ├── hooks.py                # Hooks, Decision, contexts, ApprovalRequest, SUSPEND, require_approval/deny_tools/allow_only
│   ├── output.py               # OutputSpec — strict provider schemas + parsing for typed results
│   ├── compliance.py           # verify_bundle, load_public_keys (offline evidence verification)
│   ├── cli.py                  # `agent-kit verify`
│   ├── agent/
│   │   ├── agent.py            # Agent + AgentConfig; as_tool()
│   │   ├── delegation.py       # AgentTool, DelegationContext, Delegation, child_run_id, stack_hooks
│   │   └── loop.py             # AgentLoop: retry → circuit breaker → hooks → provider → tools/delegations → audit → checkpoint → cloud
│   ├── scanning/               # base.py (TextSpan, Scanner, collect_spans), policy.py (scan_tool_output), patterns.py (PatternScanner), nullcone.py (NullconeScanner)
│   ├── durable/                # models.py (RunCheckpoint, PendingTurn, RunSummary), store.py (RunStore, SQLiteRunStore CAS), checkpointer.py
│   ├── providers/              # base.py, anthropic.py (default), openai.py, ollama.py, pricing.py (longest-prefix lookup)
│   ├── tools/                  # base.py (Tool, @tool), registry.py (allowlist), mcp.py (MCPToolset, stdio(), http())
│   ├── memory/                 # in_memory.py, sqlite.py, budget.py (token trim planning), window.py (tool-safe trimming)
│   ├── reliability/            # retry.py (RetryPolicy), circuit_breaker.py (CLOSED/OPEN/HALF_OPEN)
│   ├── audit/chain.py          # AuditChain — Merkle hash chain, verify(), export_jsonl(), restore()
│   ├── observability/tracer.py # AgentTracer (noop / console / OTLP)
│   ├── orchestrator/           # pipeline.py (LinearPipeline), dag.py (DAGOrchestrator, TaskNode)
│   ├── cloud/                  # reporter.py (CloudReporter), models.py (CloudEvent, EventType), budgets.py (BudgetGuard)
│   └── integrations/           # recorder.py (RunRecorder), claude_agent_sdk.py (ClaudeAgentObserver), openai_agents.py (AgentKitTraceProcessor, AgentKitRunHooks)
├── server/                     # agent-kit Cloud (FastAPI + SQLAlchemy async + Alembic)
│   ├── app/
│   │   ├── main.py             # app + lifespan; alert worker when ENABLE_ALERT_WORKER=1
│   │   ├── auth.py · database.py · models.py · schemas.py
│   │   ├── audit_chain.py      # server-side chain verify + append_event
│   │   ├── budgets.py          # periods, spend (incl. in-flight), trip/close
│   │   ├── routers/            # ingest, otlp, metrics, audit, alerts, support, budgets, compliance (9 routers, 39 routes)
│   │   ├── otlp/               # decode.py → normalize.py → assembler.py; pricing.py
│   │   ├── alerting/           # evaluator.py, dispatch.py
│   │   └── compliance/         # signing.py (Ed25519 + rotation), bundle.py, retention.py (holds, purge, receipts)
│   ├── migrations/versions/    # 001–007
│   └── tests/                  # 11 test files + conftest + otlp_helpers
├── tests/                      # 25 SDK test files + conftest + injection_fixtures.py (the only home of injection payloads) + fixtures/mcp_fixture_server.py
├── examples/                   # 15 runnable scripts + README
├── docs/                       # 4 cloud docs + superpowers/plans/ (12 implementation plans)
├── specs/                      # 00–17
└── .github/workflows/ci.yml
```

## 🚀 Entry Points

| What | Where |
|------|-------|
| SDK public API | `agent_kit/__init__.py` |
| CLI | `agent-kit` → `agent_kit.cli:main` (`verify` subcommand) |
| Cloud server | `server/app/main.py` — `uvicorn app.main:app` |
| SDK tests | `pytest` — 365 tests |
| Server tests | `cd server && pytest` — 166 tests |

## 📦 SDK Surface

### `Agent` / `AgentConfig` (`agent_kit/agent/agent.py`)
- `Agent(provider, tools=, config=, memory=)`; `add_tool()`, `as_tool(name, description, output_type=?)`, `config`, `audit`, `tracer`, `memory`, `last_result`
- `run(prompt, output_type=?, run_id=?) -> AgentResult[T]` · `stream(prompt)` · `resume(run_id, approvals=?, output_type=?)` · `resume_stream(...)` (sets `last_result` when exhausted)
- `AgentConfig` groups:
  - **Core**: `model`, `system_prompt`, `max_turns=20`, `max_tokens_per_turn=4096`, `allowed_tools`
  - **Reliability**: `retry_policy`, `circuit_breaker`, `audit_enabled=True`, `tracer`
  - **Cost**: `max_run_cost_usd`, `enforce_budgets` (needs `cloud`)
  - **Policy**: `hooks`, `approver` (async fn or `SUSPEND`), `approval_timeout_s=300`
  - **Typed**: `output_retries=2`
  - **Context**: `thinking`, `effort`, `prompt_caching=True`, `compaction`, `clear_tool_results`, `provider_options`, `context_budget_tokens=150_000`, `memory_window=None`
  - **Durable**: `run_store`
  - **Delegation**: `max_delegation_depth=5`

### Feature map

| Feature | Modules | Entry | Spec | Example |
|---------|---------|-------|------|---------|
| Tools + allowlist | `tools/base.py`, `tools/registry.py` | `@tool(description, idempotent, cost_estimate)` | — | `multi_tool_agent.py`, `safe_agent.py` |
| Orchestration | `orchestrator/` | `LinearPipeline`, `DAGOrchestrator` | — | `pipeline_example.py`, `research_dag.py` |
| Audit chain | `audit/chain.py` | `result.audit_root_hash`, `AuditChain.verify()` | 01 | `safe_agent.py` |
| Cloud reporting | `cloud/reporter.py` | `AgentConfig(cloud=CloudReporter(...))` | 01–04 | `cloud_monitored.py` |
| Harness adapters | `integrations/` | `ClaudeAgentObserver`, `AgentKitTraceProcessor` | 07 | `*_monitored.py` |
| Cost circuit breaker | `cloud/budgets.py` | `max_run_cost_usd`, `enforce_budgets` | 09 | — |
| Evidence verify | `compliance.py`, `cli.py` | `agent-kit verify bundle.zip` | 10 | — |
| Hooks + approvals | `hooks.py`, `agent/loop.py` | `Hooks(before_tool, after_tool, before_llm)` | 11 | `approval_gate.py` |
| MCP client | `tools/mcp.py` | `async with MCPToolset(stdio(...), http(...))` | 12 | `mcp_tools.py` |
| Typed results | `output.py` | `run(prompt, output_type=Model).parsed` | 13 | `typed_output.py` |
| Context management | `memory/budget.py`, `memory/window.py`, `providers/anthropic.py` | `Compaction`, `ClearToolResults`, `context_budget_tokens` | 14 | `long_running_agent.py` |
| Durable runs | `durable/` | `run_store=SQLiteRunStore("runs.db")`, `approver=SUSPEND`, `resume()` | 15 | `durable_approval.py` |
| Agents as tools | `agent/delegation.py`, `agent/loop.py` | `child.as_tool("research", "...")`; approvals bubble as `call/child_call` | 16 | `delegation.py` |
| Tool output scanning | `scanning/`, `hooks.py` (`Decision.findings`), `agent/loop.py` | `Hooks(after_tool=[scan_tool_output(PatternScanner(), NullconeScanner())])` | 17 | `scanned_tools.py` |

### Providers
`AnthropicProvider` (default; native content round-trip, caching, thinking, compaction, `output_config.format`) · `OpenAIProvider` (`[openai]`, `response_format`) · `OllamaProvider` (imports `openai`; the `[ollama]` extra does not install it) — all subclass `BaseProvider` with `complete()`, `stream()`, `name()`, `supports_structured_output`.

### Audit event types (client chain)
`agent_start`, `llm_complete`, `tool_call`, `agent_complete`, `circuit_breaker_state_change`, `budget_exceeded` · hooks: `tool_denied`, `llm_call_denied`, `tool_output_replaced`, `approval_{requested,granted,denied}` · `output_validation_failed` · `context_{trimmed,compacted,edited}` · durable: `run_suspended`, `run_resumed`, `tool_interrupted`. Delegations add `delegated_run_id` / `delegated_root_hash` / `delegated_cost_usd` to `tool_call`; child `run_start` carries `parent_run_id`. Scanning: `tool_output_flagged` (rule metadata, never output).
Cloud wire `EventType`: `run_start`, `turn_complete`, `run_complete`, `run_error`, `circuit_state_change`, `audit_flush`, `tool_output_flagged`.

## 📦 Cloud Server API

| Router | Routes |
|--------|--------|
| `ingest.py` | `POST /v1/events` — gzip NDJSON from SDK; populates runs/events/metrics/CB; triggers chain verify + alerts |
| `otlp.py` | `POST /v1/traces` — OTLP/HTTP protobuf/JSON; GenAI semconv + OpenInference → runs, `chain_origin="ingest"`; content never stored |
| `metrics.py` | `GET /v1/metrics/{summary,cost,runs,agents,circuit-breaker,active}` |
| `audit.py` | `GET /v1/audit/runs[/{id}[/verify\|/export]]`, `GET /v1/audit/events` — cursor-paginated, `jsonl`/`csv` export |
| `alerts.py` | channels (email/Slack/PagerDuty/webhook) + `/test`; rules (`circuit_breaker_open`, `cost_anomaly`, `error_rate`, `audit_integrity_failure`, `budget_exceeded`, `tool_output_flagged`); firings + ack |
| `support.py` | `GET /v1/support/{sla,context}`, `PATCH /v1/support/tier` (free/pro/enterprise) |
| `budgets.py` | `GET\|POST /v1/budgets`, `PATCH\|DELETE /v1/budgets/{id}`, `GET /v1/budgets/status` (polled by `BudgetGuard`) |
| `compliance.py` | `GET /.well-known/agentkit-signing-keys`; `/v1/compliance/{export,retention,holds,holds/{id}/release,deletions}` |

## 🗄️ Migrations (`server/migrations/versions/`)

| Rev | Adds |
|-----|------|
| 001 | `organizations`, `cloud_event_log`, `audit_runs`, `audit_events` |
| 002 | `agent_metric_snapshots`, `active_run_cache`, `circuit_breaker_events` |
| 003 | `alert_channels`, `alert_rules`, `alert_firings` |
| 004 | `organizations.tier`, `plan_metadata` |
| 005 | `audit_runs.chain_origin`, `active_run_cache.last_event_at` / `failure_message` |
| 006 | `budgets` |
| 007 | `organizations.audit_retention_days`, `signing_keys`, `legal_holds`, `deletion_receipts` |

## 🔧 Configuration

- `pyproject.toml` — hatchling; extras `openai`, `ollama`, `otel`, `claude-agent-sdk`, `openai-agents`, `compliance`, `mcp`, `all`, `dev`; ruff pinned `>=0.15,<0.17` with explicit `select`; mypy strict
- `server/pyproject.toml` — adds `opentelemetry-proto`, `cryptography`
- `.github/workflows/ci.yml` — SDK ruff + mypy + pytest; server ruff + pytest + `alembic upgrade head`; examples byte-compile; Py 3.11 & 3.12
- Env: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AGENTKIT_API_KEY`, `AGENTKIT_BASE_URL` (required for Cloud reporting), `DATABASE_URL`, `ENABLE_ALERT_WORKER`, `AGENTKIT_SIGNING_KEY`, `SMTP_*`

## 🔗 Key Dependencies

| SDK core | Server core |
|----------|-------------|
| `anthropic>=1.0` (uses httpx2 — respx can't intercept) | `fastapi>=0.111`, `uvicorn[standard]>=0.30` |
| `pydantic>=2.5` | `sqlalchemy[asyncio]>=2.0`, `alembic>=1.13` |
| `httpx>=0.27` | `asyncpg>=0.29` (prod), `aiosqlite>=0.20` (dev/test) |
| optional: `openai>=1.40`, `mcp>=2.0`, `cryptography>=41`, `claude-agent-sdk>=0.2`, `openai-agents>=0.22`, `opentelemetry-*>=1.24` | `opentelemetry-proto>=1.24`, `cryptography>=41` |

## 📚 Docs & Specs

- `README.md` — quick start + a section per feature (hooks, MCP, typed, context, durable); `CHANGELOG.md` — `[Unreleased]` holds everything since 0.2.0; `CONTRIBUTING.md`
- `docs/` — `cloud-quickstart.md`, `self-hosting.md`, `api-reference.md` (covers traces, budgets, compliance), `troubleshooting.md`
- `docs/superpowers/plans/` — implementation plans for tier-1 fundamentals, harness adapters, OTLP, cost breaker, compliance, hooks, MCP, typed results, context mgmt, durable runs, agents as tools, tool output scanning
- `specs/` — 00 platform · 01 audit trail · 02 fleet dashboard · 03 alerting · 04 SLA support · **05 dashboard UI (not built)** · 06 harness roadmap (tier status) · 07 harness adapters · 08 OTLP ingest · 09 cost breaker · 10 compliance exports · 11 hooks · 12 MCP client · 13 typed results · 14 context mgmt · 15 durable runs · 16 agents as tools · 17 tool output scanning

## 🧪 Tests

**SDK (`tests/`, 365)** — agent, tools, retry, circuit_breaker, audit, pipeline, dag, sqlite_memory, memory_window, cloud_reporter, provider_requests (fake clients record kwargs), context_budget, budgets, compliance, output, typed_results, mcp (real fixture server, stdio + HTTP), hooks, run_store, durable_runs, agent_tool (delegation, bubbling approvals, crash recovery), scanning (findings, policy tiers, pattern rules over `injection_fixtures`, Nullcone via MockTransport, repo payload hygiene), integrations_{recorder,claude,openai_agents}. Loop tests use `MockProvider` from `conftest.py`.

**Server (`server/tests/`, 166)** — ingest, metrics, alerts, support, audit_chain_append, otlp_{decode,normalize,ingest}, budgets, compliance, tool_output_flagged. Real in-process aiosqlite; never mock the DB.

Both: `asyncio_mode = "auto"`.

## ⚠️ Exceptions (`agent_kit/exceptions.py`, base `AgentKitError`)

| Area | Exceptions |
|------|-----------|
| Loop | `ProviderError`, `CircuitOpenError`, `MaxTurnsExceededError`, `BudgetExceededError`, `RunStoppedByHookError` |
| Tools | `ToolNotFoundError`, `ToolNotAllowedError`, `ToolExecutionError`, `MCPConnectionError`, `MCPToolError` |
| Output | `OutputValidationError` |
| Durable | `RunNotFoundError`, `RunConflictError` (CAS lost), `CheckpointError` |
| Scanning | `ScannerUnavailableError` (scanner with `fail_closed=True`) |
| Other | `AuditVerificationError`, `DAGCycleError`, `DAGMissingDependencyError` |

## 📝 Quick Start

```bash
pip install -e ".[dev]" && pytest                                   # SDK
cd server && pip install -e ".[dev]" && pytest                      # server
DATABASE_URL=sqlite+aiosqlite:///./dev.db uvicorn app.main:app --reload
```
