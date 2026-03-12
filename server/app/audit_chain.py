"""
Server-side Merkle chain verification.

Replicates the algorithm in agent_kit.audit.chain — intentionally copied
rather than imported so the server has no dependency on the client library.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.models import AuditEvent

_GENESIS_ROOT = "0" * 64


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


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
