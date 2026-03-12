# Spec 02 — Agent Fleet Dashboard

**Version:** 0.1
**Date:** 2026-03-12
**Status:** Draft
**Depends on:** `specs/00-platform-overview.md` Phase 1

---

## 1. Problem

Once an organization has more than two or three agents running in production, key operational questions become unanswerable:

- "How much did our agents cost us this week, and which one caused the spike?"
- "Is the billing agent's circuit breaker open right now?"
- "How many tokens is the summarization agent burning per run on average?"
- "Which agents had the highest error rate in the last 24 hours?"

None of these questions can be answered from the library alone — each agent's state is isolated in its own process. The fleet dashboard aggregates across all agents under an organization and makes these questions trivially answerable.

---

## 2. Scope

**In scope:**
- Cost and token usage: per-agent, per-project, over time
- Circuit breaker state: current state, state change history, open duration
- Run metrics: total runs, error rate, avg duration, avg turns per run
- Model usage breakdown
- Time-series charts (1h, 24h, 7d, 30d)
- Live view: agents active right now (runs in progress)
- REST API for programmatic access (CI checks, internal tooling)

**Out of scope (v1):**
- Agent output / conversation content (privacy)
- Tool call argument inspection
- Per-user attribution (multi-user agents)
- Comparative benchmarking between agent versions

---

## 3. Data Model

### `AgentMetricSnapshot`

One row per (org, project, agent_name, model, time_bucket). Time bucket = 1-minute resolution in hot storage, rolled up to 1-hour in cold.

```
AgentMetricSnapshot
  id                  bigserial, PK
  org_id              uuid
  project             text
  agent_name          text
  model               text
  bucket              timestamptz        -- truncated to minute
  runs_total          int
  runs_success        int
  runs_error          int
  input_tokens        bigint
  output_tokens       bigint
  cost_usd            numeric(12,6)
  total_turns         int
  total_duration_ms   bigint
  avg_turns           numeric(6,2)       -- computed on rollup
  avg_duration_ms     int                -- computed on rollup

UNIQUE: (org_id, project, agent_name, model, bucket)
INDEX: (org_id, bucket DESC)
INDEX: (org_id, project, agent_name, bucket DESC)
```

Stored in TimescaleDB (PostgreSQL extension) or InfluxDB. Time-series queries benefit heavily from hypertable partitioning by `bucket`.

### `CircuitBreakerEvent`

One row per state transition.

```
CircuitBreakerEvent
  id            uuid, PK
  org_id        uuid
  project       text
  agent_name    text
  resource      text                -- e.g. "anthropic", "openai"
  prev_state    enum(closed, open, half_open)
  new_state     enum(closed, open, half_open)
  failure_count int
  occurred_at   timestamptz, not null

INDEX: (org_id, agent_name, occurred_at DESC)
INDEX: (org_id, new_state, occurred_at DESC)  -- for "currently open" queries
```

### `ActiveRun` (ephemeral)

Held in Redis. Keyed by `run_id`. Expires after 1 hour (catches abandoned runs).

```
ActiveRun (Redis hash)
  run_id
  org_id
  project
  agent_name
  model
  started_at
  prompt_hash
  turns_so_far
  cost_so_far_usd
  tokens_so_far
```

---

## 4. Metrics Pipeline

### 4.1 On `on_run_start`
Write `ActiveRun` to Redis.

### 4.2 On `on_turn_complete`
Update `ActiveRun.turns_so_far`, `cost_so_far_usd`, `tokens_so_far` in Redis (atomic HINCRBY).

### 4.3 On `on_run_complete` or `on_run_error`
1. Delete `ActiveRun` from Redis.
2. Upsert `AgentMetricSnapshot` for the current minute bucket (INSERT … ON CONFLICT DO UPDATE with LEAST/GREATEST/SUM aggregation).
3. Increment `runs_success` or `runs_error` accordingly.

### 4.4 On `on_circuit_state_change`
Insert `CircuitBreakerEvent` row.

---

## 5. API

Base path: `/v1/metrics`

All endpoints require `Authorization: Bearer <api_key>` and are scoped to the authenticated organization.

### Common time range parameters

| Param | Type | Description |
|-------|------|-------------|
| `from` | ISO8601 | Start of window (default: 24h ago) |
| `to` | ISO8601 | End of window (default: now) |
| `resolution` | `1m\|1h\|1d` | Bucket size for time-series (auto-selected if omitted) |
| `project` | string | Filter to project |
| `agent_name` | string | Filter to agent |

---

### `GET /v1/metrics/summary`

Organization-level summary for the requested time window.

**Response `200`:**
```json
{
  "window": {"from": "...", "to": "..."},
  "total_runs": 4821,
  "runs_success": 4790,
  "runs_error": 31,
  "error_rate_pct": 0.64,
  "total_cost_usd": 142.38,
  "total_input_tokens": 8420000,
  "total_output_tokens": 2100000,
  "active_runs": 7,
  "agents_count": 12,
  "projects": ["production", "staging"]
}
```

---

### `GET /v1/metrics/cost`

Cost time-series, broken down by agent or model.

**Additional params:** `group_by=agent_name|model|project` (default `agent_name`)

