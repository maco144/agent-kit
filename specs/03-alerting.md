# Spec 03 — Alerting

**Version:** 0.1
**Date:** 2026-03-12
**Status:** Draft
**Depends on:** `specs/00-platform-overview.md` Phase 1, `specs/02-fleet-dashboard.md` (metrics pipeline)

---

## 1. Problem

Observability data is only useful if someone acts on it before the problem becomes visible to end users. Engineering teams need to be paged or notified when:

- An agent's circuit breaker opens (LLM provider degraded — all calls are failing fast)
- An agent's cost spikes unexpectedly (runaway loop, prompt injection causing excessive output)
- An audit chain integrity check fails (potential tampering or data corruption)
- An agent's error rate exceeds a threshold (systemic failure, not just noise)

Without alerting, teams discover these problems from customer complaints, not from their tooling.

---

## 2. Scope

**In scope:**
- Four alert types: circuit breaker, cost anomaly, error rate, audit integrity failure
- Notification channels: email, Slack, PagerDuty, generic webhook
- Alert rules: per-org, configurable thresholds
- Alert history: searchable log of all fired alerts
- Mute/snooze: suppress alerts for a time window
- Deduplication: one notification per alert state, not per event
- REST API for programmatic rule management

**Out of scope (v1):**
- ML-based anomaly detection (threshold-based only in v1)
- On-call rotation management (use PagerDuty for that)
- Alert dependencies / inhibition rules
- SMS notifications

---

## 3. Alert Types

### 3.1 `circuit_breaker_open`

Fires when a `CircuitBreakerEvent` with `new_state = "open"` is received for a monitored agent.

Resolves automatically when a subsequent event with `new_state = "closed"` is received.

**Configuration:**
```json
{
  "type": "circuit_breaker_open",
  "agent_name": "billing-assistant",   // "*" for all agents
  "project": "production",             // optional
  "resource": "anthropic"              // optional, filter by CB resource
}
```

No threshold — every open is an alert.

---

### 3.2 `cost_anomaly`

Fires when an agent's cost over a rolling window exceeds a threshold.

**Two modes:**

**Absolute threshold:** `cost_usd > N in last M minutes`
```json
{
  "type": "cost_anomaly",
  "mode": "absolute",
  "agent_name": "summarizer",
  "threshold_usd": 5.00,
  "window_minutes": 60
}
```

**Relative threshold:** `cost_usd > P% above rolling baseline`
```json
{
  "type": "cost_anomaly",
  "mode": "relative",
  "agent_name": "summarizer",
  "threshold_pct": 200,       // alert if cost is 2x the baseline
  "window_minutes": 60,
  "baseline_days": 7          // compare against same hour, last 7 days
}
```

Resolves when cost drops below threshold in the subsequent evaluation window.

**Evaluation cadence:** Every 1 minute via background worker.

---

### 3.3 `error_rate`

Fires when an agent's error rate over a rolling window exceeds a threshold.

```json
{
  "type": "error_rate",
  "agent_name": "billing-assistant",
  "threshold_pct": 10.0,       // alert if >10% of runs fail
  "window_minutes": 15,
  "min_runs": 5                // don't fire if fewer than 5 runs in window (avoid noise on low traffic)
}
```

Resolves when error rate drops below threshold in the subsequent evaluation window.

---

### 3.4 `audit_integrity_failure`

Fires when the ingest worker marks an `AuditRun.integrity = "failed"`. This is a high-severity alert — it indicates either a bug in the library, network corruption, or active tampering.

```json
{
  "type": "audit_integrity_failure",
  "agent_name": "*",    // almost always wildcard — any failure is critical
  "project": "*"
}
```

This alert type is **not auto-resolving.** It requires manual acknowledgement. It stays `firing` until a user dismisses it via the UI or API.

---

## 4. Alert Lifecycle

```
              ┌─────────┐
              │ inactive │  rule exists, condition not met
              └────┬─────┘
                   │ condition met
              ┌────▼──────┐
              │  firing   │  notification sent to channels
              └────┬──────┘
        ┌──────────┴──────────┐
        │ condition resolves   │ manual ack (integrity_failure)
   ┌────▼──────┐         ┌────▼──────┐
   │ resolved  │         │  acked    │
   └───────────┘         └───────────┘
```

