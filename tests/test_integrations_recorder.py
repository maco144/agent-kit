from __future__ import annotations

import threading

import pytest

from agent_kit.integrations.recorder import RunRecorder

RUN = "3b1e7d1c-0000-4000-8000-000000000001"


def test_lifecycle_emits_native_event_sequence(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="test-harness")

    rec.start(RUN, model="claude-opus-5", prompt="hi", metadata={"session_id": "s1"})
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100, tool_names=["Read"])
    rec.tool_call(RUN, "tu_1", "Read", success=True, duration_ms=12)
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=500, output_tokens=50, cache_read_tokens=1000)
    rec.complete(RUN)

    assert cloud_capture.types() == ["run_start", "turn_complete", "turn_complete", "run_complete", "audit_flush"]
    start = cloud_capture.of("run_start")[0].payload
    assert start["model"] == "claude-opus-5"
    assert start["harness"] == "test-harness"
    assert start["session_id"] == "s1"
    assert len(start["prompt_hash"]) == 64

    first, second = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (first["turn_index"], first["tool_names"]) == (0, ["Read"])
    assert first["cost_usd"] == pytest.approx((1000 * 5 + 100 * 25) / 1_000_000)
    assert second["cost_usd"] == pytest.approx((500 * 5 + 50 * 25 + 1000 * 5 * 0.1) / 1_000_000)

    done = cloud_capture.of("run_complete")[0].payload
    assert done["total_turns"] == 2
    assert done["total_tokens"] == 1000 + 100 + 500 + 50 + 1000
    assert done["total_cost_usd"] == pytest.approx(first["cost_usd"] + second["cost_usd"])
    assert done["audit_root_hash"] == cloud_capture.flush_payload(RUN)["final_root_hash"]

    assert cloud_capture.audit_types(RUN) == [
        "agent_start", "llm_complete", "tool_call", "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(RUN)
    assert {e.agent_name for e in cloud_capture.events} == {"test-harness"}
    assert {e.project for e in cloud_capture.events} == {"proj"}


def test_run_start_waits_for_a_model(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model=None, prompt=None)
    assert cloud_capture.events == []

    rec.llm_turn(RUN, "claude-sonnet-5", input_tokens=10, output_tokens=5)

    assert cloud_capture.types() == ["run_start", "turn_complete"]
    assert cloud_capture.of("run_start")[0].payload["model"] == "claude-sonnet-5"


def test_run_start_sent_at_completion_when_no_turns(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model=None, prompt=None)
    rec.complete(RUN)
    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


def test_harness_cost_is_reconciled_into_turn_costs(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100)  # $0.0075
    rec.complete(RUN, num_turns=3, harness_cost_usd=0.01)

    turns = [e.payload for e in cloud_capture.of("turn_complete")]
    assert turns[1]["reconciliation"] is True
    assert turns[1]["cost_usd"] == pytest.approx(0.0025)
    assert (turns[1]["input_tokens"], turns[1]["output_tokens"]) == (0, 0)
    assert sum(t["cost_usd"] for t in turns) == pytest.approx(0.01)
    done = cloud_capture.of("run_complete")[0].payload
    assert (done["total_cost_usd"], done["total_turns"]) == (pytest.approx(0.01), 3)


def test_no_reconciliation_when_costs_agree(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100)
    rec.complete(RUN, harness_cost_usd=0.0075)
    assert len(cloud_capture.of("turn_complete")) == 1


def test_error_emits_run_error_and_flushes_chain(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1, output_tokens=1)
    rec.error(RUN, "RateLimit", "x" * 900)

    assert cloud_capture.types() == ["run_start", "turn_complete", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], len(err["error_message"]), err["turn_count"]) == ("RateLimit", 500, 1)
    assert cloud_capture.audit_types(RUN)[-1] == "agent_error"
    cloud_capture.assert_chain_intact(RUN)


def test_calls_for_unknown_or_finished_runs_are_dropped(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.llm_turn("nope", "claude-opus-5", 1, 1)
    rec.tool_call("nope", "c", "t", success=True)
    rec.complete("nope")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.complete(RUN)
    rec.complete(RUN)
    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


def test_start_is_idempotent(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.complete(RUN)
    assert cloud_capture.types().count("run_start") == 1
    assert cloud_capture.audit_types(RUN).count("agent_start") == 1


def test_agent_name_precedence(cloud_capture, monkeypatch):
    rec = RunRecorder(cloud_capture.reporter, harness="h", agent_name="default-name")
    rec.start("r1", model="m", prompt=None)
    rec.start("r2", model="m", prompt=None, agent_name="workflow-name")
    monkeypatch.setattr(type(cloud_capture.reporter), "agent_name", property(lambda self: "configured"))
    rec.start("r3", model="m", prompt=None, agent_name="workflow-name")

    names = {e.run_id: e.agent_name for e in cloud_capture.of("run_start")}
    assert names == {"r1": "default-name", "r2": "workflow-name", "r3": "configured"}


def test_recorder_never_raises(cloud_capture, monkeypatch):
    def boom(event):
        raise RuntimeError("reporter down")

    monkeypatch.setattr(cloud_capture.reporter, "submit_threadsafe", boom)
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", 1, 1)
    rec.complete(RUN)


def test_tool_calls_from_many_threads_keep_the_chain_intact(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    threads = [
        threading.Thread(target=rec.tool_call, args=(RUN, f"c{i}", "t"), kwargs={"success": True})
        for i in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec.complete(RUN)

    assert cloud_capture.audit_types(RUN).count("tool_call") == 16
    cloud_capture.assert_chain_intact(RUN)
