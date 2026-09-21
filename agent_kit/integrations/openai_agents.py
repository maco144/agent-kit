"""
OpenAI Agents SDK adapter — report OpenAI Agents SDK runs to agent-kit Cloud.

Usage::

    from agents import add_trace_processor
    from agent_kit.cloud import CloudReporter
    from agent_kit.integrations.openai_agents import AgentKitTraceProcessor

    add_trace_processor(AgentKitTraceProcessor(CloudReporter(project="support")))

Each trace becomes one agent-kit run; OpenAI's own trace exporter keeps running. Nothing
is recorded while Agents SDK tracing is disabled.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

try:
    from agents import RunHooks
    from agents.tracing import TracingProcessor
except ImportError as e:
    raise ImportError(
        "AgentKitTraceProcessor requires the 'openai-agents' package. "
        "Install it with: pip install agent-kit-ai[openai-agents]"
    ) from e

from agent_kit.exceptions import BudgetExceededError, UnpricedModelError
from agent_kit.integrations.recorder import RunRecorder, is_priced, price_call

if TYPE_CHECKING:
    from agents import Agent, ModelResponse, RunContextWrapper, TResponseInputItem
    from agents.tracing import Span, Trace

    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.integrations")

HARNESS = "openai-agents"
_RUN_ID_NAMESPACE = uuid.UUID("5d0f6a52-8c1e-4b7a-9f3d-2e6c1a4b8d70")


def run_id_for_trace(trace_id: str) -> str:
    """agent-kit run IDs are UUIDs; Agents SDK trace IDs are not. Derive one deterministically."""
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, trace_id))


def _usage_value(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    return int(value or 0)


def _duration_ms(span: Any) -> int:
    try:
        started = datetime.fromisoformat(span.started_at)
        ended = datetime.fromisoformat(span.ended_at)
    except (TypeError, ValueError):
        return 0
    return max(0, int((ended - started).total_seconds() * 1000))


class AgentKitTraceProcessor(TracingProcessor):
    """Maps Agents SDK traces and spans onto agent-kit runs. Never raises into the SDK."""

    def __init__(self, reporter: CloudReporter, agent_name: str | None = None) -> None:
        self._recorder = RunRecorder(reporter, harness=HARNESS, agent_name=agent_name)
        self._agent_errors: dict[str, str] = {}  # trace_id -> agent span error message
        self._lock = threading.Lock()

    def on_trace_start(self, trace: Trace) -> None:
        try:
            metadata: dict[str, Any] = {"trace_id": trace.trace_id, "workflow": trace.name}
            group_id = getattr(trace, "group_id", None)
            if group_id:
                metadata["group_id"] = group_id
            self._recorder.start(
                run_id_for_trace(trace.trace_id),
                model=None,
                prompt=None,
                metadata=metadata,
                agent_name=trace.name,
            )
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_trace_start failed", exc_info=True)

    def on_trace_end(self, trace: Trace) -> None:
        try:
            with self._lock:
                error = self._agent_errors.pop(trace.trace_id, None)
            run_id = run_id_for_trace(trace.trace_id)
            if error is None:
                self._recorder.complete(run_id)
            else:
                self._recorder.error(run_id, "AgentError", error)
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_trace_end failed", exc_info=True)

    def on_span_start(self, span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            self._record_span(span)
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_span_end failed", exc_info=True)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None

    def _record_span(self, span: Any) -> None:
        data = span.span_data
        run_id = run_id_for_trace(span.trace_id)
        kind = data.type

        if kind == "response":
            response = data.response
            usage = getattr(response, "usage", None) if response is not None else None
            self._recorder.llm_turn(
                run_id,
                getattr(response, "model", None),
                input_tokens=_usage_value(usage or data.usage, "input_tokens"),
                output_tokens=_usage_value(usage or data.usage, "output_tokens"),
                duration_ms=_duration_ms(span),
            )
        elif kind == "generation":
            self._recorder.llm_turn(
                run_id,
                data.model,
                input_tokens=_usage_value(data.usage, "input_tokens"),
                output_tokens=_usage_value(data.usage, "output_tokens"),
                duration_ms=_duration_ms(span),
            )
        elif kind == "function":
            error = span.error
            self._recorder.tool_call(
                run_id,
                span.span_id,
                data.name,
                success=error is None,
                error=error.get("message") if error else None,
                duration_ms=_duration_ms(span),
            )
        elif kind == "handoff":
            self._recorder.audit(
                run_id, "handoff", actor=data.from_agent or "agent", payload={"to_agent": data.to_agent}
            )
        elif kind == "guardrail":
            self._recorder.audit(
                run_id, "guardrail", actor=data.name, payload={"triggered": data.triggered}
            )
        elif kind == "agent" and span.error:
            with self._lock:
                self._agent_errors[span.trace_id] = span.error.get("message") or "agent error"


def _model_name(agent: Any) -> str:
    model = getattr(agent, "model", None)
    if isinstance(model, str):
        return model
    name = getattr(model, "model", None)
    return name if isinstance(name, str) else ""


class AgentKitRunHooks(RunHooks[Any]):
    """
    Enforce agent-kit cost ceilings on OpenAI Agents SDK runs.

        Runner.run(agent, input, hooks=AgentKitRunHooks(reporter, max_run_cost_usd=2.0))

    Raises BudgetExceededError from on_llm_start — before the model is called — when the
    run's spend reaches ``max_run_cost_usd`` or a fleet budget covering the agent is
    exhausted. Set CloudReporter(agent_name=...) so budgets and reported runs share a name.
    """

    def __init__(
        self,
        reporter: CloudReporter,
        agent_name: str | None = None,
        max_run_cost_usd: float | None = None,
        enforce_budgets: bool = True,
    ) -> None:
        self._reporter = reporter
        self._agent_name = agent_name
        self._max_run_cost_usd = max_run_cost_usd
        self._guard = reporter.budget_guard() if enforce_budgets else None

    def _name(self, agent: Any) -> str:
        return self._reporter.agent_name or self._agent_name or str(getattr(agent, "name", "") or "agent")

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        if self._guard is not None:
            await self._guard.check(self._name(agent), self._reporter.project)
        if self._max_run_cost_usd is None and self._guard is None:
            return
        # Spend on an unpriced model would count as $0.00 and never reach a cap
        if not is_priced(_model_name(agent)):
            raise UnpricedModelError(_model_name(agent) or "<unset: pass the Agent a model name>")
        if self._max_run_cost_usd is not None:
            usage = context.usage
            spent = price_call(_model_name(agent), int(usage.input_tokens or 0), int(usage.output_tokens or 0))
            if spent >= self._max_run_cost_usd:
                raise BudgetExceededError(scope="run", limit_usd=self._max_run_cost_usd, spent_usd=spent)

    async def on_llm_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse
    ) -> None:
        if self._guard is None:
            return
        try:
            usage = response.usage
            cost = price_call(_model_name(agent), int(usage.input_tokens or 0), int(usage.output_tokens or 0))
            self._guard.record_spend(self._name(agent), self._reporter.project, cost)
        except Exception:
            logger.debug("AgentKitRunHooks.on_llm_end failed", exc_info=True)
