# Spec 07 — Harness Adapters: Claude Agent SDK + OpenAI Agents SDK

Status: **implemented** · Written 2026-09-13 · Roadmap item: 3.1 (`specs/06-harness-roadmap.md`)

## Goal

Teams running the Claude Agent SDK or the OpenAI Agents SDK get agent-kit Cloud — tamper-evident
audit trail, fleet metrics, alerts, SLA context — without switching harnesses and without any
server change.

**Done means:** a Claude Agent SDK run and an OpenAI Agents SDK run each appear in
`GET /v1/audit/runs` with `integrity: verified`, are counted by `GET /v1/metrics/summary`, and carry
token and USD cost figures — against the current server, unmodified.

## Decisions

1. **Adapters live in the SDK**, not the server. Harness activity is translated into the existing
   six `CloudEvent` types, and the `AuditChain` is built client-side, so audit records are
   tamper-evident from the source exactly as for native agent-kit agents.
2. **Both Python SDKs ship together** over one harness-neutral core.
3. **Observe only.** Adapters never block, deny, or alter harness behaviour. Enforcement (cost
   breaker, tool deny) is roadmap 3.2.
4. **No server change.** Everything the server needs already exists in the event contract; the
   harness name rides in the `run_start` payload, which the raw event log already stores.

## Architecture

```
Claude Agent SDK ── hooks ──────────────┐
                 └─ message stream ─────┤
                                        ├─► RunRecorder ──► AuditChain (per run, client-side)
OpenAI Agents SDK ── TracingProcessor ──┘        │
                                                 ▼
                              CloudReporter.submit_threadsafe(CloudEvent)
                                                 │
                                                 ▼
                                   POST /v1/events (unchanged)
```

### Units

| Unit | Responsibility | Depends on |
|---|---|---|
| `agent_kit/integrations/recorder.py` — `RunRecorder` | Harness-neutral run lifecycle: one `AuditChain` and running totals per run; emits `run_start`, `turn_complete`, `run_complete`, `run_error`, `audit_flush`; prices turns from usage; reconciles against a harness-reported total. Synchronous, thread-safe, never raises. | `CloudReporter`, `AuditChain`, provider pricing tables |
| `agent_kit/integrations/claude_agent_sdk.py` — `ClaudeAgentObserver` | `.hooks()` returns hook matchers to merge into `ClaudeAgentOptions.hooks`; `.observe(messages, prompt=None)` wraps the async message iterator from `query()` / `ClaudeSDKClient.receive_response()` and yields every message unchanged. `with_hooks(options)` merges without dropping the user's own hooks. | `RunRecorder`; `claude-agent-sdk` types only for type checking |
| `agent_kit/integrations/openai_agents.py` — `AgentKitTraceProcessor` | `TracingProcessor` implementation; register with `agents.add_trace_processor(...)` so OpenAI's default exporter keeps running. | `RunRecorder`; `openai-agents` |
| `CloudReporter.submit_threadsafe(event)` | Synchronous enqueue safe from any thread: direct `put_nowait` on the event-loop thread, `loop.call_soon_threadsafe` from other threads, direct enqueue when no loop has started (drained by the existing `atexit` flush). | — |

### `RunRecorder` interface

```python
class RunRecorder:
    def __init__(self, reporter: CloudReporter, harness: str, agent_name: str | None = None) -> None: ...

    def start(self, run_id: str, model: str | None, prompt: str | None, metadata: dict[str, Any] | None = None,
              agent_name: str | None = None) -> None
    def llm_turn(self, run_id: str, model: str | None, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                 tool_names: list[str] | None = None, duration_ms: int = 0) -> None
    def tool_call(self, run_id: str, call_id: str, tool_name: str, success: bool,
                  error: str | None = None, duration_ms: int = 0) -> None
    def audit(self, run_id: str, event_type: str, actor: str, payload: dict[str, Any]) -> None
    def complete(self, run_id: str, num_turns: int | None = None, harness_cost_usd: float | None = None) -> None
    def error(self, run_id: str, error_type: str, message: str) -> None
```

- `start` is idempotent per `run_id`; any other call for an unknown `run_id` is dropped with a
  debug log.
- `run_start` needs a model for fleet metrics. When `start` has no model, the recorder appends the
  `agent_start` audit event immediately but holds the `run_start` CloudEvent until the first
  `llm_turn` supplies one (emitted before that turn's `turn_complete`), or until `complete`/`error`,
  whichever comes first.
- `num_turns=None` on `complete` means the recorder's own count of `llm_turn` calls.
- All state is guarded by one lock; callbacks may arrive from any thread.
- Agent name precedence: the reporter's `agent_name`, then `start(agent_name=...)` (OpenAI workflow
  name), then the recorder default (Claude `"claude-agent"`, otherwise the harness name).
- Audit event types: `agent_start`, `llm_complete`, `tool_call`, plus harness-specific
  `subagent_start`, `subagent_stop`, `context_compaction`, `handoff`, `guardrail`, then
  `agent_complete` / `agent_error`. Payloads mirror the native loop (`call_id`, `success`, `error`,
  `duration_ms`, token counts). Only payload **hashes** leave the process, as today.
- `complete` and `error` emit the terminal lifecycle event, then `audit_flush` with the full chain,
  and forget the run.

### Cost

The server's fleet metrics sum `turn_complete.cost_usd`; `run_complete.total_cost_usd` is not used
for aggregation. So:

1. Each `llm_turn` prices tokens with the existing tables (`anthropic._estimate_cost` for
   `claude-*` models, `openai._estimate_cost` otherwise; unknown models log once and cost 0).
