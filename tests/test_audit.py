"""Tests for AuditChain."""

from __future__ import annotations

import pytest

from agent_kit.audit import AuditChain
from agent_kit.exceptions import AuditVerificationError


def test_audit_chain_append_and_verify():
    chain = AuditChain()
    chain.append("agent_start", actor="agent1", payload={"model": "claude"})
    chain.append("tool_call", actor="web_search", payload={"query": "test"})
    chain.append("agent_complete", actor="agent1", payload={"turns": 2})

    assert len(chain) == 3
    assert chain.verify() is True


def test_audit_chain_root_changes_on_append():
    chain = AuditChain()
    root_before = chain.root_hash()
    chain.append("event", actor="test")
    root_after = chain.root_hash()
    assert root_before != root_after


def test_audit_chain_tamper_detection():
    chain = AuditChain()
    chain.append("event1", actor="a")
    chain.append("event2", actor="b")

    # Tamper with the first event's prev_root
    events = chain._events
    # Directly mutate the frozen model's field (test only — not possible in normal use)
    tampered = events[0].model_copy(update={"prev_root": "0" * 63 + "1"})
    chain._events[0] = tampered

    with pytest.raises(AuditVerificationError):
        chain.verify()


def test_audit_chain_export_jsonl():
    chain = AuditChain()
    chain.append("start", actor="agent", payload={"key": "value"})
    jsonl = chain.export_jsonl()
    lines = jsonl.strip().split("\n")
    assert len(lines) == 1
    import json
    record = json.loads(lines[0])
    assert record["event_type"] == "start"
    assert record["actor"] == "agent"
    assert "leaf_hash" in record
    assert "prev_root" in record


def test_audit_chain_empty_verify():
    chain = AuditChain()
    assert chain.verify() is True
    assert len(chain) == 0


def test_audit_chain_events_returns_copy():
    chain = AuditChain()
    chain.append("e1", actor="a")
    events_copy = chain.events()
    events_copy.clear()
    assert len(chain) == 1  # original unaffected
