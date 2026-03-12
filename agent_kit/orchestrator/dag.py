"""DAGOrchestrator — parallel multi-agent task graph execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agent_kit.agent.agent import Agent
from agent_kit.exceptions import DAGCycleError, DAGMissingDependencyError
from agent_kit.types import AgentResult, PipelineResult


@dataclass
class TaskNode:
    """
    A single node in a DAG.

    prompt_template supports ``{input}`` (initial DAG input) and
    ``{upstream:<node_id>}`` (output of a specific upstream node).
    If inject_upstream=True, all upstream outputs are available as
    ``{upstream:<node_id>}`` automatically.
    """

    node_id: str
    agent: Agent
    prompt_template: str = "{input}"
    depends_on: list[str] = field(default_factory=list)
    inject_upstream: bool = True


@dataclass
class DAGResult:
    """Aggregated result from a full DAG execution."""

    node_results: dict[str, AgentResult] = field(default_factory=dict)
    final_output: str = ""          # output of the last node in topological order
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_ms: int = 0
    execution_order: list[str] = field(default_factory=list)


class DAGOrchestrator:
    """
    Execute a directed acyclic graph of agents with maximum parallelism.

    Nodes with no unresolved dependencies run concurrently via asyncio.
    Output from upstream nodes is injected into downstream prompts via
    ``{upstream:<node_id>}`` template substitution.

    Usage::

        from agent_kit.orchestrator.dag import DAGOrchestrator, TaskNode

        dag = DAGOrchestrator([
            TaskNode("research",  researcher_agent, "Research: {input}"),
            TaskNode("financial", analyst_agent,   "Financials: {input}"),
            TaskNode("synthesis", writer_agent,
                     "Combine:\\n{upstream:research}\\n{upstream:financial}",
                     depends_on=["research", "financial"]),
        ])
        result = await dag.execute("AI market trends 2026")
        print(result.final_output)
        print(f"Total cost: ${result.total_cost_usd:.4f}")
    """

    def __init__(
        self,
        nodes: list[TaskNode],
        max_parallel: int = 8,
    ) -> None:
        self._nodes: dict[str, TaskNode] = {n.node_id: n for n in nodes}
        self._max_parallel = max_parallel
        self.validate()

    def validate(self) -> list[str]:
        """
        Validate the DAG: check for missing dependencies and cycles.
        Returns list of error strings (empty = valid). Raises on failure.
        """
        errors: list[str] = []

        # Check all declared dependencies exist
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    errors.append(f"Node '{node.node_id}' depends on '{dep}' which is not in the DAG.")
                    raise DAGMissingDependencyError(node.node_id, dep)

        # Kahn's algorithm — detect cycles
        in_degree = {nid: 0 for nid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                in_degree[node.node_id] += 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        processed = 0
        while queue:
            nid = queue.pop()
            processed += 1
            # Find all nodes that depend on nid
            for other in self._nodes.values():
                if nid in other.depends_on:
                    in_degree[other.node_id] -= 1
                    if in_degree[other.node_id] == 0:
                        queue.append(other.node_id)

        if processed != len(self._nodes):
            # Some nodes were never processed — cycle exists
            cycle_nodes = [nid for nid, deg in in_degree.items() if deg > 0]
            raise DAGCycleError(cycle_nodes)

        return errors

    def _topological_order(self) -> list[str]:
        """Return a valid topological ordering of node IDs."""
        in_degree = {nid: 0 for nid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                in_degree[node.node_id] += 1

        queue = sorted([nid for nid, deg in in_degree.items() if deg == 0])
        order: list[str] = []

        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for other in sorted(self._nodes.keys()):
                if nid in self._nodes[other].depends_on:
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        return order

    def _build_prompt(
        self,
        node: TaskNode,
        initial_input: str,
        completed: dict[str, AgentResult],
    ) -> str:
        prompt = node.prompt_template

        # Replace {input} with the original DAG input
        prompt = prompt.replace("{input}", initial_input)

        # Replace {upstream:<node_id>} with that node's output
        for dep_id in node.depends_on:
            if dep_id in completed:
                prompt = prompt.replace(
                    f"{{upstream:{dep_id}}}", completed[dep_id].output
                )

        return prompt

    async def execute(
        self,
        initial_input: str,
        **context: Any,
    ) -> DAGResult:
        """Execute the DAG with maximum parallelism and return aggregated results."""
        t0 = time.monotonic()
        completed: dict[str, AgentResult] = {}
        execution_order: list[str] = []
        semaphore = asyncio.Semaphore(self._max_parallel)

        # Track remaining in-degree for each node
        in_degree = {nid: len(node.depends_on) for nid, node in self._nodes.items()}
        # Event per node — set when that node completes
        events: dict[str, asyncio.Event] = {nid: asyncio.Event() for nid in self._nodes}

        async def run_node(nid: str) -> None:
            node = self._nodes[nid]
            # Wait for all dependencies to complete
            for dep_id in node.depends_on:
                await events[dep_id].wait()

            async with semaphore:
                prompt = self._build_prompt(node, initial_input, completed)
                result = await node.agent.run(prompt, **context)

            completed[nid] = result
            execution_order.append(nid)
            events[nid].set()

        # Launch all nodes concurrently — each awaits its own dependencies
        await asyncio.gather(*[run_node(nid) for nid in self._nodes])

        total_cost = sum(r.total_cost_usd for r in completed.values())
        total_tokens = sum(r.total_tokens for r in completed.values())
        total_ms = int((time.monotonic() - t0) * 1000)

        # Final output = output of the last node in topological order
        topo = self._topological_order()
        final_node = topo[-1] if topo else ""
        final_output = completed[final_node].output if final_node in completed else ""

        return DAGResult(
            node_results=completed,
            final_output=final_output,
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            total_duration_ms=total_ms,
            execution_order=execution_order,
        )

    def __len__(self) -> int:
        return len(self._nodes)

    def __repr__(self) -> str:
        return f"DAGOrchestrator(nodes={list(self._nodes.keys())})"
