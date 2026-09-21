"""OpenAI Agents SDK adapter, driven through the SDK's real tracing machinery (no network)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as NS

import pytest

tracing = pytest.importorskip("agents.tracing")

from agent_kit.exceptions import BudgetExceededError, UnpricedModelError  # noqa: E402
from agent_kit.integrations.openai_agents import (  # noqa: E402
    AgentKitRunHooks,
    AgentKitTraceProcessor,
    run_id_for_trace,
)


@pytest.fixture
def processor(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    tracing.set_trace_processors([proc])
    yield proc
    tracing.set_trace_processors([])


def test_run_ids_are_uuids_derived_from_trace_ids():
    trace_id = "trace_" + "a" * 32
    assert run_id_for_trace(trace_id) == run_id_for_trace(trace_id)
    assert str(uuid.UUID(run_id_for_trace(trace_id))) == run_id_for_trace(trace_id)
    assert len(run_id_for_trace(trace_id)) == 36


def test_trace_becomes_a_run_with_turns_tools_and_audit(cloud_capture, processor):
    with tracing.trace("support-workflow", group_id="conv-1") as trace:
        with tracing.agent_span("triage"):
            with tracing.generation_span(model="gpt-4o", usage={"input_tokens": 1000, "output_tokens": 100}):
                pass
            with tracing.function_span("lookup_order", input='{"id": 1}'):
                pass
            with tracing.function_span("refund", input="{}") as failing:
                failing.set_error({"message": "payment API down", "data": None})
            with tracing.guardrail_span("pii_filter", triggered=True):
                pass
            with tracing.handoff_span(from_agent="triage", to_agent="billing"):
                pass
            with tracing.generation_span(model="gpt-4o-mini", usage={"input_tokens": 50, "output_tokens": 5}):
                pass

    run_id = run_id_for_trace(trace.trace_id)
    assert cloud_capture.types() == [
        "run_start", "turn_complete", "turn_complete", "run_complete", "audit_flush",
    ]
    assert {e.run_id for e in cloud_capture.events} == {run_id}

    start = cloud_capture.of("run_start")[0]
    assert start.agent_name == "support-workflow"
    assert start.payload["model"] == "gpt-4o"
    assert start.payload["harness"] == "openai-agents"
    assert (start.payload["trace_id"], start.payload["group_id"]) == (trace.trace_id, "conv-1")

    first, second = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (first["input_tokens"], first["output_tokens"]) == (1000, 100)
    assert first["cost_usd"] == pytest.approx((1000 * 2.5 + 100 * 10) / 1_000_000)
    assert second["cost_usd"] == pytest.approx((50 * 0.15 + 5 * 0.6) / 1_000_000)

    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "llm_complete", "tool_call", "tool_call", "guardrail", "handoff",
        "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(run_id)


def test_agent_span_error_fails_the_run(cloud_capture, processor):
    with tracing.trace("wf") as trace:
        with tracing.agent_span("worker") as agent:
            agent.set_error({"message": "MaxTurnsExceeded", "data": None})

    assert cloud_capture.types() == ["run_start", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("AgentError", "MaxTurnsExceeded")
    cloud_capture.assert_chain_intact(run_id_for_trace(trace.trace_id))


def test_response_spans_use_response_usage(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    trace = NS(trace_id="trace_" + "b" * 32, name="wf", group_id=None)
    proc.on_trace_start(trace)
    span = NS(
        trace_id=trace.trace_id,
        span_id="span_1",
        error=None,
        started_at="2026-09-13T10:00:00+00:00",
        ended_at="2026-09-13T10:00:01.250000+00:00",
        span_data=NS(type="response", response=NS(model="gpt-4o", usage=NS(input_tokens=40, output_tokens=8)), usage=None),
    )
    proc.on_span_end(span)
    proc.on_trace_end(trace)

    turn = cloud_capture.of("turn_complete")[0].payload
    assert (turn["input_tokens"], turn["output_tokens"], turn["duration_ms"]) == (40, 8, 1250)


def test_processor_never_raises_on_malformed_spans(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    proc.on_span_end(NS(trace_id="t", span_id="s", error=None, started_at=None, ended_at=None, span_data=None))
    proc.on_trace_end(NS(trace_id="never-started", name="x", group_id=None))
    proc.shutdown()
    proc.force_flush()
    assert cloud_capture.events == []


class GuardStub:
    def __init__(self, tripped: bool) -> None:
        self.tripped = tripped
        self.checks: list[tuple[str, str]] = []
        self.spend: list[float] = []

    async def check(self, agent_name: str, project: str) -> None:
        self.checks.append((agent_name, project))
        if self.tripped:
            raise BudgetExceededError(scope="budget", limit_usd=1.0, spent_usd=1.2, budget_name="openai daily")

    def record_spend(self, agent_name: str, project: str, usd: float) -> None:
        self.spend.append(usd)


def _never_called_model():
    from agents.models.interface import Model

    class NeverCalled(Model):
        async def get_response(self, *args, **kwargs):
            raise AssertionError("model must not be called when the budget is exhausted")

        def stream_response(self, *args, **kwargs):
            raise AssertionError("model must not be called when the budget is exhausted")

    return NeverCalled()


async def test_run_hooks_stop_runner_before_model_call(cloud_capture):
    from agents import Agent as OAIAgent
    from agents import RunConfig, Runner

    guard = GuardStub(tripped=True)
    cloud_capture.reporter._budget_guard = guard
    hooks = AgentKitRunHooks(cloud_capture.reporter, agent_name="support")

    with pytest.raises(BudgetExceededError) as info:
        await Runner.run(
            OAIAgent(name="support", instructions="x", model=_never_called_model()),
            "hi",
            hooks=hooks,
            run_config=RunConfig(tracing_disabled=True),
        )

    assert info.value.budget_name == "openai daily"
    assert guard.checks == [("support", "proj")]


async def test_run_hooks_per_run_cap_from_context_usage(cloud_capture):
    hooks = AgentKitRunHooks(cloud_capture.reporter, max_run_cost_usd=0.01, enforce_budgets=False)
    context = NS(usage=NS(input_tokens=2000, output_tokens=1000))  # gpt-4o: $0.015
    agent = NS(name="a", model="gpt-4o")

    with pytest.raises(BudgetExceededError) as info:
        await hooks.on_llm_start(context, agent, None, [])
    assert (info.value.scope, info.value.spent_usd) == ("run", pytest.approx(0.015))

    await AgentKitRunHooks(cloud_capture.reporter, max_run_cost_usd=1.0, enforce_budgets=False).on_llm_start(context, agent, None, [])


async def test_run_hooks_refuse_a_cap_they_cannot_price(cloud_capture):
    context = NS(usage=NS(input_tokens=0, output_tokens=0))
    capped = AgentKitRunHooks(cloud_capture.reporter, max_run_cost_usd=1.0, enforce_budgets=False)
    with pytest.raises(UnpricedModelError, match="gpt-future"):
        await capped.on_llm_start(context, NS(name="a", model="gpt-future"), None, [])

    cloud_capture.reporter._budget_guard = GuardStub(tripped=False)
    with pytest.raises(UnpricedModelError):
        await AgentKitRunHooks(cloud_capture.reporter).on_llm_start(context, NS(name="a", model="gpt-future"), None, [])


async def test_run_hooks_record_spend_on_llm_end(cloud_capture):
    guard = GuardStub(tripped=False)
    cloud_capture.reporter._budget_guard = guard
    hooks = AgentKitRunHooks(cloud_capture.reporter)
    agent = NS(name="support", model=NS(model="gpt-4o-mini"))

    await hooks.on_llm_start(NS(usage=NS(input_tokens=0, output_tokens=0)), agent, None, [])
    await hooks.on_llm_end(NS(), agent, NS(usage=NS(input_tokens=1_000_000, output_tokens=0)))

    assert guard.spend == [pytest.approx(0.15)]
