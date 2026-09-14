# Spec 09 — Cost Circuit Breaker

Status: **implemented** · Written 2026-09-14 · Roadmap item: 3.2 (`specs/06-harness-roadmap.md`)

## Goal

"Your agent stops at $50." agent-kit enforces dollar ceilings on agent spend — per run, and per
agent / project / org over a calendar period — and alerts when a ceiling trips. Enforcement covers
native agent-kit agents and, opt-in, Claude Agent SDK and OpenAI Agents SDK agents.

**Done means:** against a running server, SDK traffic pushes a `$X/day` budget over its limit; the
server marks it tripped and a `budget_exceeded` webhook alert fires; the next `Agent.run()` for a
matching agent raises `BudgetExceededError` before calling the model; raising the limit closes the
budget and resolves the alert. A per-run cap stops a run before the call that would follow reaching it.

## Decisions

1. **Two ceilings.** A per-run cap (`max_run_cost_usd`) enforced locally from the run's own spend,
   and fleet budgets defined on the server and enforced by SDKs from fetched status.
2. **Calendar periods, UTC.** `daily` resets 00:00, `weekly` Monday 00:00, `monthly` the 1st
   00:00. A tripped budget stays tripped until the period rolls over or the limit is raised above
   current spend.
3. **Fail open.** If budget status can't be fetched, agents keep running (debug log); `fail_closed`
   opts into refusing instead.
4. **Adapters enforce when asked.** Enforcement is opt-in on `ClaudeAgentObserver` and a new
   `AgentKitRunHooks`; default adapter behaviour stays observe-only.
5. **Bounded overshoot, stated plainly.** Per-run cap: at most one model call past the cap. Fleet
   budget: one call per process, plus spend by other processes within the status refresh interval.
   Claude Agent SDK stops at the next tool/subagent boundary.

## Server

### Data

Migration `006_budgets`:

```
budgets
  id            VARCHAR(36) PK
  org_id        VARCHAR(36) NOT NULL          INDEX (org_id, enabled)
  name          VARCHAR(255) NOT NULL
  project       VARCHAR(255) NOT NULL DEFAULT '*'
  agent_name    VARCHAR(255) NOT NULL DEFAULT '*'
  period        VARCHAR(16)  NOT NULL          -- daily | weekly | monthly
  limit_usd     FLOAT        NOT NULL          -- > 0
  enabled       BOOLEAN      NOT NULL DEFAULT true
  tripped_at    DATETIME     NULL            -- set on trip, cleared on close
  created_at, updated_at
```

Scope matching uses the alerting wildcard rule: `"*"` matches anything, otherwise exact match.

### Spend

`spend(budget, now)` =
- Σ `agent_metric_snapshots.cost_usd` for the org with `bucket >= period_start(now)` and matching
  project / agent, **plus**
- Σ `active_run_cache.cost_so_far_usd` for matching in-flight runs active within the last hour
  (`last_event_at`, else `started_at`) — so a runaway run counts before it finishes. A run's cost
  moves from the cache to a snapshot on completion, so nothing is counted twice.

`resets_at = period_start(now) + period`. `tripped = enabled and spend >= limit_usd`.

### Evaluation — `app/budgets.py`

`evaluate_budgets(org_id, db, now) -> list[BudgetStatus]` computes status for the org's budgets and
reconciles state:
- **Trip:** `tripped` and `tripped_at is None` (or `tripped_at < period_start`) → set
  `tripped_at = now`, fire `budget_exceeded` alerts.
- **Close:** not `tripped` and `tripped_at` set → clear it, resolve those alerts. Covers period
  rollover, a raised limit, and disabling.

Called after every committed `POST /v1/events` and `POST /v1/traces` batch for the org, on
`GET /v1/budgets` and `GET /v1/budgets/status`, after `PATCH`, and by the background worker every
60 s (for rollover). Evaluation failures are logged and never fail ingest.

### API — `app/routers/budgets.py`

| Route | Behaviour |
|---|---|
| `GET /v1/budgets` | All budgets with live status: `spent_usd`, `remaining_usd`, `tripped`, `period_start`, `resets_at` |
| `POST /v1/budgets` | Create: `name`, `period`, `limit_usd`, optional `project`, `agent_name`, `enabled`. `400` on bad period or non-positive limit |
| `PATCH /v1/budgets/{id}` | Update any field; re-evaluates immediately (raising the limit closes a tripped budget) |
| `DELETE /v1/budgets/{id}` | Delete; resolves its active alert firings |
| `GET /v1/budgets/status?project=&agent_name=` | Enabled budgets whose scope matches the given agent, with live status. Used by SDKs |

### Alerting

New rule type `budget_exceeded`, config `{"budget_id": "<id>"}` or `{"budget_id": "*"}`.
Event-driven: fires on trip with context `{budget_id, budget_name, project, agent_name, period,
limit_usd, spent_usd, resets_at}` and auto-resolves on close. Existing channels, dedup, mute, and
ack apply. Firings are per rule; a `"*"` rule's firing context names the budget that tripped first
and resolves when no budget it covers is tripped.

