"""Checkpoint models for durable runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_kit.types import AuditEventRecord, Message, PendingApproval, ToolResult, Turn

RunStatus = Literal["running", "suspended", "completed", "failed"]
CHECKPOINT_SCHEMA_VERSION = 1


class PendingTurn(BaseModel):
    """A model turn whose tool calls are not all resolved yet."""

    turn: Turn
    results: dict[str, ToolResult] = Field(default_factory=dict)  # call_id → final (post-hook) result
    started: list[str] = Field(default_factory=list)  # call_ids whose tool execution began
    approvals: list[PendingApproval] = Field(default_factory=list)  # calls awaiting a decision


class RunCheckpoint(BaseModel):
    """Everything needed to continue a run in another process."""

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    version: int
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    prompt: str
    context: dict[str, Any]
    output_type_name: str | None
    messages: list[Message]
    turns: list[Turn]
    turn_count: int
    run_cost_usd: float
    invalid_answers: int
    native_output: bool
    last_prompt_tokens: int
    last_prompt_chars: int
    audit_events: list[AuditEventRecord]
    pending: PendingTurn | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: RunStatus
    updated_at: datetime
    pending_approvals: int
