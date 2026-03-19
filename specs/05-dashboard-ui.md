# Spec 05 — Dashboard UI

**Version:** 0.1
**Date:** 2026-03-19
**Status:** Draft
**Depends on:** All backend specs (01–04) — all APIs are implemented and tested.

---

## 1. Problem

All four commercial services have fully working APIs but no user interface. The product
is currently accessible only to engineers willing to read docs and write curl commands.
Without a UI:

- There is nothing to demo to a prospective customer.
- Alert rules and notification channels can't be configured without raw API calls.
- Audit trails can't be browsed or exported without writing scripts.
- The circuit breaker and cost data that make agent-kit compelling are invisible.

A dashboard is the thing that turns the backend into a product.

---

## 2. Scope

**In scope:**

- Fleet overview: summary stats, cost chart, agent table, live active-run count
- Agent detail: cost/token time-series, run history, circuit breaker timeline
- Audit trail: run list with integrity status, event-chain drill-down, JSONL/CSV export
- Alerts: rule CRUD, channel CRUD (email/Slack/PagerDuty/webhook), firing history + ack
- Settings: API key management, SLA tier display, support context link
- Login: API key entry + validation (no OAuth in v1)
- Fully client-rendered — Next.js App Router, static export compatible

**Out of scope (v1):**

- Agent output / conversation content (privacy — stays out of scope per spec 00)
- WebSocket live streaming (polling is sufficient; WS is a v2 upgrade)
- Multi-user accounts / RBAC (single API key per org in v1)
- Dark mode (add after launch if demand exists)
- Mobile layout (desktop-first; basic responsiveness only)

---

## 3. Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | **Next.js 15** (App Router) | Spec 00 called it out; good SSR/static story; ecosystem fit |
| Language | **TypeScript** | Mandatory — no untyped API calls |
| Styling | **Tailwind CSS v4** | Consistent with the rest of the portfolio |
| Components | **shadcn/ui** | Headless, Tailwind-native, copy-paste model fits the project |
| Charts | **Recharts** | Lightweight, composable, works well with Tailwind color tokens |
| Data fetching | **SWR** | Auto-refresh, deduplication, stale-while-revalidate; simpler than React Query for this use case |
| HTTP client | **native fetch** via a typed API client module | No axios; fetch is fine here |
| Icons | **lucide-react** | Ships with shadcn/ui |
| Date handling | **date-fns** | Small, tree-shakeable |
| Deployment | **Static export** (`next export`) served by the existing Caddy reverse proxy on rising, or Vercel | No Node.js server needed for v1 |

---

## 4. Authentication

No OAuth in v1. Single-key-per-org, matching the backend model.

**Flow:**

1. On first load, check `localStorage` for `agentkit_api_key`.
2. If absent → redirect to `/login`.
3. Login page: single text input + "Connect" button. On submit, call `GET /v1/metrics/summary`
   with the entered key. 200 → store key + redirect to `/dashboard`. 401 → show error.
4. All API calls add `Authorization: Bearer <key>` from the stored value.
5. Any 401 from any API call → clear key + redirect to `/login`.
6. Settings page has a "Disconnect" button that clears the key.

**Security note:** Storing an API key in localStorage is acceptable for a developer-facing
dashboard. The key is already visible to anyone with access to the machine. Do not store in
a cookie (CSRF surface); localStorage is intentional.

---

## 5. API Client

A single module (`lib/api.ts`) wraps all backend calls. No component should call `fetch`
directly.

