# Spec 01 — Hosted Audit Trail

**Version:** 0.1
**Date:** 2026-03-12
**Status:** Draft
**Depends on:** `specs/00-platform-overview.md` Phase 1

---

## 1. Problem

The open-source `AuditChain` produces tamper-evident Merkle records, but they live in memory and die with the process. When a compliance team asks "show me every action your agent took between March 1–7," there is nowhere to point them.

Enterprises running agents for anything financially or legally sensitive (contract generation, customer communication, financial analysis) need:
- Durable, off-process storage of audit records
- Proof that records haven't been altered after the fact
- Fast search/filter across thousands of runs
- Export in formats their auditors accept

---

## 2. Scope

**In scope:**
- Receiving and durably storing `AuditEventRecord` payloads from the SDK
- Re-verifying Merkle chain integrity on ingest
- Search/filter API over stored records
- Compliance export (JSONL, CSV, PDF summary)
- Audit log viewer UI
- Per-run integrity verification endpoint

**Out of scope (v1):**
- Storing raw tool call payloads or prompt content (privacy; hash-only model)
- Third-party SIEM integrations (Splunk, Datadog) — roadmap
- Real-time streaming of audit events to external systems — roadmap

---

## 3. Data Model

### `AuditRun`

One record per `Agent.run()` call. Created on first audit event received.

```
AuditRun
  id              uuid, PK
  org_id          uuid, FK → Organization
  project         text, not null
  agent_name      text, not null
  run_id          uuid, not null, unique  -- from CloudReporter.on_run_start
  genesis_root    text(64)               -- "0"*64 constant, sanity check
  final_root_hash text(64)               -- hash of last event in chain
  event_count     int, not null
  started_at      timestamptz
  completed_at    timestamptz, nullable
  integrity       enum(verified, failed, pending)
  created_at      timestamptz, default now()
```

### `AuditEvent`

One record per `AuditEventRecord` emitted by the library.

```
AuditEvent
  id              uuid, PK
  run_id          uuid, FK → AuditRun.run_id
  org_id          uuid, FK → Organization  -- denormalized for query efficiency
  event_id        uuid, not null            -- from AuditEventRecord.event_id
  event_type      text, not null            -- "agent_start", "tool_call", etc.
  actor           text, not null
  payload_hash    text(64), not null
  prev_root       text(64), not null
  leaf_hash       text(64), not null
  seq             int, not null             -- position in chain (0-indexed)
  timestamp       timestamptz, not null
  verified        bool, not null, default false

INDEX: (org_id, run_id)
INDEX: (org_id, event_type, timestamp)
INDEX: (org_id, actor, timestamp)
INDEX: (leaf_hash)  -- for point-verification queries
```

---

## 4. Ingest

### 4.1 On receipt of `on_audit_flush`

The ingest worker receives a batch of `AuditEventRecord` objects for a completed run.

**Steps:**

1. Upsert `AuditRun` record (create if new, update `final_root_hash` and `event_count` if exists).
2. Insert all `AuditEvent` rows. On conflict (`event_id`) — ignore (idempotent delivery).
3. Trigger async chain verification job (see §4.2).
4. For runs exceeding retention threshold, schedule archival to object storage (S3/R2).

### 4.2 Chain verification

Runs asynchronously after ingest. Replicates `AuditChain.verify()` server-side:

```
For each event in seq order:
  expected_leaf = sha256(prev_root + event_type + payload_hash + timestamp.isoformat())
  if expected_leaf != stored leaf_hash:
    mark run integrity = "failed"
    emit alert: AuditIntegrityFailure (if alerting configured)
    stop
  prev_root = leaf_hash
Mark run integrity = "verified"
```

A `"failed"` integrity status is immutable — it cannot be reset. The record is preserved for forensic purposes.

---

## 5. API

Base path: `/v1/audit`

All endpoints require `Authorization: Bearer <api_key>` and are scoped to the authenticated organization.

---

### `GET /v1/audit/runs`

List audit runs with filtering and pagination.

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `project` | string | Filter by project name |
| `agent_name` | string | Filter by agent name (exact or prefix `agent_name=billing*`) |
| `from` | ISO8601 | `started_at` lower bound |
| `to` | ISO8601 | `started_at` upper bound |
| `integrity` | `verified\|failed\|pending` | Filter by chain integrity status |
| `limit` | int (max 500, default 50) | |
| `cursor` | string | Opaque pagination cursor |

**Response `200`:**
```json
{
  "runs": [
    {
      "run_id": "uuid",
      "agent_name": "billing-assistant",
      "project": "production",
      "event_count": 14,
      "started_at": "2026-03-12T09:14:22Z",
      "completed_at": "2026-03-12T09:14:31Z",
      "integrity": "verified",
      "final_root_hash": "a3f9..."
    }
  ],
  "next_cursor": "opaque-string-or-null",
  "total": 1482
}
```

