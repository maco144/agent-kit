# Changelog

All notable changes to agent-kit are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Cost circuit breaker.** `AgentConfig(max_run_cost_usd=...)` stops a run before the model call after its spend reaches the cap. `AgentConfig(enforce_budgets=True)` enforces fleet budgets — daily / weekly / monthly UTC ceilings per agent, project, or org, managed at `/v1/budgets` — raising `BudgetExceededError` before the next model call. Spend includes in-flight runs; `budget_exceeded` alert rules fire on trip and resolve on reset or a raised limit. Claude Agent SDK (`ClaudeAgentObserver(..., enforce_budgets=True)`) and OpenAI Agents SDK (`AgentKitRunHooks`) agents can be stopped too. Migration `006` adds `budgets`. See `specs/09-cost-circuit-breaker.md`.
- **OTLP trace ingest (`POST /v1/traces`).** Any OpenTelemetry-instrumented agent — GenAI semantic conventions or OpenInference, any language — reports runs, tool calls, tokens, cost, and failures to agent-kit Cloud with a standard OTLP/HTTP exporter. Audit chains are built at ingest and flagged `chain_origin: "ingest"` (the runs API now returns `chain_origin` for every run); span content is never stored. Migration `005` adds `audit_runs.chain_origin`, `active_run_cache.last_event_at`, and `active_run_cache.failure_message`; the server gains an `opentelemetry-proto` dependency.
- **Harness adapters for agent-kit Cloud.** `agent_kit.integrations.claude_agent_sdk.ClaudeAgentObserver` and `agent_kit.integrations.openai_agents.AgentKitTraceProcessor` report Claude Agent SDK and OpenAI Agents SDK runs — turns, tool calls, subagents, handoffs, guardrails, and cost — to the existing server, with the audit chain built client-side. Claude runs use the SDK's reported cost; OpenAI runs are priced from agent-kit's tables. New extras: `claude-agent-sdk`, `openai-agents`. See `specs/07-harness-adapters.md`.
- `CloudReporter.submit_threadsafe(event)` for synchronous callers on any thread, plus `CloudReporter.project` / `agent_name`.

## [0.3.0] — 2026-09-13

Tool-using agents work end to end. In 0.2.0 every agent that called a tool failed on the following
turn with a provider 400 — upgrade if you use tools.

