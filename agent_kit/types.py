"""
Shared Pydantic models for agent-kit.

RULE: Nothing in this file imports from any other agent_kit module.
      Every other module imports upward from here.
      This eliminates circular import problems.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """A tool invocation requested by the LLM."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class Message(BaseModel):
    """A single message in a conversation."""

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)  # assistant turns only
    metadata: dict[str, Any] = Field(default_factory=dict)
    native_content: list[dict[str, Any]] | None = None  # assistant blocks exactly as the provider returned them
    native_provider: str | None = None  # provider.name() that produced native_content


# ---------------------------------------------------------------------------
# Tool schemas and results
# ---------------------------------------------------------------------------


class ToolSchema(BaseModel):
    """JSON Schema description of a tool, sent to the LLM."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object
    cost_estimate: float = 0.0  # advisory USD cost per call
    idempotent: bool = False  # safe to retry without side effects


class ToolResult(BaseModel):
    """The result of executing a tool."""

    call_id: str
    tool_name: str
    output: Any
    error: str | None = None
    duration_ms: int = 0
    idempotency_key: str | None = None
    cost_usd: float = 0.0  # spend incurred inside the tool (delegated agent runs)
    tokens: int = 0


# ---------------------------------------------------------------------------
# Turns and cost tracking
# ---------------------------------------------------------------------------


class CostSummary(BaseModel):
    """Token and USD cost for a single LLM call."""

    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    priced: bool = True  # False when the model has no price: cost_usd is 0.0 because it is unknown


class Turn(BaseModel):
    """One round-trip: user/tool messages in → assistant message out."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    messages_in: list[Message] = Field(default_factory=list)
    message_out: Message | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    cost: CostSummary = Field(default_factory=CostSummary)
    duration_ms: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    context_events: list[dict[str, Any]] = Field(default_factory=list)  # server-side context management


# ---------------------------------------------------------------------------
# Agent-level results
# ---------------------------------------------------------------------------


T = TypeVar("T")


class PendingApproval(BaseModel):
    """A tool call waiting for a human decision in a suspended run."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None
    turn: int
    run_id: str | None = None  # run that owns the gated call; None for the run's own calls


class AgentResult(BaseModel, Generic[T]):
    """Final result returned by Agent.run(). ``parsed`` holds the validated output_type value."""

    output: str
    parsed: T | None = None
    turns: list[Turn] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    audit_root_hash: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    status: Literal["completed", "suspended"] = "completed"
    pending_approvals: list[PendingApproval] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reliability configs (value objects — no logic here)
# ---------------------------------------------------------------------------


class BackoffConfig(BaseModel):
    """Exponential backoff parameters."""

    initial_delay_s: float = 1.0
    multiplier: float = 2.0
    max_delay_s: float = 60.0
    jitter: bool = True


class RetryPolicyConfig(BaseModel):
    """Configuration for the retry policy."""

    max_attempts: int = 3
    backoff: BackoffConfig = Field(default_factory=BackoffConfig)
    # Exception type names (strings) to retry on; checked via isinstance at runtime.
    # Default covers transient network errors from httpx.
    retryable_on: list[str] = Field(
        default_factory=lambda: ["httpx.TimeoutException", "httpx.ConnectError", "ProviderError"]
    )


class CircuitBreakerConfig(BaseModel):
    """Configuration for the circuit breaker."""

    failure_threshold: int = 5  # consecutive failures to open
    recovery_timeout_s: float = 60.0
    success_threshold: int = 2  # successes in half-open before closing


class Compaction(BaseModel):
    """Anthropic server-side compaction: summarise earlier context past a token threshold."""

    trigger_tokens: int = 150_000  # API minimum 50_000
    instructions: str | None = None  # replaces the default summarisation prompt


class ClearToolResults(BaseModel):
    """Anthropic context editing: clear old tool results past a token threshold."""

    trigger_tokens: int = 100_000
    keep: int = 3  # most recent tool uses kept
    exclude_tools: list[str] = Field(default_factory=list)
    clear_inputs: bool = False  # also clear tool_use inputs


class RequestOptions(BaseModel):
    """Per-request model settings AgentLoop passes to providers that support them."""

    thinking: Literal["adaptive", "disabled"] | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt_caching: bool = True
    compaction: Compaction | None = None
    clear_tool_results: ClearToolResults | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)

    def is_default(self) -> bool:
        return self == RequestOptions()

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class SpanKind(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    LLM = "llm"
    DAG = "dag"
    RETRIEVAL = "retrieval"


class SpanEvent(BaseModel):
    """A structured event recorded within a trace span."""

    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEventRecord(BaseModel, frozen=True):
    """An immutable, hash-linked audit record."""

    event_id: str
    event_type: str
    actor: str
    payload_hash: str
    prev_root: str
    leaf_hash: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Tool output scanning
# ---------------------------------------------------------------------------

Severity = Literal["low", "medium", "high", "critical"]
SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class Finding(BaseModel, frozen=True):
    """One thing a scanner found in tool output. Never contains the output itself."""

    scanner: str  # e.g. "patterns", "nullcone"
    rule: str  # e.g. "unicode_tags", "ioc_domain"
    severity: Severity
    message: str  # fixed description of the rule, not the matched text
    location: str = "$"  # JSON path of the span, "$error" for the error text
    indicator: str | None = None  # matched IOC value (NullconeScanner only)


# ---------------------------------------------------------------------------
# Pipeline / DAG
# ---------------------------------------------------------------------------


class PipelineResult(BaseModel):
    """Result from LinearPipeline.run()."""

    stage_results: list[AgentResult[Any]] = Field(default_factory=list)
    final_output: str = ""
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_ms: int = 0
