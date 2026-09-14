# Spec 08 — OTLP Trace Ingest

Status: **approved design** · Written 2026-09-13 · Roadmap item: 3.1b (`specs/06-harness-roadmap.md`)

## Goal

Any agent framework that emits OpenTelemetry traces — in any language — reaches agent-kit Cloud's
audit trail, fleet metrics, alerts, and SLA context by pointing a standard OTLP/HTTP exporter at
agent-kit. No agent-kit SDK required.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.agentkit.io
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer akt_live_..."
export OTEL_RESOURCE_ATTRIBUTES="agentkit.project=support"
```

**Done means:** a trace produced by the real `opentelemetry-sdk` OTLP/HTTP exporter, carrying
GenAI semantic-convention spans, and one carrying OpenInference spans, each become a run in
`GET /v1/audit/runs` with `chain_origin: "ingest"` and `integrity: "verified"`, are counted by
`GET /v1/metrics/summary` with tokens and cost, and fire a `run_error` when the trace fails.

## Decisions

1. **Assemble runs at ingest.** `POST /v1/traces` maps spans onto the existing run lifecycle and
   drives the existing ingest handlers; fleet metrics, alerting, and support context need no change.
2. **Two conventions:** OpenTelemetry GenAI semantic conventions (`gen_ai.*`) and OpenInference
   (`openinference.span.kind`, `llm.*`). Vercel AI SDK's deprecated `ai.*` format is out of scope.
3. **The audit chain is built server-side** with the same algorithm as the SDK, and each run records
   its origin: `chain_origin = "client"` (SDK, tamper-evident from the agent) or `"ingest"`
   (OTLP, tamper-evident from ingest onward). Verification detects any change after ingest.
4. **No content storage.** Prompts, completions, tool arguments and results carried on spans are
   never persisted; only normalized names, counts, timings, and status are.

## Wire protocol

- `POST /v1/traces` — the OTLP/HTTP path standard exporters append to `OTEL_EXPORTER_OTLP_ENDPOINT`.
- Auth: existing bearer API key (`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ..."`).
- Request `Content-Type`: `application/x-protobuf` (`ExportTraceServiceRequest`) or
  `application/json` (OTLP/JSON). `Content-Encoding: gzip` supported for both.
- OTLP/JSON is decoded directly, not via `google.protobuf.json_format`: OTLP/JSON encodes trace and
  span IDs as **hex** strings (protobuf JSON would expect base64) and 64-bit integers
  (`startTimeUnixNano`, `intValue`) as strings. `status.code` may be an integer or the enum name.
- Response: `200` with `ExportTraceServiceResponse`, encoded like the request. Spans that were
  well-formed but not GenAI are accepted silently. Malformed spans are counted in
  `partial_success.rejected_spans` with an `error_message`.
- `400` for undecodable bodies (not retried by exporters); `415` for other content types; `503` when a
  concurrent write to the same run conflicts (exporters retry on 408 and 5xx).
- New server dependency: `opentelemetry-proto>=1.24` (brings `protobuf`).

## Architecture

```
POST /v1/traces
    │ decode.py      protobuf | JSON (+gzip) → list[RawSpan]
    ▼
    │ normalize.py   RawSpan → GenAISpan | None    (semconv mapper, OpenInference mapper)
    ▼
    │ assembler.py   GenAISpans grouped by trace → run lifecycle + server-side chain extension
    ▼
existing handlers: run_start · turn_complete · run_complete · run_error  (routers/ingest.py)
audit_runs / audit_events (chain_origin = "ingest") · background verification · alerts
```

### Units

