"""All agent-kit exceptions in one place."""

from __future__ import annotations

from datetime import datetime


class AgentKitError(Exception):
    """Base exception for all agent-kit errors."""


class ProviderError(AgentKitError):
    """LLM provider returned an error or is unreachable."""


class ResponseTruncatedError(AgentKitError):
    """The model hit its output token limit: tool arguments or the answer are incomplete. Not retried."""

    def __init__(self, provider: str, max_tokens: int, had_tool_calls: bool) -> None:
        self.provider = provider
        self.max_tokens = max_tokens
        self.had_tool_calls = had_tool_calls
        cut = "a tool call" if had_tool_calls else "the answer"
        super().__init__(
            f"{provider} response cut off at max_tokens ({max_tokens}) mid {cut}; "
            "raise AgentConfig(max_tokens_per_turn=...)"
        )


class ToolNotFoundError(AgentKitError):
    """Agent tried to call a tool that isn't registered."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool '{tool_name}' is not registered on this agent.")
        self.tool_name = tool_name


class ToolNotAllowedError(AgentKitError):
    """Tool is registered but not in the agent's allowed_tools list."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool '{tool_name}' is not in this agent's allowed_tools list.")
        self.tool_name = tool_name


class ToolExecutionError(AgentKitError):
    """A tool raised an unexpected exception during execution."""

    def __init__(self, tool_name: str, cause: Exception) -> None:
        super().__init__(f"Tool '{tool_name}' raised: {cause}")
        self.tool_name = tool_name
        self.cause = cause


class CircuitOpenError(AgentKitError):
    """Circuit breaker is OPEN; request rejected without attempting the call."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"Circuit breaker OPEN for '{resource}'. Try again later.")
        self.resource = resource


class MaxTurnsExceededError(AgentKitError):
    """Agent hit max_turns without producing a final response."""

    def __init__(self, max_turns: int) -> None:
        super().__init__(f"Agent exceeded max_turns={max_turns} without completing.")
        self.max_turns = max_turns


class AuditVerificationError(AgentKitError):
    """Audit chain hash verification failed — chain may have been tampered with."""


class DAGCycleError(AgentKitError):
    """DAG contains a cycle; topological sort is impossible."""

    def __init__(self, involved: list[str]) -> None:
        super().__init__(f"Cycle detected in DAG involving nodes: {involved}")
        self.involved = involved


class DAGMissingDependencyError(AgentKitError):
    """A node depends on a node ID that doesn't exist in the DAG."""

    def __init__(self, node_id: str, missing_dep: str) -> None:
        super().__init__(f"Node '{node_id}' depends on '{missing_dep}' which is not in the DAG.")
        self.node_id = node_id
        self.missing_dep = missing_dep


class BudgetExceededError(AgentKitError):
    """A per-run cost cap or a fleet budget stopped the agent before its next model call."""

    def __init__(
        self,
        scope: str,
        limit_usd: float,
        spent_usd: float,
        budget_name: str | None = None,
        resets_at: datetime | None = None,
    ) -> None:
        if scope == "run":
            message = f"Run cost ${spent_usd:.4f} reached the per-run cap of ${limit_usd:.4f}."
        elif budget_name is None:
            message = "Budget status unavailable and fail_closed is set; refusing to call the model."
        else:
            message = f"Budget '{budget_name}' exhausted: ${spent_usd:.4f} of ${limit_usd:.4f}."
            if resets_at is not None:
                message += f" Resets at {resets_at.isoformat()} UTC."
        super().__init__(message)
        self.scope = scope
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.budget_name = budget_name
        self.resets_at = resets_at


class RunStoppedByHookError(AgentKitError):
    """A hook stopped the run (before_llm deny, or a tool deny with stop_run=True)."""

    def __init__(self, stage: str, reason: str, tool_name: str | None = None) -> None:
        where = f" on '{tool_name}'" if tool_name else ""
        super().__init__(f"Run stopped by {stage} hook{where}: {reason}")
        self.stage = stage
        self.reason = reason
        self.tool_name = tool_name


class MCPConnectionError(AgentKitError):
    """A required MCP server could not be connected, or its tools could not be registered."""


class MCPToolError(AgentKitError):
    """An MCP tool call failed; surfaced to the model as a tool error."""


class OutputValidationError(AgentKitError):
    """The final answer never validated against the run's output_type."""

    def __init__(self, errors: str, raw_output: str, attempts: int) -> None:
        super().__init__(f"Output failed validation after {attempts} attempt(s):\n{errors}")
        self.errors = errors
        self.raw_output = raw_output
        self.attempts = attempts


class RunNotFoundError(AgentKitError):
    """No checkpoint exists for the run id."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No checkpoint for run '{run_id}'.")
        self.run_id = run_id


class RunConflictError(AgentKitError):
    """A run checkpoint changed underneath this writer (another worker owns the run)."""

    def __init__(self, run_id: str, expected_version: int, actual_version: int | None) -> None:
        super().__init__(
            f"Run '{run_id}' changed concurrently "
            f"(expected version {expected_version}, found {actual_version})."
        )
        self.run_id = run_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class CheckpointError(AgentKitError):
    """A checkpoint exists but cannot be resumed."""

    def __init__(self, run_id: str, reason: str) -> None:
        super().__init__(f"Checkpoint for run '{run_id}' cannot be resumed: {reason}")
        self.run_id = run_id
        self.reason = reason


class ScannerUnavailableError(AgentKitError):
    """A scanner configured to fail closed could not complete its checks."""

    def __init__(self, scanner: str, reason: str) -> None:
        super().__init__(f"Scanner '{scanner}' unavailable: {reason}")
        self.scanner = scanner
        self.reason = reason