---

### `GET /v1/audit/runs/{run_id}`

Retrieve a single run with full event list.

**Response `200`:**
```json
{
  "run_id": "uuid",
  "agent_name": "billing-assistant",
  "project": "production",
  "integrity": "verified",
  "final_root_hash": "a3f9...",
  "events": [
    {
      "seq": 0,
      "event_id": "uuid",
      "event_type": "agent_start",
      "actor": "billing-assistant",
      "payload_hash": "sha256hex",
      "prev_root": "0000...0000",
      "leaf_hash": "b12c...",
      "timestamp": "2026-03-12T09:14:22.103Z",
      "verified": true
    }
  ]
}
```

---

### `GET /v1/audit/runs/{run_id}/verify`

Re-run server-side chain verification on demand and return the result. Does not mutate stored integrity status.

**Response `200`:**
```json
{
  "run_id": "uuid",
  "verified": true,
  "event_count": 14,
  "final_root_hash": "a3f9...",
  "verified_at": "2026-03-12T10:00:00Z"
}
```

**Response `200` (failed):**
```json
{
  "run_id": "uuid",
  "verified": false,
  "broken_at_seq": 7,
  "broken_at_event_id": "uuid",
  "expected_leaf_hash": "abc...",
  "stored_leaf_hash": "xyz...",
  "verified_at": "2026-03-12T10:00:00Z"
}
```

---

### `GET /v1/audit/events`

Search individual events across all runs.

**Query parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `event_type` | string | e.g. `tool_call`, `agent_start` |
| `actor` | string | Filter by actor name |
| `from` | ISO8601 | |
| `to` | ISO8601 | |
| `project` | string | |
| `limit` | int (max 500) | |
| `cursor` | string | |

---

### `GET /v1/audit/runs/{run_id}/export`

Export audit chain for a single run.

**Query parameters:**

| Param | Values | Default |
|-------|--------|---------|
| `format` | `jsonl`, `csv`, `pdf` | `jsonl` |

- `jsonl`: One JSON object per line, identical to `AuditChain.export_jsonl()` output. Can be imported back into a local `AuditChain` for offline verification.
- `csv`: Flat tabular format for spreadsheet tooling. Columns: `seq, event_id, event_type, actor, payload_hash, prev_root, leaf_hash, timestamp, verified`.
- `pdf`: Human-readable compliance report. Includes: run metadata, integrity status, event table, root hash, verification timestamp, agent-kit version. Suitable for attachment to audit submissions.

**Response:** Binary file download with appropriate `Content-Type` and `Content-Disposition: attachment` headers.

---

### `GET /v1/audit/runs/export` (bulk)

Export multiple runs matching filter criteria (same params as `GET /v1/audit/runs`). Returns a `.zip` containing one JSONL file per run. Async for large exports — returns a job ID if >100 runs matched.

---

## 6. UI — Audit Log Viewer

### 6.1 Runs list page

- Table: Agent name, Project, Run ID (truncated), Started, Duration, Events, Integrity badge (green/red/grey)
- Filters: date range picker, project dropdown, agent name search, integrity filter
- Pagination
- "Export all (filtered)" button → triggers bulk export

### 6.2 Run detail page

- Run metadata header: agent, model, project, start/end time, total events, integrity status
- Timeline view: events as chronological list with seq number, event_type badge, actor, timestamp, hash (truncated)
- Hash chain visualization: each event shows prev_root → leaf_hash chain link (collapsible)
- "Verify integrity" button — triggers on-demand re-verification
- "Export run" dropdown (JSONL / CSV / PDF)
- If integrity = failed: red banner identifying the broken sequence position

### 6.3 Event search page

- Full-text search across `event_type` and `actor` fields
- Date range, project, agent filters
- Results table linking to parent run

---

## 7. Acceptance Criteria

- [ ] Audit events ingested with zero data loss for batches up to 10,000 events
- [ ] Chain verification completes within 2 seconds for runs up to 500 events
- [ ] `GET /v1/audit/runs` returns results within 200ms for orgs with up to 1M stored runs
- [ ] JSONL export of a 1,000-event run can be re-imported locally and passes `AuditChain.verify()`
- [ ] A chain with one tampered hash is detected, `integrity = "failed"` is set, and an alert fires (if configured)
- [ ] PDF export renders in standard PDF viewers and contains all required compliance fields
- [ ] Audit records are immutable after write — no `UPDATE` or `DELETE` on `AuditEvent` rows (append-only)
