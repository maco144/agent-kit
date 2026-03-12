"""
Shared Pydantic models for agent-kit.

RULE: Nothing in this file imports from any other agent_kit module.
      Every other module imports upward from here.
      This eliminates circular import problems.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """A single message in a conversation."""

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    tool_call_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class ToolCall(BaseModel):
    """A tool invocation requested by the LLM."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class ToolResult(BaseModel):
    """The result of executing a tool."""

    call_id: str
    tool_name: str
    output: Any
    error: str | None = None
    duration_ms: int = 0
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Turns and cost tracking
# ---------------------------------------------------------------------------


class CostSummary(BaseModel):
    """Token and USD cost for a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


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


# ---------------------------------------------------------------------------
# Agent-level results
# ---------------------------------------------------------------------------


class AgentResult(BaseModel):
    """Final result returned by Agent.run()."""

    output: str
    turns: list[Turn] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    audit_root_hash: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


from enum import Enum


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
# Pipeline / DAG
# ---------------------------------------------------------------------------


class PipelineResult(BaseModel):
    """Result from LinearPipeline.run()."""

    stage_results: list[AgentResult] = Field(default_factory=list)
    final_output: str = ""
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_ms: int = 0