**Deduplication:** An alert in `firing` state does not send a second notification if the condition remains true. It sends a second notification only on re-fire after resolving, or if the alert escalates (future feature).

**Resolution notifications:** When a `firing` alert resolves, a resolution notification is sent to all channels ("Circuit breaker closed — billing-assistant recovered").

---

## 5. Data Model

### `AlertRule`

```
AlertRule
  id              uuid, PK
  org_id          uuid, FK
  name            text, not null         -- user-defined label
  type            enum(circuit_breaker_open, cost_anomaly, error_rate, audit_integrity_failure)
  config          jsonb, not null        -- type-specific config object
  enabled         bool, default true
  channels        uuid[], FK → AlertChannel
  muted_until     timestamptz, nullable  -- snooze expiry
  created_at      timestamptz
  updated_at      timestamptz

INDEX: (org_id, type, enabled)
```

### `AlertChannel`

```
AlertChannel
  id              uuid, PK
  org_id          uuid
  name            text                   -- user label, e.g. "Slack #incidents"
  type            enum(email, slack, pagerduty, webhook)
  config          jsonb                  -- type-specific: url, routing_key, etc.
  created_at      timestamptz
```

### `AlertFiring`

One row per alert instance (rule fires → one row; resolves → updates that row).

```
AlertFiring
  id              uuid, PK
  rule_id         uuid, FK → AlertRule
  org_id          uuid
  state           enum(firing, resolved, acked)
  fired_at        timestamptz
  resolved_at     timestamptz, nullable
  acked_at        timestamptz, nullable
  acked_by        text, nullable         -- user email
  context         jsonb                  -- snapshot of triggering data
  notifications_sent  int, default 0

INDEX: (rule_id, state)
INDEX: (org_id, state, fired_at DESC)
```

---

## 6. Alert Evaluator

A background worker process runs the evaluation loop.

**Loop cadence:** Every 60 seconds.

**Per rule:**
1. Skip if `enabled = false` or `muted_until > now()`.
2. Query the metrics / event store for the relevant window.
3. Evaluate condition against threshold.
4. Check if there is an existing `AlertFiring` in `firing` state for this rule.
   - **Condition met + no existing firing:** Create new `AlertFiring`, dispatch notifications.
   - **Condition met + existing firing:** No-op (deduplicated).
   - **Condition not met + existing firing:** Update `AlertFiring.state = resolved`, `resolved_at = now()`, dispatch resolution notifications.
   - **Condition not met + no existing firing:** No-op.

**`audit_integrity_failure`** rules are evaluated differently — they're event-driven (triggered by the ingest worker), not polled. They also never auto-resolve.

---

## 7. Notification Channels

### 7.1 Email

```json
{
  "type": "email",
  "to": ["oncall@company.com", "engineering@company.com"]
}
```

**Firing email subject:** `[agent-kit] ALERT: circuit_breaker_open — billing-assistant (production)`
**Resolved email subject:** `[agent-kit] RESOLVED: circuit_breaker_open — billing-assistant (production)`

Body: plain-text summary of the alert context (agent, project, metric values, link to dashboard).

### 7.2 Slack

```json
{
  "type": "slack",
  "webhook_url": "https://hooks.slack.com/services/...",
  "channel": "#incidents"   // optional override
}
```

Payload uses Slack Block Kit. Firing message: red attachment with alert details. Resolved: green attachment. Both include a "View in Dashboard" button link.

### 7.3 PagerDuty

```json
{
  "type": "pagerduty",
  "routing_key": "r3fg...",
  "severity": "critical"   // critical | error | warning | info
}
```

Uses PagerDuty Events API v2. `dedup_key = rule_id` ensures firing → resolved correctly maps to one PD incident. Resolved alert sends `resolve` action.

### 7.4 Webhook

```json
{
  "type": "webhook",
  "url": "https://your-system.com/hooks/agentkit",
  "secret": "sha256-hmac-secret",   // optional request signing
  "headers": {"X-Custom": "value"}  // optional extra headers
}
```

