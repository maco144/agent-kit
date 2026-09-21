"""Agent — the primary user-facing primitive in agent-kit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, TypeVar, overload

from agent_kit.agent.delegation import AgentTool
from agent_kit.agent.loop import AgentLoop
from agent_kit.audit.chain import AuditChain
from agent_kit.durable import CHECKPOINT_SCHEMA_VERSION, RunCheckpoint, RunStore
from agent_kit.exceptions import AuditVerificationError, CheckpointError, RunNotFoundError
from agent_kit.hooks import SUSPEND, Suspend
from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.observability.tracer import AgentTracer
from agent_kit.output import OutputSpec
from agent_kit.providers.base import BaseProvider
from agent_kit.tools.base import Tool
from agent_kit.tools.registry import ToolRegistry
from agent_kit.types import (
    AgentResult,
    CircuitBreakerConfig,
    ClearToolResults,
    Compaction,
    RequestOptions,
    RetryPolicyConfig,
)

T = TypeVar("T")

_MAX_CLOUD_RUN_ID = 36  # agent-kit Cloud stores run ids as String(36) and addresses them in URL paths

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
        memory_window: int | None = None,
        cloud: CloudReporter | None = None,
        max_run_cost_usd: float | None = None,
        enforce_budgets: bool = False,
        hooks: Hooks | None = None,
        approver: Approver | Suspend | None = None,
        approval_timeout_s: float = 300.0,
        output_retries: int = 2,
        thinking: Literal["adaptive", "disabled"] | None = None,
        effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
        prompt_caching: bool = True,
        compaction: Compaction | None = None,
        clear_tool_results: ClearToolResults | None = None,
        provider_options: dict[str, Any] | None = None,
        context_budget_tokens: int | None = 150_000,
        run_store: RunStore | None = None,
        max_delegation_depth: int = 5,
        llm_timeout_s: float | None = 600.0,
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
        self.memory_window = memory_window  # message cap applied on append; None = token budget only
        self.cloud = cloud
        self.max_run_cost_usd = max_run_cost_usd  # per-run hard cap, enforced before each model call
        self.enforce_budgets = enforce_budgets  # fleet budgets from agent-kit Cloud (requires cloud)
        self.hooks = hooks  # before_tool / after_tool / before_llm policy hooks
        self.approver = approver  # awaited when a before_tool hook asks; SUSPEND parks the run instead
        self.approval_timeout_s = approval_timeout_s  # no answer in time → deny
        self.output_retries = output_retries  # repair turns after an invalid typed answer
        self.thinking = thinking  # Anthropic thinking type
        self.effort = effort  # Anthropic output_config.effort / OpenAI reasoning_effort
        self.prompt_caching = prompt_caching  # Anthropic cache breakpoints (system + conversation)
        self.compaction = compaction  # Anthropic server-side compaction; disables client trimming
        self.clear_tool_results = clear_tool_results  # Anthropic server-side tool-result clearing
        self.provider_options = provider_options or {}  # merged into every provider request
        self.context_budget_tokens = context_budget_tokens  # over budget → cut history once to half
        self.run_store = run_store  # checkpoints: resume after crashes, suspend for approvals
        self.max_delegation_depth = max_delegation_depth  # nested agent-tool levels allowed below a top-level run
        self.llm_timeout_s = llm_timeout_s  # wall-clock cap per model call, streams included; then ProviderError


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
        if self._config.approver is SUSPEND and self._config.run_store is None:
            raise ValueError("approver=SUSPEND requires AgentConfig(run_store=...)")
        self._memory = memory or InMemoryStore(window=self._config.memory_window)
        self._registry = ToolRegistry(
            tools=tools or [],
            allowed_tools=self._config.allowed_tools,
        )
        self._tracer = self._config.tracer or AgentTracer()
        self._audit: AuditChain | None = AuditChain() if self._config.audit_enabled else None
        self.last_result: AgentResult[Any] | None = None

    def add_tool(self, t: Tool) -> "Agent":
        """Register a tool and return self for fluent chaining."""
        self._registry.register(t)
        return self

    @property
    def config(self) -> AgentConfig:
        return self._config

    def as_tool(self, name: str, description: str, *, output_type: Any = None) -> AgentTool:
        """
        Expose this agent as a tool another agent can delegate to.

        Each call inside an agent loop is a fresh child run with its own memory and audit chain. The child
        keeps its provider, tools, and hooks; the calling run adds its hooks (run after the child's), its
        approver and run store, and its remaining cost cap. The model passes ``task``; the tool returns the
        child's answer, or its ``parsed`` value as JSON when ``output_type`` is set.
        """
        return AgentTool(self, name, description, output_type)

    def _check_run_id(self, run_id: str | None) -> None:
        if run_id is not None and self._config.cloud is not None and len(run_id) > _MAX_CLOUD_RUN_ID:
            raise ValueError("run_id must be at most 36 characters when reporting to agent-kit Cloud")

    @overload
    async def run(
        self, prompt: str, *, output_type: type[T], run_id: str | None = None, **context: Any
    ) -> AgentResult[T]: ...

    @overload
    async def run(
        self, prompt: str, *, output_type: None = None, run_id: str | None = None, **context: Any
    ) -> AgentResult[Any]: ...

    async def run(
        self, prompt: str, *, output_type: Any = None, run_id: str | None = None, **context: Any
    ) -> AgentResult[Any]:
        """
        Run the agent on a prompt and return the final result.

        With ``output_type`` (any type Pydantic can validate), the final answer is constrained to its
        JSON Schema — natively when the provider supports structured outputs, otherwise via the system
        prompt — and validated into ``result.parsed``. Invalid answers are sent back to the model up to
        ``AgentConfig.output_retries`` times. Context kwargs reach hooks as ``ctx.context``.

        Raises:
            MaxTurnsExceededError: if the agent runs out of turns
            CircuitOpenError: if the provider circuit breaker is OPEN
            ProviderError: if the LLM call fails and retries are exhausted
            OutputValidationError: if a typed answer never validates
        """
        self._check_run_id(run_id)
        self.last_result = await self._make_loop().run(prompt, output_type=output_type, run_id=run_id, **context)
        return self.last_result

    async def stream(
        self, prompt: str, *, output_type: Any = None, run_id: str | None = None, **context: Any
    ) -> AsyncIterator[str]:
        """
        Stream the agent's response as text chunks.

        Runs the same loop as run(): tools execute between turns, and retry,
        circuit breaking, audit, and cloud reporting all apply. Retry covers
        opening each provider stream; a failure mid-stream propagates. The
        completed AgentResult is available as ``agent.last_result`` once the
        iterator is exhausted.

        With ``output_type``, chunks are the raw JSON of each answer (including invalid attempts before
        a repair); ``agent.last_result.parsed`` holds the validated value.
        """
        self._check_run_id(run_id)
        loop = self._make_loop()
        async for chunk in loop.stream(prompt, output_type=output_type, run_id=run_id, **context):
            yield chunk
        self.last_result = loop.result

    @overload
    async def resume(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: type[T]
    ) -> AgentResult[T]: ...

    @overload
    async def resume(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: None = None
    ) -> AgentResult[Any]: ...

    async def resume(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: Any = None
    ) -> AgentResult[Any]:
        """
        Continue a checkpointed run — after a crash, a failure, or a suspension for approval.

        ``approvals`` answers pending approvals by call id; unanswered ones keep the run suspended. Build
        the Agent with the same tools and hooks as the original: memory and the audit chain are restored
        from the checkpoint. Pass the run's ``output_type`` again for typed runs.

        Raises:
            RunNotFoundError: no checkpoint for ``run_id``
            RunConflictError: another worker resumed or advanced the run first
            CheckpointError: the checkpoint cannot be resumed (schema, output type, audit chain)
        """
        checkpoint = await self._load_checkpoint(run_id)
        if checkpoint.status == "completed":
            self.last_result = self._stored_result(checkpoint, output_type)
            return self.last_result
        self._restore(checkpoint, output_type)
        self.last_result = await self._make_loop().resume(checkpoint, approvals or {}, output_type)
        return self.last_result

    async def resume_stream(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: Any = None
    ) -> AsyncIterator[str]:
        """Streaming resume(); ``agent.last_result`` is set when the iterator is exhausted."""
        checkpoint = await self._load_checkpoint(run_id)
        if checkpoint.status == "completed":
            self.last_result = self._stored_result(checkpoint, output_type)
            return
        self._restore(checkpoint, output_type)
        loop = self._make_loop()
        async for chunk in loop.resume_stream(checkpoint, approvals or {}, output_type):
            yield chunk
        self.last_result = loop.result

    async def _load_checkpoint(self, run_id: str) -> RunCheckpoint:
        if self._config.run_store is None:
            raise ValueError("resume() requires AgentConfig(run_store=...)")
        checkpoint = await self._config.run_store.load(run_id)
        if checkpoint is None:
            raise RunNotFoundError(run_id)
        self._check_schema(checkpoint)
        return checkpoint

    @staticmethod
    def _check_schema(checkpoint: RunCheckpoint) -> None:
        if checkpoint.schema_version > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                checkpoint.run_id,
                f"schema version {checkpoint.schema_version} is newer than {CHECKPOINT_SCHEMA_VERSION}",
            )

    @staticmethod
    def _stored_result(checkpoint: RunCheckpoint, output_type: Any) -> AgentResult[Any]:
        result: AgentResult[Any] = AgentResult.model_validate(checkpoint.result or {"output": ""})
        if output_type is not None and result.parsed is not None:
            result.parsed = OutputSpec.from_type(output_type).adapter.validate_python(result.parsed)
        return result

    @staticmethod
    def _check_output_type(checkpoint: RunCheckpoint, output_type: Any) -> None:
        name = OutputSpec.from_type(output_type).name if output_type is not None else None
        if name != checkpoint.output_type_name:
            raise CheckpointError(
                checkpoint.run_id,
                f"output_type {name!r} does not match the run's {checkpoint.output_type_name!r}",
            )

    @staticmethod
    def _restored_audit(checkpoint: RunCheckpoint) -> AuditChain:
        try:
            return AuditChain.restore(checkpoint.audit_events)
        except AuditVerificationError as exc:
            raise CheckpointError(checkpoint.run_id, f"audit chain failed verification: {exc}") from exc

    def _restore(self, checkpoint: RunCheckpoint, output_type: Any) -> None:
        self._check_output_type(checkpoint, output_type)
        if self._audit is not None:
            self._audit = self._restored_audit(checkpoint)
        self._memory.clear()
        self._memory.add_many(checkpoint.messages)

    async def _open_delegated(
        self, run_id: str, output_type: Any, **overrides: Any
    ) -> tuple[AgentLoop, RunCheckpoint | None]:
        """A loop for one delegated run over fresh memory and audit, restored from its checkpoint if unfinished."""
        memory = InMemoryStore(window=self._config.memory_window)
        audit = AuditChain() if self._config.audit_enabled else None
        store: RunStore | None = overrides.get("run_store", self._config.run_store)
        checkpoint = await store.load(run_id) if store is not None else None
        if checkpoint is not None:
            self._check_schema(checkpoint)
            if checkpoint.status != "completed":
                self._check_output_type(checkpoint, output_type)
                if audit is not None:
                    audit = self._restored_audit(checkpoint)
                memory.add_many(checkpoint.messages)
        return self._make_loop(memory=memory, audit=audit, **overrides), checkpoint

    def _make_loop(self, **overrides: Any) -> AgentLoop:
        kwargs: dict[str, Any] = dict(
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
            output_retries=self._config.output_retries,
            llm_timeout_s=self._config.llm_timeout_s,
            request_options=RequestOptions(
                thinking=self._config.thinking,
                effort=self._config.effort,
                prompt_caching=self._config.prompt_caching,
                compaction=self._config.compaction,
                clear_tool_results=self._config.clear_tool_results,
                provider_options=dict(self._config.provider_options),
            ),
            context_budget_tokens=self._config.context_budget_tokens,
            run_store=self._config.run_store,
            max_delegation_depth=self._config.max_delegation_depth,
        )
        kwargs.update(overrides)
        return AgentLoop(**kwargs)

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
