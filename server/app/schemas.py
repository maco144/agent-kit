"""Pydantic v2 request/response schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class IngestResponse(BaseModel):
    accepted: int
    message: str = "ok"


# ---------------------------------------------------------------------------
# Audit runs
# ---------------------------------------------------------------------------


class AuditEventSchema(BaseModel):
    seq: int
    event_id: str
    event_type: str
    actor: str
    payload_hash: str
    prev_root: str
    leaf_hash: str
    timestamp: datetime
    verified: bool

    model_config = {"from_attributes": True}


class AuditRunSummary(BaseModel):
    run_id: str
    agent_name: str
    project: str
    event_count: int
    started_at: datetime | None
    completed_at: datetime | None
    integrity: str
    final_root_hash: str

    model_config = {"from_attributes": True}


class AuditRunDetail(AuditRunSummary):
    events: list[AuditEventSchema] = Field(default_factory=list)


class AuditRunList(BaseModel):
    runs: list[AuditRunSummary]
    next_cursor: str | None
    total: int


class AuditEventList(BaseModel):
    events: list[AuditEventSchema]
    next_cursor: str | None
    total: int


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class VerifySuccess(BaseModel):
    run_id: str
    verified: bool = True
    event_count: int
    final_root_hash: str
    verified_at: datetime


class VerifyFailure(BaseModel):
    run_id: str
    verified: bool = False
    broken_at_seq: int
    broken_at_event_id: str
    expected_leaf_hash: str
    stored_leaf_hash: str
    verified_at: datetime


# ---------------------------------------------------------------------------
# Fleet Dashboard — metrics
# ---------------------------------------------------------------------------


class MetricsWindow(BaseModel):
    from_: datetime = Field(alias="from")
    to: datetime

    model_config = {"populate_by_name": True}


class MetricsSummary(BaseModel):
    window: dict[str, str]
    total_runs: int
    runs_success: int
    runs_error: int
    error_rate_pct: float
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    active_runs: int
    agents_count: int
    projects: list[str]


class CostDataPoint(BaseModel):
    bucket: datetime
    cost_usd: float
    input_tokens: int
    output_tokens: int


class CostSeries(BaseModel):
    label: str
    project: str
    data: list[CostDataPoint]
    total_cost_usd: float


class CostResponse(BaseModel):
    group_by: str
    resolution: str
    series: list[CostSeries]


class RunsDataPoint(BaseModel):
    bucket: datetime
    runs_total: int
    runs_success: int
    runs_error: int
    avg_turns: float
    avg_duration_ms: int


class RunsSeries(BaseModel):
    label: str
    data: list[RunsDataPoint]


class RunsResponse(BaseModel):
    resolution: str
    series: list[RunsSeries]


class AgentSummary(BaseModel):
    agent_name: str
    project: str
    models_used: list[str]
    runs_total: int
    error_rate_pct: float
    total_cost_usd: float
    avg_cost_per_run_usd: float
    avg_turns: float
    circuit_breaker_state: str
    last_seen: datetime | None


class AgentsResponse(BaseModel):
    agents: list[AgentSummary]


class CBEventDetail(BaseModel):
    prev_state: str
    new_state: str
    failure_count: int
    occurred_at: datetime
    duration_open_ms: int | None = None


class CBAgentDetail(BaseModel):
    agent_name: str
    resource: str
    current_state: str
    events: list[CBEventDetail]


class CircuitBreakerResponse(BaseModel):
    agents: list[CBAgentDetail]


class ActiveRunDetail(BaseModel):
    run_id: str
    agent_name: str
    project: str
    model: str
    started_at: datetime
    elapsed_ms: int
    turns_so_far: int
    cost_so_far_usd: float
    tokens_so_far: int

    model_config = {"from_attributes": True}


class ActiveRunsResponse(BaseModel):
    active_runs: list[ActiveRunDetail]
    count: int


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


class AlertChannelSchema(BaseModel):
    id: str
    name: str
    type: str
    config: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertRuleSchema(BaseModel):
    id: str
    name: str
    type: str
    config: dict[str, Any]
    enabled: bool
    channel_ids: list[str]
    muted_until: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AlertFiringSchema(BaseModel):
    id: str
    rule_id: str
    state: str
    fired_at: datetime
    resolved_at: datetime | None
    acked_at: datetime | None
    acked_by: str | None
    context: dict[str, Any]
    notifications_sent: int

    model_config = {"from_attributes": True}


class CreateChannelRequest(BaseModel):
    name: str
    type: str  # email | slack | pagerduty | webhook
    config: dict[str, Any] = Field(default_factory=dict)


class CreateChannelResponse(BaseModel):
    channel: AlertChannelSchema
    test_sent: bool


class CreateRuleRequest(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    channel_ids: list[str] = Field(default_factory=list)
    enabled: bool = True


class UpdateRuleRequest(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    channel_ids: list[str] | None = None
    muted_until: datetime | None = None


class AckFiringRequest(BaseModel):
    comment: str | None = None
