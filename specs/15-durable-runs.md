# Spec 15 — Durable Runs

Status: **approved** · Written 2026-09-14 · Roadmap item: 2.5 (`specs/06-harness-roadmap.md`)

## Goal

Agent runs survive crashes, deploys, and humans who take a day to answer. A run checkpoints at every turn
boundary, can suspend on an approval and be resumed from another process, and never runs a
non-idempotent tool twice.

**Done means:** process A runs an agent whose `refund` tool requires approval with `approver=SUSPEND`; the
run returns `status="suspended"` with the pending call and process A exits; process B builds the same
`Agent` and calls `agent.resume(run_id, approvals={call_id: True})`; the refund runs once, the run
completes, and the audit chain verifies end to end. A process killed while a non-idempotent tool is running
resumes with that call reported to the model as interrupted; an idempotent one re-runs; two workers resuming
the same run cannot both execute a tool.

## Decisions

1. **Checkpoint at turn boundaries**, not event-sourced replay. Tools, hooks, and providers are code; the
   resuming process rebuilds the same `Agent` and only data is restored.
2. **Suspension is a normal outcome:** `AgentResult.status == "suspended"`, not an exception.
3. **Interrupted calls:** idempotent tools re-run; others become a tool error the model sees.
4. **Storage:** an async `RunStore` protocol with `SQLiteRunStore`; every write is compare-and-swap on a
   version number.
5. **No server changes.** A suspended run stops reporting; Cloud keeps its `run_id` and receives the rest of
   the run's events after resume.

## API

### `agent_kit/types.py`

```python
class PendingApproval(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None
    turn: int


class AgentResult(BaseModel, Generic[T]):
    ...                                                   # existing fields
    run_id: str | None = None
    status: Literal["completed", "suspended"] = "completed"
    pending_approvals: list[PendingApproval] = Field(default_factory=list)
```

`AgentResult.total_cost_usd` / `total_tokens` become the sums over the run's own `turns` (previously the
agent tracer's cumulative totals, which included earlier runs on the same agent).

### `agent_kit/hooks.py`

```python
SUSPEND: Final                                            # sentinel approver
Approver = Callable[[ApprovalRequest], Awaitable[bool]]   # unchanged; AgentConfig.approver accepts Approver | SUSPEND
```

### `agent_kit/exceptions.py`

```python
class RunNotFoundError(AgentKitError):     run_id: str
class RunConflictError(AgentKitError):     run_id: str; expected_version: int; actual_version: int | None
class CheckpointError(AgentKitError):      run_id: str   # unusable checkpoint (bad schema version, output type mismatch, audit chain fails verification)
```

### `agent_kit/durable/` (new package)

```python
RunStatus = Literal["running", "suspended", "completed", "failed"]
CHECKPOINT_SCHEMA_VERSION = 1


class PendingTurn(BaseModel):
    turn: Turn                                   # the model turn whose tool calls are being resolved
    results: dict[str, ToolResult] = {}          # call_id → final (post-hook) result
    started: list[str] = []                      # call_ids whose tool execution began
    approvals: list[PendingApproval] = []        # calls awaiting a decision


class RunCheckpoint(BaseModel):
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    version: int                                 # store CAS version; 1 for the first write
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    prompt: str
    context: dict[str, Any]
    output_type_name: str | None
    messages: list[Message]                      # full memory history (system messages included)
    turns: list[Turn]
    turn_count: int
    run_cost_usd: float
    invalid_answers: int
    native_output: bool
    last_prompt_tokens: int
    last_prompt_chars: int
    audit_events: list[AuditEventRecord]
    pending: PendingTurn | None = None
    result: dict[str, Any] | None = None         # AgentResult.model_dump(mode="json") when completed
    error: str | None = None                     # "ExceptionType: message" when failed


class RunSummary(BaseModel):
    run_id: str
    status: RunStatus
    updated_at: datetime
    pending_approvals: int


class RunStore(Protocol):
    async def save(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint
    async def load(self, run_id: str) -> RunCheckpoint | None
    async def mark_tool_started(self, run_id: str, call_id: str, expected_version: int) -> int
    async def list(self, status: RunStatus | None = None, limit: int = 100) -> list[RunSummary]
    async def delete(self, run_id: str) -> None


class SQLiteRunStore:                            # implements RunStore
    def __init__(self, path: str | Path = ":memory:") -> None
    def close(self) -> None
```

- `save` writes `checkpoint` with `version = expected_version + 1` (and `updated_at = now`) only if the
  stored version equals `expected_version` (`0` = must not exist); returns the stored checkpoint;
  otherwise raises `RunConflictError`.
