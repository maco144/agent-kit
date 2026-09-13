# Changelog

All notable changes to agent-kit are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **SMTP delivery for email alert channels.** Configure with `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls`/`ssl`/`none`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`. Without `SMTP_HOST`, email notifications are logged as before. Previously email channels never sent mail.
- `agent_kit/py.typed` — the package now advertises its inline type hints to downstream type checkers (PEP 561).
- GitHub Actions CI (`.github/workflows/ci.yml`) — ruff, mypy, and pytest for the SDK on Python 3.11/3.12; ruff, pytest, and an Alembic `upgrade head` smoke test for the cloud server.
- `CONTRIBUTING.md`, `CHANGELOG.md`, and `examples/README.md`.
- `docs/api-reference.md` now documents `GET /v1/audit/runs/{run_id}/export` and `GET /v1/audit/events`.

### Changed
- Relicensed from FSL-1.1-Apache-2.0 to the **Rising Sun License v1.0** — free for personal, educational, and research use; commercial deployments connect to the Nous network.
- `PROJECT_INDEX.json` now covers the cloud server (13 modules, server tests, server dependencies) alongside the SDK.

### Fixed
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