| Unit | Responsibility |
|---|---|
| `server/app/otlp/decode.py` | `decode_request(body: bytes, content_type: str, content_encoding: str) -> list[RawSpan]`. `RawSpan`: `trace_id` (hex), `span_id` (hex), `parent_span_id` (hex or `""`), `name`, `start_ns`, `end_ns`, `status_error: bool`, `status_message`, `attributes: dict[str, Any]`, `resource: dict[str, Any]`. Raises `DecodeError`. |
| `server/app/otlp/normalize.py` | `normalize(span: RawSpan) -> GenAISpan \| None`. `GenAISpan`: `kind` (`llm` \| `tool` \| `agent`), `convention` (`otel-genai` \| `openinference`), `name` (agent/tool name), `model`, `input_tokens` (uncached), `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `tool_call_id`, `conversation_id`, plus the RawSpan identity/timing/status fields. Content attributes are never copied. |
| `server/app/otlp/pricing.py` | Server copy of the SDK's Anthropic and OpenAI price tables with longest-prefix lookup (the server does not depend on the SDK, as with `audit_chain.py`). |
| `server/app/otlp/assembler.py` | `async assemble(spans: list[RawSpan], org_id: str, db) -> AssembleResult`: finalizes idle runs, then groups spans by trace and applies the lifecycle below. |
| `server/app/audit_chain.py` | Gains `append_event(db, run, event_type, actor, payload, timestamp)` — computes `payload_hash` / `leaf_hash` from the run's current root and inserts the next `AuditEvent`. |
| `server/app/routers/otlp.py` | The endpoint: content negotiation, auth, decode, assemble, commit, response encoding, background verification. |
| `server/migrations/versions/005_otlp_ingest.py` | `audit_runs.chain_origin VARCHAR(16) NOT NULL DEFAULT 'client'`; `active_run_cache.last_event_at DATETIME NULL`. |
| `server/app/schemas.py` | `AuditRunSummary.chain_origin`. |

## Span mapping

A span is GenAI if either mapper recognises it; otherwise it is ignored (but still counts toward
trace completion when it is the root).

| `GenAISpan.kind` | GenAI semconv (`gen_ai.operation.name`) | OpenInference (`openinference.span.kind`) |
|---|---|---|
| `llm` | `chat`, `generate_content`, `text_completion` | `LLM` |
| `tool` | `execute_tool` | `TOOL` |
| `agent` | `invoke_agent`, `invoke_workflow` | `AGENT`, `CHAIN` |

Other operations (`embeddings`, `create_agent`, `plan`, retrieval, reranking…) are ignored.

| Field | GenAI semconv | OpenInference |
|---|---|---|
| model | `gen_ai.response.model` → `gen_ai.request.model` | `llm.model_name` |
| input tokens (total) | `gen_ai.usage.input_tokens` | `llm.token_count.prompt` |
| output tokens | `gen_ai.usage.output_tokens` | `llm.token_count.completion` |
| cache read | `gen_ai.usage.cache_read.input_tokens` | `llm.token_count.prompt_details.cache_read` |
| cache write | `gen_ai.usage.cache_write.input_tokens` | `llm.token_count.prompt_details.cache_write` |
| tool name | `gen_ai.tool.name` | `tool.name` |
| tool call id | `gen_ai.tool.call.id` | `tool_call.id` → `tool.id` |
| agent name | `gen_ai.agent.name` → `gen_ai.workflow.name` → span name | `agent.name` → span name |
| conversation | `gen_ai.conversation.id` | `session.id` |
| failure | span status `ERROR` or `error.type` present | span status `ERROR` |

Both conventions count cached tokens inside the input total, so
`input_tokens (uncached) = max(0, total − cache_read − cache_write)`. Cost uses
`pricing.estimate(model, uncached, output, cache_read, cache_write)` with the SDK's multipliers
(cache read 0.1×, 0.025× for `claude-fable-5-1`; cache write 1.25×); unknown models cost 0.

Run attribution:
- `run_id = uuid5(OTLP_NAMESPACE, trace_id)`; `trace_id`, `harness`, and `conversation_id` go in the `run_start` payload (kept in `cloud_event_log`).
- `project` = resource attribute `agentkit.project`, else `"default"`.
- `agent_name` = the name of the latest-ending `agent` span received before completion (outer
  spans end after the spans they contain, so this converges on the outermost agent or workflow),
  else resource `service.name`, else `"otel-agent"`. `AuditRun.agent_name` and
  `ActiveRunCache.agent_name` are updated when a later-ending agent span arrives.
- `harness` = the convention of the first GenAI span (`otel-genai` / `openinference`).

## Run lifecycle (per trace, per request)

Spans in a request are sorted by `end_ns`, grouped by `trace_id`, and processed in order:

1. **Start.** The first GenAI span of an unseen trace creates the run: `AuditRun(chain_origin="ingest")`,
   an `agent_start` audit event, and a `run_start` event (model = the first `llm` span's model in
   this trace's batch, else empty — filled in on `ActiveRunCache.model` when the first `llm` span
   arrives). `ActiveRunCache.last_event_at` is set on every span applied.
2. **`llm` span →** `llm_complete` audit event + `turn_complete` (tokens, cost, duration).
3. **`tool` span →** `tool_call` audit event (`call_id`, `success`, `duration_ms`).
4. **`agent` span →** `agent_invoke` audit event (`name`, `success`). An errored agent span with no
   GenAI parent marks the trace failed.
5. **Completion.** When the trace's root span (empty `parent_span_id`) arrives for an active run:
   `agent_complete` + `run_complete`, or `agent_error` + `run_error` if the root span or a
   top-level agent span errored. The run leaves `ActiveRunCache`; background verification runs.
6. **Idle finalization.** Before processing, the org's ingest-origin runs whose
   `last_event_at` is older than 5 minutes are completed as in step 5 (successful unless a failure
   was recorded). Covers exporters that filter out non-GenAI root spans.
7. **Late spans.** A GenAI span for an already-completed run appends its audit event (the chain
   stays append-only and verifiable) but does not update fleet metrics.

Audit payloads contain only normalized fields (`model`, token counts, `cost_usd`, tool/agent name,
`call_id`, `success`, `duration_ms`); `payload_hash` is SHA-256 of the sorted-key JSON, matching the
SDK. Event timestamps are span end times.

## Failure handling

- A span that fails normalization is counted as rejected; the rest of the request proceeds.
- A request is one transaction. An integrity conflict on `(run_id, seq)` (two requests extending the
  same run at once) rolls the request back and returns `503`, so the exporter retries the batch.
- Retries are idempotent: each span's audit event has a deterministic
  `event_id = uuid5(run_id, span_id + ":" + event_type)`, and a span whose event already exists is
  skipped entirely — no second turn, no double-counted tokens or cost.
- Alert evaluation for `run_error` and circuit-breaker events is unchanged.

## Testing

- `server/tests/test_otlp_decode.py` — protobuf and JSON fixtures (hex IDs, string int64,
  enum-name status, gzip), undecodable bodies.
- `server/tests/test_otlp_normalize.py` — semconv and OpenInference span shapes from the published
  conventions; cached-token subtraction; non-GenAI spans; content attributes never retained.
- `server/tests/test_otlp_ingest.py` — through the FastAPI app: run creation, spans split across
  requests, root last, errored trace → `run_error` + alert, idle finalization, late span,
  idempotent retry, `chain_origin` in `/v1/audit/runs`, verified integrity, metrics summary; one test
  records spans with the real `opentelemetry-sdk` (in-memory exporter), encodes them with the OTLP
  exporter's own protobuf encoder, and posts those exact bytes to the in-process app.

## Out of scope

- OTLP metrics and logs; OTLP/gRPC.
- Vercel AI SDK legacy `ai.*` attributes.
- Storing prompt / completion / tool content.
- Dashboard filtering by `chain_origin` or harness.
