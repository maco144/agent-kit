# Spec 16 — Agent as Tool

Status: **implemented** · Written 2026-09-15 · Roadmap item: 2.6 (`specs/06-harness-roadmap.md`)

## Goal

An agent can delegate work to another agent the same way it calls a tool — and delegation cannot escape the
parent's policy, budget, approvals, durability, or audit. `DAGOrchestrator` stays for static graphs; this is
for delegation the model decides on at run time.

**Done means:** a `lead` agent with `run_store=SQLiteRunStore(...)`, `approver=SUSPEND`, and
`max_run_cost_usd=2.00` has two agent tools, `research` and `refunds`. The model calls both in one turn.
`research` completes; `refunds` reaches `issue_refund`, which its own hook gates with `require_approval`. The
lead run returns `status="suspended"` with one pending approval, `call_id="<lead call>/<refunds call>"`, and
the process exits. A second process calls `lead.resume(run_id, approvals={that_id: True})`; the refund runs
once, `refunds` completes, the lead finishes with `total_cost_usd` covering all three runs, and the lead's
audit chain records each child's final root hash. A `deny_tools("close_account")` hook on `lead` blocks
`close_account` inside any child. Killing the process while `refunds` is mid-run and resuming `lead` resumes
the `refunds` child from its checkpoint instead of reporting the delegation as interrupted.

## Decisions

1. **Delegation only.** The parent calls the child, receives its answer as a tool result, and keeps control.
   No handoffs (child's answer becoming the run's answer).
2. **Every delegation is a fresh child run.** The child `Agent` is a template: each call builds a new loop
   with new memory and a new audit chain from the child's config. The child instance is never mutated, so
   concurrent calls in one turn are safe.
3. **Policy stacks.** The child runs its own hooks, then the parent's (transitively up the tree); any deny
   wins. Run-scoped settings — approver, approval timeout, run store — come from the parent run.
4. **Approvals bubble up.** A suspended child suspends the parent; the child's pending approvals appear in the
   parent's `pending_approvals` under path-prefixed call ids, and `parent.resume(...)` routes answers down.
5. **Loop-aware `AgentTool`.** The loop recognises `AgentTool` and calls `delegate()` with the parent run's
   state; suspension is a return value, never an exception inside tool code.
6. **Deterministic child run ids** — `uuid5` of `"{parent_run_id}/{call_id}"` — so resume and crash recovery
   find the child checkpoint, and ids stay 36 characters and URL-safe for agent-kit Cloud.
7. **No server changes.** Child runs report as ordinary runs; `run_start` carries `parent_run_id` in its
   payload. Server-side run trees are a follow-up.

## API

### `agent_kit/types.py`

```python
class ToolResult(BaseModel):
    ...                                  # existing fields
    cost_usd: float = 0.0                # spend incurred inside the tool (delegated runs)
    tokens: int = 0


class PendingApproval(BaseModel):
    ...                                  # existing fields
    run_id: str | None = None            # run that owns the gated call; None for the run's own calls
```

`call_id` of a bubbled approval is the path from the top-level run: `"{call_id}/{child_call_id}"`, one
segment per level. `tool_name`, `arguments`, `reason`, and `turn` describe the gated call in the run that owns
it.

### `agent_kit/durable/models.py`

```python
class PendingTurn(BaseModel):
    ...                                                      # existing fields
    delegated_cost_usd: dict[str, float] = Field(default_factory=dict)  # call_id → child spend already added to the run
    delegated_tokens: dict[str, int] = Field(default_factory=dict)
    delegated_root_hash: dict[str, str] = Field(default_factory=dict)  # call_id → completed child's audit root


class RunCheckpoint(BaseModel):
    ...                                  # existing fields
    parent_run_id: str | None = None
    parent_call_id: str | None = None
```

All new fields have defaults; `CHECKPOINT_SCHEMA_VERSION` stays `1`.

### `agent_kit/agent/delegation.py` (new)