### Added
- `Agent.stream()` now runs the full agent loop — tools execute between turns, and retry, circuit breaking, audit, and cloud reporting apply. The finished `AgentResult` is available as `agent.last_result` (also set by `run()`). Provider `stream()` accepts `tools` and may yield a final `Turn` after its text chunks; text-only providers keep working.
- Tool calls within one turn run concurrently; synchronous tools run in a worker thread instead of blocking the event loop.
- `CostSummary.cache_read_tokens` / `cache_write_tokens`; Anthropic cost includes cache reads and writes.
- `specs/06-harness-roadmap.md` — the plan for closing harness gaps (fundamentals, parity, differentiators).
- **SMTP delivery for email alert channels.** Configure with `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls`/`ssl`/`none`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`. Without `SMTP_HOST`, email notifications are logged as before. Previously email channels never sent mail.
- `agent_kit/py.typed` — the package now advertises its inline type hints to downstream type checkers (PEP 561).
- GitHub Actions CI (`.github/workflows/ci.yml`) — ruff, mypy, and pytest for the SDK on Python 3.11/3.12; ruff, pytest, and an Alembic `upgrade head` smoke test for the cloud server.
- `CONTRIBUTING.md`, `CHANGELOG.md`, and `examples/README.md`.
- `docs/api-reference.md` now documents `GET /v1/audit/runs/{run_id}/export` and `GET /v1/audit/events`.

### Changed
- The source distribution is limited to the SDK (`agent_kit/`, `tests/`, `examples/`, README, LICENSE, CHANGELOG); it previously swept in `server/` and untracked local files.
- Relicensed from FSL-1.1-Apache-2.0 to the **Rising Sun License v1.0** — free for personal, educational, and research use; commercial deployments connect to the Nous network.
- `PROJECT_INDEX.json` now covers the cloud server (13 modules, server tests, server dependencies) alongside the SDK.

### Fixed
- **Multi-turn tool use failed on Anthropic and OpenAI.** Assistant tool calls were not stored in history, so the request after a tool call carried an empty assistant turn and orphaned tool results, which both APIs reject. `Message.tool_calls` now round-trips through both adapters (and Ollama) and `SQLiteMemory` (existing databases migrate automatically); parallel tool results share one message and failed tools set `is_error`.
- Memory windows could split a tool call from its results; trimming now keeps tool exchanges intact.
- Cost tracking reported $0 for Claude Opus 5, Sonnet 5, and Fable; billed Opus 4.5–4.8 at 3× actual; understated Haiku 4.5; priced `gpt-4o-mini` as `gpt-4o`. Prices now use longest-prefix matching, and unknown models log a warning.
- `BaseProvider.stream()` was declared `async def` while every implementation is an async generator, so `Agent.stream()` failed type checking. The annotation now matches the runtime contract. No behaviour change — streaming worked correctly at runtime.
- `AgentLoop.run()` bound one local name to both a `ToolResult` and an `AgentResult`; the tool-call result is now `tool_result`.
- `docs/self-hosting.md` was not runnable: it installed from a nonexistent `requirements.txt` (the Dockerfile failed at `COPY`), listed `SECRET_KEY` and `LOG_LEVEL` env vars the server never reads, the seed script omitted the required `ApiKey.key_prefix`, and the Docker image baked `ENABLE_ALERT_WORKER=1` into a `--workers 4` process (duplicate alert evaluations).
- `docs/troubleshooting.md` referenced a nonexistent `AGENTKIT_LOG_LEVEL` variable; it now shows how to enable the `agent_kit.cloud` debug logger.
- `docs/api-reference.md` documented the PagerDuty channel key as `integration_key`, but dispatch read `routing_key`, so channels created from the docs never paged. The docs now say `routing_key`, and dispatch also accepts `integration_key` so existing channels start working.
- `docs/api-reference.md` now documents `GET /healthz`.
- The server test suite never exited: aiosqlite ≥ 0.22 uses non-daemon worker threads and the shared test engine was never disposed, so `pytest` hung after the last test (and would hang the CI server job).
- `OpenAIProvider.stream()` failed `mypy --strict` against openai 2.x (`**kwargs` defeated the `stream=True` overload). No behaviour change.
- `server/agentkit_cloud.db` (an empty dev database) is no longer tracked; `*.db` is gitignored.
- Restored a clean lint and type baseline: 18 ruff findings in the SDK, 13 in the server, and 10 mypy errors — dead locals, unused imports, bare `Callable` annotations, and a mid-module `import` in `types.py`.

## [0.2.0] — 2026

### Added
- **agent-kit Cloud** — an ingest + observability backend (`server/`, FastAPI + SQLAlchemy + Alembic):
  - Spec 01: hosted, tamper-evident audit trail with server-side Merkle chain verification and JSONL/CSV export.
  - Spec 02: fleet dashboard metrics API (`/summary`, `/cost`, `/runs`, `/agents`, `/circuit-breaker`, `/active`).
  - Spec 03: alerting — rules, channels (email/Slack/PagerDuty/webhook), background evaluator, ack workflow.
  - Spec 04: SLA-backed support context API and tier management.
- `CloudReporter` — batched, gzip-compressed, fire-and-forget lifecycle event shipping from the SDK.
- `DAGOrchestrator` — parallel multi-agent execution with cycle detection.
- `SQLiteMemory` — persistent, thread-safe conversation memory.
- Circuit breaker state transitions are recorded in the audit chain.

## [0.1.0]

### Added
- Initial release: `Agent`, `AgentConfig`, the `@tool` decorator with JSON Schema generation, `ToolRegistry` allowlist enforcement, Anthropic/OpenAI/Ollama providers, `LinearPipeline`, `RetryPolicy`, `CircuitBreaker`, the `AuditChain` Merkle log, and `AgentTracer` (noop/console/OTLP).
