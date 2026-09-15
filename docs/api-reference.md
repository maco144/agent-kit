# API Reference

All endpoints require `Authorization: Bearer <api_key>` unless noted.

Base URL: `https://ingest.agentkit.io` (or your self-hosted server URL).

---

## Authentication

API keys are per-org. Pass the key as a Bearer token:

```
Authorization: Bearer akt_live_your_key_here
```

Missing or invalid keys return `401 Unauthorized`.

---

## Ingest

### POST /v1/events

Ingest a batch of agent lifecycle events. Called automatically by `CloudReporter`.

**Request**

- Content-Type: `application/x-ndjson`
- Content-Encoding: `gzip`
- Body: gzip-compressed NDJSON — one JSON object per line, up to 200 events per request

Each event object:

```json
{
  "event_id": "uuid-v4",
  "event_type": "run_start | turn_complete | run_complete | run_error | circuit_state_change | audit_flush | tool_output_flagged",
  "run_id": "uuid-v4",
  "agent_name": "billing-agent",
  "project": "production",
  "occurred_at": "2026-03-12T14:00:00",
  "payload": { ... }
}
```

Payload shapes by `event_type`:

| `event_type` | Payload fields |
|---|---|
| `run_start` | `model`, `prompt_hash` |
| `turn_complete` | `turn_index`, `input_tokens`, `output_tokens`, `cost_usd`, `duration_ms`, `tool_names` |
| `run_complete` | `total_cost_usd`, `total_tokens`, `total_turns`, `audit_root_hash` |
| `run_error` | `error_type`, `error_message`, `turn_count` |
| `circuit_state_change` | `resource`, `prev_state`, `new_state`, `failure_count` |
| `audit_flush` | `final_root_hash`, `event_count`, `events[]` |
| `tool_output_flagged` | `call_id`, `tool_name`, `action` (`allowed` / `wrapped` / `blocked` / `stopped`), `max_severity`, `findings[]` (`scanner`, `rule`, `severity`, `location`, `indicator`) — never tool output |

**Response** `200 OK`

```json
{"accepted": 5, "message": "ok"}
```

---

### POST /v1/traces