```typescript
// lib/api.ts — shape (not full implementation)

export interface ApiClientConfig {
  baseUrl: string;   // e.g. "https://api.agentkit.io" or "http://localhost:8000"
  apiKey: string;
}

// One typed function per endpoint:
export async function getSummary(cfg: ApiClientConfig, params?: SummaryParams): Promise<MetricsSummary>
export async function getCost(cfg: ApiClientConfig, params?: CostParams): Promise<CostResponse>
export async function getRuns(cfg: ApiClientConfig, params?: RunsParams): Promise<RunsResponse>
export async function getAgents(cfg: ApiClientConfig, params?: AgentsParams): Promise<AgentsResponse>
export async function getCircuitBreaker(cfg: ApiClientConfig, params?: CBParams): Promise<CircuitBreakerResponse>
export async function getActiveRuns(cfg: ApiClientConfig, params?: ActiveParams): Promise<ActiveRunsResponse>

export async function getAuditRuns(cfg: ApiClientConfig, params?: AuditRunParams): Promise<AuditRunsResponse>
export async function getAuditEvents(cfg: ApiClientConfig, runId: string): Promise<AuditEventsResponse>
export async function exportAuditRun(cfg: ApiClientConfig, runId: string, format: "jsonl" | "csv"): Promise<Blob>

export async function getAlertRules(cfg: ApiClientConfig): Promise<AlertRulesResponse>
export async function createAlertRule(cfg: ApiClientConfig, rule: CreateAlertRuleRequest): Promise<AlertRule>
export async function deleteAlertRule(cfg: ApiClientConfig, ruleId: string): Promise<void>
export async function getAlertChannels(cfg: ApiClientConfig): Promise<AlertChannelsResponse>
export async function createAlertChannel(cfg: ApiClientConfig, ch: CreateChannelRequest): Promise<AlertChannel>
export async function deleteAlertChannel(cfg: ApiClientConfig, channelId: string): Promise<void>
export async function getFiringHistory(cfg: ApiClientConfig, params?: FiringHistoryParams): Promise<FiringHistoryResponse>
export async function ackAlert(cfg: ApiClientConfig, firingId: string): Promise<void>

export async function getSupportContext(cfg: ApiClientConfig): Promise<SupportContextResponse>
```

All functions throw an `ApiError` (with `status: number` and `message: string`) on non-2xx
responses. The SWR hooks in each page wrap these and handle 401 centrally.

---

## 6. Pages and Routes

```
/login                   API key entry
/dashboard               Fleet overview (default landing after login)
/agents/[name]           Agent detail (name = URL-encoded agent_name)
/audit                   Audit run list
/audit/[runId]           Audit run detail — event chain + integrity status
/alerts                  Alert rules + firing history
/alerts/new              Create alert rule (modal or page)
/settings                API key, channels, SLA info
```

---

## 7. Page Specifications

### 7.1 `/login`

- Centered card, 400px wide.
- agent-kit wordmark + tagline ("Production observability for AI agents").
- Text input: placeholder `akt_live_...`. Password input type (masked).
- "Connect" button — disabled while validating.
- On 401: inline error "Invalid API key. Check your agent-kit Cloud settings."
- No signup flow in v1 — link to docs for how to get a key.

---

### 7.2 `/dashboard` — Fleet Overview

**Polling:** Summary + agents every 30s. Active runs every 5s.

**Layout:**