**POST body:**
```json
{
  "event": "alert.firing",     // or "alert.resolved"
  "alert_id": "uuid",
  "rule_name": "billing-assistant cost spike",
  "type": "cost_anomaly",
  "agent_name": "billing-assistant",
  "project": "production",
  "fired_at": "2026-03-12T10:00:00Z",
  "context": {
    "cost_usd_in_window": 12.40,
    "threshold_usd": 5.00,
    "window_minutes": 60
  }
}
```

If `secret` is set, each request includes `X-AgentKit-Signature: sha256=<hmac-hex>` computed over the raw body.

**Delivery:** 3 attempts with exponential backoff (2s, 4s, 8s). Failures logged to `AlertFiring.context`.

---

## 8. API

Base path: `/v1/alerts`

### `GET /v1/alerts/rules`
List all rules for the org.

**Response:** `{"rules": [AlertRule]}`

### `POST /v1/alerts/rules`
Create a new rule.

**Body:** `{name, type, config, channels: [channel_id], enabled}`

**Response `201`:** Created rule.

### `PATCH /v1/alerts/rules/{rule_id}`
Update rule config, enable/disable, or set mute window.

**Body (any subset):** `{name, config, enabled, muted_until, channels}`

### `DELETE /v1/alerts/rules/{rule_id}`
Delete rule and all associated firing history.

### `GET /v1/alerts/channels`
List notification channels.

### `POST /v1/alerts/channels`
Create a notification channel.

**Body:** `{name, type, config}`

**Response `201`:** Created channel + `test_sent: true` (a test notification is always sent on creation).

### `POST /v1/alerts/channels/{channel_id}/test`
Re-send a test notification to verify the channel is still reachable.

### `GET /v1/alerts/firing`
List current and historical alert firings.

**Query params:** `state=firing|resolved|acked`, `rule_id`, `from`, `to`, `limit`, `cursor`

### `POST /v1/alerts/firing/{firing_id}/ack`
Acknowledge a firing alert (required for `audit_integrity_failure` to dismiss).

**Body:** `{comment: "Investigated — false positive from test run"}` (optional)

---

## 9. UI

### 9.1 Alerts overview page

**Top section:** Active alerts count (badge), rules count, channels count.

**Active alerts panel:** Cards for each currently-firing alert. Shows: alert type, agent, project, fired_at (relative time), key metric. "Ack" button for integrity failure alerts.

**Rules table:** Name, type, target agent/project, channels, status (enabled/disabled/muted), last fired.

"+ New Rule" button → rule creation modal.

### 9.2 Rule creation modal

Step 1: Choose alert type (4 options, with icon and one-line description each).
Step 2: Configure the rule (dynamic form based on type). Inline validation.
Step 3: Select notification channels (multi-select from existing channels, or "Add new channel" inline).
Step 4: Name the rule. Review summary. Save.

### 9.3 Alert history page

Table: Alert name, Type, Agent, Project, Fired at, Resolved at (or "Active"), Duration open.
Filterable by type, state, agent, date range.
Click row → detail drawer with full context JSON.

### 9.4 Channel management page

List of channels with type badge (Email / Slack / PagerDuty / Webhook), name, "Test" button, "Edit" button, "Delete" button.

---

## 10. Acceptance Criteria

- [ ] Circuit breaker open alert fires within 60 seconds of state transition for an enabled rule
- [ ] Cost anomaly alert fires within 2 minutes of threshold crossing (1-min evaluation cadence + up to 60s)
- [ ] Deduplication: a single firing condition sends exactly 1 notification, not N notifications for N evaluation cycles
- [ ] Resolution notification is sent within 2 evaluation cycles of condition clearing
- [ ] Webhook delivery includes correct HMAC signature when secret is configured
- [ ] PagerDuty: firing creates an incident, resolved sends the `resolve` action with the same `dedup_key`
- [ ] Muted rule fires 0 notifications while muted; fires correctly after `muted_until` elapses
- [ ] `audit_integrity_failure` alert requires explicit ack and does not auto-resolve
- [ ] Test notification on channel creation succeeds within 5 seconds or returns a clear error