- `mark_tool_started` appends `call_id` to `pending.started` under the same CAS rule and returns the new
  version.
- `SQLiteRunStore`: table `runs(run_id TEXT PRIMARY KEY, version INTEGER, status TEXT, updated_at TEXT,
  pending_approvals INTEGER, checkpoint TEXT)` with an index on `status`; each operation runs in
  `asyncio.to_thread` under a `threading.Lock`, CAS via `UPDATE … WHERE run_id=? AND version=?` (row
  count checked) or `INSERT` for version 0.

### `AgentConfig` / `Agent`

```python
AgentConfig(run_store: RunStore | None = None, approver: Approver | SUSPEND | None = None, ...)

async def run(self, prompt, *, output_type=None, run_id: str | None = None, **context) -> AgentResult[...]
async def stream(self, prompt, *, output_type=None, run_id: str | None = None, **context) -> AsyncIterator[str]
async def resume(self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type=None) -> AgentResult[...]
async def resume_stream(self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type=None) -> AsyncIterator[str]
```

- `approver=SUSPEND` without `run_store` → `ValueError` at `Agent()`.
- `run_id=` without `run_store` is accepted and used as the run id (no persistence).
- `resume()` / `resume_stream()` without `run_store` → `ValueError`.
- `run_id` and `approvals` are reserved keywords (never forwarded into hook context). `resume` has the same
  typing overloads as `run`.

`agent_kit.durable` exports `RunStore`, `SQLiteRunStore`, `RunCheckpoint`, `RunSummary`, `PendingTurn`;
`agent_kit` exports `SUSPEND`.

## Behaviour

Everything below applies only when `run_store` is set; without it runs behave as today (plus the
`run_id`, `status`, and totals fields on `AgentResult`).

### Starting a run

1. `run_id` = the given id or a new UUID4. `json.dumps(context)` must succeed, else `TypeError("run
   context must be JSON-serialisable when run_store is set")` before any provider call.
2. Save the initial checkpoint (`status="running"`, `expected_version=0`); an existing run id raises
   `ValueError("run '<id>' already exists; use agent.resume()")` (mapped from the `RunConflictError`).
3. The loop keeps the last saved checkpoint and its version; all store writes from one loop are serialised
   through an `asyncio.Lock`.

### Checkpoint points

| # | When | Status | `pending` |
|---|---|---|---|
| A | A model turn with tool calls has been added to memory, before any tool runs | `running` | `PendingTurn(turn)` |
| B | Immediately before a tool function is called (after `before_tool` hooks allow it) | unchanged | `mark_tool_started` |
| C | All of the turn's tool calls are resolved, results added to memory, turn recorded | `running` | `None` |
| D | A turn resolved with approvals still pending | `suspended` | `PendingTurn` with `results` and `approvals` |
| E | The run produced its final result | `completed` | `None`, `result` set |
| F | The run raised (any exception except the conflict below) | `failed` | the last saved checkpoint's, with `error` set |

A process that dies leaves the last A/B/C checkpoint with `status="running"`. A failure (F) keeps the state
of the last successful checkpoint, so resume retries from there; work after it (e.g. a model call whose
turn was never checkpointed) is redone. A `RunConflictError` from any write aborts the run without writing
F (another worker owns it).

### Suspending for approval

With `approver=SUSPEND`, a `before_tool` `ask` for a call:
- appends `approval_requested` (as today), records `PendingApproval(call_id, tool_name, arguments, reason,
  turn)`, and does not run the tool;
- other calls in the turn resolve normally (run, deny, or suspend).

When the turn's calls have all resolved or suspended and at least one is pending: tool results for the
resolved calls stay in `pending.results` (not memory), the run audits `run_suspended` (`turn`,
`pending_call_ids`), writes checkpoint D, and returns `AgentResult(status="suspended", run_id,
pending_approvals, output="", turns, totals, audit_root_hash)`. No `run_complete` or `audit_flush` is sent to
Cloud. `stream()` ends and `agent.last_result` holds the same result.

### Resuming — `agent.resume(run_id, approvals=None, output_type=None)`

1. `load(run_id)`; `None` → `RunNotFoundError`. `schema_version` newer than supported → `CheckpointError`.
2. `status="completed"` → return `AgentResult` rebuilt from `result` (with `parsed` re-validated when
   `output_type` is given); nothing else happens.