## SDK

### Error

```python
class BudgetExceededError(AgentKitError):
    scope: Literal["run", "budget"]
    limit_usd: float
    spent_usd: float
    budget_name: str | None      # fleet budgets
    resets_at: datetime | None   # fleet budgets
```

### Per-run cap

`AgentConfig(max_run_cost_usd: float | None = None)`. `AgentLoop` checks before every provider call
(`run()` and `stream()`): if the run's accumulated `cost_usd` ≥ the cap → append a
`budget_exceeded` audit event (`scope`, `limit_usd`, `spent_usd`) and raise. Existing error
handling reports `run_error` to Cloud.

### Fleet budgets — `agent_kit/cloud/budgets.py`

```python
class BudgetGuard:
    def __init__(self, reporter: CloudReporter, refresh_interval_s: float = 30.0, fail_closed: bool = False): ...
    async def check(self, agent_name: str, project: str) -> None      # raises BudgetExceededError
    def record_spend(self, agent_name: str, project: str, usd: float) -> None
```

- Fetches `GET {base_url}/v1/budgets/status?project=&agent_name=` with the reporter's key; caches
  per `(project, agent_name)` for `refresh_interval_s`.
- Effective spend = server `spent_usd` + this process's `record_spend` total since that fetch; trips
  when ≥ `limit_usd` (or when the server already reports `tripped`).
- Fetch failure: `fail_closed=False` → allow and retry on the next check; `True` → raise
  `BudgetExceededError(scope="budget", budget_name=None)`.
- `CloudReporter.budget_guard(refresh_interval_s=30.0, fail_closed=False) -> BudgetGuard` returns one
  shared guard per reporter.

`AgentConfig(enforce_budgets: bool = False)`: requires `cloud` (`Agent` raises `ValueError` at
construction otherwise); the loop calls `check()` before each
provider call and `record_spend()` after each turn, using the reporter's agent name and project.

## Adapters

### Claude Agent SDK

`ClaudeAgentObserver(reporter, agent_name=None, max_run_cost_usd=None, enforce_budgets=False)`:
- `with_hooks(options)` sets `options.max_budget_usd = max_run_cost_usd` when the caller hasn't set
  one — the SDK enforces the per-run cap natively.
- With `enforce_budgets`, `PreToolUse` and `SubagentStart` hooks await `guard.check()`; when it
  raises they record a `budget_exceeded` audit event and return
  `{"continue_": False, "stopReason": "<reason>"}` (plus `permissionDecision: "deny"` for
  `PreToolUse`), halting the agent at that boundary. Each turn's priced cost is passed to
  `guard.record_spend()`.

### OpenAI Agents SDK

New `AgentKitRunHooks(reporter, agent_name=None, max_run_cost_usd=None, enforce_budgets=True)`
(`agents.RunHooks`), passed as `Runner.run(agent, input, hooks=...)`:
- `on_llm_start`: per-run spend = `context.usage` tokens priced at the agent's model (string, or a
  model object's `.model`); ≥ cap → raise. With `enforce_budgets`, `await guard.check()`. The
  exception propagates out of `Runner.run` unchanged before the model is called (verified against
  openai-agents 0.22).
- `on_llm_end`: `guard.record_spend()` with the call's priced cost.
- Works alongside `AgentKitTraceProcessor`, which keeps reporting spend.

## Failure handling

- Guard fetch errors never raise unless `fail_closed`.
- Budget evaluation errors on the server are logged; ingest still succeeds.
- Hooks swallow unexpected errors (observe-only semantics) — only a confirmed budget trip stops an agent.

## Testing

- **Server** `tests/test_budgets.py`: period boundaries (day/week/month, UTC); spend from snapshots +
  in-flight runs, wildcard scopes, other orgs excluded; trip sets `tripped_at` and fires a
  `budget_exceeded` firing; rollover and limit raise close and resolve; disabled budgets never trip;
  CRUD validation; status endpoint scoping; ingest triggers evaluation.
- **SDK** `tests/test_budgets.py`: per-run cap stops before the next call (provider call count),
  audit event, `run()` and `stream()`; guard trip from status, local spend accumulation, refresh
  interval, fail-open vs fail-closed (fake HTTP transport, no network).
- **Adapters**: Claude hooks return the stop output when tripped and nothing otherwise;
  `max_budget_usd` set by `with_hooks`. OpenAI `AgentKitRunHooks` raises from `on_llm_start` through
  a real `Runner.run` with a stub model that must never be called.
- **End to end**: running server + webhook receiver; the "Done means" scenario.

## Out of scope

Dashboard UI; rolling windows; server-enforced per-run caps; cost forecasting; reserving estimated
cost before a call; per-budget notification channels (use alert rules).
