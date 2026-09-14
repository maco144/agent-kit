"""
Server-side Merkle chain verification and extension.

Replicates the algorithm in agent_kit.audit.chain — intentionally copied
rather than imported so the server has no dependency on the client library.
SDK runs arrive with their chain built client-side and are only verified here;
OTLP runs have their chain built here with append_event.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.models import AuditEvent, AuditRun

GENESIS_ROOT = "0" * 64
_GENESIS_ROOT = GENESIS_ROOT


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def payload_hash(payload: dict[str, Any]) -> str:
    """Hash an audit payload exactly as the SDK does."""
    return _sha256(json.dumps(payload, sort_keys=True, default=str))


def _expected_leaf(prev_root: str, event_type: str, payload_hash: str, ts: datetime) -> str:
    raw = prev_root + event_type + payload_hash + ts.isoformat()
    return _sha256(raw)


def verify_chain(events: list[AuditEvent]) -> tuple[bool, int | None, str | None, str | None]:
    """
    Re-derive all hashes in sequence order.

    Returns:
        (True, None, None, None)                             — chain intact
        (False, broken_seq, expected_hash, stored_hash)      — chain broken
    """
    root = _GENESIS_ROOT
    for event in sorted(events, key=lambda e: e.seq):
        expected = _expected_leaf(root, event.event_type, event.payload_hash, event.timestamp)
        if expected != event.leaf_hash:
            return False, event.seq, expected, event.leaf_hash
        if event.prev_root != root:
            return False, event.seq, root, event.prev_root
        root = event.leaf_hash
    return True, None, None, None


def append_event(
    run: AuditRun,
    *,
    event_id: str,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    timestamp: datetime,
) -> AuditEvent:
    """
    Extend a server-built chain by one event and advance the run's root.

    Returns the new AuditEvent; the caller adds it to the session.
    """
    prev_root = run.final_root_hash or GENESIS_ROOT
    p_hash = payload_hash(payload)
    leaf = _expected_leaf(prev_root, event_type, p_hash, timestamp)
    event = AuditEvent(
        run_id=run.run_id,
        org_id=run.org_id,
        event_id=event_id,
        event_type=event_type,
        actor=actor[:255],
        payload_hash=p_hash,
        prev_root=prev_root,
        leaf_hash=leaf,
        seq=run.event_count or 0,
        timestamp=timestamp,
        verified=False,
    )
    run.final_root_hash = leaf
    run.event_count = (run.event_count or 0) + 1
    return event