**Response `200`:**
```json
{
  "group_by": "agent_name",
  "resolution": "1h",
  "series": [
    {
      "label": "billing-assistant",
      "project": "production",
      "data": [
        {"bucket": "2026-03-12T00:00:00Z", "cost_usd": 3.21, "input_tokens": 180000, "output_tokens": 42000},
        {"bucket": "2026-03-12T01:00:00Z", "cost_usd": 2.88, "input_tokens": 162000, "output_tokens": 38000}
      ],
      "total_cost_usd": 87.14
    }
  ]
}
```

---

### `GET /v1/metrics/runs`

Run count and error rate time-series.

**Response `200`:**
```json
{
  "resolution": "1h",
  "series": [
    {
      "label": "billing-assistant",
      "data": [
        {
          "bucket": "2026-03-12T00:00:00Z",
          "runs_total": 120,
          "runs_success": 119,
          "runs_error": 1,
          "avg_turns": 3.2,
          "avg_duration_ms": 4210
        }
      ]
    }
  ]
}
```

---

### `GET /v1/metrics/agents`

List all agents seen in the time window with summary stats.

**Response `200`:**
```json
{
  "agents": [
    {
      "agent_name": "billing-assistant",
      "project": "production",
      "models_used": ["claude-sonnet-4-6"],
      "runs_total": 4200,
      "error_rate_pct": 0.4,
      "total_cost_usd": 87.14,
      "avg_cost_per_run_usd": 0.021,
      "avg_turns": 3.2,
      "circuit_breaker_state": "closed",
      "last_seen": "2026-03-12T10:14:00Z"
    }
  ]
}
```

`circuit_breaker_state` reflects the most recent `CircuitBreakerEvent.new_state` for this agent.

---

### `GET /v1/metrics/circuit-breaker`

Circuit breaker event history and current state per agent.

**Response `200`:**
```json
{
  "agents": [
    {
      "agent_name": "billing-assistant",
      "resource": "anthropic",
      "current_state": "closed",
      "events": [
        {
          "prev_state": "closed",
          "new_state": "open",
          "failure_count": 5,
          "occurred_at": "2026-03-12T08:22:11Z",
          "duration_open_ms": 61000
        }
      ]
    }
  ]
}
```

`duration_open_ms` is populated when there is a subsequent `closed` or `half_open` transition after an `open`.

---

### `GET /v1/metrics/active`

Live view: runs currently in progress.

**Response `200`:**
```json
{
  "active_runs": [
    {
      "run_id": "uuid",
      "agent_name": "summarizer",
      "project": "production",
      "model": "claude-sonnet-4-6",
      "started_at": "2026-03-12T10:14:38Z",
      "elapsed_ms": 4200,
      "turns_so_far": 2,
      "cost_so_far_usd": 0.008,
      "tokens_so_far": 3200
    }
  ],
  "count": 7
}
```

Intended for polling at 5–10s intervals. A WebSocket endpoint (`/v1/metrics/active/stream`) will be added in a later iteration.

---

## 6. UI

### 6.1 Overview page

**Top bar:** Total cost (selected period), Total runs, Error rate %, Active runs (live count, refreshed every 10s)

**Period selector:** Last 1h / 24h / 7d / 30d / Custom

**Cost over time chart:** Line chart, one series per agent (top 5 by cost, others grouped as "Other"). X-axis: time. Y-axis: USD.

**Agent table** (below chart):

| Agent | Project | Runs | Error rate | Avg cost/run | Circuit breaker | Last active |
|-------|---------|------|-----------|-------------|----------------|------------|
| billing-assistant | production | 4,200 | 0.4% | $0.021 | 🟢 closed | 2 min ago |
| summarizer | production | 890 | 1.2% | $0.006 | 🔴 open | 14s ago |

Circuit breaker state is color-coded: green (closed), red (open), yellow (half_open). Click row → agent detail page.

### 6.2 Agent detail page

**Header:** Agent name, project, models used, first seen / last seen

**Tabs:**

**Cost & Tokens**
- Stacked area chart: input tokens (blue) / output tokens (orange) over time
- Cost line chart over time
- Stats: total cost, avg cost/run, cost trend (↑↓ vs previous period)

**Runs**
- Run volume bar chart (success / error stacked)
- Error rate line overlay
- Avg duration line chart
- Table of recent runs: run_id, started_at, duration, turns, cost, status (success/error)

**Circuit Breaker**
- Current state badge (large, prominent)
- Timeline of state transitions with duration in each state
- Open count in period, total open duration

### 6.3 Live view page

Real-time list of active runs. Auto-refreshes every 5 seconds. Shows elapsed time, current turn count, live cost estimate. Intended for on-call use during incidents.

---

## 7. Acceptance Criteria

- [ ] `GET /v1/metrics/summary` responds within 150ms for orgs with up to 10M metric rows
- [ ] `GET /v1/metrics/active` reflects runs started within the last 10 seconds
- [ ] Circuit breaker state transitions appear in the dashboard within 30 seconds of occurring
- [ ] Cost data is accurate to within 1% of summing `AgentResult.total_cost_usd` across all runs
- [ ] Time-series charts render correctly for all four period selections without gaps in data
- [ ] Agent table correctly shows `"open"` circuit breaker state when most recent transition was `→ open` with no subsequent `→ closed`
- [ ] Bulk export of 90 days of cost data (CSV) completes within 60 seconds
