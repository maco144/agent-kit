# Spec 11 — Hooks and Approval Gates

Status: **approved design** · Written 2026-09-14 · Roadmap item: 2.3 (`specs/06-harness-roadmap.md`)

## Goal

Teams put policy around what an agent may do — block tools, require a human to approve risky ones,
redact what tools return, and stop runs outright — with every decision recorded in the audit chain.

**Done means:** an agent whose `refund` tool requires approval pauses the call, awaits the approver,
runs the tool only when approved, feeds a denial back to the model when not, times out to a denial,
and records each step as an audit event; an `after_tool` hook redacts output before the model or
memory ever sees it; a `before_llm` hook stops a run with `RunStoppedByHookError`.

## Decisions

1. **Three hook points:** `before_tool`, `after_tool`, `before_llm`. Hooks may be sync or async.
2. **Ask = inline async approver.** `AgentConfig(approver=..., approval_timeout_s=300)`. Suspend to
   storage and resume in another process arrive with 2.5 (durable runs).
3. **Fail closed.** A hook that raises is a deny. An `ask` with no approver, a denial, or a timeout is a
   deny.
4. **Denials inform the model by default.** A denied tool call returns a tool error the model can adapt
   to; `stop_run=True` (and every `before_llm` deny) ends the run instead.
5. **Everything is audited** through the existing chain, so Cloud, evidence bundles, and OTLP need no
   change.

## API — `agent_kit/hooks.py`

```python
class Decision:
    kind: Literal["allow", "deny", "ask", "replace"]
    reason: str | None
    output: Any            # replace only
    stop_run: bool         # deny only

    @classmethod
    def allow(cls) -> Decision
    @classmethod
    def deny(cls, reason: str, stop_run: bool = False) -> Decision
    @classmethod
    def ask(cls, reason: str | None = None) -> Decision
    @classmethod
    def replace(cls, output: Any, reason: str | None = None) -> Decision


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    context: dict[str, Any]          # kwargs passed to Agent.run()/stream()


@dataclass(frozen=True)
class ToolResultContext(ToolCallContext):
    output: Any
    error: str | None
    duration_ms: int


@dataclass(frozen=True)
class LLMCallContext:
    run_id: str
    turn: int
    model: str
    message_count: int
    run_cost_usd: float
    context: dict[str, Any]


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    reason: str | None
    context: dict[str, Any]


HookResult = Decision | None
BeforeToolHook = Callable[[ToolCallContext], HookResult | Awaitable[HookResult]]
AfterToolHook = Callable[[ToolResultContext], HookResult | Awaitable[HookResult]]
BeforeLLMHook = Callable[[LLMCallContext], HookResult | Awaitable[HookResult]]
Approver = Callable[[ApprovalRequest], Awaitable[bool]]


@dataclass
class Hooks:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    before_llm: list[BeforeLLMHook] = field(default_factory=list)


def require_approval(*tool_names: str, reason: str | None = None) -> BeforeToolHook
def deny_tools(*tool_names: str, reason: str, stop_run: bool = False) -> BeforeToolHook
def allow_only(*tool_names: str, reason: str = "tool not permitted by policy") -> BeforeToolHook
```

`AgentConfig` gains `hooks: Hooks | None = None`, `approver: Approver | None = None`,
`approval_timeout_s: float = 300.0`.

`agent_kit/exceptions.py` gains:

```python
class RunStoppedByHookError(AgentKitError):
    stage: Literal["before_tool", "after_tool", "before_llm"]
    reason: str
    tool_name: str | None
```

## Semantics

### `before_tool` (per tool call, before execution; after the `allowed_tools` check)

- Hooks run in order; `None` and `allow()` continue. The first `deny` or `ask` decides.
- `replace` is invalid here and is treated as a deny (`"invalid decision 'replace' from before_tool hook"`).
- `ask(reason)`: append `approval_requested`; await `approver(ApprovalRequest)` with
  `asyncio.wait_for(..., approval_timeout_s)`.
  - `True` → `approval_granted`, execute the tool.
  - `False` → `approval_denied` (`timed_out: false`), deny with `"approval denied"`.
  - Timeout → `approval_denied` (`timed_out: true`), deny with `"approval timed out after Ns"`.
  - Approver raises → `approval_denied` (`error`), deny with `"approver error: …"`.
  - No approver configured → deny with `"approval required but no approver is configured"`.
- `deny(reason)`: append `tool_denied` (`reason`, `stage`); the tool does not run; the `ToolResult`
  has `error="Tool call denied: <reason>"` and the tool message is marked `is_error`.
- `deny(reason, stop_run=True)`: as above, then raise `RunStoppedByHookError` after all of the turn's
  tool calls have resolved (other concurrent calls finish or are denied normally).

### `after_tool` (per tool call, after execution — including tool errors)

- Hooks run in order, each seeing the output as left by the previous hook.
- `replace(output, reason)`: output becomes `output`; append `tool_output_replaced` (`reason`). The
  original output is never stored in memory or sent to the model.
- `deny(reason)`: output is withheld; `ToolResult.error="Tool output blocked: <reason>"`, `output=None`;
  append `tool_denied` (`stage: "after_tool"`). `stop_run=True` raises as for `before_tool`.
- `ask` is invalid here → deny.

### `before_llm` (before every provider call, after budget checks)

- `deny(reason)` appends `llm_call_denied` and raises `RunStoppedByHookError(stage="before_llm")`.
- `ask` and `replace` are invalid → deny.

### Errors and concurrency

- A hook (sync or async) that raises → deny with `"hook error: <ExceptionType>: <message>"`.
- Tool calls in a turn still run concurrently; their hooks and approvals do too.
- `run()` and `stream()` behave identically. `RunStoppedByHookError` follows the normal error path
  (`run_error` to Cloud).

### Audit payloads

| Event | Actor | Payload |
|---|---|---|
| `tool_denied` | tool name | `call_id`, `stage`, `reason`, `stop_run` |
| `approval_requested` | tool name | `call_id`, `reason` |
| `approval_granted` | tool name | `call_id` |
| `approval_denied` | tool name | `call_id`, `timed_out`, `error` |
| `tool_output_replaced` | tool name | `call_id`, `reason` |
| `llm_call_denied` | `"agent"` | `turn`, `reason` |

Arguments and outputs are not placed in audit payloads (payloads are hashed, but hooks commonly
handle sensitive data).

## Testing — `tests/test_hooks.py`

Using a scripted provider and real tools: before_tool allow/deny/ask/replace-invalid; ordering
(first non-allow wins); sync and async hooks; hook exception fails closed; approver granted / denied /
timeout / raises / missing; concurrent approvals in one turn; deny message reaches the provider's next
request as a tool error; `stop_run` raises after the turn resolves; after_tool replace chaining and the
original output absent from memory; after_tool deny; before_llm deny raises; helpers
(`require_approval`, `deny_tools`, `allow_only`); every audit event type and payload; `run()` and
`stream()` parity.

## Out of scope

Suspend/resume across processes (2.5); hooks inside the Claude Agent SDK / OpenAI Agents SDK adapters;
rewriting tool arguments or model messages; a server-side approvals UI.