```
┌─────────────────────────────────────────────────────────────┐
│ Header: "Fleet Overview"          Period picker  [Refresh]   │
├──────────┬──────────┬──────────┬──────────────────────────  │
│ Total     │ Error    │ Total    │ Active runs (live, 5s)      │
│ runs      │ rate     │ cost     │ ● 7 running                │
│ 4,821     │ 0.64%    │ $142.38  │                            │
├──────────┴──────────┴──────────┴────────────────────────────┤
│ Cost over time (line chart, one series per agent, top 5)     │
│ [24h ▼]                                                      │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│ Agents                                               Search  │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Agent        Project  Runs  Error  Avg cost  CB  Last   │ │
│ │ billing-…    prod     4,200 0.4%   $0.021   🟢  2m ago │ │
│ │ summarizer   prod       890 1.2%   $0.006   🔴  14s ago│ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

**Period picker:** 1h / 24h / 7d / 30d / Custom. Drives `from`/`to` on all API calls on the page.

**CB state badge:** `🟢` closed, `🔴` open, `🟡` half_open. Tooltip shows last transition time.

**Agent row click** → navigate to `/agents/[name]`.

**Active runs section** (below agents table or sidebar if count > 0):

| Run ID | Agent | Started | Elapsed | Turns | Cost |
|--------|-------|---------|---------|-------|------|
| a3f... | summarizer | 10:14:38 | 4.2s | 2 | $0.008 |

Auto-refreshes every 5s. Shows "No active runs" when empty.

---

### 7.3 `/agents/[name]` — Agent Detail

**Header:** Agent name (large), project badge, models used (pill list), first seen / last seen.

**Period picker:** same as dashboard, persisted in URL (`?from=...&to=...`).

**Tabs:**

#### Tab: Cost & Tokens

- Stacked area chart: input tokens (blue) / output tokens (orange) over time.
- Secondary line chart: cost (USD) over time.
- Stat row: Total cost | Avg cost/run | Cost this period vs previous period (↑↓ % change).

#### Tab: Runs

- Stacked bar chart: success (green) / error (red) per bucket.
- Error rate % line overlay on secondary Y-axis.
- Avg duration line chart (separate, below).
- Recent runs table (last 50):

  | Run ID | Started | Duration | Turns | Cost | Status |
  |--------|---------|----------|-------|------|--------|
  | a3f… | 10:14:38 | 4.2s | 3 | $0.008 | ✅ |
  | b9c… | 10:12:01 | 2.1s | 2 | $0.005 | ❌ |

  Status ✅ = success, ❌ = error. Click row → `/audit/[runId]`.

#### Tab: Circuit Breaker

- Large state badge: CLOSED / OPEN / HALF_OPEN with color and last-updated time.
- State timeline (horizontal): each segment colored by state, width proportional to duration.
  Tooltip on hover shows: state, entered at, duration.
- Stats: Times opened this period | Total open duration | Avg recovery time.
- Event table:

  | Transition | Failure count | At | Open for |
  |-----------|---------------|----|----------|
  | closed → open | 5 | 08:22:11 | 61s |
  | open → half_open | — | 08:23:12 | — |
  | half_open → closed | — | 08:23:14 | — |

---

### 7.4 `/audit` — Audit Run List

**Polling:** None (on-demand). Manual refresh button.

**Filters:** Project (dropdown), Agent (text), Integrity (All / Verified / Failed / Pending), Date range.

**Table:**

| Run ID | Agent | Project | Started | Events | Integrity | Actions |
|--------|-------|---------|---------|--------|-----------|---------|
| a3f… | billing | prod | 10:14 | 12 | ✅ verified | View Export |
| b9c… | summarizer | prod | 10:12 | 8 | ❌ failed | View Export |
| d1e… | … | … | 10:01 | 5 | ⏳ pending | View |

Integrity badge: ✅ verified (green), ❌ failed (red), ⏳ pending (grey).

**Export button** per row: downloads JSONL or CSV (dropdown). Calls
`GET /v1/audit/runs/{run_id}/export?format=jsonl`.

---

### 7.5 `/audit/[runId]` — Run Detail

**Header:** Run ID, agent, project, started/completed timestamps, integrity badge (large).

**Integrity failure banner** (if `integrity = "failed"`):
> ⚠️ Chain integrity check failed at event N. This may indicate data corruption or tampering.
> The hash mismatch is at `leaf_hash` of event `<event_id>`. Contact support.

**Event chain table** (all events in sequence):

| # | Event type | Actor | Timestamp | Payload hash | Leaf hash | Status |
|---|-----------|-------|-----------|-------------|----------|--------|
| 1 | agent_start | run_id | 10:14:38.001 | `3fa2…` | `a1b2…` | ✅ |
| 2 | llm_complete | anthropic | 10:14:38.821 | `9dc1…` | `f3e4…` | ✅ |
| 3 | circuit_breaker_state_change | anthropic | 10:14:39.100 | `2bc3…` | `d5f6…` | ✅ |

Hovering a row shows a tooltip with the full hashes and prev_root.

**Export:** JSONL / CSV buttons in header.

---

### 7.6 `/alerts` — Alerts

**Split layout:** Left panel = Alert Rules. Right panel = Firing History.

**Alert Rules panel:**

- "New rule" button → modal (see below).
- Table:

  | Type | Agent | Channel | Status | Actions |
  |------|-------|---------|--------|---------|
  | circuit_breaker_open | billing-assistant | Slack #ops | Active | Delete |
  | cost_anomaly | * | PagerDuty | Active | Delete |

**New rule modal fields:**

- Type (select): circuit_breaker_open / cost_anomaly / error_rate / audit_integrity_failure
- Agent name (text, `*` for all)
- Project (text, optional)
- Threshold (shown/hidden based on type):
  - cost_anomaly: `threshold_usd` (number)
  - error_rate: `threshold_pct` (number)
  - circuit_breaker_open / audit_integrity_failure: no threshold
- Channel (select from configured channels)

**Channels section** (below rules or separate tab):

- "Add channel" button → modal.
- Table: Name | Type | Target | Actions.
- Channel types: Email / Slack / PagerDuty / Webhook.
- Per-type form fields:
  - Email: `to_address`
  - Slack: `webhook_url`
  - PagerDuty: `routing_key`
  - Webhook: `url`, optional `secret`

**Firing History panel:**

| Alert | Agent | Fired at | Resolved at | Acked |
|-------|-------|----------|-------------|-------|
| circuit_breaker_open | billing | 08:22:11 | 08:23:14 | ✅ |
| cost_anomaly | summarizer | 09:41:00 | — (firing) | Ack |

"Ack" button → calls `POST /v1/alerts/firings/{id}/ack`.
Firing rows where `resolved_at` is null are highlighted (still active).

---

### 7.7 `/settings`

**API Key section:**
- Shows current key prefix (first 12 chars) + masked remainder.
- "Disconnect" button: clears localStorage + redirects to `/login`.
- Note: "To rotate your key, contact support or use the API."

**SLA Tier section:**
- Current tier badge (Free / Pro / Enterprise).
- Feature table comparing tiers.
- Upgrade CTA if on Free/Pro.

**Support section:**
- Shows context pulled from `GET /v1/support/context`:
  active agents count, open circuit breakers, recent audit failures, last 24h cost.
- "Open support ticket" button (links to support email or form).

---

## 8. Shared Components

| Component | Used by |
|-----------|---------|
| `<StatCard>` | Dashboard summary stats |
| `<PeriodPicker>` | Dashboard, Agent detail |
| `<AgentTable>` | Dashboard |
| `<CostChart>` | Dashboard overview, Agent detail |
| `<RunsChart>` | Agent detail |
| `<CBBadge>` | Agent table, Agent detail |
| `<CBTimeline>` | Agent detail Circuit Breaker tab |
| `<AuditEventTable>` | Audit run detail |
| `<IntegrityBadge>` | Audit list + detail |
| `<AlertRuleForm>` | Alerts modal |
| `<ChannelForm>` | Alerts modal |
| `<FiringTable>` | Alerts |
| `<ActiveRunsTable>` | Dashboard live section |
| `<SWRProvider>` | Root layout — provides SWR config + 401 redirect |
| `<ApiClientContext>` | Root layout — injects API key + base URL |

---

## 9. Polling Strategy

| Data | Interval | Notes |
|------|----------|-------|
| Active runs | 5s | Live feel; small payload |
| Summary stats | 30s | Acceptable staleness for overview |
| Agent list + CB states | 30s | Same |
| Cost / runs time-series | 60s | Charts don't need second-level freshness |
| Audit runs list | On demand | Manual refresh |
| Alerts firing history | 30s | Fast enough for on-call use |

SWR `refreshInterval` is set per hook. The 5s active-run hook is paused when the browser
tab is not visible (`refreshWhenHidden: false`).

---

## 10. Project Structure

```
dashboard/
  app/
    layout.tsx              Root layout — ApiClientContext, SWRProvider, nav
    login/page.tsx
    dashboard/page.tsx
    agents/[name]/page.tsx
    audit/page.tsx
    audit/[runId]/page.tsx
    alerts/page.tsx
    settings/page.tsx
  components/
    charts/
      CostChart.tsx
      RunsChart.tsx
      CBTimeline.tsx
    ui/                     shadcn/ui primitives (button, card, badge, table, …)
    StatCard.tsx
    AgentTable.tsx
    ActiveRunsTable.tsx
    CBBadge.tsx
    AuditEventTable.tsx
    IntegrityBadge.tsx
    AlertRuleForm.tsx
    ChannelForm.tsx
    FiringTable.tsx
    PeriodPicker.tsx
  lib/
    api.ts                  Typed API client (all backend calls)
    auth.ts                 localStorage key helpers + 401 handler
    hooks/
      useSummary.ts
      useAgents.ts
      useCost.ts
      useRuns.ts
      useCircuitBreaker.ts
      useActiveRuns.ts
      useAuditRuns.ts
      useAuditEvents.ts
      useAlertRules.ts
      useAlertChannels.ts
      useFiringHistory.ts
      useSupportContext.ts
    utils/
      format.ts             formatCost, formatTokens, formatDuration, formatRelativeTime
      colors.ts             CB state → Tailwind color mapping
  public/
    logo.svg
  next.config.ts
  tailwind.config.ts
  tsconfig.json
  package.json
