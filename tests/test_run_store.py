"""SQLiteRunStore and Checkpointer."""

from __future__ import annotations

from datetime import datetime

import pytest

from agent_kit.audit.chain import AuditChain
from agent_kit.durable import Checkpointer, PendingTurn, RunCheckpoint, SQLiteRunStore
from agent_kit.exceptions import RunConflictError
from agent_kit.types import Message, PendingApproval, ToolCall, ToolResult, Turn


def checkpoint(run_id: str = "r1", **update: object) -> RunCheckpoint:
    chain = AuditChain()
    chain.append("agent_start", actor=run_id, payload={"p": 1})
    tc = ToolCall(tool_name="refund", arguments={"order_id": "42"}, call_id="c1")
    now = datetime.utcnow()
    cp = RunCheckpoint(
        run_id=run_id, version=0, status="running", created_at=now, updated_at=now,
        prompt="refund 42", context={"tenant": "acme"}, output_type_name=None,
        messages=[
            Message(role="user", content="refund 42"),
            Message(role="assistant", content="", tool_calls=[tc],
                    native_content=[{"type": "thinking", "thinking": "", "signature": "s"}], native_provider="anthropic"),
        ],
        turns=[], turn_count=1, run_cost_usd=0.01, invalid_answers=0, native_output=False,
        last_prompt_tokens=10, last_prompt_chars=40, audit_events=chain.events(),
        pending=PendingTurn(
            turn=Turn(tool_calls=[tc]),
            results={"c0": ToolResult(call_id="c0", tool_name="lookup", output={"ok": True})},
            approvals=[PendingApproval(call_id="c1", tool_name="refund", arguments={"order_id": "42"}, turn=1)],
        ),
    )
    return cp.model_copy(update=update)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    s = SQLiteRunStore(":memory:" if request.param == "memory" else tmp_path / "runs.db")
    yield s
    s.close()


async def test_insert_load_round_trip(store):
    saved = await store.save(checkpoint(), 0)
    assert saved.version == 1
    loaded = await store.load("r1")
    assert loaded == saved
    assert loaded.messages[1].native_content == [{"type": "thinking", "thinking": "", "signature": "s"}]
    assert AuditChain.restore(loaded.audit_events).verify()
    assert await store.load("missing") is None


async def test_cas_conflicts(store):
    await store.save(checkpoint(), 0)
    with pytest.raises(RunConflictError) as exc:
        await store.save(checkpoint(), 0)
    assert (exc.value.expected_version, exc.value.actual_version) == (0, 1)
    v2 = await store.save(checkpoint(status="suspended"), 1)
    assert v2.version == 2
    with pytest.raises(RunConflictError):
        await store.save(checkpoint(), 1)
    with pytest.raises(RunConflictError) as missing:
        await store.save(checkpoint("nope"), 3)
    assert missing.value.actual_version is None


async def test_mark_tool_started(store):
    await store.save(checkpoint(), 0)
    assert await store.mark_tool_started("r1", "c1", 1) == 2
    assert (await store.load("r1")).pending.started == ["c1"]
    with pytest.raises(RunConflictError):
        await store.mark_tool_started("r1", "c2", 1)


async def test_list_and_delete(store):
    await store.save(checkpoint("a"), 0)
    await store.save(checkpoint("b", status="suspended"), 0)
    await store.save(checkpoint("c", status="completed", pending=None), 0)
    assert {s.run_id for s in await store.list()} == {"a", "b", "c"}
    suspended = await store.list(status="suspended")
    assert [(s.run_id, s.pending_approvals) for s in suspended] == [("b", 1)]
    await store.delete("b")
    assert await store.load("b") is None


async def test_checkpointer_tracks_versions_and_fail(store):
    cp = Checkpointer(store)
    await cp.create(checkpoint())
    await cp.mark_started("c1")
    await cp.save(checkpoint(turn_count=2))
    assert cp.current is not None and cp.current.version == 3
    await cp.fail("RuntimeError: boom")
    failed = await store.load("r1")
    assert (failed.status, failed.error, failed.version, failed.turn_count) == ("failed", "RuntimeError: boom", 4, 2)


def test_checkpoints_without_delegation_fields_still_load():
    data = checkpoint().model_dump(mode="json")
    for key in ("parent_run_id", "parent_call_id"):
        data.pop(key)
    for key in ("delegated_cost_usd", "delegated_tokens", "delegated_root_hash"):
        data["pending"].pop(key)
    for approval in data["pending"]["approvals"]:
        approval.pop("run_id")
    for result in data["pending"]["results"].values():
        result.pop("cost_usd")
        result.pop("tokens")

    cp = RunCheckpoint.model_validate(data)

    assert (cp.parent_run_id, cp.parent_call_id) == (None, None)
    assert cp.pending is not None
    assert (cp.pending.delegated_cost_usd, cp.pending.delegated_tokens, cp.pending.delegated_root_hash) == ({}, {}, {})
    assert cp.pending.approvals[0].run_id is None
    assert (cp.pending.results["c0"].cost_usd, cp.pending.results["c0"].tokens) == (0.0, 0)
