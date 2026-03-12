"""Wire-format Pydantic models for the agent-kit Cloud ingest API."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    RUN_START = "run_start"
    TURN_COMPLETE = "turn_complete"
    RUN_COMPLETE = "run_complete"
    RUN_ERROR = "run_error"
    CIRCUIT_STATE_CHANGE = "circuit_state_change"
    AUDIT_FLUSH = "audit_flush"


class CloudEvent(BaseModel):
    """A single lifecycle event shipped from the SDK to the ingest API."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType
    run_id: str
    agent_name: str
    project: str
    occurred_at: datetime = Field(default_factory=datetime.utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
