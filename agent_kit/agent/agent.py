"""Agent — the primary user-facing primitive in agent-kit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_kit.agent.loop import AgentLoop
from agent_kit.audit.chain import AuditChain
from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.observability.tracer import AgentTracer
from agent_kit.providers.base import BaseProvider
from agent_kit.tools.base import Tool
from agent_kit.tools.registry import ToolRegistry
from agent_kit.types import (
    AgentResult,
    CircuitBreakerConfig,
    RetryPolicyConfig,
)

if TYPE_CHECKING:
    from agent_kit.cloud.reporter import CloudReporter
    from agent_kit.hooks import Approver, Hooks


class AgentConfig:
    """
    Configuration for an Agent.

    All fields have production-safe defaults — you don't need to configure
    anything to get a working agent with retry, circuit breaking, and auditing.
    """

    def __init__(
        self,
        model: str | None = None,
        system_prompt: str = "",
        max_turns: int = 20,
        max_tokens_per_turn: int = 4096,
        allowed_tools: list[str] | None = None,
        retry_policy: RetryPolicyConfig | None = None,
        circuit_breaker: CircuitBreakerConfig | None = None,
        audit_enabled: bool = True,
        tracer: AgentTracer | None = None,
        memory_window: int = 50,
        cloud: CloudReporter | None = None,
        max_run_cost_usd: float | None = None,
        enforce_budgets: bool = False,
        hooks: Hooks | None = None,
        approver: Approver | None = None,
        approval_timeout_s: float = 300.0,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_tokens_per_turn = max_tokens_per_turn
        self.allowed_tools = allowed_tools
        self.retry_policy = retry_policy or RetryPolicyConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreakerConfig()
        self.audit_enabled = audit_enabled
        self.tracer = tracer
        self.memory_window = memory_window
        self.cloud = cloud
        self.max_run_cost_usd = max_run_cost_usd  # per-run hard cap, enforced before each model call
        self.enforce_budgets = enforce_budgets  # fleet budgets from agent-kit Cloud (requires cloud)
        self.hooks = hooks  # before_tool / after_tool / before_llm policy hooks
        self.approver = approver  # awaited when a before_tool hook asks for approval
        self.approval_timeout_s = approval_timeout_s  # no answer in time → deny


class Agent:
    """
    The primary agent primitive in agent-kit.

    An Agent wraps a provider, a set of tools, memory, a tracer, and an audit
    chain, then drives the AgentLoop on each call to run().

    Every Agent instance has its own memory — to share memory across runs,
    pass the same InMemoryStore instance to multiple agents.

    Usage::

        from agent_kit import Agent
        from agent_kit.providers import AnthropicProvider

        # Minimal — sane defaults for everything
        agent = Agent(AnthropicProvider())
        result = await agent.run("Explain the Monty Hall problem.")
        print(result.output)
        print(f"Cost: ${result.total_cost_usd:.4f}")

        # With tools
        agent = Agent(AnthropicProvider(), tools=[my_tool])

        # Full config
        agent = Agent(
            AnthropicProvider(),
            config=AgentConfig(
                system_prompt="You are a helpful assistant.",
                max_turns=10,
                retry_policy=RetryPolicyConfig(max_attempts=3),
                audit_enabled=True,
            ),
        )
    """

    def __init__(
        self,
        provider: BaseProvider,
        tools: list[Tool] | None = None,
        config: AgentConfig | None = None,
        memory: InMemoryStore | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or AgentConfig()
        if self._config.enforce_budgets and self._config.cloud is None:
            raise ValueError("enforce_budgets=True requires AgentConfig(cloud=CloudReporter(...))")
        self._memory = memory or InMemoryStore(window=self._config.memory_window)
        self._registry = ToolRegistry(
            tools=tools or [],
            allowed_tools=self._config.allowed_tools,
        )
        self._tracer = self._config.tracer or AgentTracer()
        self._audit: AuditChain | None = AuditChain() if self._config.audit_enabled else None
        self.last_result: AgentResult | None = None

    def add_tool(self, t: Tool) -> "Agent":
        """Register a tool and return self for fluent chaining."""
        self._registry.register(t)
        return self

    async def run(self, prompt: str, **context: Any) -> AgentResult:
        """
        Run the agent on a prompt and return the final result.

        Context kwargs are available for future middleware hooks but do not
        affect the core loop in v0.1.

        Raises:
            MaxTurnsExceededError: if the agent runs out of turns
            CircuitOpenError: if the provider circuit breaker is OPEN
            ProviderError: if the LLM call fails and retries are exhausted
        """
        self.last_result = await self._make_loop().run(prompt, **context)
        return self.last_result

    async def stream(self, prompt: str, **context: Any) -> AsyncIterator[str]:
        """
        Stream the agent's response as text chunks.

        Runs the same loop as run(): tools execute between turns, and retry,
        circuit breaking, audit, and cloud reporting all apply. Retry covers
        opening each provider stream; a failure mid-stream propagates. The
        completed AgentResult is available as ``agent.last_result`` once the
        iterator is exhausted.
        """
        loop = self._make_loop()
        async for chunk in loop.stream(prompt, **context):
            yield chunk
        self.last_result = loop.result

    def _make_loop(self) -> AgentLoop:
        return AgentLoop(
            provider=self._provider,
            registry=self._registry,
            memory=self._memory,
            tracer=self._tracer,
            audit=self._audit,
            model=self._config.model,
            system_prompt=self._config.system_prompt,
            max_turns=self._config.max_turns,
            max_tokens_per_turn=self._config.max_tokens_per_turn,
            retry_policy=self._config.retry_policy,
            circuit_breaker_config=self._config.circuit_breaker,
            reporter=self._config.cloud,
            max_run_cost_usd=self._config.max_run_cost_usd,
            budget_guard=(
                self._config.cloud.budget_guard()
                if self._config.enforce_budgets and self._config.cloud is not None
                else None
            ),
            hooks=self._config.hooks,
            approver=self._config.approver,
            approval_timeout_s=self._config.approval_timeout_s,
        )

    @property
    def audit(self) -> AuditChain | None:
        """Access the audit chain for this agent."""
        return self._audit

    @property
    def tracer(self) -> AgentTracer:
        return self._tracer

    @property
    def memory(self) -> InMemoryStore:
        return self._memory

    def __repr__(self) -> str:
        tools = list(self._registry._tools.keys())
        return (
            f"Agent(provider={self._provider.name()!r}, "
            f"tools={tools}, "
            f"model={self._config.model or self._provider.config.default_model!r})"
        )
