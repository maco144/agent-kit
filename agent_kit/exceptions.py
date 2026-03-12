"""All agent-kit exceptions in one place."""


class AgentKitError(Exception):
    """Base exception for all agent-kit errors."""


class ProviderError(AgentKitError):
    """LLM provider returned an error or is unreachable."""


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