```python
DELEGATION_NAMESPACE: uuid.UUID          # fixed namespace for child run ids


def child_run_id(parent_run_id: str, call_id: str) -> str:
    return str(uuid.uuid5(DELEGATION_NAMESPACE, f"{parent_run_id}/{call_id}"))


@dataclass(frozen=True)
class DelegationContext:
    """Parent run state handed to AgentTool.delegate()."""
    parent_run_id: str
    call_id: str
    depth: int                           # depth of the child run (top-level run = 0, its children = 1)
    max_depth: int
    context: dict[str, Any]              # parent run context
    hooks: Hooks | None                  # parent's effective (already stacked) hooks
    approver: Approver | Suspend | None
    approval_timeout_s: float
    run_store: RunStore | None
    remaining_cost_usd: float | None     # parent cap minus parent spend; None = uncapped
    budget_guard: BudgetGuard | None
    reporter: CloudReporter | None
    approvals: dict[str, bool]           # answers for this child, prefix stripped


@dataclass(frozen=True)
class Delegation:
    run_id: str | None                   # None when no child run started (depth limit)
    status: Literal["completed", "suspended", "failed"]
    result: ToolResult | None            # completed or failed
    approvals: list[PendingApproval]     # suspended: child's pending approvals, ids already prefixed
    cost_usd: float                      # child's cumulative spend (all levels below)
    tokens: int
    root_hash: str | None                # child's audit root hash when completed


class AgentTool(Tool):
    agent: Agent
    output_type: Any

    async def delegate(self, task: str, ctx: DelegationContext) -> Delegation: ...
    async def __call__(self, call_id: str | None = None, **kwargs: Any) -> ToolResult: ...
        # outside a loop: runs the child standalone with its own config
```

Input schema: `{"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}`.

### `AgentConfig` / `Agent`

```python
AgentConfig(..., max_delegation_depth: int = 5)     # read from the top-level run, inherited by children

def as_tool(self, name: str, description: str, *, output_type: Any = None) -> AgentTool
```

`name` must match `^[A-Za-z0-9_-]{1,64}$`; `description` must be non-empty (`ValueError` otherwise).
Called standalone (`await tool(task=...)`), the child runs with its own full config; if it suspends, the
result is the tool error `delegated agent suspended; run it as a tool inside an agent to resume approvals`. With
`output_type`, the tool output is the child's `parsed` value as JSON (`model_dump(mode="json")` /
`TypeAdapter.dump_python(mode="json")`); without it, the child's text output.

`Agent.config` exposes the `AgentConfig`. `Agent._make_loop(**overrides)` accepts any `AgentLoop` keyword
(delegation passes `memory`, `audit`, `hooks`, `approver`, `approval_timeout_s`, `run_store`,
`max_run_cost_usd`, `budget_guard`, `reporter`, `delegation_depth`, `max_delegation_depth`, `parent_run_id`,
`parent_call_id`). `Agent._open_delegated(run_id, output_type, **overrides) -> (AgentLoop, RunCheckpoint | None)`
builds a loop over fresh memory and audit, restored from the run's checkpoint when one exists that is not
completed. `AgentLoop.spend() -> (cost_usd, tokens)` reports a run's spend so far, including a run that raised.

### `agent_kit/cloud/reporter.py`

```python
async def on_run_start(self, run_id: str, model: str | None, prompt: str, parent_run_id: str | None = None) -> None
```

`payload["parent_run_id"]` is set only when not `None`.

## Behaviour

### Building a child run

`delegate()` builds the child loop from `self.agent`'s config with these overrides:

