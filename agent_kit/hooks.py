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
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from agent_kit.types import SEVERITY_ORDER, Finding

logger = logging.getLogger("agent_kit.hooks")

DecisionKind = Literal["allow", "deny", "ask", "replace"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str | None = None
    output: Any = None
    stop_run: bool = False
    findings: tuple[Finding, ...] = ()  # recorded as tool_output_flagged when returned from an after_tool hook

    @classmethod
    def allow(cls, findings: Sequence[Finding] = ()) -> Decision:
        return cls("allow", findings=tuple(findings))

    @classmethod
    def deny(cls, reason: str, stop_run: bool = False, findings: Sequence[Finding] = ()) -> Decision:
        return cls("deny", reason=reason, stop_run=stop_run, findings=tuple(findings))

    @classmethod
    def ask(cls, reason: str | None = None) -> Decision:
        return cls("ask", reason=reason)

    @classmethod
    def replace(cls, output: Any, reason: str | None = None, findings: Sequence[Finding] = ()) -> Decision:
        return cls("replace", reason=reason, output=output, findings=tuple(findings))


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


def flagged_payload(call_id: str, tool_name: str, action: str, findings: Sequence[Finding]) -> dict[str, Any]:
    """Audit and Cloud payload for a decision that carries findings: rule metadata only, never tool output."""
    top = max(findings, key=lambda f: SEVERITY_ORDER[f.severity])
    return {
        "call_id": call_id,
        "tool_name": tool_name,
        "action": action,
        "max_severity": top.severity,
        "findings": [
            {
                "scanner": f.scanner,
                "rule": f.rule,
                "severity": f.severity,
                "location": f.location,
                "indicator": f.indicator,
            }
            for f in findings
        ],
    }


_POLICY_ATTR = "_agentkit_policy"  # (helper name, tool names, matches MCP-prefixed names)


def _restrictive_match(names: frozenset[str], tool_name: str) -> bool:
    """Exact, or an MCP tool ``<server>__<name>`` — widening a restriction never lets a tool through."""
    return tool_name in names or any(tool_name.endswith(f"__{n}") for n in names)


def require_approval(*tool_names: str, reason: str | None = None) -> BeforeToolHook:
    """Ask the approver before any of these tools run, local or MCP (``<server>__<name>``)."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        if _restrictive_match(names, ctx.tool_name):
            return Decision.ask(reason or f"{ctx.tool_name} requires approval")
        return None

    setattr(hook, _POLICY_ATTR, ("require_approval", names, True))
    return hook


def deny_tools(*tool_names: str, reason: str, stop_run: bool = False) -> BeforeToolHook:
    """Never run these tools, local or MCP (``<server>__<name>``)."""
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return Decision.deny(reason, stop_run=stop_run) if _restrictive_match(names, ctx.tool_name) else None

    setattr(hook, _POLICY_ATTR, ("deny_tools", names, True))
    return hook


def allow_only(*tool_names: str, reason: str = "tool not permitted by policy") -> BeforeToolHook:
    """
    Deny every tool not listed (the model still sees all tools, unlike allowed_tools).

    Names match exactly: list MCP tools by their full ``<server>__<name>``, so an allowlist entry
    never admits a same-named tool from another server.
    """
    names = frozenset(tool_names)

    def hook(ctx: ToolCallContext) -> HookResult:
        return None if ctx.tool_name in names else Decision.deny(reason)

    setattr(hook, _POLICY_ATTR, ("allow_only", names, False))
    return hook


def warn_unknown_policy_names(hooks: Hooks | None, tool_names: Sequence[str]) -> None:
    """Log each policy helper that names a tool the agent doesn't have — a typo there matches nothing."""
    if hooks is None:
        return
    for hook in hooks.before_tool:
        policy = getattr(hook, _POLICY_ATTR, None)
        if policy is None:
            continue
        helper, names, prefixed = policy
        unknown = sorted(
            n for n in names
            if not any(t == n or (prefixed and t.endswith(f"__{n}")) for t in tool_names)
        )
        if unknown:
            logger.warning(
                "%s names tool(s) this agent does not have: %s — it will never match them. "
                "Check the spelling; MCP tools are named '<server>__<tool>'.",
                helper,
                ", ".join(repr(n) for n in unknown),
            )
