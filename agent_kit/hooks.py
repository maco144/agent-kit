"""
Hooks and approval gates — policy around what an agent may do.

    from agent_kit.hooks import Decision, Hooks, require_approval, deny_tools

    config = AgentConfig(
        hooks=Hooks(
            before_tool=[deny_tools("delete_account", reason="never in prod"), require_approval("refund")],
            after_tool=[redact_card_numbers],
            before_llm=[stop_after_hours],
        ),
        approver=approve,            # async (ApprovalRequest) -> bool
        approval_timeout_s=300,
    )

Hooks may be sync or async and return a Decision or None (allow). Every path that
isn't a clear allow is a deny: a hook that raises, an invalid decision, an ask with no
approver, a denied or timed-out approval. Decisions are recorded in the audit chain.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

DecisionKind = Literal["allow", "deny", "ask", "replace"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str | None = None
    output: Any = None
    stop_run: bool = False

    @classmethod
    def allow(cls) -> Decision:
        return cls("allow")

    @classmethod
    def deny(cls, reason: str, stop_run: bool = False) -> Decision:
        return cls("deny", reason=reason, stop_run=stop_run)

    @classmethod
    def ask(cls, reason: str | None = None) -> Decision:
        return cls("ask", reason=reason)

    @classmethod
    def replace(cls, output: Any, reason: str | None = None) -> Decision:
        return cls("replace", reason=reason, output=output)


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResultContext(ToolCallContext):
    output: Any = None
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class LLMCallContext:
    run_id: str
    turn: int
    model: str
    message_count: int
    run_cost_usd: float
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    run_id: str
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    call_id: str
    reason: str | None
    context: dict[str, Any] = field(default_factory=dict)


HookResult = Decision | None
BeforeToolHook = Callable[[ToolCallContext], HookResult | Awaitable[HookResult]]
AfterToolHook = Callable[[ToolResultContext], HookResult | Awaitable[HookResult]]
BeforeLLMHook = Callable[[LLMCallContext], HookResult | Awaitable[HookResult]]
Approver = Callable[[ApprovalRequest], Awaitable[bool]]


class Suspend:
    """Approver sentinel: suspend the run until the approval is answered via Agent.resume()."""

    def __repr__(self) -> str:
        return "SUSPEND"


SUSPEND: Final = Suspend()


@dataclass
class Hooks:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    before_llm: list[BeforeLLMHook] = field(default_factory=list)


async def run_hook(hook: Callable[[Any], Any], ctx: Any) -> Decision:
    """Call a sync or async hook; anything but a Decision or None fails closed."""
    try:
        result = hook(ctx)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return Decision.deny(f"hook error: {type(exc).__name__}: {exc}")
    if result is None:
        return Decision.allow()
    if not isinstance(result, Decision):
        return Decision.deny(f"hook error: returned {type(result).__name__}, expected Decision or None")
    return result


def require_approval(*tool_names: str, reason: str | None = None) -> BeforeToolHook:
    """Ask the approver before any of these tools run."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        if ctx.tool_name in names:
            return Decision.ask(reason or f"{ctx.tool_name} requires approval")
        return None

    return hook


def deny_tools(*tool_names: str, reason: str, stop_run: bool = False) -> BeforeToolHook:
    """Never run these tools."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return Decision.deny(reason, stop_run=stop_run) if ctx.tool_name in names else None

    return hook


def allow_only(*tool_names: str, reason: str = "tool not permitted by policy") -> BeforeToolHook:
    """Deny every tool not listed (the model still sees all tools, unlike allowed_tools)."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return None if ctx.tool_name in names else Decision.deny(reason)

    return hook