| Setting | Value |
|---|---|
| memory | new `InMemoryStore(window=child.memory_window)` |
| audit | new `AuditChain()` when the child has `audit_enabled` |
| hooks | `Hooks(before_tool=child + parent, after_tool=child + parent, before_llm=child + parent)` (child first; parent lists are already stacked from above) |
| approver, approval_timeout_s, run_store | parent's (the child config's values are ignored) |
| max_run_cost_usd | `min(child cap, ctx.remaining_cost_usd)`, ignoring `None`s |
| budget_guard | child's own when the child has `enforce_budgets`, else parent's |
| reporter | child's `cloud` when set, else parent's |
| run id | `child_run_id(ctx.parent_run_id, ctx.call_id)` |
| context | `{**ctx.context, "parent_run_id": ctx.parent_run_id}` |
| depth | `ctx.depth`; the child's own `AgentTool` calls get `ctx.depth + 1` |

Everything else — provider, model, tools, `allowed_tools`, system prompt, `max_turns`, retry, circuit breaker,
`output_retries`, thinking/effort/caching/compaction, `context_budget_tokens` — comes from the child's config.
Children run non-streaming; `parent.stream()` yields only the parent's text.

If `ctx.depth > ctx.max_depth`, `delegate()` returns `status="failed"` with the tool error
`delegation depth limit (<max>) exceeded` and starts no run.

### Starting or continuing a delegation

In `_run_tool`, after the `before_tool` gate passes and the call is marked started (checkpoint B), an
`AgentTool` call goes to `delegate(task, ctx)` instead of `tool(**arguments)`. With a run store, `delegate()`
first loads the child run id:

| Child checkpoint | Action |
|---|---|
| none | start a new child run (`run_id` = child id) |
| `completed` | return the stored result (`Agent._stored_result`); nothing re-runs |
| `running`, `suspended`, `failed` | `resume` the child with `ctx.approvals` |

Without a run store there is nothing to load; the child starts fresh.

### Outcomes in the parent

- **completed** — the tool result is the child output with `cost_usd` / `tokens` set to the child's totals;
  `after_tool` hooks run on it as for any tool.
- **failed** — the tool result is the error (see Errors); `after_tool` hooks run.
- **suspended** — no result is recorded. The call's parked approvals are the child's approvals with call ids
  prefixed by `"{call_id}/"` (the child has already prefixed deeper levels) and `run_id` set to the owning
  run where it was `None`. `_suspended` becomes `dict[str, list[PendingApproval]]`. The parent suspends
  through the existing path (checkpoint D); `AgentResult.pending_approvals` is the flat list.

### Cost

When `delegate()` returns (any status), the parent adds
`delta = delegation.cost_usd - pending.delegated_cost_usd.get(call_id, 0.0)` to `_run_cost_usd`, records the
new cumulative value in `pending.delegated_cost_usd[call_id]` (tokens likewise; a completed child's root hash
goes to `pending.delegated_root_hash[call_id]`). Delegations that started no run (`run_id is None`) record
nothing. The next
`_enforce_budgets()` sees the child's spend against the run cap. Fleet spend is recorded by the child loop
under the child's reporter (`agent_name` / `project`), matching how the server attributes the child run; the
parent does not record it again.

`_totals()` becomes: sum of turn costs + sum of `tool_result.cost_usd` over recorded turns + sum of
`pending.delegated_cost_usd` for calls without a recorded result (tokens likewise). The server sums per-run
`turn_complete` costs, so parent and child spend are not double-counted in Cloud.

### Resuming

`_resolve_tools`, for each tool call of the pending turn without a result:

1. **Own approval** (`a.call_id == tc.call_id`) — unchanged.
2. **Delegated approvals** (`a.call_id.startswith(tc.call_id + "/")`) — if `answers` contains none of those
   ids, leave the call parked. Otherwise remove the call's delegated approvals from `pending.approvals`, strip
   the prefix from the answered ids, and run `_run_tool(tc, gate=False, approvals=stripped)`. The child
   resumes; unanswered approvals keep it — and the parent — suspended, re-parked with the child's current
   list.
3. **Started, no result, `AgentTool`, run store set** — `_run_tool(tc, gate=False, approvals={})`, which
   resumes or replays the child from its checkpoint. No `tool_interrupted` event. (Started implies the gate
   already passed.)
4. **Started, no result, otherwise** — unchanged (`tool_interrupted` unless idempotent).

