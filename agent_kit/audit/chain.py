"""Hash-linked audit chain — tamper-evident record of all agent actions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from agent_kit.exceptions import AuditVerificationError
from agent_kit.types import AuditEventRecord

_GENESIS_ROOT = "0" * 64  # SHA256 of empty string equivalent


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _compute_leaf_hash(
    prev_root: str,
    event_type: str,
    payload_hash: str,
    timestamp: datetime,
) -> str:
    raw = prev_root + event_type + payload_hash + timestamp.isoformat()
    return _sha256(raw)


class AuditChain:
    """
    Append-only, hash-linked audit log.

    Every event's leaf_hash is computed from the previous root + event data,
    creating a chain where any tampering with a historical record invalidates
    all subsequent hashes.

    This is the same construction used in the AIOS Merkle audit chain
    (eudaimonia/kernel/audit/chain.py), extracted here as a standalone primitive.

    Usage::

        chain = AuditChain()
        chain.append("agent_start", actor="my_agent", payload={"model": "claude"})
        chain.append("tool_call", actor="web_search", payload={"query": "..."})
        assert chain.verify()
        print(chain.root_hash())
    """

    def __init__(self) -> None:
        self._events: list[AuditEventRecord] = []
        self._current_root = _GENESIS_ROOT

    def append(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEventRecord:
        """Append an event to the chain and return the resulting record."""
        payload_str = json.dumps(payload or {}, sort_keys=True, default=str)
        payload_hash = _sha256(payload_str)
        timestamp = datetime.utcnow()
        leaf_hash = _compute_leaf_hash(
            self._current_root, event_type, payload_hash, timestamp
        )
        record = AuditEventRecord(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            actor=actor,
            payload_hash=payload_hash,
            prev_root=self._current_root,
            leaf_hash=leaf_hash,
            timestamp=timestamp,
        )
        self._events.append(record)
        self._current_root = leaf_hash
        return record

    def root_hash(self) -> str:
        """Current root hash — changes with every new event."""
        return self._current_root

    def verify(self) -> bool:
        """
        Re-derive all hashes from scratch and confirm they match stored values.

        Returns True if the chain is intact; raises AuditVerificationError if not.
        """
        root = _GENESIS_ROOT
        for event in self._events:
            if event.prev_root != root:
                raise AuditVerificationError(
                    f"Chain broken at event {event.event_id}: "
                    f"expected prev_root={root!r}, got {event.prev_root!r}"
                )
            expected = _compute_leaf_hash(
                root, event.event_type, event.payload_hash, event.timestamp
            )
            if event.leaf_hash != expected:
                raise AuditVerificationError(
                    f"Hash mismatch at event {event.event_id}: "
                    f"expected {expected!r}, got {event.leaf_hash!r}"
                )
            root = event.leaf_hash
        return True

    def events(self) -> list[AuditEventRecord]:
        """Return a copy of the event list."""
        return list(self._events)

    def export_jsonl(self) -> str:
        """Serialize the chain as newline-delimited JSON (one record per line)."""
        lines = []
        for event in self._events:
            lines.append(
                json.dumps(
                    {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "actor": event.actor,
                        "payload_hash": event.payload_hash,
                        "prev_root": event.prev_root,
                        "leaf_hash": event.leaf_hash,
                        "timestamp": event.timestamp.isoformat(),
                    }
                )
            )
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._events)

    def __bool__(self) -> bool:
        # Always truthy — an empty chain is still a valid chain object.
        return True

    def __repr__(self) -> str:
        return f"AuditChain(events={len(self._events)}, root={self._current_root[:12]}...)"
