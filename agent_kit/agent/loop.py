"""AgentLoop — the execution engine that drives a single agent run."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from agent_kit.audit.chain import AuditChain
from agent_kit.exceptions import MaxTurnsExceededError
from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.observability.tracer import AgentTracer
from agent_kit.providers.base import BaseProvider
from agent_kit.reliability.circuit_breaker import CircuitBreaker
from agent_kit.reliability.retry import with_retry
from agent_kit.tools.registry import ToolRegistry
from agent_kit.types import (
    AgentResult,
    CircuitBreakerConfig,
    Message,
    RetryPolicyConfig,
    SpanKind,
    ToolResult,
    Turn,
)

if TYPE_CHECKING:
    from agent_kit.cloud.reporter import CloudReporter


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
        self._turns: list[Turn] = []

    async def run(self, prompt: str, **context: Any) -> AgentResult:
        """Execute the agent loop and return the final result."""
        run_id = str(uuid.uuid4())

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
                    messages = self._memory.history(include_system=False)
                    tool_schemas = self._registry.schemas()

                    # --- LLM call with circuit breaker + retry ---
                    with self._tracer.span(
                        "llm.complete",
                        kind=SpanKind.LLM,
                        turn=turn_count,
                        model=self._model or self._provider.config.default_model,
                    ) as llm_span:
                        turn: Turn = await with_retry(
                            self._cb_call,
                            self._retry_policy,
                            run_id,
                            self._provider.complete,
                            messages,
                            model=self._model,
                            tools=tool_schemas if tool_schemas else None,
                            system=self._system_prompt or None,
                            max_tokens=self._max_tokens_per_turn,
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

                    # Add assistant message to memory
                    if turn.message_out:
                        self._memory.add(turn.message_out)

                    # No tool calls → we have the final answer
                    if not turn.tool_calls:
                        final_output = turn.message_out.content if turn.message_out else ""
                        self._turns.append(turn)
                        if self._reporter:
                            await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                        break

                    # --- Execute tool calls ---
                    for tc in turn.tool_calls:
                        with self._tracer.span(
                            f"tool.{tc.tool_name}",
                            kind=SpanKind.TOOL,
                            tool=tc.tool_name,
                        ) as tool_span:
                            t0 = time.monotonic()
                            try:
                                tool = self._registry.get(tc.tool_name)
                                result = await tool(call_id=tc.call_id, **tc.arguments)
                            except Exception as exc:
                                result_error = str(exc)
                                result = ToolResult(
                                    call_id=tc.call_id,
                                    tool_name=tc.tool_name,
                                    output=None,
                                    error=result_error,
                                    duration_ms=int((time.monotonic() - t0) * 1000),
                                )

                            tool_span.set_attribute("duration_ms", result.duration_ms)
                            tool_span.set_attribute("success", result.error is None)

                            self._tracer.record_tool_call(
                                tc.tool_name,
                                result.duration_ms,
                                result.error is None,
                            )

                        # Audit: tool execution
                        if self._audit:
                            self._audit.append(
                                "tool_call",
                                actor=tc.tool_name,
                                payload={
                                    "call_id": tc.call_id,
                                    "success": result.error is None,
                                    "error": result.error,
                                    "duration_ms": result.duration_ms,
                                },
                            )

                        # Feed tool result back as a tool message
                        output_str = (
                            json.dumps(result.output, default=str)
                            if result.output is not None
                            else f"Error: {result.error}"
                        )
                        self._memory.add(
                            Message(
                                role="tool",
                                content=output_str,
                                tool_call_id=tc.call_id,
                            )
                        )
                        turn.tool_results.append(result)

                    self._turns.append(turn)
                    if self._reporter:
                        await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)

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
                    },
                )

            root_span.set_attribute("total_turns", turn_count)
            root_span.set_attribute("total_cost_usd", self._tracer.cumulative_cost_usd())

        result = AgentResult(
            output=final_output,
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

        return result

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
            if self._reporter:
                new_state = self._cb.state
                if prev_state != new_state:
                    await self._reporter.on_circuit_state_change(
                        run_id=run_id,
                        resource=self._provider.name(),
                        prev_state=prev_state.value,
                        new_state=new_state.value,
                        failure_count=self._cb.stats().failure_count,
                    )
            raise
        if self._reporter:
            new_state = self._cb.state
            if prev_state != new_state:
                await self._reporter.on_circuit_state_change(
                    run_id=run_id,
                    resource=self._provider.name(),
                    prev_state=prev_state.value,
                    new_state=new_state.value,
                    failure_count=self._cb.stats().failure_count,
                )
        return result