2. When the harness reports an authoritative total (`ResultMessage.total_cost_usd`), `complete`
   emits one extra `turn_complete` with `cost_usd = harness_total − priced_sum`, zero tokens, and
   `payload.reconciliation = true` — only if the difference exceeds $0.000001.
3. `run_complete.total_cost_usd` carries the authoritative figure.

## Claude Agent SDK mapping

Correlation key: `session_id` (present on every hook input and on `SystemMessage(subtype="init")`
and `ResultMessage`). Each `observe()` call is one run with a fresh `run_id`; its `session_id` goes
in run metadata. Hook events for a session with no observed run are dropped with a debug log —
`observe()` is required.

| Source | Recorder call |
|---|---|
| `SystemMessage(subtype="init")` — or the first message of any other type | `start(model=data["model"] or options model, prompt=prompt arg, metadata={"session_id": ...})` |
| `AssistantMessage` with `usage` (dedupe on `message_id`) | `llm_turn(model, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, tool_names=[ToolUseBlock names])` |
| `PreToolUse` hook | record start time for `tool_use_id` |
| `PostToolUse` hook | `tool_call(tool_use_id, tool_name, success=True, duration_ms)` |
| `PostToolUseFailure` hook | `tool_call(..., success=False, error=error)` |
| `SubagentStart` / `SubagentStop` hooks | `audit("subagent_start" / "subagent_stop", actor=agent_type, {agent_id})` |
| `PreCompact` hook | `audit("context_compaction", actor="claude-agent-sdk", {trigger})` |
| `ResultMessage` | `is_error` → `error("ResultError", subtype/errors)`; else `complete(num_turns, harness_cost_usd=total_cost_usd)` |
| Exception raised by the wrapped iterator | `error(type(exc).__name__, str(exc))`, then re-raise unchanged |
| Iterator exhausted with no `ResultMessage` | `error("IncompleteRun", "message stream ended without a result")` |

Hook callbacks return `{}` (no decision, no output change) and swallow their own exceptions.

## OpenAI Agents SDK mapping

Correlation key: `run_id = uuid5(namespace, trace_id)`. Agents SDK trace IDs (`trace_<32 hex>`, 38
characters) don't fit the server's `String(36)` run ID columns; the raw `trace_id` goes in run
metadata.

| Source | Recorder call |
|---|---|
| `on_trace_start(trace)` | `start(model=None, prompt=None, metadata={"workflow": trace.name, "group_id": ...})`; agent name defaults to `trace.name` |
| `on_span_end` — `ResponseSpanData` | `llm_turn(model=response.model, usage.input_tokens, usage.output_tokens)` — OpenAI `input_tokens` already include cached tokens, so no cache split |
| `on_span_end` — `GenerationSpanData` | `llm_turn(model, usage["input_tokens"], usage["output_tokens"])` |
| `on_span_end` — `FunctionSpanData` | `tool_call(span_id, name, success=span.error is None, error=span.error message, duration from started_at/ended_at)` |
| `on_span_end` — `HandoffSpanData` | `audit("handoff", actor=from_agent, {to_agent})` |
| `on_span_end` — `GuardrailSpanData` | `audit("guardrail", actor=name, {triggered})` |
| `on_span_end` — `AgentSpanData` with `span.error` | remember the error for the trace |
| `on_trace_end(trace)` | remembered error → `error(...)`; else `complete()` |
| `TurnSpanData`, `TaskSpanData` | ignored — their usage duplicates response/generation spans |
| `shutdown()` / `force_flush()` | no-op (reporter flushes on its own schedule and at exit) |

The trace carries no model, so `start` is called with `model=None` and the recorder's deferred
`run_start` rule supplies the first response's model.

## Failure handling

- Adapters never raise into the host harness. Every hook, processor callback, and per-message
  record step runs inside `try/except Exception` with a debug log.
- `observe()` re-raises the harness's own exceptions unchanged after recording `run_error`.
- Missing or partial usage records zero tokens. Missing model skips pricing.
- `CloudReporter` stays fire-and-forget; a full queue drops events with a debug log, as today.

## Packaging

- Optional extras: `agent-kit[claude-agent-sdk]` (`claude-agent-sdk>=0.2`),
  `agent-kit[openai-agents]` (`openai-agents>=0.22`); both added to `all`.
- Integration modules import harness SDK types under `TYPE_CHECKING`; `openai_agents.py` imports
  `agents.tracing` at module import and raises a clear `ImportError` naming the extra if missing.
- Examples: `examples/claude_agent_sdk_monitored.py`, `examples/openai_agents_monitored.py`.

## Testing

- `tests/test_integrations_recorder.py` — lifecycle ordering, idempotent start, unknown-run drops,
  pricing, cost reconciliation, audit chain `verify()`, thread-safe submission from a worker thread.
- `tests/test_integrations_claude.py` — feed recorded-shape messages (dataclass fakes) and hook
  input dicts through `observe()` and `hooks()`; assert emitted `CloudEvent`s and chain integrity;
  exception passthrough; hook merge preserves user hooks. One smoke test builds real
  `claude_agent_sdk` types, skipped when the extra is absent.
- `tests/test_integrations_openai_agents.py` — drive the processor with span fakes; assert events,
  handoff/guardrail audit, error traces. One smoke test uses real `agents.tracing` span data types,
  skipped when absent.
- Tests capture events by replacing `CloudReporter.submit_threadsafe` with a recorder; no network.
- CI SDK job installs both extras.

## Out of scope

- Enforcement on foreign harnesses (3.2 cost circuit breaker, `PreToolUse` deny).
- OTLP / OpenTelemetry GenAI ingest endpoint (3.1b).
- TypeScript harnesses.
- Dashboard filtering by harness.