A child whose first checkpoint was never written (crash between checkpoint B and the child's `create`) is
started fresh: `create` precedes the child's first model call, so nothing had run.

### Audit

- Parent `tool_call` for a completed or failed delegation adds `delegated_run_id`, `delegated_root_hash`
  (completed only), and `delegated_cost_usd`. The parent chain therefore commits to the child chain's final
  root hash.
- The child chain is independent. Its `agent_start` `context_keys` include `parent_run_id`.
- `run_suspended` in the parent lists the prefixed pending call ids (existing payload).

### Cloud

Child runs report as ordinary runs under their reporter's `agent_name` / `project`, with `parent_run_id` in
`run_start`. No server changes.

### Errors

| Raised in the child | Parent |
|---|---|
| `BudgetExceededError` with `scope="run"` where the child's own cap was the binding limit, and any exception not listed below (e.g. `MaxTurnsExceededError`, `ProviderError`, `CircuitOpenError`, `OutputValidationError`) | Tool error `delegated agent failed: <ExceptionType>: <message>`; child checkpoint marked `failed` (existing behaviour) |
| `BudgetExceededError` with `scope="run"` where `ctx.remaining_cost_usd` was the binding limit, or any fleet `scope` | Re-raised |
| `RunStoppedByHookError` | Re-raised — `stop_run` stops the run tree |
| `RunConflictError`, `CheckpointError` | Re-raised (parent checkpoint not marked failed for `RunConflictError`, as today) |

The binding limit is the parent's when `ctx.remaining_cost_usd is not None` and
(`child cap is None` or `ctx.remaining_cost_usd <= child cap`).

### Run id length (pre-existing fix)

agent-kit Cloud stores run ids as `String(36)` and addresses them in URL paths. `Agent.run()` / `stream()`
raise `ValueError("run_id must be at most 36 characters when reporting to agent-kit Cloud")` when `cloud` is
set and a caller-supplied `run_id` is longer. Generated and child ids are UUIDs.

## Testing

`tests/test_agent_tool.py`, scripted with `MockProvider`:

- `as_tool` schema, name/description validation; standalone `await tool(task=...)` runs the child.
- Two calls to the same child in one turn run concurrently with separate memory; the child instance's memory
  and audit are untouched.
- `output_type` child returns JSON of `parsed`.
- Parent `deny_tools` blocks a child tool; child hook runs before parent hook; `before_llm` from the parent
  stops a child (`RunStoppedByHookError` propagates).
- Child spend rolls into parent `total_cost_usd`; parent cap trips before the parent's next model call; a
  parent-bound cap inside the child propagates `BudgetExceededError`; a child-bound cap becomes a tool error.
- Suspension bubbles with prefixed ids and `run_id`; resume routes answers; partial answers keep both
  suspended; two-level nesting (`a/b/c` ids); a denied child approval completes the child.
- Crash recovery: pending turn with the delegation call started and a running child checkpoint resumes the
  child (no `tool_interrupted`); completed child checkpoint replays without model calls; no child checkpoint
  starts fresh.
- Child `RunConflictError` propagates.
- Parent `tool_call` audit payload carries `delegated_run_id` / `delegated_root_hash`; both chains verify.
- Child `run_start` payload has `parent_run_id`; child ids are 36-char UUIDs and stable for the same inputs.
- Depth limit returns a tool error without starting a run.
- `run_id` longer than 36 characters with `cloud` set raises.

## Docs

`examples/delegation.py` (lead agent with a research child and a refunds child that suspends for approval),
README section, CHANGELOG entry, `examples/README.md` row, roadmap 2.6 ticked.

## Out of scope

- Handoffs (transferring control of the run to another agent).
- Typed tool inputs for agent tools (the input is a `task` string).
- Passing the parent's conversation to the child.
- Streaming child text through the parent's stream.
- Server `parent_run_id` column, run-tree API, and dashboard view.
- A verifier that walks parent and child chains together.