OTLP/HTTP trace export. Any OpenTelemetry-instrumented agent can report here with a standard exporter — no agent-kit SDK:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.agentkit.io
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer akt_live_..."
export OTEL_RESOURCE_ATTRIBUTES="agentkit.project=support"
```

- `Content-Type: application/x-protobuf` or `application/json`; `Content-Encoding: gzip` optional.
- Spans following the OpenTelemetry GenAI semantic conventions (`gen_ai.operation.name`) or OpenInference (`openinference.span.kind`) become runs — one trace, one run. Other spans are accepted and ignored.
- `chat` / `generate_content` / `text_completion` / `LLM` spans are turns (tokens, cost); `execute_tool` / `TOOL` spans are tool calls; `invoke_agent` / `invoke_workflow` / `AGENT` / `CHAIN` spans name the run.
- Project comes from the `agentkit.project` resource attribute (default `default`); agent name from the outermost agent span, else `service.name`.
- A run completes when the trace's root span arrives, or after 5 minutes without new spans. A failed root span or outermost agent span records a run error; failed tool spans don't.
- The audit chain is built at ingest, so these runs report `"chain_origin": "ingest"`.
- Prompt, completion, and tool argument/result content on spans is never stored.

**Response** `200 OK` — `ExportTraceServiceResponse` in the request's encoding. Malformed spans are reported in `partial_success.rejected_spans` (JSON: `partialSuccess.rejectedSpans`).

| Status | Meaning |
|---|---|
| `400` | Body isn't a decodable OTLP export |
| `401` | Missing or invalid API key |
| `415` | Content type other than protobuf or JSON |
| `503` | Concurrent write to the same run — exporters retry automatically |

---

## Audit

### GET /v1/audit/runs

List audit runs for the org.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `project` | string | — | Filter by project name |
| `agent_name` | string | — | Filter by agent name |
| `integrity` | string | — | `verified`, `failed`, or `pending` |
| `limit` | int | 50 | Max results (1–200) |
| `cursor` | string | — | Pagination cursor from previous response |

**Response** `200 OK`

```json
{
  "runs": [
    {
      "run_id": "uuid",
      "agent_name": "billing-agent",
      "project": "production",
      "event_count": 12,
      "started_at": "2026-03-12T14:00:00",
      "completed_at": "2026-03-12T14:00:45",
      "integrity": "verified",
      "chain_origin": "client",
      "final_root_hash": "abc123..."
    }
  ],
  "next_cursor": "eyJ...",
  "total": 142
}
```

`chain_origin` is `client` when the audit chain was built by the agent-kit SDK (tamper-evident from the agent) and `ingest` when it was built from OTLP spans at `POST /v1/traces` (tamper-evident from ingest onward).

---

### GET /v1/audit/runs/{run_id}

Get a single audit run with its full event chain.

**Response** `200 OK`

```json
{
  "run_id": "uuid",
  "agent_name": "billing-agent",
  "project": "production",
  "event_count": 12,
  "integrity": "verified",
  "final_root_hash": "abc123...",
  "events": [
    {
      "seq": 0,
      "event_id": "uuid",
      "event_type": "run_start",
      "actor": "billing-agent",
      "payload_hash": "sha256...",
      "prev_root": "0000...0000",
      "leaf_hash": "sha256...",
      "timestamp": "2026-03-12T14:00:00",
      "verified": true
    }
  ]
}
```

---

### GET /v1/audit/runs/{run_id}/verify

Re-verify the Merkle chain integrity for a run.

**Response** `200 OK` (verified)

```json
{
  "run_id": "uuid",
  "verified": true,
  "event_count": 12,
  "final_root_hash": "abc123...",
  "verified_at": "2026-03-12T15:00:00"
}
```

**Response** `200 OK` (failed)

```json
{
  "run_id": "uuid",
  "verified": false,
  "broken_at_seq": 4,
  "broken_at_event_id": "uuid",
  "expected_leaf_hash": "abc...",
  "stored_leaf_hash": "xyz...",
  "verified_at": "2026-03-12T15:00:00"
}
```

---

### GET /v1/audit/runs/{run_id}/export

Export a run's full audit chain as a file, for archival or third-party verification.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `format` | string | `jsonl` | `jsonl` or `csv` |

`jsonl` emits one JSON object per line and is byte-compatible with the SDK's
`AuditChain.export_jsonl()`, so a hosted export and a local export of the same run
verify identically. `csv` is a flat tabular format that adds the `seq` and `verified`
columns.

**Response** `200 OK` — `application/x-ndjson` or `text/csv`, sent as an attachment
(`Content-Disposition: attachment; filename="audit_{run_id}.jsonl"`).

```
{"event_id":"uuid","event_type":"run_start","actor":"billing-agent","payload_hash":"sha256...","prev_root":"0000...0000","leaf_hash":"sha256...","timestamp":"2026-03-12T14:00:00"}
{"event_id":"uuid","event_type":"tool_call","actor":"billing-agent","payload_hash":"sha256...","prev_root":"sha256...","leaf_hash":"sha256...","timestamp":"2026-03-12T14:00:03"}
```

**Response** `404 Not Found` — run does not exist in this org.

---

### GET /v1/audit/events

Search audit events across every run in the org. Use this when you know *what*
happened but not *which run* it happened in.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `event_type` | string | — | Exact match, e.g. `tool_call`, `circuit_open` |
| `actor` | string | — | Filter by actor (usually the agent name) |
| `from` | datetime | — | ISO-8601 lower bound on `timestamp`, inclusive |
| `to` | datetime | — | ISO-8601 upper bound on `timestamp`, inclusive |
| `project` | string | — | Filter by the parent run's project |
| `limit` | int | 50 | Max results (1–200) |
| `cursor` | string | — | Pagination cursor from the previous response |

Results are ordered by `timestamp` descending.

**Response** `200 OK`

```json
{
  "events": [
    {
      "seq": 4,
      "event_id": "uuid",
      "event_type": "tool_call",
      "actor": "billing-agent",
      "payload_hash": "sha256...",
      "prev_root": "sha256...",
      "leaf_hash": "sha256...",
      "timestamp": "2026-03-12T14:00:03",
      "verified": true
    }
  ],
  "next_cursor": "eyJ...",
  "total": 1284
}
```

---

## Metrics

All metrics endpoints accept the same time-window query parameters:

| Param | Type | Default | Description |
|---|---|---|---|
| `from` | ISO datetime | 24h ago | Window start |
| `to` | ISO datetime | now | Window end |
| `project` | string | — | Filter by project |
| `agent_name` | string | — | Filter by agent |

### GET /v1/metrics/summary

High-level org totals for the window.

**Response** `200 OK`

```json
{
  "window": {"from": "2026-03-11T14:00:00", "to": "2026-03-12T14:00:00"},
  "total_runs": 1420,
  "runs_success": 1398,
  "runs_error": 22,
  "error_rate_pct": 1.55,
  "total_cost_usd": 14.2847,
  "total_input_tokens": 4200000,
  "total_output_tokens": 1800000,
  "active_runs": 3,
  "agents_count": 5,
  "projects": ["production", "staging"]
}
```

---

### GET /v1/metrics/cost

Cost time-series, grouped by agent/model/project.

**Additional query parameters**

| Param | Values | Default | Description |
|---|---|---|---|
| `resolution` | `1m`, `1h`, `1d` | auto | Bucket size (auto-selects based on window) |
| `group_by` | `agent_name`, `model`, `project` | `agent_name` | Series grouping |

**Response** `200 OK`

```json
{
  "group_by": "agent_name",
  "resolution": "1h",
  "series": [
    {
      "label": "billing-agent",
      "project": "production",
      "total_cost_usd": 9.42,
      "data": [
        {"bucket": "2026-03-12T13:00:00", "cost_usd": 1.2, "input_tokens": 300000, "output_tokens": 120000}
      ]
    }
  ]
}
```

Resolution auto-selection: `≤2h window → 1m`, `≤72h → 1h`, `>72h → 1d`.

---

### GET /v1/metrics/runs

Run volume and error rate time-series.

**Response** `200 OK`

```json
{
  "resolution": "1h",
  "series": [
    {
      "label": "billing-agent",
      "data": [
        {
          "bucket": "2026-03-12T13:00:00",
          "runs_total": 42,
          "runs_success": 41,
          "runs_error": 1,
          "avg_turns": 3.2,
          "avg_duration_ms": 4800
        }
      ]
    }
  ]
}
```

---

### GET /v1/metrics/agents

Per-agent summary table with latest circuit breaker state.

**Response** `200 OK`

```json
{
  "agents": [
    {
      "agent_name": "billing-agent",
      "project": "production",
      "models_used": ["claude-sonnet-4-6"],
      "runs_total": 1420,
      "error_rate_pct": 1.55,
      "total_cost_usd": 9.42,
      "avg_cost_per_run_usd": 0.00663,
      "avg_turns": 3.2,
      "circuit_breaker_state": "closed",
      "last_seen": "2026-03-12T13:58:00"
    }
  ]
}
```

---

### GET /v1/metrics/circuit-breaker

Circuit breaker event history grouped by (agent, resource).

**Response** `200 OK`

```json
{
  "agents": [
    {
      "agent_name": "billing-agent",
      "resource": "anthropic",
      "current_state": "closed",
      "events": [
        {
          "prev_state": "closed",
          "new_state": "open",
          "failure_count": 5,
          "occurred_at": "2026-03-12T11:00:00",
          "duration_open_ms": 62000
        }
      ]
    }
  ]
}
```

`duration_open_ms` is the time between an `open` transition and the next `closed` or `half_open` transition. `null` if the breaker is still open.

---

### GET /v1/metrics/active

Live view of in-progress agent runs (1-hour stale cutoff).

**Response** `200 OK`

```json
{
  "count": 2,
  "active_runs": [
    {
      "run_id": "uuid",
      "agent_name": "billing-agent",
      "project": "production",
      "model": "claude-sonnet-4-6",
      "started_at": "2026-03-12T14:01:00",
      "elapsed_ms": 12400,
      "turns_so_far": 2,
      "cost_so_far_usd": 0.0042,
      "tokens_so_far": 1200
    }
  ]
}
```

---

## Alerts

### Channels

#### POST /v1/alerts/channels

Create a notification channel. Sends a test notification on creation.

**Request body**

```json
{
  "name": "ops-slack",
  "type": "slack",
  "config": {
    "webhook_url": "https://hooks.slack.com/services/..."
  }
}
```

Supported types and their `config` fields:

| Type | Config fields |
|---|---|
| `email` | `to` (list of addresses). Sent over SMTP when the server has `SMTP_HOST` configured ([self-hosting](self-hosting.md#email-alerts-smtp)); otherwise logged to `agentkit.cloud.alerts` and not delivered |
| `slack` | `webhook_url` |
| `pagerduty` | `routing_key` (Events API v2 integration key), `severity` (optional, default `error`) |
| `webhook` | `url`, `secret` (optional, for HMAC signing) |

**Response** `201 Created`

```json
{
  "channel": {"id": "uuid", "name": "ops-slack", "type": "slack", "config": {...}, "created_at": "..."},
  "test_sent": true
}
```

#### GET /v1/alerts/channels

List all channels for the org.

#### DELETE /v1/alerts/channels/{id}

Delete a channel. Does not affect rules that reference it.

#### POST /v1/alerts/channels/{id}/test

Send a test notification to the channel.

---

### Rules

#### POST /v1/alerts/rules

Create an alert rule.

**Request body**

```json
{
  "name": "CB open — any agent",
  "type": "circuit_breaker_open",
  "config": {"agent_name": "*"},
  "channel_ids": ["uuid"],
  "enabled": true
}
```

Rule types and their `config` fields:

| Type | Config fields | Trigger |
|---|---|---|
| `circuit_breaker_open` | `agent_name` (glob, `*` = any), `resource` (optional) | Event-driven, immediate |
| `audit_integrity_failure` | `agent_name` (optional) | Event-driven, immediate |
| `cost_anomaly` | `threshold_usd` (float), `window_hours` (int) | Polled every 60s |
| `error_rate` | `threshold_pct` (float), `window_hours` (int), `min_runs` (int) | Polled every 60s |
| `budget_exceeded` | `budget_id` (a budget ID, or `*` for any budget) | Event-driven: fires when a budget trips, resolves when it closes |
| `tool_output_flagged` | `agent_name`, `project` (exact or `*`), `min_severity` (`low` / `medium` / `high` / `critical`, default `high`) | Event-driven: fires when an SDK scanner flags tool output at or above `min_severity`; never auto-resolves. Context: `run_id`, `agent_name`, `project`, `tool_name`, `action`, `max_severity`, `rules`, `indicators` |

**Response** `201 Created` — returns the created `AlertRuleSchema`.

#### GET /v1/alerts/rules

List all rules.

#### GET /v1/alerts/rules/{id}

Get a single rule.

#### PATCH /v1/alerts/rules/{id}

Update a rule. All fields are optional.

```json
{
  "enabled": false,
  "muted_until": "2026-03-13T09:00:00"
}
```

Setting `muted_until` suppresses notifications until that time. The rule still evaluates and creates `AlertFiring` records, but no notifications are dispatched.

#### DELETE /v1/alerts/rules/{id}

Delete a rule and all its associated `AlertFiring` records.

---

### Firings

#### GET /v1/alerts/firing

List currently-firing alerts.

**Response** `200 OK`

```json
[
  {
    "id": "uuid",
    "rule_id": "uuid",
    "state": "firing",
    "fired_at": "2026-03-12T11:00:00",
    "resolved_at": null,
    "acked_at": null,
    "acked_by": null,
    "context": {"agent_name": "billing-agent", "resource": "anthropic"},
    "notifications_sent": 1
  }
]
```

#### POST /v1/alerts/firing/{id}/ack

Acknowledge a firing alert.

```json
{"comment": "Investigating — on-call eng"}
```

Sets `state = "acked"`. The alert remains visible until resolved.

---

## Budgets

Spend ceilings that stop agents. A budget covers matching agents (`project` and `agent_name`, `*` = any) over a UTC calendar period — `daily` (resets 00:00), `weekly` (Monday 00:00), or `monthly` (the 1st). Spend is the period's completed-run cost plus the cost so far of runs still in flight, so a runaway run counts before it finishes.

A budget **trips** when spend reaches `limit_usd` and stays tripped until the period resets or the limit is raised above current spend. SDKs with `AgentConfig(enforce_budgets=True)` (and the Claude / OpenAI adapters with enforcement enabled) refuse the next model call while a budget covering them is tripped. Budgets are re-evaluated after every ingest batch, on reads and edits, and every 60 seconds by the alert worker.

### Budget object

```json
{
  "id": "uuid",
  "name": "support daily",
  "project": "*",
  "agent_name": "support-bot",
  "period": "daily",
  "limit_usd": 200.0,
  "enabled": true,
  "spent_usd": 212.41,
  "remaining_usd": 0.0,
  "tripped": true,
  "tripped_at": "2026-09-14T14:20:11",
  "period_start": "2026-09-14T00:00:00",
  "resets_at": "2026-09-15T00:00:00"
}
```

### GET /v1/budgets

All budgets for the org with live status. **Response** `200 OK` — `{"budgets": [Budget]}`.

### POST /v1/budgets

```json
{"name": "support daily", "period": "daily", "limit_usd": 200, "agent_name": "support-bot", "project": "*", "enabled": true}
```

`name`, `period`, `limit_usd` required. **Response** `201 Created` — the budget. `400` if `period` isn't `daily`/`weekly`/`monthly` or `limit_usd` ≤ 0.

### PATCH /v1/budgets/{id}

Update any of `name`, `period`, `limit_usd`, `project`, `agent_name`, `enabled`. Re-evaluates immediately — raising the limit above current spend closes a tripped budget and resolves its alerts. **Response** `200 OK` — the budget.

### DELETE /v1/budgets/{id}

**Response** `204 No Content`. Resolves the budget's active `budget_exceeded` alerts.

### GET /v1/budgets/status

The enabled budgets covering one agent, with live status — what SDKs poll to enforce budgets.

| Param | Description |
|---|---|
| `project` | The agent's project |
| `agent_name` | The agent's name |

**Response** `200 OK` — `{"budgets": [Budget]}`.

### `budget_exceeded` alerts

Create an alert rule with `"type": "budget_exceeded"` and `"config": {"budget_id": "<id>"}` (or `"*"` for every budget). It fires once when the budget trips, with context `budget_id`, `budget_name`, `project`, `agent_name`, `period`, `limit_usd`, `spent_usd`, `resets_at`, and resolves when the budget closes. A `*` rule resolves once no budget is tripped.

---

## Compliance

Evidence that supports record-keeping obligations (for example EU AI Act Article 12 logging and SOC 2 audit evidence). agent-kit is not a certification and makes no claim of legal compliance.

### GET /.well-known/agentkit-signing-keys

**No authentication.** Ed25519 public keys that sign evidence bundles and deletion receipts. Retired keys stay published so older bundles keep verifying.

```json
{"keys": [{"kid": "ak-3f9a2c1d0b7e", "alg": "Ed25519", "public_key": "<base64 32 bytes>", "created_at": "2026-09-14T00:00:00", "retired_at": null, "active": true}]}
```

Verifiers should fetch keys from your agent-kit endpoint (or a pinned copy) — never trust a key supplied inside a bundle.

### GET /v1/compliance/export

A signed evidence bundle (`application/zip`) of audit runs started in `[from, to)`.

| Param | Description |
|---|---|
| `from`, `to` | ISO-8601 datetimes (UTC); required, `from` < `to` |
| `project`, `agent_name` | Optional scope filters |

| File | Contents |
|---|---|
| `manifest.json` | Format `agentkit-evidence-bundle/1`, org, scope, retention policy and active legal holds at export time, counts, SHA-256 of every other file, signing `kid` |
| `manifest.sig` | `{"kid", "alg": "Ed25519", "signature"}` over the exact `manifest.json` bytes |
| `runs.jsonl` | Run metadata: `chain_origin`, `final_root_hash`, `event_count`, `integrity`, timestamps |
| `events.jsonl` | Every chain link (`prev_root`, `payload_hash`, `leaf_hash`, `timestamp`, `seq`) — hashes only |
| `verification.json` | Each chain re-verified at export time |
| `deletions.jsonl` | Signed deletion receipts for runs purged in the period |

`400` if `from >= to` or the scope holds more than 10,000 runs (narrow the range). Verify with `agent-kit verify` (`pip install agent-kit[compliance]`).

### GET /v1/compliance/retention · PUT /v1/compliance/retention

```json
{"tier": "enterprise", "audit_retention_days": 2555, "source": "override", "configurable": true}
```

| Tier | Audit retention |
|---|---|
| Free | 7 days |
| Pro | 90 days |
| Enterprise | 365 days by default; `PUT {"audit_retention_days": 1..2555}` (or `null` to reset) |

`PUT` returns `403` below enterprise and `400` out of range. Retention applies to audit runs and events only; purging requires the background worker (`ENABLE_ALERT_WORKER=1`).

### Legal holds

- `GET /v1/compliance/holds` — `{"holds": [{"id", "project", "run_id", "reason", "created_at", "released_at"}]}`
- `POST /v1/compliance/holds` — `{"project": "claims", "reason": "case #4471"}` or `{"run_id": "…", "reason": "…"}`; exactly one scope (`400` otherwise, `404` for an unknown run)
- `POST /v1/compliance/holds/{id}/release`

Active holds block retention purges of matching runs.

### GET /v1/compliance/deletions

`?from=&to=` (on `deleted_at`). Each receipt records `run_id, org_id, project, agent_name, final_root_hash, event_count, chain_origin, started_at, completed_at, deleted_at, reason` plus `kid` and `signature`. The signature covers `json.dumps(<those fields>, sort_keys=True, separators=(",", ":"))` encoded as UTF-8, datetimes ISO-8601 or `null`.

---

## Support

### GET /v1/support/sla

Return the SLA definition for the authenticated org's current tier.

**Response** `200 OK`

```json
{
  "tier": "pro",
  "p1_response_hours": 4,
  "p2_response_hours": 8,
  "p3_response_hours": 24,
  "p1_coverage": "business_hours",
  "p2_coverage": "business_hours",
  "p3_coverage": "business_hours",
  "max_contacts": 3
}
```

SLA matrix:

| Tier | P1 | P2 | P3 | Coverage | Max contacts |
|---|---|---|---|---|---|
| `free` | — | — | — | none | — |
| `pro` | 4h | 8h | 24h | business hours | 3 |
| `enterprise` | 1h | 4h | 24h | 24/7 (P1) | unlimited |

---

### GET /v1/support/context

Rich operational snapshot for support sidebar widgets. Aggregates fleet state across all tables.

**Query parameters**

| Param | Type | Default | Description |
|---|---|---|---|
| `period_hours` | int | 24 | Lookback window (1–168) |

**Response** `200 OK`

```json
{
  "org_id": "uuid",
  "org_name": "Acme Corp",
  "tier": "enterprise",
  "sla": { ... },
  "period_hours": 24,
  "generated_at": "2026-03-12T14:30:00",
  "metrics": {
    "total_runs": 1420,
    "runs_success": 1398,
    "runs_error": 22,
    "error_rate_pct": 1.55,
    "total_cost_usd": 14.28,
    "total_input_tokens": 4200000,
    "total_output_tokens": 1800000,
    "active_runs": 3,
    "agents_seen": 5
  },
  "circuit_breaker": {
    "open_agents": ["billing-agent"],
    "recent_events": [
      {"agent_name": "billing-agent", "resource": "anthropic", "prev_state": "closed", "new_state": "open", "failure_count": 5, "occurred_at": "..."}
    ]
  },
  "alerts": {
    "firing_count": 1,
    "recent_firings": [
      {"id": "uuid", "rule_name": "CB open", "state": "firing", "fired_at": "...", "resolved_at": null, "context": {...}}
    ]
  },
  "audit": {
    "total_runs": 1420,
    "verified_runs": 1415,
    "failed_runs": 2,
    "pending_runs": 3
  },
  "agents": [
    {
      "agent_name": "billing-agent",
      "project": "production",
      "runs_total": 1420,
      "error_rate_pct": 1.55,
      "total_cost_usd": 9.42,
      "circuit_breaker_state": "open",
      "last_seen": "2026-03-12T13:58:00"
    }
  ]
}
```

---

### PATCH /v1/support/tier

Update the org's support tier and optional plan metadata.

```json
{
  "tier": "enterprise",
  "plan_metadata": {
    "cse_name": "Jane Smith",
    "slack_channel": "#agentkit-support-acme",
    "contract_id": "ENT-0042"
  }
}
```

Valid tiers: `free`, `pro`, `enterprise`.

**Response** `200 OK`

```json
{
  "org_id": "uuid",
  "tier": "enterprise",
  "plan_metadata": {"cse_name": "Jane Smith", ...},
  "sla": { ... }
}
```

---

## Health

### GET /healthz

Unauthenticated liveness probe. Returns `200 OK` with `{"status": "ok"}`.

---

## Webhook signatures

When a webhook channel is configured with a `secret`, every delivery includes:

```
X-AgentKit-Signature: sha256=<hex-digest>
```

To verify:

```python
import hmac, hashlib

def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```
