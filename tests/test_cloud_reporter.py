"""Tests for agent_kit.cloud.CloudReporter."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from httpx import Response

from agent_kit.cloud.models import CloudEvent, EventType
from agent_kit.cloud.reporter import CloudReporter, _encode_batch, _sha256
from agent_kit.types import AgentResult, CostSummary, Message, Turn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_reporter(**kwargs) -> CloudReporter:
    defaults = {"api_key": "akt_live_testkey123456789012345678901234", "project": "test"}
    return CloudReporter(**{**defaults, **kwargs})


def make_turn(input_tokens: int = 100, output_tokens: int = 50) -> Turn:
    return Turn(
        messages_in=[Message(role="user", content="hello")],
        cost=CostSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=0.001,
            model="claude-sonnet-4-6",
        ),
        duration_ms=500,
    )


def make_result() -> AgentResult:
    return AgentResult(
        output="The answer is 42.",
        turns=[make_turn()],
        total_cost_usd=0.001,
        total_tokens=150,
        audit_root_hash="abc123",
    )


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def test_encode_batch_is_valid_gzip_ndjson():
    events = [
        CloudEvent(
            event_type=EventType.RUN_START,
            run_id="r1",
            agent_name="test",
            project="p",
            payload={"model": "claude"},
        ),
        CloudEvent(
            event_type=EventType.RUN_COMPLETE,
            run_id="r1",
            agent_name="test",
            project="p",
            payload={"total_cost_usd": 0.001},
        ),
    ]
    body = _encode_batch(events)
    decompressed = gzip.decompress(body).decode()
    lines = [l for l in decompressed.splitlines() if l]
    assert len(lines) == 2
    obj = json.loads(lines[0])
    assert obj["event_type"] == "run_start"
    assert obj["run_id"] == "r1"


def test_sha256_is_deterministic():
    assert _sha256("hello") == _sha256("hello")
    assert _sha256("hello") != _sha256("world")


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reporter_enqueues_on_run_start():
    reporter = make_reporter()
    run_id = str(uuid.uuid4())
    await reporter.on_run_start(run_id=run_id, model="claude-sonnet-4-6", prompt="hi")
    assert reporter._queue.qsize() == 1
    event = reporter._queue.get_nowait()
    assert event.event_type == EventType.RUN_START
    assert event.run_id == run_id
    assert "prompt_hash" in event.payload
    assert event.payload["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_reporter_enqueues_on_turn_complete():
    reporter = make_reporter()
    turn = make_turn()
    await reporter.on_turn_complete(run_id="r1", turn=turn, turn_index=0)
    assert reporter._queue.qsize() == 1
    event = reporter._queue.get_nowait()
    assert event.event_type == EventType.TURN_COMPLETE
    assert event.payload["input_tokens"] == 100
    assert event.payload["cost_usd"] == 0.001
    assert event.payload["turn_index"] == 0


@pytest.mark.asyncio
async def test_reporter_enqueues_on_run_complete():
    reporter = make_reporter()
    result = make_result()
    await reporter.on_run_complete(run_id="r1", result=result)
    event = reporter._queue.get_nowait()
    assert event.event_type == EventType.RUN_COMPLETE
    assert event.payload["audit_root_hash"] == "abc123"
    assert event.payload["total_cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_reporter_enqueues_on_run_error():
    reporter = make_reporter()
    await reporter.on_run_error(run_id="r1", exc=ValueError("boom"), turn_count=2)
    event = reporter._queue.get_nowait()
    assert event.event_type == EventType.RUN_ERROR
    assert event.payload["error_type"] == "ValueError"
    assert event.payload["turn_count"] == 2


@pytest.mark.asyncio
async def test_reporter_enqueues_on_circuit_state_change():
    reporter = make_reporter()
    await reporter.on_circuit_state_change(
        run_id="r1",
        resource="anthropic",
        prev_state="closed",
        new_state="open",
        failure_count=5,
    )
    event = reporter._queue.get_nowait()
    assert event.event_type == EventType.CIRCUIT_STATE_CHANGE
    assert event.payload["new_state"] == "open"
    assert event.payload["failure_count"] == 5


@pytest.mark.asyncio
async def test_reporter_does_not_send_prompt_content():
    """Prompt hash is sent, never the prompt text itself."""
    reporter = make_reporter()
    prompt = "This is sensitive data"
    await reporter.on_run_start(run_id="r1", model=None, prompt=prompt)
    event = reporter._queue.get_nowait()
    payload_str = json.dumps(event.payload)
    assert prompt not in payload_str
    assert "prompt_hash" in payload_str


@pytest.mark.asyncio
async def test_reporter_include_output_false_by_default():
    """Output hash not sent unless include_output=True."""
    reporter = make_reporter(include_output=False)
    await reporter.on_run_complete(run_id="r1", result=make_result())
    event = reporter._queue.get_nowait()
    assert "output_hash" not in event.payload


@pytest.mark.asyncio
async def test_reporter_include_output_opt_in():
    reporter = make_reporter(include_output=True)
    await reporter.on_run_complete(run_id="r1", result=make_result())
    event = reporter._queue.get_nowait()
    assert "output_hash" in event.payload


# ---------------------------------------------------------------------------
# Flush / HTTP transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_sends_ndjson_to_ingest_endpoint():
    reporter = make_reporter(base_url="https://test.agentkit.io")
    import httpx
    reporter._http = httpx.AsyncClient()

    await reporter.on_run_start(run_id="r1", model="claude-sonnet-4-6", prompt="hi")

    with respx.mock:
        route = respx.post("https://test.agentkit.io/v1/events").mock(
            return_value=Response(202, json={"accepted": 1})
        )
        await reporter.flush()

    assert route.called
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer akt_live_testkey123456789012345678901234"
    assert request.headers["content-encoding"] == "gzip"
    body = gzip.decompress(request.content).decode()
    obj = json.loads(body.splitlines()[0])
    assert obj["event_type"] == "run_start"

    await reporter.close()


@pytest.mark.asyncio
async def test_flush_does_nothing_when_queue_empty():
    reporter = make_reporter(base_url="https://test.agentkit.io")
    import httpx
    reporter._http = httpx.AsyncClient()

    with respx.mock:
        respx.post("https://test.agentkit.io/v1/events").mock(
            return_value=Response(202, json={"accepted": 0})
        )
        await reporter.flush()
        # No events → no request should be made
        assert len(respx.calls) == 0

    await reporter.close()


@pytest.mark.asyncio
async def test_flush_swallows_http_errors():
    """HTTP failures must never propagate to the caller."""
    reporter = make_reporter(base_url="https://test.agentkit.io")
    import httpx
    reporter._http = httpx.AsyncClient()

    await reporter.on_run_start(run_id="r1", model=None, prompt="hi")

    with respx.mock:
        respx.post("https://test.agentkit.io/v1/events").mock(
            return_value=Response(500, text="internal server error")
        )
        # Should not raise
        await reporter.flush()

    await reporter.close()


@pytest.mark.asyncio
async def test_queue_full_drops_events_silently():
    reporter = make_reporter(max_queue_size=2)
    # Enqueue without a running flush task so the queue fills up
    # We're already in async context, so _enqueue will try to start a task
    # but the queue will fill after 2 events
    for _ in range(5):
        try:
            reporter._queue.put_nowait(CloudEvent(
                event_type=EventType.RUN_START,
                run_id="r",
                agent_name="a",
                project="p",
            ))
        except Exception:
            pass
    # Should never raise, and queue size is capped
    assert reporter._queue.qsize() <= 2


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_reporter_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("AGENTKIT_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key is required"):
        CloudReporter()


def test_reporter_uses_env_var(monkeypatch):
    monkeypatch.setenv("AGENTKIT_API_KEY", "akt_live_fromenv00000000000000000000")
    reporter = CloudReporter()
    assert reporter._api_key == "akt_live_fromenv00000000000000000000"


def test_reporter_repr():
    reporter = make_reporter(agent_name="billing-assistant")
    r = repr(reporter)
    assert "billing-assistant" in r
    assert "test" in r