3. `output_type` name must equal `output_type_name` (both `None`, or equal names) → else `CheckpointError`.
4. Restore: memory `clear()` then `add_many(messages)`; the agent's audit chain replaced by
   `AuditChain.restore(audit_events)` (which re-verifies — failure raises `CheckpointError`); turns,
   counters, run cost, typed-output mode, token-estimate state; context for hooks.
5. Save `status="running"` with `expected_version=checkpoint.version` (claims the run; a concurrent resume
   loses with `RunConflictError`). Audit `run_resumed` (`turn`, `from_status`).
6. If `pending` is set, resolve it before the next model call — for each tool call of `pending.turn`, in
   order:
   - result in `pending.results` → keep;
   - pending approval and `approvals[call_id]` is `True` → audit `approval_granted` (`call_id`, `via:
     "resume"`), checkpoint B, execute the tool, run `after_tool` hooks (no `before_tool` re-check);
   - pending approval and `approvals[call_id]` is `False` → audit `approval_denied` (`call_id`,
     `timed_out: false`, `error: null`, `via: "resume"`) and the call is denied with `"approval denied"`
     (`tool_denied`, tool error `"Tool call denied: approval denied"`);
   - pending approval with no answer → stays pending;
   - in `pending.started` (no result) and the tool's schema is `idempotent` → run through the normal path
     (hooks, checkpoint B, execution);
   - in `pending.started` and not idempotent → result `error="Tool call interrupted before completion; not
     retried"`; audit `tool_interrupted` (`call_id`);
   - otherwise (never started) → the normal path; a `before_tool` `ask` may suspend again.
   Then, if approvals remain pending → checkpoint D and return suspended (no model call). Otherwise add the
   tool messages in call order, audit `tool_call` events, record the turn, send `turn_complete`, checkpoint C.
   Answers in `approvals` for call ids that are not pending are ignored.
7. Continue the normal loop; Cloud `run_start` is not re-sent (the run id is unchanged).

`resume_stream` is identical, yielding text from model turns after the resume point.

### Audit summary

| Event | Actor | Payload |
|---|---|---|
| `run_suspended` | run id | `turn`, `pending_call_ids` |
| `run_resumed` | run id | `turn`, `from_status` |
| `tool_interrupted` | tool name | `call_id` |
| `approval_granted` / `approval_denied` | tool name | existing payloads plus `via: "resume"` when answered on resume |

### `AuditChain.restore`

`AuditChain.restore(events: list[AuditEventRecord]) -> AuditChain` (classmethod): a chain holding `events`
with its root set to the last `leaf_hash`, verified with `verify()`.

## Testing

- **`tests/test_run_store.py`** — `SQLiteRunStore` (file and `:memory:`): insert with version 0, duplicate
  insert conflict, CAS update and stale-version conflict, `mark_tool_started` CAS and ordering, `load` of
  missing run, `list` by status with pending counts, `delete`; checkpoint JSON round-trip including
  native content, `ToolResult`, `AuditEventRecord` timestamps.
- **`tests/test_durable_runs.py`** (scripted providers, real tools, file-backed store, a second `Agent`
  instance standing in for another process):
  - suspend on approval → result fields, audit, checkpoint D; resume approve → tool runs once, run completes,
    audit chain verifies across both halves; resume deny → model sees the denial.
  - mixed turn: an allowed call runs while another suspends; results reach memory together in call order.
  - partial answers keep the run suspended with no model call; answers for unknown call ids ignored.
  - crash simulation: a tool raising `KeyboardInterrupt` after checkpoint B; resume re-runs an idempotent
    tool, reports a non-idempotent one as interrupted, runs never-started calls; provider raising after
    checkpoint C → resume repeats only the model call.
  - failure → status `failed` with error; resume retries from the last good checkpoint.
  - concurrent resume: two agents resume the same run concurrently; exactly one executes the tool, the
    other raises `RunConflictError`.
  - completed run → `resume` returns the stored result without provider calls.
  - typed output across suspension (`parsed` on the resumed result; mismatched `output_type` →
    `CheckpointError`); run cost and `max_run_cost_usd` carry across resume; `resume_stream` parity; a
    duplicate `run_id` raises; non-JSON context raises; `SUSPEND` without a store raises; totals equal the
    run's own turns on a multi-run agent.
- **Live** (outside CI): two real processes against Ollama — A suspends and exits, B resumes and completes;
  a process killed with SIGKILL during a slow non-idempotent tool, resumed by a new process.

## Out of scope

Cloud-hosted run store and approvals UI; approval expiry; checkpoint encryption and retention;
event-sourced replay; resuming `Pipeline` / DAG runs; incremental (delta) checkpoint storage.
