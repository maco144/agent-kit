# Changelog

All notable changes to agent-kit are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A run that ends mid tool call no longer poisons the next request.** A cancelled run (`asyncio.wait_for`, task cancel) or a crash over persistent memory left the model's tool call without a result, and every later run on the same Agent was rejected by the provider. Unanswered calls now get their result — or `Tool call interrupted: the run ended before it returned` — when the run exits and again before the next run starts. Cancelled runs are marked `failed` in the run store and reported as `run_error` instead of staying `running`.
- **Cut-off responses are no longer treated as complete.** A response stopped at the output limit (`stop_reason: max_tokens`, `finish_reason: length`) raises `ResponseTruncatedError` instead of running a tool on partial arguments or returning half an answer as a completed run. A stream that closes before the provider finishes the message raises `ProviderError` (retried if no text had been yielded) instead of yielding a partial turn with empty tool arguments and $0 cost.
- **Cost caps fail closed on unpriced models.** A model missing from agent-kit's price tables cost $0.00, so `max_run_cost_usd` and fleet budgets never tripped for it — including every OpenAI model newer than the table. With a cap set, an unpriced model now raises `UnpricedModelError` (in `Agent` runs and `AgentKitRunHooks`); `agent_kit.providers.set_price(prefix, input, output)` prices it. `CostSummary.priced` says whether a cost is known. Ollama models are priced at $0 instead of logging a missing-price warning.
- **OpenAI cached prompt tokens bill at the cached rate.** `prompt_tokens` includes `prompt_tokens_details.cached_tokens`, and all of it was billed at the full input rate — overstating cost and tripping caps early for any agent with a stable prompt prefix. `OpenAIProvider` and the OpenAI Agents SDK adapter (`AgentKitTraceProcessor`, `AgentKitRunHooks`) now split cached input out (`CostSummary.cache_read_tokens`) and bill it at 0.5x, or at `set_price(..., cached_input_usd_per_mtok=...)`. The server's OTLP pricing billed OpenAI cache reads at Anthropic's 0.1x; it now uses 0.5x.
- **A stalled model call can no longer hang a run.** Provider timeouts are per read, and Anthropic's keepalive `ping` events reset them, so a stream that stopped producing content never timed out. `AgentConfig(llm_timeout_s=600.0)` caps every model call, streams included, end to end; past it the call raises `ProviderError`, retried when no text had been yielded. `None` disables it.
- **Server: one bad event no longer sinks its batch.** Ingest skipped a failing event but not its failed flush: the session was left needing a rollback, the batch's commit raised, and the request returned 500 — so the SDK retried the same poisoned batch and then dropped every event in it. Each event now runs in a savepoint and a failure is logged with its event id. The worker had the same flaw: one alert rule that failed to flush aborted the cycle, and budgets and retention were never evaluated for any org. Rules and per-org budget evaluation now run in savepoints too.

### Added
- **Server deployment stack.** `server/Dockerfile` and `server/docker-compose.yml` run `api`, `worker`, and `postgres`; the API applies Alembic migrations at start (`RUN_MIGRATIONS=0` skips it). See `docs/self-hosting.md` and `specs/18-cloud-deployment.md`.
- **Standalone worker.** `python -m app.worker` evaluates polled alert rules, fleet budgets, and retention purges every 60 seconds as its own process, so exactly one evaluator runs however many API workers serve traffic. `ENABLE_ALERT_WORKER=1` still runs it inside the API for local development.
- **`agentkit-server` admin CLI** — `create-org`, `list-orgs`, `create-key`, `list-keys`, `revoke-key`. The server has no sign-up UI; this is how orgs and API keys are provisioned.
- `GET /` returns a service pointer (name, version, `/docs`, `/healthz`, source) instead of a 404.

## [0.4.1] — 2026-09-15

### Changed
- **`CloudReporter` requires a server URL.** Pass `base_url=` or set `AGENTKIT_BASE_URL`. There is no hosted agent-kit Cloud endpoint yet, and the previous default (`https://ingest.agentkit.io`) was on a domain agent-kit does not control — anyone who registered it could have received API keys and run metadata from SDKs that set `AGENTKIT_API_KEY` without a `base_url`.
- `AnthropicProvider` defaults to `claude-opus-5` (was `claude-sonnet-4-6`). Pass `default_model=` to choose another model; Claude Opus 5 runs adaptive thinking by default.

## [0.4.0] — 2026-09-15

First release on PyPI, as **`agent-kit-ai`** (`pip install agent-kit-ai`; the import name stays `agent_kit`, the CLI stays `agent-kit`). The name `agent-kit` is unavailable on PyPI.

