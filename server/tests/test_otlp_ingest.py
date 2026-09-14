"""POST /v1/traces end to end: runs, audit chains, metrics, lifecycle edge cases."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ActiveRunCache, AuditEvent, AuditRun, CloudEventLog
from app.otlp.assembler import IDLE_TIMEOUT, run_id_for_trace
from app.otlp.decode import JSON, PROTOBUF
from tests.otlp_helpers import SpanSpec, json_body, new_trace_id, protobuf_body

RESOURCE = {"service.name": "support-svc", "agentkit.project": "otlp"}


async def post(client, spans, content_type=PROTOBUF, resource=RESOURCE):
    body = protobuf_body(spans, resource) if content_type == PROTOBUF else json_body(spans, resource)
    return await client.post("/v1/traces", content=body, headers={"Content-Type": content_type})


def agent_trace(trace_id: str, *, tool_error: bool = False, root_error: bool = False) -> list[SpanSpec]:
    now = time.time_ns()
    root = SpanSpec(trace_id, "POST /chat", {"http.method": "POST"}, start_ns=now, duration_ms=900, error=root_error)
    agent = SpanSpec(trace_id, "invoke_agent support",
                     {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "support"},
                     parent=root.span_id, start_ns=now + 1_000, duration_ms=800)
    chat1 = SpanSpec(trace_id, "chat claude-opus-5",
                     {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-opus-5",
                      "gen_ai.usage.input_tokens": 2000, "gen_ai.usage.output_tokens": 100,
                      "gen_ai.usage.cache_read.input_tokens": 1000,
                      "gen_ai.input.messages": "SECRET PROMPT"},
                     parent=agent.span_id, start_ns=now + 2_000, duration_ms=300)
    tool = SpanSpec(trace_id, "execute_tool lookup_order",
                    {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
                     "gen_ai.tool.call.id": "call_1", "gen_ai.tool.call.arguments": "SECRET ARGS"},
                    parent=agent.span_id, start_ns=now + 400_000_000, duration_ms=50, error=tool_error)
    chat2 = SpanSpec(trace_id, "chat claude-opus-5",
                     {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-opus-5",
                      "gen_ai.usage.input_tokens": 500, "gen_ai.usage.output_tokens": 40},
                     parent=agent.span_id, start_ns=now + 500_000_000, duration_ms=200)
    return [root, agent, chat1, tool, chat2]


async def get_run(db, trace_id) -> AuditRun | None:
    result = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id_for_trace(trace_id)))
    return result.scalar_one_or_none()


async def audit_types(db, trace_id) -> list[str]:
    result = await db.execute(
        select(AuditEvent.event_type)
        .where(AuditEvent.run_id == run_id_for_trace(trace_id))
        .order_by(AuditEvent.seq)
    )
    return list(result.scalars().all())


@pytest.mark.parametrize("content_type", [PROTOBUF, JSON])
async def test_semconv_trace_becomes_verified_run_with_metrics(client, db, content_type):
    trace_id = new_trace_id()

    resp = await post(client, agent_trace(trace_id), content_type)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(content_type)
    run = await get_run(db, trace_id)
    assert run is not None
    await db.refresh(run)
    assert (run.chain_origin, run.integrity, run.agent_name, run.project) == ("ingest", "verified", "support", "otlp")
    assert run.completed_at is not None
    assert await audit_types(db, trace_id) == [
        "agent_start", "llm_complete", "tool_call", "llm_complete", "agent_invoke", "agent_complete",
    ]

    runs = (await client.get("/v1/audit/runs?project=otlp")).json()["runs"]
    assert [(r["chain_origin"], r["integrity"]) for r in runs] == [("ingest", "verified")]
    verify = (await client.get(f"/v1/audit/runs/{run.run_id}/verify")).json()
    assert verify["verified"] is True

    summary = (await client.get("/v1/metrics/summary?project=otlp")).json()
    assert (summary["total_runs"], summary["runs_success"]) == (1, 1)
    assert (summary["total_input_tokens"], summary["total_output_tokens"]) == (1000 + 500, 140)
    expected = (1000 * 5 + 100 * 25 + 1000 * 5 * 0.1 + 500 * 5 + 40 * 25) / 1_000_000
    assert summary["total_cost_usd"] == pytest.approx(expected, abs=1e-6)

    agents = (await client.get("/v1/metrics/agents?project=otlp")).json()["agents"]
    assert [(a["agent_name"], a["models_used"]) for a in agents] == [("support", ["claude-opus-5"])]


async def test_no_span_content_is_persisted(client, db):
    trace_id = new_trace_id()
    await post(client, agent_trace(trace_id))

    logs = (await db.execute(select(CloudEventLog).where(CloudEventLog.run_id == run_id_for_trace(trace_id)))).scalars().all()
    assert logs
    assert "SECRET" not in repr([log.payload for log in logs])


async def test_openinference_trace(client, db):
    trace_id = new_trace_id()
    now = time.time_ns()
    agent = SpanSpec(trace_id, "AgentExecutor", {"openinference.span.kind": "AGENT"}, start_ns=now, duration_ms=500)
    llm = SpanSpec(trace_id, "ChatOpenAI", {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o",
                                            "llm.token_count.prompt": 800, "llm.token_count.completion": 60},
                   parent=agent.span_id, start_ns=now + 1_000, duration_ms=200)
    tool = SpanSpec(trace_id, "search", {"openinference.span.kind": "TOOL", "tool.name": "search"},
                    parent=agent.span_id, start_ns=now + 300_000_000, duration_ms=20)

    assert (await post(client, [agent, llm, tool], JSON)).status_code == 200

    run = await get_run(db, trace_id)
    assert run is not None
    await db.refresh(run)
    assert (run.agent_name, run.integrity, run.completed_at is not None) == ("AgentExecutor", "verified", True)
    logs = (await db.execute(select(CloudEventLog).where(CloudEventLog.event_type == "run_start",
                                                          CloudEventLog.run_id == run.run_id))).scalars().all()
    assert logs[0].payload["harness"] == "openinference"
    assert logs[0].payload["trace_id"] == trace_id


async def test_spans_across_requests_complete_when_root_arrives(client, db):
    trace_id = new_trace_id()
    root, agent, chat1, tool, chat2 = agent_trace(trace_id)

    await post(client, [chat1, tool])
    run = await get_run(db, trace_id)
    assert run is not None and run.completed_at is None
    cache = (await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run.run_id))).scalar_one()
    assert (cache.turns_so_far, cache.model, cache.last_event_at is not None) == (1, "claude-opus-5", True)

    await post(client, [chat2, agent])
    await post(client, [root])

    await db.refresh(run)
    assert run.completed_at is not None and run.integrity == "verified"
    assert run.agent_name == "support"
    assert (await audit_types(db, trace_id))[-1] == "agent_complete"


async def test_failed_agent_span_fails_the_run(client, db):
    trace_id = new_trace_id()
    spans = agent_trace(trace_id)
    spans[1].error = True  # the invoke_agent span

    await post(client, spans)

    assert (await audit_types(db, trace_id))[-1] == "agent_error"
    errors = (await db.execute(select(CloudEventLog).where(CloudEventLog.event_type == "run_error"))).scalars().all()
    assert [e.payload["error_type"] for e in errors if e.run_id == run_id_for_trace(trace_id)] == ["AgentError"]
    summary = (await client.get("/v1/metrics/summary?project=otlp")).json()
    assert summary["runs_error"] >= 1


async def test_failed_root_span_fails_the_run(client, db):
    trace_id = new_trace_id()
    await post(client, agent_trace(trace_id, root_error=True))
    errors = (await db.execute(select(CloudEventLog).where(CloudEventLog.event_type == "run_error",
                                                            CloudEventLog.run_id == run_id_for_trace(trace_id)))).scalars().all()
    assert errors[0].payload["error_type"] == "TraceError"


async def test_failed_tool_does_not_fail_the_run(client, db):
    trace_id = new_trace_id()
    await post(client, agent_trace(trace_id, tool_error=True))
    assert (await audit_types(db, trace_id))[-1] == "agent_complete"


async def test_trace_without_genai_spans_creates_nothing(client, db):
    trace_id = new_trace_id()
    resp = await post(client, [SpanSpec(trace_id, "GET /healthz", {"http.method": "GET"})])
    assert resp.status_code == 200
    assert await get_run(db, trace_id) is None


async def test_idle_runs_are_finalized_on_a_later_request(client, db):
    trace_id = new_trace_id()
    _, agent, chat1, tool, chat2 = agent_trace(trace_id)
    await post(client, [chat1, tool, chat2, agent])  # root never exported
    run = await get_run(db, trace_id)
    assert run is not None and run.completed_at is None

    cache = (await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run.run_id))).scalar_one()
    cache.last_event_at = datetime.utcnow() - IDLE_TIMEOUT - timedelta(seconds=1)
    await db.commit()

    await post(client, [SpanSpec(new_trace_id(), "GET /unrelated", {})])

    await db.refresh(run)
    assert run.completed_at is not None and run.integrity == "verified"
    assert (await audit_types(db, trace_id))[-1] == "agent_complete"


async def test_late_span_extends_the_chain_but_not_metrics(client, db):
    trace_id = new_trace_id()
    await post(client, agent_trace(trace_id))
    before = (await client.get("/v1/metrics/summary?project=otlp")).json()

    late = SpanSpec(trace_id, "chat claude-opus-5", {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-opus-5",
                                                     "gen_ai.usage.input_tokens": 99999})
    assert (await post(client, [late])).status_code == 200

    run = await get_run(db, trace_id)
    assert run is not None
    await db.refresh(run)
    assert (await audit_types(db, trace_id))[-1] == "llm_complete"
    assert run.integrity == "verified"
    assert (await client.get(f"/v1/audit/runs/{run.run_id}/verify")).json()["verified"] is True
    after = (await client.get("/v1/metrics/summary?project=otlp")).json()
    assert after["total_input_tokens"] == before["total_input_tokens"]


async def test_retried_batches_are_idempotent(client, db):
    trace_id = new_trace_id()
    _, agent, chat1, tool, chat2 = agent_trace(trace_id)

    for _ in range(2):
        assert (await post(client, [chat1, tool, chat2])).status_code == 200

    run = await get_run(db, trace_id)
    assert run is not None
    cache = (await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run.run_id))).scalar_one()
    assert (cache.turns_so_far, cache.input_tokens) == (2, 1500)
    assert await audit_types(db, trace_id) == ["agent_start", "llm_complete", "tool_call", "llm_complete"]


async def test_write_conflict_returns_503(client, monkeypatch):
    async def conflict(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception("duplicate seq"))

    monkeypatch.setattr("app.routers.otlp.assemble", conflict)
    resp = await post(client, agent_trace(new_trace_id()))
    assert resp.status_code == 503


async def test_unsupported_and_undecodable_requests(client):
    assert (await client.post("/v1/traces", content=b"x", headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await client.post("/v1/traces", content=b"{nope", headers={"Content-Type": JSON})).status_code == 400


async def test_requires_auth():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.post("/v1/traces", content=b"", headers={"Content-Type": PROTOBUF})
    assert resp.status_code == 401


async def test_partial_success_reports_rejected_spans(client):
    import json

    good = agent_trace(new_trace_id())
    document = json.loads(json_body(good, RESOURCE))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"].append({"traceId": "zz", "spanId": "yy"})

    resp = await client.post("/v1/traces", content=json.dumps(document).encode(), headers={"Content-Type": JSON})

    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedSpans"] == "1"


async def test_real_opentelemetry_sdk_export_bytes(client, db):
    """Spans recorded by the real SDK and encoded by the OTLP exporter's own encoder."""
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "sdk-svc", "agentkit.project": "otel-sdk"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agent")

    with tracer.start_as_current_span("invoke_agent triage", attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage",
    }) as agent_span:
        with tracer.start_as_current_span("chat gpt-4o", attributes={
            "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 300, "gen_ai.usage.output_tokens": 30,
        }):
            pass
        with tracer.start_as_current_span("execute_tool refund", attributes={
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "refund",
        }):
            pass
    trace_id = format(agent_span.get_span_context().trace_id, "032x")

    body = encode_spans(exporter.get_finished_spans()).SerializeToString()
    resp = await client.post("/v1/traces", content=body, headers={"Content-Type": PROTOBUF})

    assert resp.status_code == 200
    run = await get_run(db, trace_id)
    assert run is not None
    await db.refresh(run)
    assert (run.project, run.agent_name, run.integrity) == ("otel-sdk", "triage", "verified")
    assert await audit_types(db, trace_id) == ["agent_start", "llm_complete", "tool_call", "agent_invoke", "agent_complete"]
