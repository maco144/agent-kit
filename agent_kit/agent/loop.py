"""AgentLoop — the execution engine that drives a single agent run."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_kit.audit.chain import AuditChain
from agent_kit.exceptions import (
    BudgetExceededError,
    MaxTurnsExceededError,
    OutputValidationError,
    RunStoppedByHookError,
)
from agent_kit.hooks import ApprovalRequest, LLMCallContext, ToolCallContext, ToolResultContext, run_hook
from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.observability.tracer import AgentTracer
from agent_kit.output import OutputParseError, OutputSpec
from agent_kit.providers.base import BaseProvider
from agent_kit.reliability.circuit_breaker import CircuitBreaker
from agent_kit.reliability.retry import with_retry
from agent_kit.tools.registry import ToolRegistry
from agent_kit.types import (
    AgentResult,
    CircuitBreakerConfig,
    CostSummary,
    Message,
    RetryPolicyConfig,
    SpanKind,
    ToolCall,
    ToolResult,
    ToolSchema,
    Turn,
)

if TYPE_CHECKING:
    from agent_kit.cloud.budgets import BudgetGuard
    from agent_kit.cloud.reporter import CloudReporter
    from agent_kit.hooks import Approver, Hooks

_REPAIR_PROMPT = (
    "Your response did not match the required output schema:\n{errors}\n"
    "Respond again with only the corrected JSON."
)


class AgentLoop:
    """
    Drives a single agent run from initial prompt to final text output.

    Responsibilities:
    - Maintains conversation history in memory
    - Dispatches LLM calls through the circuit breaker + retry policy
    - Executes tool calls and feeds results back into the next turn
    - Emits audit events for every significant action
    - Records observability spans and cost
    - Enforces max_turns limit
    - Optionally reports lifecycle events to agent-kit Cloud

    This class is not meant to be instantiated directly — use Agent.run().
    """

    def __init__(
        self,
        provider: BaseProvider,
        registry: ToolRegistry,
        memory: InMemoryStore,
        tracer: AgentTracer,
        audit: AuditChain | None,
        model: str | None,
        system_prompt: str,
        max_turns: int,
        max_tokens_per_turn: int,
        retry_policy: RetryPolicyConfig,
        circuit_breaker_config: CircuitBreakerConfig,
        reporter: CloudReporter | None = None,
        max_run_cost_usd: float | None = None,
        budget_guard: BudgetGuard | None = None,
        hooks: Hooks | None = None,
        approver: Approver | None = None,
        approval_timeout_s: float = 300.0,
        output_retries: int = 2,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._memory = memory
        self._tracer = tracer
        self._audit = audit
        self._model = model
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        self._max_tokens_per_turn = max_tokens_per_turn
        self._retry_policy = retry_policy
        self._cb = CircuitBreaker(
            provider.name(),
            circuit_breaker_config,
        )
        self._reporter = reporter
        self._max_run_cost_usd = max_run_cost_usd
        self._budget_guard = budget_guard
        self._run_cost_usd = 0.0
        self._hooks = hooks
        self._approver = approver
        self._approval_timeout_s = approval_timeout_s
        self._output_retries = output_retries
        self._run_id = ""
        self._context: dict[str, Any] = {}
        self._pending_stop: RunStoppedByHookError | None = None
        self._turns: list[Turn] = []
        self.result: AgentResult[Any] | None = None

    async def run(self, prompt: str, output_type: Any = None, **context: Any) -> AgentResult[Any]:
        """Execute the agent loop and return the final result."""
        async for _ in self._execute(prompt, streaming=False, context=context, output_type=output_type):
            pass
        assert self.result is not None
        return self.result

    async def stream(self, prompt: str, output_type: Any = None, **context: Any) -> AsyncIterator[str]:
        """Execute the agent loop, yielding text as it streams. ``self.result`` is set at the end."""
        async for chunk in self._execute(prompt, streaming=True, context=context, output_type=output_type):
            yield chunk

    async def _execute(
        self, prompt: str, streaming: bool, context: dict[str, Any], output_type: Any = None
    ) -> AsyncIterator[str]:
        """The agent loop. Yields text chunks when ``streaming``; sets ``self.result`` on success."""
        run_id = str(uuid.uuid4())
        self._run_id = run_id
        self._context = dict(context)

        # Typed runs: constrain natively when the provider can, else describe the schema in the prompt
        spec = OutputSpec.from_type(output_type) if output_type is not None else None
        native_capable = (
            spec is not None
            and spec.native_compatible
            and bool(getattr(self._provider, "supports_structured_output", False))
        )
        # Some providers' native constraint rules out tool calls: prompt mode until the model answers
        native = native_capable and (
            bool(getattr(self._provider, "structured_output_with_tools", True))
            or not self._registry.schemas()
        )
        system = self._system_prompt
        if spec is not None and not native:
            system = f"{system}\n\n{spec.instructions()}" if system else spec.instructions()
        output_kwargs: dict[str, Any] = {"output_schema": spec} if native else {}
        parsed: Any = None
        invalid_answers = 0

        if self._reporter:
            await self._reporter.on_run_start(
                run_id=run_id,
                model=self._model or self._provider.config.default_model,
                prompt=prompt,
            )

        with self._tracer.span("agent.run", kind=SpanKind.AGENT, run_id=run_id) as root_span:
            # Audit: agent start
            if self._audit:
                self._audit.append(
                    "agent_start",
                    actor=run_id,
                    payload={"prompt_preview": prompt[:200], "context_keys": list(context.keys())},
                )

            # Seed memory with the user prompt
            self._memory.add(Message(role="user", content=prompt))

            turn_count = 0
            final_output = ""

            try:
                while turn_count < self._max_turns:
                    turn_count += 1
                    await self._enforce_budgets()
                    messages = self._memory.history(include_system=False)
                    await self._gate_llm(turn_count, len(messages))
                    tool_schemas = self._registry.schemas()

                    # --- LLM call with circuit breaker + retry ---
                    with self._tracer.span(
                        "llm.complete",
                        kind=SpanKind.LLM,
                        turn=turn_count,
                        model=self._model or self._provider.config.default_model,
                    ) as llm_span:
                        turn: Turn | None = None
                        if streaming:
                            # Retry and circuit breaking cover opening the stream. A failure
                            # after text has been yielded propagates: replaying would duplicate it.
                            it, item = await with_retry(
                                self._cb_call,
                                self._retry_policy,
                                run_id,
                                self._open_stream,
                                messages,
                                tool_schemas or None,
                                system,
                                output_kwargs,
                            )
                            chunks: list[str] = []
                            while item is not None:
                                if isinstance(item, Turn):
                                    turn = item
                                else:
                                    chunks.append(item)
                                    yield item
                                item = await anext(it, None)
                            if turn is None:  # provider streams text only
                                turn = Turn(
                                    messages_in=messages,
                                    message_out=Message(role="assistant", content="".join(chunks)),
                                    cost=CostSummary(
                                        model=self._model or self._provider.config.default_model
                                    ),
                                )
                        else:
                            turn = await with_retry(
                                self._cb_call,
                                self._retry_policy,
                                run_id,
                                self._provider.complete,
                                messages,
                                model=self._model,
                                tools=tool_schemas if tool_schemas else None,
                                system=system or None,
                                max_tokens=self._max_tokens_per_turn,
                                **output_kwargs,
                            )
                        llm_span.set_attribute("input_tokens", turn.cost.input_tokens)
                        llm_span.set_attribute("output_tokens", turn.cost.output_tokens)
                        llm_span.set_attribute("cost_usd", turn.cost.cost_usd)

                    # Audit: LLM response
                    if self._audit:
                        self._audit.append(
                            "llm_complete",
                            actor=self._provider.name(),
                            payload={
                                "model": turn.cost.model,
                                "input_tokens": turn.cost.input_tokens,
                                "output_tokens": turn.cost.output_tokens,
                                "has_tool_calls": len(turn.tool_calls) > 0,
                            },
                        )

                    # Track cost
                    self._tracer.record_cost(
                        tokens=turn.cost.total_tokens,
                        model=turn.cost.model,
                        usd=turn.cost.cost_usd,
                    )
                    self._run_cost_usd += turn.cost.cost_usd
                    if self._budget_guard is not None and self._reporter is not None:
                        self._budget_guard.record_spend(
                            self._reporter.agent_name, self._reporter.project, turn.cost.cost_usd
                        )

                    # Add assistant message to memory
                    if turn.message_out:
                        self._memory.add(turn.message_out)

                    # No tool calls → a final answer (validated when the run is typed)
                    if not turn.tool_calls:
                        final_output = turn.message_out.content if turn.message_out else ""
                        self._turns.append(turn)
                        if self._reporter:
                            await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                        if spec is None:
                            break
                        try:
                            parsed = spec.parse(final_output)
                            break
                        except OutputParseError as exc:
                            invalid_answers += 1
                            self._audit_event(
                                "output_validation_failed",
                                "agent",
                                {
                                    "turn": turn_count,
                                    "attempt": invalid_answers,
                                    "native": native,
                                    "errors": exc.errors[:500],
                                },
                            )
                            if invalid_answers > self._output_retries:
                                raise OutputValidationError(
                                    exc.errors, final_output, invalid_answers
                                ) from None
                            self._memory.add(
                                Message(role="user", content=_REPAIR_PROMPT.format(errors=exc.errors))
                            )
                            if native_capable and not native:
                                # The model has stopped calling tools; constrain the repair natively
                                native = True
                                output_kwargs = {"output_schema": spec}
                            continue

                    # --- Execute tool calls concurrently; record results in call order ---
                    tool_results = await asyncio.gather(
                        *(self._run_tool(tc, turn_count) for tc in turn.tool_calls)
                    )
                    for tc, tool_result in zip(turn.tool_calls, tool_results):
                        # Audit: tool execution
                        if self._audit:
                            self._audit.append(
                                "tool_call",
                                actor=tc.tool_name,
                                payload={
                                    "call_id": tc.call_id,
                                    "success": tool_result.error is None,
                                    "error": tool_result.error,
                                    "duration_ms": tool_result.duration_ms,
                                },
                            )

                        # Feed tool result back as a tool message
                        output_str = (
                            f"Error: {tool_result.error}"
                            if tool_result.error
                            else json.dumps(tool_result.output, default=str)
                        )
                        self._memory.add(
                            Message(
                                role="tool",
                                content=output_str,
                                tool_call_id=tc.call_id,
                                metadata={"is_error": True} if tool_result.error else {},
                            )
                        )
                        turn.tool_results.append(tool_result)

                    self._turns.append(turn)
                    if self._reporter:
                        await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                    if self._pending_stop is not None:
                        raise self._pending_stop

                else:
                    raise MaxTurnsExceededError(self._max_turns)

            except Exception as exc:
                if self._reporter:
                    await self._reporter.on_run_error(run_id, exc, turn_count)
                raise

            # Audit: agent complete
            if self._audit:
                self._audit.append(
                    "agent_complete",
                    actor=run_id,
                    payload={
                        "turns": turn_count,
                        "total_tokens": self._tracer.cumulative_tokens(),
                        "total_cost_usd": self._tracer.cumulative_cost_usd(),
                        "output_type": spec.name if spec else None,
                    },
                )

            root_span.set_attribute("total_turns", turn_count)
            root_span.set_attribute("total_cost_usd", self._tracer.cumulative_cost_usd())

        result: AgentResult[Any] = AgentResult(
            output=final_output,
            parsed=parsed,
            turns=self._turns,
            total_cost_usd=self._tracer.cumulative_cost_usd(),
            total_tokens=self._tracer.cumulative_tokens(),
            audit_root_hash=self._audit.root_hash() if self._audit else None,
            trace_id=self._tracer.trace_id,
        )

        if self._reporter:
            await self._reporter.on_run_complete(run_id, result)
            if self._audit:
                await self._reporter.on_audit_flush(
                    run_id=run_id,
                    events=self._audit.events(),
                    final_root_hash=self._audit.root_hash(),
                )

        self.result = result

    async def _enforce_budgets(self) -> None:
        """Stop before a model call when the run cap or a fleet budget is exhausted."""
        try:
            if self._max_run_cost_usd is not None and self._run_cost_usd >= self._max_run_cost_usd:
                raise BudgetExceededError(
                    scope="run", limit_usd=self._max_run_cost_usd, spent_usd=self._run_cost_usd
                )
            if self._budget_guard is not None and self._reporter is not None:
                await self._budget_guard.check(self._reporter.agent_name, self._reporter.project)
        except BudgetExceededError as exc:
            if self._audit:
                self._audit.append(
                    "budget_exceeded",
                    actor=exc.budget_name or "run",
                    payload={
                        "scope": exc.scope,
                        "limit_usd": exc.limit_usd,
                        "spent_usd": exc.spent_usd,
                    },
                )
            raise

    async def _open_stream(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        system: str,
        output_kwargs: dict[str, Any],
    ) -> tuple[AsyncIterator[str | Turn], str | Turn | None]:
        """Start a provider stream and pull its first item, so connection failures are retryable."""
        it = self._provider.stream(
            messages,
            model=self._model,
            tools=tools,
            system=system or None,
            max_tokens=self._max_tokens_per_turn,
            **output_kwargs,
        ).__aiter__()
        return it, await anext(it, None)

    async def _run_tool(self, tc: ToolCall, turn: int) -> ToolResult:
        """Gate, execute, and filter one tool call inside its span. Never raises."""
        with self._tracer.span(f"tool.{tc.tool_name}", kind=SpanKind.TOOL, tool=tc.tool_name) as tool_span:
            t0 = time.monotonic()

            def failed(error: str) -> ToolResult:
                return ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error=error,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )

            try:
                tool = self._registry.get(tc.tool_name)
            except Exception as exc:
                tool_result = failed(str(exc))
            else:
                denial = await self._gate_tool(tc, turn)
                if denial is not None:
                    tool_result = failed(f"Tool call denied: {denial}")
                else:
                    try:
                        tool_result = await tool(call_id=tc.call_id, **tc.arguments)
                    except Exception as exc:
                        tool_result = failed(str(exc))
                    tool_result = await self._filter_output(tc, turn, tool_result)

            tool_span.set_attribute("duration_ms", tool_result.duration_ms)
            tool_span.set_attribute("success", tool_result.error is None)

        self._tracer.record_tool_call(tc.tool_name, tool_result.duration_ms, tool_result.error is None)
        return tool_result

    async def _gate_tool(self, tc: ToolCall, turn: int) -> str | None:
        """Run before_tool hooks. Returns a denial reason, or None to execute."""
        if self._hooks is None or not self._hooks.before_tool:
            return None
        ctx = ToolCallContext(
            run_id=self._run_id,
            turn=turn,
            tool_name=tc.tool_name,
            arguments=dict(tc.arguments),
            call_id=tc.call_id,
            context=self._context,
        )
        for hook in self._hooks.before_tool:
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            if decision.kind == "ask":
                return await self._request_approval(ctx, decision.reason)
            if decision.kind == "deny":
                return self._deny_tool(ctx, "before_tool", decision.reason or "denied", decision.stop_run)
            return self._deny_tool(ctx, "before_tool", f"invalid decision '{decision.kind}' from before_tool hook")
        return None

    async def _request_approval(self, ctx: ToolCallContext, reason: str | None) -> str | None:
        self._audit_event("approval_requested", ctx.tool_name, {"call_id": ctx.call_id, "reason": reason})
        if self._approver is None:
            return self._deny_tool(ctx, "before_tool", "approval required but no approver is configured")
        request = ApprovalRequest(
            run_id=ctx.run_id,
            turn=ctx.turn,
            tool_name=ctx.tool_name,
            arguments=dict(ctx.arguments),
            call_id=ctx.call_id,
            reason=reason,
            context=ctx.context,
        )
        try:
            approved = await asyncio.wait_for(self._approver(request), timeout=self._approval_timeout_s)
        except asyncio.TimeoutError:
            self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": True, "error": None})
            return self._deny_tool(ctx, "before_tool", f"approval timed out after {self._approval_timeout_s:g}s")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": False, "error": error})
            return self._deny_tool(ctx, "before_tool", f"approver error: {error}")
        if approved:
            self._audit_event("approval_granted", ctx.tool_name, {"call_id": ctx.call_id})
            return None
        self._audit_event("approval_denied", ctx.tool_name, {"call_id": ctx.call_id, "timed_out": False, "error": None})
        return self._deny_tool(ctx, "before_tool", "approval denied")

    def _deny_tool(self, ctx: ToolCallContext, stage: str, reason: str, stop_run: bool = False) -> str:
        self._audit_event(
            "tool_denied", ctx.tool_name, {"call_id": ctx.call_id, "stage": stage, "reason": reason, "stop_run": stop_run}
        )
        if stop_run and self._pending_stop is None:
            self._pending_stop = RunStoppedByHookError(stage, reason, tool_name=ctx.tool_name)
        return reason

    async def _filter_output(self, tc: ToolCall, turn: int, result: ToolResult) -> ToolResult:
        """Run after_tool hooks; replacements chain, a deny withholds the output."""
        if self._hooks is None or not self._hooks.after_tool:
            return result
        output = result.output
        for hook in self._hooks.after_tool:
            ctx = ToolResultContext(
                run_id=self._run_id,
                turn=turn,
                tool_name=tc.tool_name,
                arguments=dict(tc.arguments),
                call_id=tc.call_id,
                context=self._context,
                output=output,
                error=result.error,
                duration_ms=result.duration_ms,
            )
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            if decision.kind == "replace":
                output = decision.output
                self._audit_event("tool_output_replaced", tc.tool_name, {"call_id": tc.call_id, "reason": decision.reason})
                continue
            if decision.kind == "deny":
                reason = self._deny_tool(ctx, "after_tool", decision.reason or "denied", decision.stop_run)
            else:
                reason = self._deny_tool(ctx, "after_tool", f"invalid decision '{decision.kind}' from after_tool hook")
            return result.model_copy(update={"output": None, "error": f"Tool output blocked: {reason}"})
        return result.model_copy(update={"output": output})

    async def _gate_llm(self, turn: int, message_count: int) -> None:
        if self._hooks is None or not self._hooks.before_llm:
            return
        ctx = LLMCallContext(
            run_id=self._run_id,
            turn=turn,
            model=self._model or self._provider.config.default_model,
            message_count=message_count,
            run_cost_usd=self._run_cost_usd,
            context=self._context,
        )
        for hook in self._hooks.before_llm:
            decision = await run_hook(hook, ctx)
            if decision.kind == "allow":
                continue
            reason = (
                decision.reason or "denied"
                if decision.kind == "deny"
                else f"invalid decision '{decision.kind}' from before_llm hook"
            )
            self._audit_event("llm_call_denied", "agent", {"turn": turn, "reason": reason})
            raise RunStoppedByHookError("before_llm", reason)

    def _audit_event(self, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        if self._audit:
            self._audit.append(event_type, actor=actor, payload=payload)

    async def _cb_call(
        self, run_id: str, fn: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """
        Thin wrapper around CircuitBreaker.call that detects state transitions
        and reports them to the CloudReporter.
        """
        prev_state = self._cb.state
        try:
            result = await self._cb.call(fn, *args, **kwargs)
        except Exception:
            new_state = self._cb.state
            if prev_state != new_state:
                await self._report_cb_transition(run_id, prev_state, new_state)
            raise
        new_state = self._cb.state
        if prev_state != new_state:
            await self._report_cb_transition(run_id, prev_state, new_state)
        return result

    async def _report_cb_transition(self, run_id: str, prev_state: Any, new_state: Any) -> None:
        """Record a circuit breaker state transition in both the audit chain and CloudReporter."""
        failure_count = self._cb.stats().failure_count
        if self._audit:
            self._audit.append(
                "circuit_breaker_state_change",
                actor=self._provider.name(),
                payload={
                    "resource": self._provider.name(),
                    "prev_state": prev_state.value,
                    "new_state": new_state.value,
                    "failure_count": failure_count,
                },
            )
        if self._reporter:
            await self._reporter.on_circuit_state_change(
                run_id=run_id,
                resource=self._provider.name(),
                prev_state=prev_state.value,
                new_state=new_state.value,
                failure_count=failure_count,
            )
