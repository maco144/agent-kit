"""Tests for DAGOrchestrator."""

from __future__ import annotations

import asyncio

import pytest

from agent_kit import Agent
from agent_kit.exceptions import DAGCycleError, DAGMissingDependencyError
from agent_kit.orchestrator.dag import DAGOrchestrator, TaskNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_echo_provider(prefix: str = ""):
    """Provider that echoes the last user message, optionally prefixed."""
    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, Turn

    class EchoProvider:
        config = ProviderConfig(default_model="mock")

        def name(self): return "mock"

        async def complete(self, messages, **kw):
            last = messages[-1].content if messages else ""
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content=f"{prefix}{last}"),
                cost=CostSummary(total_tokens=5, cost_usd=0.0001),
            )

        async def stream(self, *a, **kw): yield ""

    return EchoProvider()


def make_static_provider(response: str):
    """Provider that always returns the same response."""
    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, Turn

    class StaticProvider:
        config = ProviderConfig(default_model="mock")
        def name(self): return "mock"
        async def complete(self, messages, **kw):
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content=response),
                cost=CostSummary(total_tokens=5, cost_usd=0.0001),
            )
        async def stream(self, *a, **kw): yield response

    return StaticProvider()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dag_single_node():
    agent = Agent(make_static_provider("done"))
    dag = DAGOrchestrator([TaskNode("only", agent, "{input}")])
    result = await dag.execute("hello")
    assert result.final_output == "done"
    assert result.node_results["only"].output == "done"


@pytest.mark.asyncio
async def test_dag_linear_chain():
    """A → B → C: each node depends on the previous."""
    results_order = []

    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, Turn

    class OrderRecorder:
        def __init__(self, name, response):
            self.config = ProviderConfig(default_model="mock")
            self._name = name
            self._response = response
        def name(self): return "mock"
        async def complete(self, messages, **kw):
            results_order.append(self._name)
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content=self._response),
                cost=CostSummary(total_tokens=2),
            )
        async def stream(self, *a, **kw): yield ""

    dag = DAGOrchestrator([
        TaskNode("a", Agent(OrderRecorder("a", "A_out")), "{input}"),
        TaskNode("b", Agent(OrderRecorder("b", "B_out")), "{upstream:a}", depends_on=["a"]),
        TaskNode("c", Agent(OrderRecorder("c", "C_out")), "{upstream:b}", depends_on=["b"]),
    ])
    result = await dag.execute("start")

    # a must finish before b, b before c
    assert results_order.index("a") < results_order.index("b")
    assert results_order.index("b") < results_order.index("c")
    assert result.final_output == "C_out"


@pytest.mark.asyncio
async def test_dag_parallel_fan_out():
    """Root → [B, C] → D: B and C run in parallel."""
    start_times: dict[str, float] = {}
    end_times: dict[str, float] = {}

    import time

    from agent_kit.providers.base import ProviderConfig
    from agent_kit.types import CostSummary, Message, Turn

    class TimedProvider:
        def __init__(self, name, delay, response):
            self.config = ProviderConfig(default_model="mock")
            self._name = name
            self._delay = delay
            self._response = response
        def name(self): return "mock"
        async def complete(self, messages, **kw):
            start_times[self._name] = time.monotonic()
            await asyncio.sleep(self._delay)
            end_times[self._name] = time.monotonic()
            return Turn(
                messages_in=messages,
                message_out=Message(role="assistant", content=self._response),
                cost=CostSummary(total_tokens=2),
            )
        async def stream(self, *a, **kw): yield ""

    dag = DAGOrchestrator([
        TaskNode("root", Agent(TimedProvider("root", 0, "root_out")), "{input}"),
        TaskNode("b", Agent(TimedProvider("b", 0.05, "B_out")), "{upstream:root}", depends_on=["root"]),
        TaskNode("c", Agent(TimedProvider("c", 0.05, "C_out")), "{upstream:root}", depends_on=["root"]),
        TaskNode("d", Agent(TimedProvider("d", 0, "D_out")),
                 "{upstream:b} + {upstream:c}", depends_on=["b", "c"]),
    ])

    t0 = time.monotonic()
    result = await dag.execute("go")
    elapsed = time.monotonic() - t0

    # B and C run in parallel — total time should be ~0.05s not ~0.10s
    assert elapsed < 0.09, f"Expected parallel execution but took {elapsed:.3f}s"
    assert "b" in result.node_results
    assert "c" in result.node_results
    assert result.final_output == "D_out"


@pytest.mark.asyncio
async def test_dag_upstream_injection():
    """Verify {upstream:X} substitution works correctly."""
    dag = DAGOrchestrator([
        TaskNode("a", Agent(make_static_provider("ALPHA")), "{input}"),
        TaskNode("b", Agent(make_static_provider("BETA")),  "{input}"),
        TaskNode("c", Agent(make_echo_provider()),
                 "combine: {upstream:a} and {upstream:b}",
                 depends_on=["a", "b"]),
    ])
    result = await dag.execute("start")
    # The echo provider returns what was passed to it, so c's output reflects the prompt
    assert "ALPHA" in result.node_results["c"].turns[0].messages_in[-1].content
    assert "BETA" in result.node_results["c"].turns[0].messages_in[-1].content


@pytest.mark.asyncio
async def test_dag_cost_accumulation():
    dag = DAGOrchestrator([
        TaskNode("a", Agent(make_static_provider("x")), "{input}"),
        TaskNode("b", Agent(make_static_provider("y")), "{input}"),
        TaskNode("c", Agent(make_static_provider("z")), "{input}"),
    ])
    result = await dag.execute("go")
    assert result.total_cost_usd == pytest.approx(0.0003)  # 3 × 0.0001
    assert result.total_tokens == 15  # 3 × 5


def test_dag_cycle_detection():
    dag_nodes = [
        TaskNode("a", Agent(make_static_provider("x")), depends_on=["b"]),
        TaskNode("b", Agent(make_static_provider("y")), depends_on=["a"]),
    ]
    with pytest.raises(DAGCycleError):
        DAGOrchestrator(dag_nodes)


def test_dag_missing_dependency():
    with pytest.raises(DAGMissingDependencyError):
        DAGOrchestrator([
            TaskNode("a", Agent(make_static_provider("x")), depends_on=["nonexistent"]),
        ])


def test_dag_validate_clean():
    dag = DAGOrchestrator([
        TaskNode("a", Agent(make_static_provider("x"))),
        TaskNode("b", Agent(make_static_provider("y")), depends_on=["a"]),
    ])
    errors = dag.validate()
    assert errors == []
