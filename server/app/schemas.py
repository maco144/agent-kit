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