### Added
- **Tool output scanning.** `Hooks(after_tool=[scan_tool_output(PatternScanner(), NullconeScanner())])` screens every tool result — web pages, MCP results, child-agent answers — before the model reads it. `PatternScanner` catches hidden Unicode tag text, chat-template control tokens, instruction overrides, bidi and zero-width tricks, encoded payloads, markdown data exfiltration, and jailbreak persona switches; `NullconeScanner` looks up URLs, domains, IPs, and hashes found in the output against the Nullcone threat database (opt-in, cached, fail-open, confidence-filtered, pauses on HTTP 429). The most severe finding decides: stop the run (`stop_run_at`), block the output (`block_at`, default high), wrap it in an `agentkit_scan` untrusted-content envelope (`warn_at`, default medium), or allow and record. Hook decisions carry `findings`; the loop records them as `tool_output_flagged` audit events and Cloud events (rule metadata only, never output), and agent-kit Cloud gains a `tool_output_flagged` alert rule with `min_severity`. A lead agent's scanner covers its delegated children, and a hook shared by lead and child runs once. Example `examples/scanned_tools.py`. See `specs/17-tool-output-scanning.md`.
- **Agents as tools.** `agent.as_tool(name, description, output_type=None)` lets another agent delegate to it. Every call is a fresh child run (own memory and audit chain) that keeps the child's provider, tools, and hooks and inherits the calling run's hooks (run after the child's, so delegation can't bypass a `deny_tools`), approver, run store, and remaining `max_run_cost_usd`. A child suspended on an approval suspends the parent; its approvals appear in `pending_approvals` as `"<call>/<child call>"` and `parent.resume(run_id, approvals=...)` routes them down. Child spend counts toward the parent cap and `total_cost_usd`; the parent's `tool_call` audit event records `delegated_run_id` and `delegated_root_hash`; child runs report to Cloud with `parent_run_id` on `run_start`. Crashed delegations resume the child from its checkpoint. `AgentConfig(max_delegation_depth=5)`. `ToolResult` gains `cost_usd` / `tokens`; `PendingApproval` gains `run_id`. With `cloud` set, a caller-supplied `run_id` longer than 36 characters raises `ValueError`. Example `examples/delegation.py`. See `specs/16-agent-as-tool.md`.
- **Durable runs.** `AgentConfig(run_store=SQLiteRunStore("runs.db"))` checkpoints every run at turn boundaries; `agent.resume(run_id)` / `resume_stream()` continue after a crash, failure, or kill. `approver=SUSPEND` parks approvals instead of awaiting them inline: `run()` returns `AgentResult(status="suspended", pending_approvals=[...])` and `agent.resume(run_id, approvals={call_id: True})` answers them later from any process. Tools interrupted mid-execution re-run only when `idempotent=True`; others are reported to the model as interrupted. Checkpoint writes are compare-and-swap (`RunConflictError`), so concurrent resumes can't execute a tool twice. Memory, turns, run cost, and the audit chain (`AuditChain.restore`) carry across; new audit events `run_suspended`, `run_resumed`, `tool_interrupted`. `run(..., run_id=...)` sets the run id; `AgentResult` gains `run_id`, `status`, `pending_approvals`. Example `examples/durable_approval.py`. See `specs/15-durable-runs.md`.
- **Context management.** Anthropic prompt caching is on by default (system-prompt breakpoint + automatic conversation caching; `AgentConfig(prompt_caching=False)` opts out). `AgentConfig(thinking=..., effort=...)` set Anthropic thinking and `output_config.effort` (OpenAI/Ollama `reasoning_effort`); `provider_options={...}` is merged into every request. Opt-in server-side context management: `Compaction(...)` (summarisation, beta `compact-2026-01-12`) and `ClearToolResults(...)` (beta `context-management-2025-06-27`), with compaction cost counted across `usage.iterations` and audit events `context_compacted` / `context_edited`. History is trimmed by tokens: `context_budget_tokens` (default 150K) cuts the oldest turns once to half the budget at a tool-safe boundary, keeping the prompt prefix stable between cuts; audited as `context_trimmed`. `Message.native_content` preserves provider blocks; `SQLiteMemory` persists them. Stores gain `trim_oldest()`. Example `examples/long_running_agent.py`. See `specs/14-context-management.md`.
- **Typed results.** `await agent.run(prompt, output_type=Model)` returns `AgentResult[Model]` with a validated `.parsed` — any type Pydantic validates (models, dataclasses, `TypedDict`, lists, enums, unions). Anthropic (`output_config.format`) and OpenAI / Ollama (`response_format`) constrain the answer natively; other providers, schemas strict mode can't express (open dicts, recursion), and Ollama runs with tools get the schema in the system prompt. Invalid answers are sent back with the validation errors up to `AgentConfig(output_retries=2)` times, then `OutputValidationError` is raised; failures are audited as `output_validation_failed`. Providers accept `output_schema` and declare `supports_structured_output`. Example `examples/typed_output.py`. See `specs/13-typed-results.md`.
- **MCP tools.** `async with MCPToolset(stdio(...), http(...)) as mcp:` connects Model Context Protocol servers over stdio or streamable HTTP and exposes their tools as ordinary agent-kit tools (`server__tool`), so allowlists, hooks, approvals, budgets, and audit apply. `require_approval_unless_read_only(mcp)` gates tools not marked read-only. New extra `agent-kit[mcp]` (`mcp>=2.0`); example `examples/mcp_tools.py`. See `specs/12-mcp-client.md`.
- **Hooks and approval gates.** `AgentConfig(hooks=Hooks(before_tool=[...], after_tool=[...], before_llm=[...]), approver=..., approval_timeout_s=...)`. Hooks return allow / deny / ask / replace: deny tools (the model sees a tool error, or `stop_run=True` raises `RunStoppedByHookError`), require human approval through an async approver with a timeout, redact or block tool output before it reaches memory or the model, and stop runs before a model call. Fail-closed throughout; every decision is audited. Helpers `require_approval`, `deny_tools`, `allow_only`; example `examples/approval_gate.py`. See `specs/11-hooks-approval-gates.md`.
- **Compliance exports.** `GET /v1/compliance/export` returns an Ed25519-signed evidence bundle of audit chains for a period — runs, every chain link, export-time verification, retention policy, legal holds, and deletion receipts — that `agent-kit verify` (new `agent-kit[compliance]` extra and `agent-kit` CLI) checks offline against keys published at `/.well-known/agentkit-signing-keys`. Audit retention follows the tier (7 / 90 / 365 days; enterprise configurable to 7 years); legal holds block purges; every purged run leaves a signed deletion receipt. Server signs with `AGENTKIT_SIGNING_KEY` when set. Migration `007`; the server gains a `cryptography` dependency. See `specs/10-compliance-exports.md`.
- **Cost circuit breaker.** `AgentConfig(max_run_cost_usd=...)` stops a run before the model call after its spend reaches the cap. `AgentConfig(enforce_budgets=True)` enforces fleet budgets — daily / weekly / monthly UTC ceilings per agent, project, or org, managed at `/v1/budgets` — raising `BudgetExceededError` before the next model call. Spend includes in-flight runs; `budget_exceeded` alert rules fire on trip and resolve on reset or a raised limit. Claude Agent SDK (`ClaudeAgentObserver(..., enforce_budgets=True)`) and OpenAI Agents SDK (`AgentKitRunHooks`) agents can be stopped too. Migration `006` adds `budgets`. See `specs/09-cost-circuit-breaker.md`.
- **OTLP trace ingest (`POST /v1/traces`).** Any OpenTelemetry-instrumented agent — GenAI semantic conventions or OpenInference, any language — reports runs, tool calls, tokens, cost, and failures to agent-kit Cloud with a standard OTLP/HTTP exporter. Audit chains are built at ingest and flagged `chain_origin: "ingest"` (the runs API now returns `chain_origin` for every run); span content is never stored. Migration `005` adds `audit_runs.chain_origin`, `active_run_cache.last_event_at`, and `active_run_cache.failure_message`; the server gains an `opentelemetry-proto` dependency.
- **Harness adapters for agent-kit Cloud.** `agent_kit.integrations.claude_agent_sdk.ClaudeAgentObserver` and `agent_kit.integrations.openai_agents.AgentKitTraceProcessor` report Claude Agent SDK and OpenAI Agents SDK runs — turns, tool calls, subagents, handoffs, guardrails, and cost — to the existing server, with the audit chain built client-side. Claude runs use the SDK's reported cost; OpenAI runs are priced from agent-kit's tables. New extras: `claude-agent-sdk`, `openai-agents`. See `specs/07-harness-adapters.md`.
- `CloudReporter.submit_threadsafe(event)` for synchronous callers on any thread, plus `CloudReporter.project` / `agent_name`.

### Changed
- `AgentConfig.memory_window`, `InMemoryStore(window=)`, and `SQLiteMemory(window=)` default to `None` (no message cap); agents trim by `context_budget_tokens` instead. Pass a window explicitly to keep a message cap.
- Anthropic agent runs send the system prompt as a cached text block plus top-level `cache_control`.
- Minimum versions: `anthropic>=1.0`, `openai>=1.40` (structured output parameters).

### Fixed
- The `ollama` extra installed nothing, but `OllamaProvider` needs the `openai` package; `agent-kit-ai[ollama]` now installs `openai>=1.40`.
- `AgentResult.total_cost_usd` / `total_tokens` included every earlier run on the same `Agent`; they now cover only the run's own turns.
- Anthropic thinking blocks were dropped from conversation history, which breaks tool-using turns on models that think (Claude Opus 5 and Sonnet 5 do by default); assistant turns now round-trip every content block verbatim.
- The 50-message memory window rewrote the start of the history on every turn once exceeded, so prompt caching missed from then on (and replayed thinking blocks fail the Claude Fable 5.1 conversation check).
- A tool that returned `None` was reported to the model as `Error: None`; it is now sent as `null`, and only real tool errors are marked as errors.

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
