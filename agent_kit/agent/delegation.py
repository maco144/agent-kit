"""
Agents as tools — delegation to a child run under the parent run's policy, budget, and durability.

    research = Agent(provider, tools=[web_search]).as_tool("research", "Investigate a question.")
    lead = Agent(provider, tools=[research], config=AgentConfig(max_run_cost_usd=2.00))

Inside an agent loop every call is a fresh child run (see specs/16-agent-as-tool.md). The child keeps its own
provider, tools, and hooks; the parent run supplies its hooks (run after the child's), approver, run store, and
remaining cost cap. A suspended child suspends the parent with the child's approvals under prefixed call ids.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agent_kit.exceptions import BudgetExceededError, CheckpointError, RunConflictError, RunStoppedByHookError
from agent_kit.hooks import Hooks
from agent_kit.output import OutputSpec
from agent_kit.tools.base import Tool
from agent_kit.types import AgentResult, PendingApproval, ToolResult, ToolSchema

if TYPE_CHECKING:
    from agent_kit.agent.agent import Agent
    from agent_kit.agent.loop import AgentLoop
    from agent_kit.cloud.budgets import BudgetGuard
    from agent_kit.cloud.reporter import CloudReporter
    from agent_kit.durable import RunStore
    from agent_kit.hooks import Approver, Suspend

DELEGATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/maco144/agent-kit/delegation")

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TASK_SCHEMA = {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}


def child_run_id(parent_run_id: str, call_id: str) -> str:
    """Deterministic, 36-character run id for the child run of one delegated tool call."""
    return str(uuid.uuid5(DELEGATION_NAMESPACE, f"{parent_run_id}/{call_id}"))


def stack_hooks(child: Hooks | None, parent: Hooks | None) -> Hooks | None:
    """The child's hooks run first, then the parent's; any deny wins. A hook on both levels runs once."""
    if child is None or parent is None:
        return child or parent

    def merged(own: list[Any], inherited: list[Any]) -> list[Any]:
        return [*own, *(h for h in inherited if not any(h is o for o in own))]

    return Hooks(
        before_tool=merged(child.before_tool, parent.before_tool),
        after_tool=merged(child.after_tool, parent.after_tool),
        before_llm=merged(child.before_llm, parent.before_llm),
    )


@dataclass(frozen=True)
class DelegationContext:
    """Parent run state handed to AgentTool.delegate()."""

    parent_run_id: str
    call_id: str
    depth: int  # depth of the child run (top-level run = 0, its children = 1)
    max_depth: int
    context: dict[str, Any]  # parent run context
    hooks: Hooks | None  # parent's effective (already stacked) hooks
    approver: Approver | Suspend | None
    approval_timeout_s: float
    run_store: RunStore | None
    remaining_cost_usd: float | None  # parent cap minus parent spend; None = uncapped
    budget_guard: BudgetGuard | None
    reporter: CloudReporter | None
    approvals: dict[str, bool]  # answers for this child, prefix stripped


@dataclass(frozen=True)
class Delegation:
    """Outcome of one delegated call, as the parent loop sees it."""

    run_id: str | None  # None when no child run started (depth limit)
    status: Literal["completed", "suspended", "failed"]
    result: ToolResult | None  # completed or failed
    approvals: list[PendingApproval]  # suspended: the child's pending approvals, ids already prefixed
    cost_usd: float  # child's cumulative spend, every level below included
    tokens: int
    root_hash: str | None  # child's audit root hash when completed


class AgentTool(Tool):
    """An Agent exposed as a tool. Build with ``Agent.as_tool()``."""

    def __init__(self, agent: Agent, name: str, description: str, output_type: Any = None) -> None:
        if not _NAME.match(name):
            raise ValueError(f"agent tool name must match ^[A-Za-z0-9_-]{{1,64}}$, got {name!r}")
        if not description.strip():
            raise ValueError("agent tool description must not be empty")
        schema = ToolSchema(name=name, description=description.strip(), parameters=dict(_TASK_SCHEMA))
        super().__init__(self._standalone, schema)
        self.agent = agent
        self.output_type = output_type

    async def delegate(self, task: str, ctx: DelegationContext) -> Delegation:
        """Start, resume, or replay this call's child run under the parent run's policy and budget."""
        t0 = time.monotonic()
        if ctx.depth > ctx.max_depth:
            error = f"delegation depth limit ({ctx.max_depth}) exceeded"
            return Delegation(None, "failed", self._result(ctx, t0, error=error), [], 0.0, 0, None)

        run_id = child_run_id(ctx.parent_run_id, ctx.call_id)
        loop, checkpoint = await self.agent._open_delegated(run_id, self.output_type, **self._overrides(ctx))
        try:
            if checkpoint is not None and checkpoint.status == "completed":
                stored = self.agent._stored_result(checkpoint, self.output_type)
                return self._completed(ctx, t0, run_id, stored, stored.total_cost_usd, stored.total_tokens)
            if checkpoint is not None:
                result = await loop.resume(checkpoint, ctx.approvals, self.output_type)
            else:
                context = {**ctx.context, "parent_run_id": ctx.parent_run_id}
                result = await loop.run(task, output_type=self.output_type, run_id=run_id, **context)
        except (RunStoppedByHookError, RunConflictError, CheckpointError):
            raise
        except BudgetExceededError as exc:
            if exc.scope != "run" or self._parent_bound(ctx):
                raise
            return self._failed(ctx, t0, run_id, loop, exc)
        except Exception as exc:
            return self._failed(ctx, t0, run_id, loop, exc)

        cost, tokens = loop.spend()
        if result.status == "suspended":
            approvals = [
                a.model_copy(update={"call_id": f"{ctx.call_id}/{a.call_id}", "run_id": a.run_id or run_id})
                for a in result.pending_approvals
            ]
            return Delegation(run_id, "suspended", None, approvals, cost, tokens, None)
        return self._completed(ctx, t0, run_id, result, cost, tokens)

    async def _standalone(self, task: str) -> Any:
        """Outside an agent loop: run the child once with its own full config."""
        run_id = str(uuid.uuid4())
        loop, _ = await self.agent._open_delegated(run_id, self.output_type)
        result = await loop.run(task, output_type=self.output_type, run_id=run_id)
        if result.status == "suspended":
            raise RuntimeError("delegated agent suspended; run it as a tool inside an agent to resume approvals")
        return self._output(result)

    def _overrides(self, ctx: DelegationContext) -> dict[str, Any]:
        config = self.agent.config
        caps = [c for c in (config.max_run_cost_usd, ctx.remaining_cost_usd) if c is not None]
        overrides: dict[str, Any] = {
            "hooks": stack_hooks(config.hooks, ctx.hooks),
            "approver": ctx.approver,
            "approval_timeout_s": ctx.approval_timeout_s,
            "run_store": ctx.run_store,
            "max_run_cost_usd": min(caps) if caps else None,
            "delegation_depth": ctx.depth,
            "max_delegation_depth": ctx.max_depth,
            "parent_run_id": ctx.parent_run_id,
            "parent_call_id": ctx.call_id,
        }
        if not config.enforce_budgets:
            overrides["budget_guard"] = ctx.budget_guard
        if config.cloud is None:
            overrides["reporter"] = ctx.reporter
        return overrides

    def _parent_bound(self, ctx: DelegationContext) -> bool:
        own = self.agent.config.max_run_cost_usd
        return ctx.remaining_cost_usd is not None and (own is None or ctx.remaining_cost_usd <= own)

    def _output(self, result: AgentResult[Any]) -> Any:
        if self.output_type is None:
            return result.output
        return OutputSpec.from_type(self.output_type).adapter.dump_python(result.parsed, mode="json")

    def _completed(
        self, ctx: DelegationContext, t0: float, run_id: str, result: AgentResult[Any], cost: float, tokens: int
    ) -> Delegation:
        tool_result = self._result(ctx, t0, output=self._output(result), cost_usd=cost, tokens=tokens)
        return Delegation(run_id, "completed", tool_result, [], cost, tokens, result.audit_root_hash)

    def _failed(self, ctx: DelegationContext, t0: float, run_id: str, loop: AgentLoop, exc: Exception) -> Delegation:
        cost, tokens = loop.spend()
        error = f"delegated agent failed: {type(exc).__name__}: {exc}"
        tool_result = self._result(ctx, t0, error=error, cost_usd=cost, tokens=tokens)
        return Delegation(run_id, "failed", tool_result, [], cost, tokens, None)

    def _result(
        self,
        ctx: DelegationContext,
        t0: float,
        output: Any = None,
        error: str | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> ToolResult:
        return ToolResult(
            call_id=ctx.call_id,
            tool_name=self.schema.name,
            output=output,
            error=error,
            duration_ms=int((time.monotonic() - t0) * 1000),
            cost_usd=cost_usd,
            tokens=tokens,
        )

    def __repr__(self) -> str:
        return f"AgentTool(name={self.schema.name!r}, agent={self.agent!r})"