```

The `dashboard/` directory lives at the repo root alongside `agent_kit/` and `server/`.

---

## 11. Environment Variables

```bash
# dashboard/.env.local (development)
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000   # backend URL

# Production (set in Vercel or Caddy env)
NEXT_PUBLIC_API_BASE_URL=https://api.agentkit.io
```

No server-side secrets — the dashboard is purely client-rendered. The API key is user-supplied
and never baked into the build.

---

## 12. Build Phases

### Phase A — Foundation (unblocks everything)
- Project scaffold: Next.js + Tailwind + shadcn/ui + SWR
- `lib/api.ts` with all endpoint types
- `lib/auth.ts` + 401 redirect logic
- `/login` page
- Root layout with nav + `ApiClientContext`

### Phase B — Fleet Dashboard (highest demo value)
- `/dashboard` page: summary stats, cost chart, agent table
- Active runs section (5s polling)
- `<PeriodPicker>`, `<StatCard>`, `<CostChart>`, `<AgentTable>`, `<CBBadge>`

### Phase C — Agent Detail
- `/agents/[name]` with all three tabs
- `<RunsChart>`, `<CBTimeline>`, recent runs table

### Phase D — Audit Trail
- `/audit` list with integrity badges + export
- `/audit/[runId]` event chain detail
- `<AuditEventTable>`, `<IntegrityBadge>`

### Phase E — Alerts
- `/alerts` with rules CRUD, channel CRUD, firing history + ack
- All alert forms and channel type fields

### Phase F — Settings + Polish
- `/settings`: API key display, SLA tier, support context
- Empty states for all pages
- Error boundaries
- Loading skeletons for all data-fetching components
- Basic responsiveness audit

---

## 13. Acceptance Criteria

- [ ] Login with valid key → `/dashboard` loads with real data within 2s
- [ ] Login with invalid key → error message, no redirect
- [ ] 401 from any API call → clears key, redirects to `/login`
- [ ] Period picker changes propagate to all charts and stats on the page
- [ ] CB badge shows correct color for all three states
- [ ] Active runs section refreshes without full page reload
- [ ] Audit run with `integrity=failed` shows red banner with event location
- [ ] Audit export downloads valid JSONL that passes `AuditChain.verify()`
- [ ] Alert rule creation round-trips correctly (create → appears in list → delete → gone)
- [ ] Slack channel: saving with a valid webhook URL shows success; invalid URL shows API error
- [ ] All pages render without JS errors in browser console
- [ ] No API calls contain raw prompt content (payload hashes only)
- [ ] `npm run build` produces zero TypeScript errors
