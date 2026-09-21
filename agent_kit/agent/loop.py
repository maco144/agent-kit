"""AgentLoop — the execution engine that drives a single agent run."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_kit.agent.delegation import AgentTool, Delegation, DelegationContext, child_run_id
from agent_kit.audit.chain import AuditChain
from agent_kit.durable import Checkpointer, PendingTurn, RunCheckpoint, RunStatus, RunStore
from agent_kit.exceptions import (
    BudgetExceededError,
    MaxTurnsExceededError,
    OutputValidationError,
    RunConflictError,
    RunStoppedByHookError,
    UnpricedModelError,
)
from agent_kit.hooks import (
    ApprovalRequest,
    Decision,
    LLMCallContext,
    Suspend,
    ToolCallContext,
    ToolResultContext,
    flagged_payload,
    run_hook,
)
from agent_kit.memory.budget import DEFAULT_TOKENS_PER_CHAR, plan_trim, prompt_chars
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
    PendingApproval,
    RequestOptions,
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

logger = logging.getLogger(__name__)

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
        approver: Approver | Suspend | None = None,
        approval_timeout_s: float = 300.0,
        output_retries: int = 2,
        request_options: RequestOptions | None = None,
        context_budget_tokens: int | None = None,
        run_store: RunStore | None = None,
        delegation_depth: int = 0,
        max_delegation_depth: int = 5,
        parent_run_id: str | None = None,
        parent_call_id: str | None = None,
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
        self._request_options = request_options or RequestOptions()
        self._context_budget_tokens = context_budget_tokens
        self._last_prompt_tokens = 0  # prompt tokens the provider reported for the previous call
        self._last_prompt_chars = 0  # characters sent in that call
        self._run_id = ""
        self._context: dict[str, Any] = {}
        self._pending_stop: RunStoppedByHookError | None = None
        self._turns: list[Turn] = []
        self._checkpointer = Checkpointer(run_store) if run_store is not None else None
        self._delegation_depth = delegation_depth  # 0 for a top-level run
        self._max_delegation_depth = max_delegation_depth
        self._parent_run_id = parent_run_id  # set on delegated (child) runs
        self._parent_call_id = parent_call_id
        self._prompt = ""
        self._output_type_name: str | None = None
        self._pending: PendingTurn | None = None  # the model turn whose tool calls are being resolved
        self._suspended: dict[str, list[PendingApproval]] = {}  # calls parked by approver=SUSPEND this turn
        self.result: AgentResult[Any] | None = None

    async def run(
        self, prompt: str, output_type: Any = None, run_id: str | None = None, **context: Any
    ) -> AgentResult[Any]:
        """Execute the agent loop and return the final result."""
        async for _ in self._execute(prompt, False, context, output_type, run_id=run_id):
            pass
        assert self.result is not None
        return self.result

    async def stream(
        self, prompt: str, output_type: Any = None, run_id: str | None = None, **context: Any
    ) -> AsyncIterator[str]:
        """Execute the agent loop, yielding text as it streams. ``self.result`` is set at the end."""
        async for chunk in self._execute(prompt, True, context, output_type, run_id=run_id):
            yield chunk

    async def resume(
        self, checkpoint: RunCheckpoint, approvals: dict[str, bool], output_type: Any = None
    ) -> AgentResult[Any]:
        """Continue a checkpointed run; ``approvals`` answers pending approvals by call id."""
        async for _ in self._execute(
            checkpoint.prompt, False, checkpoint.context, output_type, restore=checkpoint, approvals=approvals
        ):
            pass
        assert self.result is not None
        return self.result

    async def resume_stream(
        self, checkpoint: RunCheckpoint, approvals: dict[str, bool], output_type: Any = None
    ) -> AsyncIterator[str]:
        """Streaming resume(). ``self.result`` is set at the end."""
        async for chunk in self._execute(
            checkpoint.prompt, True, checkpoint.context, output_type, restore=checkpoint, approvals=approvals
        ):
            yield chunk

    async def _execute(
        self,
        prompt: str,
        streaming: bool,
        context: dict[str, Any],
        output_type: Any = None,
        *,
        run_id: str | None = None,
        restore: RunCheckpoint | None = None,
        approvals: dict[str, bool] | None = None,
    ) -> AsyncIterator[str]:
        """The agent loop. Yields text chunks when ``streaming``; sets ``self.result`` when it ends."""
        run_id = restore.run_id if restore else (run_id or str(uuid.uuid4()))
        self._run_id = run_id
        self._context = dict(context)
        self._prompt = prompt

        # Typed runs: constrain natively when the provider can, else describe the schema in the prompt
        spec = OutputSpec.from_type(output_type) if output_type is not None else None
        native_capable = (
            spec is not None
            and spec.native_compatible
            and bool(getattr(self._provider, "supports_structured_output", False))
        )
        # Some providers' native constraint rules out tool calls: prompt mode until the model answers
        native = restore.native_output if restore else native_capable and (
            bool(getattr(self._provider, "structured_output_with_tools", True))
            or not self._registry.schemas()
        )
        system = self._system_prompt
        if spec is not None and not native:
            system = f"{system}\n\n{spec.instructions()}" if system else spec.instructions()
        output_kwargs: dict[str, Any] = {"output_schema": spec} if native else {}
        parsed: Any = None
        invalid_answers = restore.invalid_answers if restore else 0
        self._output_type_name = spec.name if spec else None

        options_kwargs: dict[str, Any] = {}
        if getattr(self._provider, "supports_request_options", False):
            options_kwargs["options"] = self._request_options
        elif not self._request_options.is_default():
            logger.warning(
                "%s does not accept request options; thinking/effort/caching/context settings are ignored",
                self._provider.name(),
            )

        if self._checkpointer is not None and restore is None:
            try:
                json.dumps(context)
            except TypeError:
                raise TypeError("run context must be JSON-serialisable when run_store is set") from None
            if await self._checkpointer.store.load(run_id) is not None:
                raise ValueError(f"run '{run_id}' already exists; use agent.resume()")

        if self._reporter and restore is None:
            await self._reporter.on_run_start(
                run_id=run_id,
                model=self._model or self._provider.config.default_model,
                prompt=prompt,
                parent_run_id=self._parent_run_id,
            )

        with self._tracer.span("agent.run", kind=SpanKind.AGENT, run_id=run_id) as root_span:
            turn_count = 0
            resumed_pending: PendingTurn | None = None
            if restore is None:
                # Audit: agent start
                if self._audit:
                    self._audit.append(
                        "agent_start",
                        actor=run_id,
                        payload={"prompt_preview": prompt[:200], "context_keys": list(context.keys())},
                    )
                # A run that died mid tool call (cancelled, or a crash over persistent memory) left calls
                # without results; every later request would be rejected until they are answered
                self._answer_unfinished_calls()
                # Seed memory with the user prompt
                self._memory.add(Message(role="user", content=prompt))
                if self._checkpointer is not None:
                    await self._checkpointer.create(self._snapshot("running", turn_count, invalid_answers, native))
            else:
                assert self._checkpointer is not None
                turn_count = restore.turn_count
                self._turns = list(restore.turns)
                self._run_cost_usd = restore.run_cost_usd
                self._last_prompt_tokens = restore.last_prompt_tokens
                self._last_prompt_chars = restore.last_prompt_chars
                resumed_pending = restore.pending
                self._checkpointer.current = restore
                # Claim the run: a concurrent resume of the same checkpoint loses here
                await self._checkpointer.save(restore.model_copy(update={"status": "running", "error": None}))
                self._audit_event("run_resumed", run_id, {"turn": turn_count, "from_status": restore.status})

            final_output = ""
            suspended = False

            try:
                while True:
                    if resumed_pending is not None:
                        self._pending, resumed_pending = resumed_pending, None
                        turn = self._pending.turn
                        answers = approvals or {}
                    else:
                        if turn_count >= self._max_turns:
                            raise MaxTurnsExceededError(self._max_turns)
                        turn_count += 1
                        await self._enforce_budgets()
                        tool_schemas = self._registry.schemas()
                        self._trim_context(turn_count, system, tool_schemas)
                        messages = self._memory.history(include_system=False)
                        self._last_prompt_chars = prompt_chars(system, tool_schemas, messages)
                        await self._gate_llm(turn_count, len(messages))

                        # --- LLM call with circuit breaker + retry ---
                        with self._tracer.span(
                            "llm.complete",
                            kind=SpanKind.LLM,
                            turn=turn_count,
                            model=self._model or self._provider.config.default_model,
                        ) as llm_span:
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
                                    {**output_kwargs, **options_kwargs},
                                )
                                chunks: list[str] = []
                                streamed: Turn | None = None
                                while item is not None:
                                    if isinstance(item, Turn):
                                        streamed = item
                                    else:
                                        chunks.append(item)
                                        yield item
                                    item = await anext(it, None)
                                if streamed is not None:
                                    turn = streamed
                                else:  # provider streams text only
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
                                    **options_kwargs,
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

                        self._last_prompt_tokens = (
                            turn.cost.input_tokens + turn.cost.cache_read_tokens + turn.cost.cache_write_tokens
                        )
                        self._audit_context_events(turn_count, turn)

                        # Track cost
                        self._tracer.record_cost(
                            tokens=turn.cost.total_tokens,
                            model=turn.cost.model,
                            usd=turn.cost.cost_usd,
                        )
                        self._run_cost_usd += turn.cost.cost_usd
                        if not turn.cost.priced and (
                            self._max_run_cost_usd is not None or self._budget_guard is not None
                        ):
                            # $0.00 here means "unknown": counting it would let the cap pass forever
                            raise UnpricedModelError(turn.cost.model)
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

                        self._pending = PendingTurn(turn=turn)
                        answers = {}
                        await self._checkpoint("running", turn_count, invalid_answers, native)  # A

                    # --- Resolve tool calls concurrently; record results in call order ---
                    waiting = await self._resolve_tools(turn_count, self._pending, answers)
                    if waiting:
                        self._audit_event(
                            "run_suspended",
                            run_id,
                            {"turn": turn_count, "pending_call_ids": [a.call_id for a in waiting]},
                        )
                        await self._checkpoint("suspended", turn_count, invalid_answers, native)  # D
                        suspended = True
                        break
                    self._record_tool_results(self._pending)
                    self._pending = None
                    self._turns.append(turn)
                    if self._reporter:
                        await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                    await self._checkpoint("running", turn_count, invalid_answers, native)  # C
                    if self._pending_stop is not None:
                        raise self._pending_stop

            except (Exception, asyncio.CancelledError) as exc:
                self._answer_unfinished_calls()
                if self._reporter:
                    await self._reporter.on_run_error(run_id, exc, turn_count)
                if self._checkpointer is not None and not isinstance(exc, RunConflictError):
                    await self._checkpointer.fail(f"{type(exc).__name__}: {exc}")
                raise

            # Audit: agent complete
            if self._audit and not suspended:
                self._audit.append(
                    "agent_complete",
                    actor=run_id,
                    payload={
                        "turns": turn_count,
                        "total_tokens": self._totals()[1],
                        "total_cost_usd": self._totals()[0],
                        "output_type": spec.name if spec else None,
                    },
                )

            root_span.set_attribute("total_turns", turn_count)
            root_span.set_attribute("total_cost_usd", self._totals()[0])

        if suspended:
            assert self._pending is not None
            self.result = AgentResult(
                output="",
                run_id=run_id,
                status="suspended",
                pending_approvals=list(self._pending.approvals),
                turns=list(self._turns),
                total_cost_usd=self._totals()[0],
                total_tokens=self._totals()[1],
                audit_root_hash=self._audit.root_hash() if self._audit else None,
                trace_id=self._tracer.trace_id,
            )
            return

        result: AgentResult[Any] = AgentResult(
            output=final_output,
            parsed=parsed,
            turns=self._turns,
            total_cost_usd=self._totals()[0],
            total_tokens=self._totals()[1],
            run_id=run_id,
            audit_root_hash=self._audit.root_hash() if self._audit else None,
            trace_id=self._tracer.trace_id,
        )
        await self._checkpoint(
            "completed", turn_count, invalid_answers, native, result=result.model_dump(mode="json")
        )  # E

        if self._reporter:
            await self._reporter.on_run_complete(run_id, result)
            if self._audit:
                await self._reporter.on_audit_flush(
                    run_id=run_id,
                    events=self._audit.events(),
                    final_root_hash=self._audit.root_hash(),
                )

        self.result = result

    def _snapshot(
        self,
        status: RunStatus,
        turn_count: int,
        invalid_answers: int,
        native: bool,
        result: dict[str, Any] | None = None,
    ) -> RunCheckpoint:
        now = datetime.utcnow()
        current = self._checkpointer.current if self._checkpointer else None
        return RunCheckpoint(
            run_id=self._run_id,
            version=current.version if current else 0,
            status=status,
            created_at=current.created_at if current else now,
            updated_at=now,
            prompt=self._prompt,
            context=self._context,
            output_type_name=self._output_type_name,
            messages=self._memory.history(),
            turns=list(self._turns),
            turn_count=turn_count,
            run_cost_usd=self._run_cost_usd,
            invalid_answers=invalid_answers,
            native_output=native,
            last_prompt_tokens=self._last_prompt_tokens,
            last_prompt_chars=self._last_prompt_chars,
            audit_events=self._audit.events() if self._audit else [],
            pending=self._pending.model_copy(deep=True) if self._pending else None,
            result=result,
            parent_run_id=self._parent_run_id,
            parent_call_id=self._parent_call_id,
        )

    async def _checkpoint(
        self,
        status: RunStatus,
        turn_count: int,
        invalid_answers: int,
        native: bool,
        result: dict[str, Any] | None = None,
    ) -> None:
        if self._checkpointer is not None:
            await self._checkpointer.save(self._snapshot(status, turn_count, invalid_answers, native, result))

    async def _resolve_tools(
        self, turn_number: int, pending: PendingTurn, answers: dict[str, bool]
    ) -> list[PendingApproval]:
        """Resolve every tool call of a pending turn concurrently; return approvals still waiting."""
        order = {tc.call_id: i for i, tc in enumerate(pending.turn.tool_calls)}

        async def resolve(tc: ToolCall) -> None:
            if tc.call_id in pending.results:
                return
            approval = next((a for a in pending.approvals if a.call_id == tc.call_id), None)
            prefix = f"{tc.call_id}/"
            delegated = [a for a in pending.approvals if a.call_id.startswith(prefix)]
            result: ToolResult | None
            if approval is not None:
                if tc.call_id not in answers:
                    return
                pending.approvals.remove(approval)
                if answers[tc.call_id]:
                    self._audit_event("approval_granted", tc.tool_name, {"call_id": tc.call_id, "via": "resume"})
                    result = await self._run_tool(tc, turn_number, gate=False)
                else:
                    self._audit_event(
                        "approval_denied",
                        tc.tool_name,
                        {"call_id": tc.call_id, "timed_out": False, "error": None, "via": "resume"},
                    )
                    reason = self._deny_tool(self._tool_context(tc, turn_number), "before_tool", "approval denied")
                    result = ToolResult(
                        call_id=tc.call_id, tool_name=tc.tool_name, output=None, error=f"Tool call denied: {reason}"
                    )
            elif delegated:
                # Approvals parked by a suspended child run: route this call's answers down to it
                if not any(a.call_id in answers for a in delegated):
                    return
                for a in delegated:
                    pending.approvals.remove(a)
                child_answers = {k[len(prefix):]: v for k, v in answers.items() if k.startswith(prefix)}
                result = await self._run_tool(tc, turn_number, gate=False, approvals=child_answers)
            elif tc.call_id in pending.started and self._resumes_delegation(tc.tool_name):
                # The child run checkpoints its own tools: resume or replay it rather than report an interruption
                result = await self._run_tool(tc, turn_number, gate=False)
            elif tc.call_id in pending.started and not self._is_idempotent(tc.tool_name):
                # Started before a crash with no recorded result: never run a side effect twice
                self._audit_event("tool_interrupted", tc.tool_name, {"call_id": tc.call_id})
                result = ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error="Tool call interrupted before completion; not retried",
                )
            else:
                result = await self._run_tool(tc, turn_number)
            if result is None:
                pending.approvals.extend(self._suspended.pop(tc.call_id))
            else:
                pending.results[tc.call_id] = result

        await asyncio.gather(*(resolve(tc) for tc in pending.turn.tool_calls))
        pending.approvals.sort(key=lambda a: order[a.call_id.split("/", 1)[0]])
        return list(pending.approvals)

    def _record_tool_results(self, pending: PendingTurn) -> None:
        """Audit the turn's tool results and add them to memory, in call order."""
        turn = pending.turn
        for tc in turn.tool_calls:
            tool_result = pending.results[tc.call_id]
            if self._audit:
                payload: dict[str, Any] = {
                    "call_id": tc.call_id,
                    "success": tool_result.error is None,
                    "error": tool_result.error,
                    "duration_ms": tool_result.duration_ms,
                }
                if tc.call_id in pending.delegated_cost_usd:
                    payload["delegated_run_id"] = child_run_id(self._run_id, tc.call_id)
                    payload["delegated_cost_usd"] = pending.delegated_cost_usd[tc.call_id]
                    if tc.call_id in pending.delegated_root_hash:
                        payload["delegated_root_hash"] = pending.delegated_root_hash[tc.call_id]
                self._audit.append("tool_call", actor=tc.tool_name, payload=payload)
            self._memory.add(_tool_message(tool_result))
            turn.tool_results.append(tool_result)

    def _answer_unfinished_calls(self) -> None:
        """Give the last model turn's unanswered tool calls a result: the one it produced, else an error."""
        messages = self._memory.history(include_system=False)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "tool":
                continue
            if messages[i].role != "assistant":
                return
            answered = {m.tool_call_id for m in messages[i + 1:]}
            results = self._pending.results if self._pending is not None else {}
            for tc in messages[i].tool_calls:
                if tc.call_id not in answered:
                    self._memory.add(_tool_message(results.get(tc.call_id) or ToolResult(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        output=None,
                        error="Tool call interrupted: the run ended before it returned",
                    )))
            return

    def _is_idempotent(self, tool_name: str) -> bool:
        try:
            return self._registry.get(tool_name).schema.idempotent
        except Exception:
            return False

    def _resumes_delegation(self, tool_name: str) -> bool:
        """A started agent-tool call resumes its checkpointed child run instead of counting as interrupted."""
        if self._checkpointer is None:
            return False
        try:
            return isinstance(self._registry.get(tool_name), AgentTool)
        except Exception:
            return False

    def _tool_context(self, tc: ToolCall, turn: int) -> ToolCallContext:
        return ToolCallContext(
            run_id=self._run_id,
            turn=turn,
            tool_name=tc.tool_name,
            arguments=dict(tc.arguments),
            call_id=tc.call_id,
            context=self._context,
        )

    async def _mark_started(self, call_id: str) -> None:
        if self._pending is not None:
            if call_id in self._pending.started:
                return
            self._pending.started.append(call_id)
        if self._checkpointer is not None:
            await self._checkpointer.mark_started(call_id)  # B

    def _totals(self) -> tuple[float, int]:
        """Cost and tokens of this run's turns and the delegated runs they started."""
        cost = sum(t.cost.cost_usd + sum(r.cost_usd for r in t.tool_results) for t in self._turns)
        tokens = sum(t.cost.total_tokens + sum(r.tokens for r in t.tool_results) for t in self._turns)
        if self._pending is not None:  # delegations of the unresolved turn
            cost += sum(self._pending.delegated_cost_usd.values())
            tokens += sum(self._pending.delegated_tokens.values())
        return cost, tokens

    def spend(self) -> tuple[float, int]:
        """This run's cost and tokens so far, delegated runs included — also for a run that raised."""
        return self._run_cost_usd, self._totals()[1]

    def _trim_context(self, turn: int, system: str, tools: list[ToolSchema]) -> None:
        """Cut history once to half the token budget when the next request would exceed it."""
        budget = self._context_budget_tokens
        if budget is None or self._request_options.compaction is not None:
            return
        messages = self._memory.history(include_system=False)
        chars = prompt_chars(system, tools, messages)
        # Reported tokens are exact for what was already sent; only the new characters are estimated.
        # (A ratio from a short prompt is dominated by fixed template overhead and overestimates.)
        if self._last_prompt_tokens and self._last_prompt_chars:
            estimated = self._last_prompt_tokens + (chars - self._last_prompt_chars) * DEFAULT_TOKENS_PER_CHAR
        else:
            estimated = chars * DEFAULT_TOKENS_PER_CHAR
        ratio = max(estimated, 0.0) / chars if chars else DEFAULT_TOKENS_PER_CHAR
        drop, before, _ = plan_trim(messages, chars, budget, ratio)
        if drop == 0:
            return
        removed = self._memory.trim_oldest(drop)
        if removed == 0:  # the only messages left form the current exchange
            return
        after = math.ceil(prompt_chars(system, tools, self._memory.history(include_system=False)) * ratio)
        self._audit_event(
            "context_trimmed",
            "agent",
            {
                "turn": turn,
                "removed_messages": removed,
                "estimated_tokens_before": before,
                "estimated_tokens_after": after,
                "budget_tokens": budget,
            },
        )

    def _audit_context_events(self, turn_number: int, turn: Turn) -> None:
        """Record server-side compaction and context edits reported by the provider."""
        for event in turn.context_events:
            if event.get("type") == "compaction":
                payload: dict[str, Any] = {
                    "turn": turn_number,
                    "input_tokens": event.get("input_tokens", 0),
                    "output_tokens": event.get("output_tokens", 0),
                }
                self._audit_event("context_compacted", self._provider.name(), payload)
            else:
                payload = {"turn": turn_number, "edit": event.get("type")}
                for key in ("cleared_tool_uses", "cleared_thinking_turns", "cleared_input_tokens"):
                    if key in event:
                        payload[key] = event[key]
                self._audit_event("context_edited", self._provider.name(), payload)

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

    async def _run_tool(
        self, tc: ToolCall, turn: int, gate: bool = True, approvals: dict[str, bool] | None = None
    ) -> ToolResult | None:
        """Gate, execute, and filter one tool call inside its span. None when parked for approval."""
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
                denial = await self._gate_tool(tc, turn) if gate else None
                if tc.call_id in self._suspended:
                    return None  # parked until Agent.resume() answers the approval
                if denial is not None:
                    tool_result = failed(f"Tool call denied: {denial}")
                else:
                    await self._mark_started(tc.call_id)
                    if isinstance(tool, AgentTool):
                        delegated = await self._delegate(tool, tc, turn, approvals or {})
                        if delegated is None:
                            return None  # the child run suspended; its approvals are parked under this call
                        tool_result = delegated
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

    async def _delegate(
        self, tool: AgentTool, tc: ToolCall, turn: int, approvals: dict[str, bool]
    ) -> ToolResult | None:
        """Run a delegated child run. None when it suspended — its approvals are parked under this call."""
        remaining = (
            None if self._max_run_cost_usd is None else max(0.0, self._max_run_cost_usd - self._run_cost_usd)
        )
        ctx = DelegationContext(
            parent_run_id=self._run_id,
            call_id=tc.call_id,
            depth=self._delegation_depth + 1,
            max_depth=self._max_delegation_depth,
            context=self._context,
            hooks=self._hooks,
            approver=self._approver,
            approval_timeout_s=self._approval_timeout_s,
            run_store=self._checkpointer.store if self._checkpointer is not None else None,
            remaining_cost_usd=remaining,
            budget_guard=self._budget_guard,
            reporter=self._reporter,
            approvals=approvals,
        )
        delegation = await tool.delegate(str(tc.arguments.get("task", "")), ctx)
        self._add_delegated_spend(tc.call_id, delegation)
        if delegation.status == "suspended":
            self._suspended[tc.call_id] = delegation.approvals
            return None
        assert delegation.result is not None
        return delegation.result

    def _add_delegated_spend(self, call_id: str, delegation: Delegation) -> None:
        """Add the child's spend not yet counted in this run; remember the child's root hash."""
        if delegation.run_id is None or self._pending is None:
            return
        pending = self._pending
        self._run_cost_usd += delegation.cost_usd - pending.delegated_cost_usd.get(call_id, 0.0)
        pending.delegated_cost_usd[call_id] = delegation.cost_usd
        pending.delegated_tokens[call_id] = delegation.tokens
        if delegation.root_hash is not None:
            pending.delegated_root_hash[call_id] = delegation.root_hash

    async def _gate_tool(self, tc: ToolCall, turn: int) -> str | None:
        """Run before_tool hooks. Returns a denial reason, or None to execute."""
        if self._hooks is None or not self._hooks.before_tool:
            return None
        ctx = self._tool_context(tc, turn)
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
        if isinstance(self._approver, Suspend):
            self._suspended[ctx.call_id] = [
                PendingApproval(
                    call_id=ctx.call_id,
                    tool_name=ctx.tool_name,
                    arguments=dict(ctx.arguments),
                    reason=reason,
                    turn=ctx.turn,
                )
            ]
            return None
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
            if decision.findings:
                await self._record_flagged(tc, decision)
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

    async def _record_flagged(self, tc: ToolCall, decision: Decision) -> None:
        """Audit and report the findings an after_tool hook attached to its decision."""
        action = {"allow": "allowed", "replace": "wrapped"}.get(decision.kind, "blocked")
        if decision.kind == "deny" and decision.stop_run:
            action = "stopped"
        self._audit_event(
            "tool_output_flagged", tc.tool_name, flagged_payload(tc.call_id, tc.tool_name, action, decision.findings)
        )
        if self._reporter:
            await self._reporter.on_tool_output_flagged(
                self._run_id, tc.tool_name, tc.call_id, action, decision.findings
            )

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


def _tool_message(result: ToolResult) -> Message:
    """A tool result as the conversation message the model reads next."""
    return Message(
        role="tool",
        content=f"Error: {result.error}" if result.error else json.dumps(result.output, default=str),
        tool_call_id=result.call_id,
        metadata={"is_error": True} if result.error else {},
    )
