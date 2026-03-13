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
  "event_type": "run_start | turn_complete | run_complete | run_error | circuit_state_change | audit_flush",
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

**Response** `200 OK`

```json
{"accepted": 5, "message": "ok"}
```

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
      "final_root_hash": "abc123..."
    }
  ],
  "next_cursor": "eyJ...",
  "total": 142
}
```

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
| `email` | `to` (email address) |
| `slack` | `webhook_url` |
| `pagerduty` | `integration_key` (Events API v2 routing key) |
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
