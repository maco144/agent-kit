# OTLP Trace Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /v1/traces` turns OpenTelemetry GenAI-semconv and OpenInference spans into agent-kit runs with server-built audit chains, fleet metrics, and run errors.

**Architecture:** `decode.py` turns OTLP/HTTP protobuf or JSON into `RawSpan`s; `normalize.py` maps both conventions onto `GenAISpan`; `assembler.py` applies spans per trace — starting runs, extending the audit chain with `audit_chain.append_event`, and driving the existing ingest handlers for `run_start` / `turn_complete` / `run_complete` / `run_error`. Ingest-built runs verify synchronously on completion.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, `opentelemetry-proto` (protobuf), pytest + pytest-asyncio (auto mode), `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-common` for the wire-fidelity test.

**Spec:** `specs/08-otlp-ingest.md`

## Global Constraints

- Server never imports the `agent_kit` SDK.
- No span content is stored: never read or persist `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `llm.input_messages`, `llm.output_messages`, `input.value`, `output.value`, or `tool_call.function.arguments`. Normalization copies only the fields in the spec's mapping table.
- `run_id` values are UUIDs (`String(36)` columns).
- Audit `payload_hash` = SHA-256 of `json.dumps(payload, sort_keys=True, default=str)`; leaf = SHA-256 of `prev_root + event_type + payload_hash + timestamp.isoformat()` — identical to the SDK and `verify_chain`.
- Timestamps are naive UTC `datetime`s with microsecond precision derived from span nanoseconds.
- `cd server && ruff check app tests && pytest` clean; `alembic upgrade head` succeeds on a fresh SQLite database.
- Server tests use the in-process SQLite fixtures; no network.

## Spec amendment found while planning

`active_run_cache` also gains `failure_message VARCHAR(500) NULL` — the outermost agent span's failure must survive across requests until the run completes (including idle finalization). Recorded in the spec in Task 5.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `server/pyproject.toml` | `opentelemetry-proto` dependency; dev: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-common` | Modify |
| `server/migrations/versions/005_otlp_ingest.py` | `audit_runs.chain_origin`, `active_run_cache.last_event_at`, `active_run_cache.failure_message` | Create |
| `server/app/models.py` | Matching columns | Modify |
| `server/app/schemas.py` | `AuditRunSummary.chain_origin` | Modify |
| `server/app/audit_chain.py` | `GENESIS_ROOT`, `payload_hash`, `append_event` | Modify |
| `server/app/otlp/__init__.py` | Package docstring | Create |
| `server/app/otlp/decode.py` | OTLP protobuf/JSON → `DecodedBatch`; response encoding | Create |
| `server/app/otlp/normalize.py` | `GenAISpan`, semconv + OpenInference mappers | Create |
| `server/app/otlp/pricing.py` | Server copy of model prices | Create |
| `server/app/otlp/assembler.py` | Run lifecycle over spans | Create |
| `server/app/routers/otlp.py` | `POST /v1/traces` | Create |
| `server/app/main.py` | Include the router | Modify |
| `server/tests/otlp_helpers.py` | Span builders for protobuf and JSON bodies | Create |
| `server/tests/test_audit_chain_append.py` | `append_event` + `chain_origin` exposure | Create |
| `server/tests/test_otlp_decode.py` | Decoder | Create |
| `server/tests/test_otlp_normalize.py` | Mappers + pricing | Create |
| `server/tests/test_otlp_ingest.py` | Endpoint + assembler end to end | Create |
| `docs/api-reference.md`, `docs/cloud-quickstart.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/08-otlp-ingest.md`, `PROJECT_INDEX.md` | Docs | Modify |

---

### Task 1: Schema, dependency, and server-side chain extension

**Files:**
- Modify: `server/pyproject.toml`, `server/app/models.py`, `server/app/schemas.py`, `server/app/audit_chain.py`
- Create: `server/migrations/versions/005_otlp_ingest.py`, `server/tests/test_audit_chain_append.py`

**Interfaces:**
- Produces: `AuditRun.chain_origin: str` (`"client"` default), `ActiveRunCache.last_event_at: datetime | None`, `ActiveRunCache.failure_message: str | None`; `audit_chain.GENESIS_ROOT`, `audit_chain.payload_hash(payload: dict[str, Any]) -> str`, `audit_chain.append_event(run: AuditRun, *, event_id: str, event_type: str, actor: str, payload: dict[str, Any], timestamp: datetime) -> AuditEvent` (caller adds the row); `AuditRunSummary.chain_origin`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_audit_chain_append.py
"""Server-built audit chains and chain_origin exposure."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.audit_chain import GENESIS_ROOT, append_event, payload_hash, verify_chain
from app.models import AuditRun


def test_payload_hash_matches_sdk_serialisation():
    import hashlib
    import json

    payload = {"b": 2, "a": {"z": 1, "y": [1, 2]}}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    assert payload_hash(payload) == expected


def test_append_event_builds_a_verifiable_chain():
    run = AuditRun(
        org_id="org", project="p", agent_name="a", run_id=str(uuid.uuid4()),
        final_root_hash=GENESIS_ROOT, event_count=0, chain_origin="ingest",
    )
    t0 = datetime(2026, 9, 13, 12, 0, 0, 123456)
    events = [
        append_event(run, event_id=str(uuid.uuid4()), event_type=kind, actor="x",
                     payload={"i": i}, timestamp=t0 + timedelta(seconds=i))
        for i, kind in enumerate(["agent_start", "llm_complete", "tool_call", "agent_complete"])
    ]

    assert [e.seq for e in events] == [0, 1, 2, 3]
    assert events[0].prev_root == GENESIS_ROOT
    assert run.final_root_hash == events[-1].leaf_hash
    assert run.event_count == 4
    assert verify_chain(events) == (True, None, None, None)


async def test_audit_runs_expose_chain_origin(client, db, org_and_key):
    org, _ = org_and_key
    db.add(AuditRun(org_id=org.id, project="p", agent_name="sdk-agent", run_id=str(uuid.uuid4()),
                    final_root_hash=GENESIS_ROOT, event_count=0))
    await db.commit()

    resp = await client.get("/v1/audit/runs")

    assert resp.status_code == 200
    assert resp.json()["runs"][0]["chain_origin"] == "client"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_audit_chain_append.py -v`
Expected: FAIL — `ImportError: cannot import name 'GENESIS_ROOT'`

- [ ] **Step 3: Implement**

`server/pyproject.toml` dependencies gain `"opentelemetry-proto>=1.24",`; dev extras gain `"opentelemetry-sdk>=1.24",` and `"opentelemetry-exporter-otlp-proto-common>=1.24",`.

`server/app/models.py` — `AuditRun`, after `integrity`:

```python
    chain_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="client"
    )  # client (built by the SDK) | ingest (built from OTLP spans)
```

`ActiveRunCache`, after `cost_so_far_usd`:

```python
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
```

`server/app/schemas.py` — `AuditRunSummary`, after `integrity`:

```python
    chain_origin: str = "client"
```

```python
# server/migrations/versions/005_otlp_ingest.py
"""OTLP ingest: audit chain origin and per-run span activity.

Revision ID: 005
Revises: 004
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_runs",
        sa.Column("chain_origin", sa.String(16), nullable=False, server_default="client"),
    )
    op.add_column("active_run_cache", sa.Column("last_event_at", sa.DateTime, nullable=True))
    op.add_column("active_run_cache", sa.Column("failure_message", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("active_run_cache", "failure_message")
    op.drop_column("active_run_cache", "last_event_at")
    op.drop_column("audit_runs", "chain_origin")
```

`server/app/audit_chain.py` — replace the module header through `_GENESIS_ROOT` and add helpers:

```python
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
```

- [ ] **Step 4: Run tests and migration**

Run: `cd server && pip install -e ".[dev]" && pytest tests/test_audit_chain_append.py -v && pytest && ruff check app tests && DATABASE_URL=sqlite+aiosqlite:////tmp/otlp005.db alembic upgrade head`
Expected: PASS; migration runs through `004 -> 005`.

- [ ] **Step 5: Commit**

```bash
git add server/pyproject.toml server/app/models.py server/app/schemas.py server/app/audit_chain.py server/migrations/versions/005_otlp_ingest.py server/tests/test_audit_chain_append.py
git commit -m "feat(server): chain_origin and server-side audit chain extension"
```

---

### Task 2: OTLP/HTTP decoding

**Files:**
- Create: `server/app/otlp/__init__.py`, `server/app/otlp/decode.py`, `server/tests/otlp_helpers.py`, `server/tests/test_otlp_decode.py`

**Interfaces:**
- Produces: `decode.PROTOBUF`, `decode.JSON`, `DecodeError`, `RawSpan(trace_id, span_id, parent_span_id, name, start_ns, end_ns, status_error, status_message, attributes, resource)`, `DecodedBatch(spans, rejected, error_message)`, `decode_request(body, content_type, content_encoding="") -> DecodedBatch`, `encode_response(content_type, rejected, error_message) -> bytes`. Test helpers: `SpanSpec`, `new_trace_id()`, `new_span_id()`, `protobuf_body(spans, resource)`, `json_body(spans, resource)`.

- [ ] **Step 1: Write the helpers and failing tests**

```python
# server/tests/otlp_helpers.py
"""Build OTLP/HTTP trace export bodies for tests."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Status


def new_trace_id() -> str:
    return os.urandom(16).hex()


def new_span_id() -> str:
    return os.urandom(8).hex()


@dataclass
class SpanSpec:
    trace_id: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=new_span_id)
    parent: str = ""
    start_ns: int = field(default_factory=time.time_ns)
    duration_ms: int = 5
    error: bool = False
    error_message: str = "boom"

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ms * 1_000_000


def _any(value: Any) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, list):
        out = AnyValue()
        out.array_value.values.extend(_any(v) for v in value)
        return out
    return AnyValue(string_value=str(value))


def protobuf_body(spans: list[SpanSpec], resource: dict[str, Any] | None = None) -> bytes:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(
        KeyValue(key=k, value=_any(v)) for k, v in (resource or {}).items()
    )
    scope_spans = resource_spans.scope_spans.add()
    for spec in spans:
        span = scope_spans.spans.add()
        span.trace_id = bytes.fromhex(spec.trace_id)
        span.span_id = bytes.fromhex(spec.span_id)
        if spec.parent:
            span.parent_span_id = bytes.fromhex(spec.parent)
        span.name = spec.name
        span.start_time_unix_nano = spec.start_ns
        span.end_time_unix_nano = spec.end_ns
        if spec.error:
            span.status.code = Status.STATUS_CODE_ERROR
            span.status.message = spec.error_message
        span.attributes.extend(KeyValue(key=k, value=_any(v)) for k, v in spec.attributes.items())
    return request.SerializeToString()


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_json_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _json_attributes(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _json_value(v)} for k, v in attributes.items()]


def json_body(spans: list[SpanSpec], resource: dict[str, Any] | None = None) -> bytes:
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": _json_attributes(resource or {})},
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "traceId": s.trace_id,
                                    "spanId": s.span_id,
                                    "parentSpanId": s.parent,
                                    "name": s.name,
                                    "startTimeUnixNano": str(s.start_ns),
                                    "endTimeUnixNano": str(s.end_ns),
                                    "status": {"code": "STATUS_CODE_ERROR", "message": s.error_message}
                                    if s.error
                                    else {},
                                    "attributes": _json_attributes(s.attributes),
                                }
                                for s in spans
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()
```

```python
# server/tests/test_otlp_decode.py
from __future__ import annotations

import gzip
import json

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from app.otlp.decode import JSON, PROTOBUF, DecodeError, decode_request, encode_response
from tests.otlp_helpers import SpanSpec, json_body, new_span_id, new_trace_id, protobuf_body

RESOURCE = {"service.name": "svc", "agentkit.project": "support"}


def _specs() -> list[SpanSpec]:
    trace = new_trace_id()
    root = SpanSpec(trace, "invoke_agent support", {"gen_ai.operation.name": "invoke_agent"})
    child = SpanSpec(
        trace, "chat gpt-4o",
        {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 1200, "ratio": 0.5,
         "gen_ai.response.finish_reasons": ["stop"], "flag": True},
        parent=root.span_id, error=True, error_message="rate limited",
    )
    return [root, child]


@pytest.mark.parametrize(("encode", "content_type"), [(protobuf_body, PROTOBUF), (json_body, JSON)])
def test_decodes_spans_identically_from_protobuf_and_json(encode, content_type):
    specs = _specs()
    batch = decode_request(encode(specs, RESOURCE), content_type)

    assert batch.rejected == 0
    root, child = batch.spans
    assert (root.trace_id, root.span_id, root.parent_span_id) == (specs[0].trace_id, specs[0].span_id, "")
    assert child.parent_span_id == specs[0].span_id
    assert (child.name, child.start_ns, child.end_ns) == ("chat gpt-4o", specs[1].start_ns, specs[1].end_ns)
    assert (child.status_error, child.status_message) == (True, "rate limited")
    assert root.status_error is False
    assert child.attributes == {
        "gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 1200, "ratio": 0.5,
        "gen_ai.response.finish_reasons": ["stop"], "flag": True,
    }
    assert child.resource == RESOURCE


@pytest.mark.parametrize(("encode", "content_type"), [(protobuf_body, PROTOBUF), (json_body, JSON)])
def test_gzip_bodies_are_decompressed(encode, content_type):
    body = gzip.compress(encode(_specs(), RESOURCE))
    assert len(decode_request(body, content_type, "gzip").spans) == 2


def test_json_accepts_integer_status_codes():
    specs = _specs()
    document = json.loads(json_body(specs))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["status"] = {"code": 2}
    batch = decode_request(json.dumps(document).encode(), JSON)
    assert batch.spans[1].status_error is True


def test_malformed_json_spans_are_rejected_individually():
    good = SpanSpec(new_trace_id(), "ok")
    document = json.loads(json_body([good]))
    spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append({"traceId": "not-hex", "spanId": new_span_id(), "name": "bad"})
    spans.append({"spanId": new_span_id(), "name": "missing trace"})

    batch = decode_request(json.dumps(document).encode(), JSON)

    assert [s.name for s in batch.spans] == ["ok"]
    assert batch.rejected == 2
    assert batch.error_message


@pytest.mark.parametrize(
    ("body", "content_type", "encoding"),
    [
        (b"not a protobuf", PROTOBUF, ""),
        (b"{not json", JSON, ""),
        (b"[]", JSON, ""),
        (b"plain", JSON, "gzip"),
    ],
)
def test_undecodable_bodies_raise(body, content_type, encoding):
    with pytest.raises(DecodeError):
        decode_request(body, content_type, encoding)


def test_response_encoding():
    assert ExportTraceServiceResponse.FromString(encode_response(PROTOBUF, 0, "")) == ExportTraceServiceResponse()
    partial = ExportTraceServiceResponse.FromString(encode_response(PROTOBUF, 3, "bad span"))
    assert (partial.partial_success.rejected_spans, partial.partial_success.error_message) == (3, "bad span")
    assert json.loads(encode_response(JSON, 0, "")) == {}
    assert json.loads(encode_response(JSON, 2, "bad")) == {"partialSuccess": {"rejectedSpans": "2", "errorMessage": "bad"}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_otlp_decode.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.otlp'`

- [ ] **Step 3: Implement**

```python
# server/app/otlp/__init__.py
"""OTLP trace ingest: decode, normalize GenAI spans, assemble agent-kit runs."""
```

```python
# server/app/otlp/decode.py
"""Decode OTLP/HTTP trace export requests (protobuf or JSON) into plain spans."""

from __future__ import annotations

import base64
import binascii
import gzip
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.message import DecodeError as ProtobufDecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

PROTOBUF = "application/x-protobuf"
JSON = "application/json"

_STATUS_ERROR = 2
_HEX = frozenset("0123456789abcdef")


class DecodeError(ValueError):
    """The request body could not be decoded as an OTLP trace export."""


@dataclass
class RawSpan:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    start_ns: int
    end_ns: int
    status_error: bool
    status_message: str
    attributes: dict[str, Any]
    resource: dict[str, Any]


@dataclass
class DecodedBatch:
    spans: list[RawSpan] = field(default_factory=list)
    rejected: int = 0
    error_message: str = ""

    def reject(self, message: str) -> None:
        self.rejected += 1
        self.error_message = self.error_message or message


def decode_request(body: bytes, content_type: str, content_encoding: str = "") -> DecodedBatch:
    if content_encoding.strip().lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise DecodeError(f"invalid gzip body: {exc}") from exc
    if content_type == PROTOBUF:
        return _decode_protobuf(body)
    if content_type == JSON:
        return _decode_json(body)
    raise DecodeError(f"unsupported content type {content_type!r}")


def encode_response(content_type: str, rejected: int, error_message: str) -> bytes:
    """ExportTraceServiceResponse in the request's encoding."""
    if content_type == PROTOBUF:
        response = ExportTraceServiceResponse()
        if rejected:
            response.partial_success.rejected_spans = rejected
            response.partial_success.error_message = error_message
        return bytes(response.SerializeToString())
    if not rejected:
        return b"{}"
    return json.dumps(
        {"partialSuccess": {"rejectedSpans": str(rejected), "errorMessage": error_message}}
    ).encode()


# ---------------------------------------------------------------------------
# Protobuf
# ---------------------------------------------------------------------------


def _decode_protobuf(body: bytes) -> DecodedBatch:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except ProtobufDecodeError as exc:
        raise DecodeError(f"invalid protobuf body: {exc}") from exc

    batch = DecodedBatch()
    for resource_spans in request.resource_spans:
        resource = _pb_attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                if len(span.trace_id) != 16 or len(span.span_id) != 8:
                    batch.reject("span with invalid trace_id or span_id")
                    continue
                batch.spans.append(
                    RawSpan(
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=span.parent_span_id.hex(),
                        name=span.name,
                        start_ns=span.start_time_unix_nano,
                        end_ns=span.end_time_unix_nano,
                        status_error=span.status.code == _STATUS_ERROR,
                        status_message=span.status.message,
                        attributes=_pb_attributes(span.attributes),
                        resource=resource,
                    )
                )
    return batch


def _pb_attributes(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    return {kv.key: _pb_value(kv.value) for kv in attributes}


def _pb_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_pb_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return _pb_attributes(value.kvlist_value.values)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind)


# ---------------------------------------------------------------------------
# JSON — OTLP/JSON uses hex IDs and string int64s, so no json_format
# ---------------------------------------------------------------------------


def _decode_json(body: bytes) -> DecodedBatch:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecodeError(f"invalid JSON body: {exc}") from exc
    if not isinstance(document, dict):
        raise DecodeError("OTLP/JSON body must be an object")

    batch = DecodedBatch()
    for resource_spans in _list(document.get("resourceSpans")):
        resource = _json_attributes(_dict(_dict(resource_spans).get("resource")).get("attributes"))
        for scope_spans in _list(_dict(resource_spans).get("scopeSpans")):
            for span in _list(_dict(scope_spans).get("spans")):
                try:
                    batch.spans.append(_json_span(span, resource))
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    batch.reject(f"malformed span: {exc}")
    return batch


def _json_span(span: dict[str, Any], resource: dict[str, Any]) -> RawSpan:
    parent = span.get("parentSpanId") or ""
    status = _dict(span.get("status"))
    code = status.get("code", 0)
    return RawSpan(
        trace_id=_hex_id(span["traceId"], 16),
        span_id=_hex_id(span["spanId"], 8),
        parent_span_id=_hex_id(parent, 8) if parent else "",
        name=str(span.get("name", "")),
        start_ns=int(span.get("startTimeUnixNano") or 0),
        end_ns=int(span.get("endTimeUnixNano") or 0),
        status_error=code in (_STATUS_ERROR, str(_STATUS_ERROR), "STATUS_CODE_ERROR"),
        status_message=str(status.get("message", "")),
        attributes=_json_attributes(span.get("attributes")),
        resource=resource,
    )


def _hex_id(value: Any, size_bytes: int) -> str:
    text = str(value).lower()
    if len(text) != size_bytes * 2 or not set(text) <= _HEX:
        raise ValueError(f"invalid id {value!r}")
    return text


def _json_attributes(attributes: Any) -> dict[str, Any]:
    return {
        str(item["key"]): _json_value(item.get("value"))
        for item in _list(attributes)
        if isinstance(item, dict) and "key" in item
    }


def _json_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_json_value(v) for v in _list(_dict(value["arrayValue"]).get("values"))]
    if "kvlistValue" in value:
        return _json_attributes(_dict(value["kvlistValue"]).get("values"))
    if "bytesValue" in value:
        try:
            return base64.b64decode(value["bytesValue"]).hex()
        except (binascii.Error, TypeError):
            return None
    return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest tests/test_otlp_decode.py -v && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/otlp server/tests/otlp_helpers.py server/tests/test_otlp_decode.py
git commit -m "feat(server): decode OTLP/HTTP trace exports (protobuf and JSON)"
```

---

### Task 3: Normalization and pricing

**Files:**
- Create: `server/app/otlp/normalize.py`, `server/app/otlp/pricing.py`, `server/tests/test_otlp_normalize.py`

**Interfaces:**
- Consumes: `RawSpan` (Task 2).
- Produces: `GenAISpan(raw, kind, convention, name, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, tool_call_id, conversation_id, failed)` with property `duration_ms`; `normalize(span: RawSpan) -> GenAISpan | None`; `pricing.estimate_cost(model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0) -> float`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_otlp_normalize.py
from __future__ import annotations

import dataclasses

import pytest

from app.otlp.decode import RawSpan
from app.otlp.normalize import normalize
from app.otlp.pricing import estimate_cost

CONTENT = {
    "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"secret"}]}]',
    "gen_ai.output.messages": "secret answer",
    "gen_ai.system_instructions": "secret system",
    "gen_ai.tool.call.arguments": '{"card": "4111"}',
    "gen_ai.tool.call.result": "secret result",
    "llm.input_messages": "secret",
    "input.value": "secret",
    "output.value": "secret",
}


def raw(attributes: dict, name: str = "span", error: bool = False, duration_ms: int = 250) -> RawSpan:
    return RawSpan(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id="", name=name,
        start_ns=1_000_000_000, end_ns=1_000_000_000 + duration_ms * 1_000_000,
        status_error=error, status_message="", attributes=attributes, resource={},
    )


def test_semconv_chat_span():
    span = normalize(raw({
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-opus-5",
        "gen_ai.response.model": "claude-opus-5-20260801",
        "gen_ai.usage.input_tokens": 1500,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "gen_ai.usage.cache_write.input_tokens": 100,
        "gen_ai.conversation.id": "conv-1",
        **CONTENT,
    }, name="chat claude-opus-5"))

    assert span is not None
    assert (span.kind, span.convention) == ("llm", "otel-genai")
    assert span.model == "claude-opus-5-20260801"
    assert (span.input_tokens, span.output_tokens) == (400, 200)
    assert (span.cache_read_tokens, span.cache_write_tokens) == (1000, 100)
    assert (span.conversation_id, span.failed, span.duration_ms) == ("conv-1", False, 250)


@pytest.mark.parametrize(("operation", "kind"), [
    ("generate_content", "llm"), ("text_completion", "llm"),
    ("execute_tool", "tool"), ("invoke_agent", "agent"), ("invoke_workflow", "agent"),
])
def test_semconv_operation_kinds(operation, kind):
    span = normalize(raw({"gen_ai.operation.name": operation}))
    assert span is not None and span.kind == kind


def test_semconv_tool_and_agent_names():
    tool = normalize(raw({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
                          "gen_ai.tool.call.id": "call_9", "error.type": "timeout"}))
    agent = normalize(raw({"gen_ai.operation.name": "invoke_agent"}, name="invoke_agent billing"))
    workflow = normalize(raw({"gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "refunds"}))

    assert tool is not None and (tool.name, tool.tool_call_id, tool.failed) == ("lookup_order", "call_9", True)
    assert agent is not None and agent.name == "billing"
    assert workflow is not None and workflow.name == "refunds"


def test_openinference_llm_span():
    span = normalize(raw({
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-4o",
        "llm.token_count.prompt": 900,
        "llm.token_count.completion": 50,
        "llm.token_count.prompt_details.cache_read": 400,
        "session.id": "sess-7",
        **CONTENT,
    }, error=True))

    assert span is not None
    assert (span.kind, span.convention, span.model) == ("llm", "openinference", "gpt-4o")
    assert (span.input_tokens, span.output_tokens, span.cache_read_tokens) == (500, 50, 400)
    assert (span.conversation_id, span.failed) == ("sess-7", True)


@pytest.mark.parametrize(("kind", "expected"), [("TOOL", "tool"), ("AGENT", "agent"), ("CHAIN", "agent"), ("llm", "llm")])
def test_openinference_kinds(kind, expected):
    span = normalize(raw({"openinference.span.kind": kind, "tool.name": "search", "tool.id": "t1", "agent.name": "planner"}))
    assert span is not None and span.kind == expected


def test_openinference_names():
    tool = normalize(raw({"openinference.span.kind": "TOOL", "tool.name": "search", "tool_call.id": "tc_1"}))
    agent = normalize(raw({"openinference.span.kind": "AGENT"}, name="AgentExecutor"))
    assert tool is not None and (tool.name, tool.tool_call_id) == ("search", "tc_1")
    assert agent is not None and agent.name == "AgentExecutor"


@pytest.mark.parametrize("attributes", [
    {},
    {"http.method": "POST"},
    {"gen_ai.operation.name": "embeddings"},
    {"gen_ai.operation.name": "create_agent"},
    {"openinference.span.kind": "RETRIEVER"},
    {"openinference.span.kind": "EMBEDDING"},
])
def test_non_genai_spans_are_ignored(attributes):
    assert normalize(raw(attributes)) is None


def test_no_content_attributes_survive_normalization():
    span = normalize(raw({"gen_ai.operation.name": "chat", **CONTENT}))
    assert span is not None
    values = {k: v for k, v in dataclasses.asdict(span).items() if k != "raw"}
    assert not any("secret" in str(v) or "4111" in str(v) for v in values.values())


def test_garbage_token_counts_become_zero():
    span = normalize(raw({"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": "lots",
                          "gen_ai.usage.output_tokens": None}))
    assert span is not None and (span.input_tokens, span.output_tokens) == (0, 0)


@pytest.mark.parametrize(("model", "args", "usd"), [
    ("claude-opus-5", (1_000_000, 1_000_000), 30.0),
    ("claude-opus-4-8-20260101", (1_000_000, 0), 5.0),
    ("claude-sonnet-5", (0, 1_000_000), 10.0),
    ("claude-haiku-4-5", (1_000_000, 0), 1.0),
    ("gpt-4o-mini", (1_000_000, 0), 0.15),
    ("gpt-4o", (1_000_000, 0), 2.5),
    ("mystery-model", (1_000_000, 1_000_000), 0.0),
    ("", (1_000_000, 1_000_000), 0.0),
])
def test_pricing(model, args, usd):
    assert estimate_cost(model, *args) == pytest.approx(usd)


def test_pricing_cache_multipliers():
    assert estimate_cost("claude-opus-5", 0, 0, 1_000_000, 1_000_000) == pytest.approx(0.5 + 6.25)
    assert estimate_cost("claude-fable-5-1", 0, 0, 1_000_000) == pytest.approx(0.25)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_otlp_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.otlp.normalize'`

- [ ] **Step 3: Implement**

```python
# server/app/otlp/pricing.py
"""
Model prices for OTLP-ingested runs.

A copy of the SDK's tables (agent_kit/providers/anthropic.py, openai.py) — the
server doesn't depend on the SDK. Keep the two in step when prices change.
"""

from __future__ import annotations

# USD per million tokens (input, output). Longest matching prefix wins.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5":    (10.00, 50.00),
    "claude-mythos-5":   (10.00, 50.00),
    "claude-opus-5":     (5.00,  25.00),
    "claude-opus-4-8":   (5.00,  25.00),
    "claude-opus-4-7":   (5.00,  25.00),
    "claude-opus-4-6":   (5.00,  25.00),
    "claude-opus-4-5":   (5.00,  25.00),
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-5":   (2.00,  10.00),
    "claude-sonnet-4":   (3.00,  15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-3-7-sonnet": (3.00,  15.00),
    "claude-3-5-sonnet": (3.00,  15.00),
    "claude-3-5-haiku":  (0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-haiku":    (0.25,  1.25),
    "gpt-4o":            (2.50,  10.00),
    "gpt-4o-mini":       (0.15,  0.60),
    "gpt-4-turbo":       (10.00, 30.00),
    "gpt-4":             (30.00, 60.00),
    "gpt-3.5-turbo":     (0.50,  1.50),
    "o1":                (15.00, 60.00),
    "o1-mini":           (3.00,  12.00),
}

_CACHE_READ_MULTIPLIER: dict[str, float] = {"claude-fable-5-1": 0.025}
_CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD for one model call; 0.0 for unpriced models. ``input_tokens`` excludes cached tokens."""
    matches = [prefix for prefix in _PRICES if model and model.startswith(prefix)]
    if not matches:
        return 0.0
    in_rate, out_rate = _PRICES[max(matches, key=len)]
    read_multiplier = next(
        (m for prefix, m in _CACHE_READ_MULTIPLIER.items() if model.startswith(prefix)), 0.1
    )
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * in_rate * read_multiplier
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
```

```python
# server/app/otlp/normalize.py
"""
Map GenAI spans onto one shape.

Supports OpenTelemetry GenAI semantic conventions (``gen_ai.*``) and OpenInference
(``openinference.span.kind``). Only names, counts, identifiers and status are
copied — prompt, completion, and tool content attributes are never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.otlp.decode import RawSpan

_SEMCONV_KINDS = {
    "chat": "llm",
    "generate_content": "llm",
    "text_completion": "llm",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "invoke_workflow": "agent",
}
_OPENINFERENCE_KINDS = {"LLM": "llm", "TOOL": "tool", "AGENT": "agent", "CHAIN": "agent"}


@dataclass
class GenAISpan:
    raw: RawSpan
    kind: str  # "llm" | "tool" | "agent"
    convention: str  # "otel-genai" | "openinference"
    name: str
    model: str
    input_tokens: int  # uncached input
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_call_id: str
    conversation_id: str
    failed: bool

    @property
    def duration_ms(self) -> int:
        return max(0, (self.raw.end_ns - self.raw.start_ns) // 1_000_000)


def normalize(span: RawSpan) -> GenAISpan | None:
    """Return the GenAI view of a span, or None if neither convention applies."""
    operation = span.attributes.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation in _SEMCONV_KINDS:
        return _semconv(span, _SEMCONV_KINDS[operation])
    kind = span.attributes.get("openinference.span.kind")
    if isinstance(kind, str) and kind.upper() in _OPENINFERENCE_KINDS:
        return _openinference(span, _OPENINFERENCE_KINDS[kind.upper()])
    return None


def _semconv(span: RawSpan, kind: str) -> GenAISpan:
    a = span.attributes
    if kind == "agent":
        name = _str(a.get("gen_ai.agent.name")) or _str(a.get("gen_ai.workflow.name")) or _target(span.name)
    elif kind == "tool":
        name = _str(a.get("gen_ai.tool.name")) or _target(span.name)
    else:
        name = ""
    cache_read = _int(a.get("gen_ai.usage.cache_read.input_tokens"))
    cache_write = _int(a.get("gen_ai.usage.cache_write.input_tokens"))
    return GenAISpan(
        raw=span,
        kind=kind,
        convention="otel-genai",
        name=name,
        model=_str(a.get("gen_ai.response.model")) or _str(a.get("gen_ai.request.model")),
        input_tokens=max(0, _int(a.get("gen_ai.usage.input_tokens")) - cache_read - cache_write),
        output_tokens=_int(a.get("gen_ai.usage.output_tokens")),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        tool_call_id=_str(a.get("gen_ai.tool.call.id")),
        conversation_id=_str(a.get("gen_ai.conversation.id")),
        failed=span.status_error or bool(a.get("error.type")),
    )


def _openinference(span: RawSpan, kind: str) -> GenAISpan:
    a = span.attributes
    if kind == "agent":
        name = _str(a.get("agent.name")) or span.name
    elif kind == "tool":
        name = _str(a.get("tool.name")) or span.name
    else:
        name = ""
    cache_read = _int(a.get("llm.token_count.prompt_details.cache_read"))
    cache_write = _int(a.get("llm.token_count.prompt_details.cache_write"))
    return GenAISpan(
        raw=span,
        kind=kind,
        convention="openinference",
        name=name,
        model=_str(a.get("llm.model_name")),
        input_tokens=max(0, _int(a.get("llm.token_count.prompt")) - cache_read - cache_write),
        output_tokens=_int(a.get("llm.token_count.completion")),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        tool_call_id=_str(a.get("tool_call.id")) or _str(a.get("tool.id")),
        conversation_id=_str(a.get("session.id")),
        failed=span.status_error,
    )


def _target(span_name: str) -> str:
    """'invoke_agent billing' → 'billing'; names without an operation prefix pass through."""
    _, _, rest = span_name.partition(" ")
    return rest or span_name


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest tests/test_otlp_normalize.py -v && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/otlp/normalize.py server/app/otlp/pricing.py server/tests/test_otlp_normalize.py
git commit -m "feat(server): normalize GenAI semconv and OpenInference spans"
```

---

### Task 4: Run assembly and `POST /v1/traces`

**Files:**
- Create: `server/app/otlp/assembler.py`, `server/app/routers/otlp.py`, `server/tests/test_otlp_ingest.py`
- Modify: `server/app/main.py`

**Interfaces:**
- Consumes: Tasks 1–3; `app.routers.ingest._process_event(raw: dict, org_id: str, db)`.
- Produces: `assembler.run_id_for_trace(trace_id: str) -> str`, `assembler.IDLE_TIMEOUT`, `async assemble(spans, org_id, db, now=None) -> AssembleResult(runs_started, runs_finished)`; route `POST /v1/traces`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_otlp_ingest.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_otlp_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.otlp.assembler'`

- [ ] **Step 3: Implement**

```python
# server/app/otlp/assembler.py
"""
Assemble OTLP GenAI spans into agent-kit runs.

One trace is one run. Spans extend a server-built audit chain and drive the same
ingest handlers SDK events use, so fleet metrics, alerting, and support context
see OTLP runs like any other. See specs/08-otlp-ingest.md for the lifecycle.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_chain import GENESIS_ROOT, append_event, verify_chain
from app.models import ActiveRunCache, AuditEvent, AuditRun
from app.otlp.decode import RawSpan
from app.otlp.normalize import GenAISpan, normalize
from app.otlp.pricing import estimate_cost
from app.routers.ingest import _process_event

IDLE_TIMEOUT = timedelta(minutes=5)

_RUN_NAMESPACE = uuid.UUID("0b6f4d2a-7c1e-4e3b-9a4f-3d2c1b0a9e8f")
_EVENT_TYPES = {"llm": "llm_complete", "tool": "tool_call", "agent": "agent_invoke"}


def run_id_for_trace(trace_id: str) -> str:
    """Trace IDs are 32 hex chars; run IDs are UUIDs."""
    return str(uuid.uuid5(_RUN_NAMESPACE, trace_id))


@dataclass
class AssembleResult:
    runs_started: list[str] = field(default_factory=list)
    runs_finished: list[str] = field(default_factory=list)


async def assemble(
    spans: list[RawSpan], org_id: str, db: AsyncSession, now: datetime | None = None
) -> AssembleResult:
    """Finalize idle runs, then apply ``spans`` trace by trace in end-time order."""
    result = AssembleResult()
    current = now or datetime.utcnow()
    await _finalize_idle_runs(org_id, db, current, result)

    by_trace: dict[str, list[RawSpan]] = defaultdict(list)
    for span in sorted(spans, key=lambda s: s.end_ns):
        by_trace[span.trace_id].append(span)
    for trace_id, trace_spans in by_trace.items():
        await _apply_trace(trace_id, trace_spans, org_id, db, current, result)
    return result


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


async def _apply_trace(
    trace_id: str,
    spans: list[RawSpan],
    org_id: str,
    db: AsyncSession,
    now: datetime,
    result: AssembleResult,
) -> None:
    run_id = run_id_for_trace(trace_id)
    normalized = [(span, normalize(span)) for span in spans]
    genai = [g for _, g in normalized if g is not None]

    run = await _get_run(db, run_id)
    if run is not None and run.org_id != org_id:
        return
    if run is None and not genai:
        return
    cache = await _get_cache(db, run_id) if run is not None else None

    late_events = False
    for span, g in normalized:
        if g is not None:
            if run is None:
                run, cache = await _start_run(run_id, trace_id, g, genai, org_id, db)
                result.runs_started.append(run_id)
            applied = await _apply_span(run, cache, g, db, now)
            late_events = late_events or (applied and run.completed_at is not None)
        if not span.parent_span_id and run is not None and run.completed_at is None:
            await _finish_run(run, cache, db, _timestamp(span.end_ns), root=span)
            cache = None
            result.runs_finished.append(run_id)
            late_events = False

    if run is not None and late_events:
        await _verify(run, db)


async def _start_run(
    run_id: str,
    trace_id: str,
    first: GenAISpan,
    batch: list[GenAISpan],
    org_id: str,
    db: AsyncSession,
) -> tuple[AuditRun, ActiveRunCache | None]:
    resource = first.raw.resource
    project = _str(resource.get("agentkit.project")) or "default"
    agent_name = _str(resource.get("service.name")) or "otel-agent"
    model = next((g.model for g in batch if g.kind == "llm" and g.model), "")
    started_at = _timestamp(min(g.raw.start_ns for g in batch))
    context = {
        "harness": first.convention,
        "trace_id": trace_id,
        "conversation_id": first.conversation_id,
    }

    run = AuditRun(
        org_id=org_id,
        project=project,
        agent_name=agent_name,
        run_id=run_id,
        genesis_root=GENESIS_ROOT,
        final_root_hash=GENESIS_ROOT,
        event_count=0,
        started_at=started_at,
        integrity="pending",
        chain_origin="ingest",
    )
    db.add(run)
    db.add(
        append_event(
            run,
            event_id=_event_id(run_id, "agent_start"),
            event_type="agent_start",
            actor=run_id,
            payload=context,
            timestamp=started_at,
        )
    )
    await _process_event(
        {
            "event_id": _event_id(run_id, "run_start"),
            "event_type": "run_start",
            "run_id": run_id,
            "agent_name": agent_name,
            "project": project,
            "occurred_at": started_at.isoformat(),
            "payload": {"model": model, "prompt_hash": "", **context},
        },
        org_id,
        db,
    )
    return run, await _get_cache(db, run_id)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


async def _apply_span(
    run: AuditRun, cache: ActiveRunCache | None, span: GenAISpan, db: AsyncSession, now: datetime
) -> bool:
    """Append the span's audit event; update metrics while the run is active. False if already applied."""
    event_type = _EVENT_TYPES[span.kind]
    event_id = _event_id(run.run_id, f"{span.raw.span_id}:{event_type}")
    if await _event_exists(db, event_id):
        return False

    cost = (
        estimate_cost(span.model, span.input_tokens, span.output_tokens, span.cache_read_tokens, span.cache_write_tokens)
        if span.kind == "llm"
        else 0.0
    )
    db.add(
        append_event(
            run,
            event_id=event_id,
            event_type=event_type,
            actor=_actor(span),
            payload=_audit_payload(span, cost),
            timestamp=_timestamp(span.raw.end_ns),
        )
    )
    if cache is None:  # run already finished: a late span extends the chain only
        return True

    cache.last_event_at = now
    if span.kind == "llm":
        if not cache.model and span.model:
            cache.model = span.model
        await _process_event(
            {
                "event_id": _event_id(run.run_id, f"{span.raw.span_id}:turn"),
                "event_type": "turn_complete",
                "run_id": run.run_id,
                "agent_name": cache.agent_name,
                "project": cache.project,
                "occurred_at": _timestamp(span.raw.end_ns).isoformat(),
                "payload": {
                    "turn_index": cache.turns_so_far,
                    "input_tokens": span.input_tokens,
                    "output_tokens": span.output_tokens,
                    "cost_usd": cost,
                    "duration_ms": span.duration_ms,
                    "tool_names": [],
                },
            },
            run.org_id,
            db,
        )
    elif span.kind == "agent":
        # Outer spans end after the spans they contain, so the latest agent span
        # seen is the outermost one so far: it names the run and decides failure.
        if span.name:
            run.agent_name = span.name
            cache.agent_name = span.name
        cache.failure_message = f"agent {span.name or 'span'} failed" if span.failed else None
    return True


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


async def _finish_run(
    run: AuditRun,
    cache: ActiveRunCache | None,
    db: AsyncSession,
    finished_at: datetime,
    root: RawSpan | None,
) -> None:
    failure: tuple[str, str] | None = None
    if root is not None and root.status_error:
        failure = ("TraceError", root.status_message or f"root span {root.name!r} failed")
    elif cache is not None and cache.failure_message:
        failure = ("AgentError", cache.failure_message)

    turns = cache.turns_so_far if cache else 0
    cost = cache.cost_so_far_usd if cache else 0.0
    tokens = (cache.input_tokens + cache.output_tokens) if cache else 0
    base = {
        "run_id": run.run_id,
        "agent_name": run.agent_name,
        "project": run.project,
        "occurred_at": finished_at.isoformat(),
    }

    if failure is not None:
        error_type, message = failure
        message = message[:500]
        db.add(
            append_event(
                run,
                event_id=_event_id(run.run_id, "agent_error"),
                event_type="agent_error",
                actor=run.run_id,
                payload={"error_type": error_type, "error_message": message},
                timestamp=finished_at,
            )
        )
        await _process_event(
            {
                **base,
                "event_id": _event_id(run.run_id, "run_error"),
                "event_type": "run_error",
                "payload": {"error_type": error_type, "error_message": message, "turn_count": turns},
            },
            run.org_id,
            db,
        )
    else:
        db.add(
            append_event(
                run,
                event_id=_event_id(run.run_id, "agent_complete"),
                event_type="agent_complete",
                actor=run.run_id,
                payload={"turns": turns, "total_tokens": tokens, "total_cost_usd": cost},
                timestamp=finished_at,
            )
        )
        await _process_event(
            {
                **base,
                "event_id": _event_id(run.run_id, "run_complete"),
                "event_type": "run_complete",
                "payload": {
                    "total_cost_usd": cost,
                    "total_tokens": tokens,
                    "total_turns": turns,
                    "audit_root_hash": run.final_root_hash,
                },
            },
            run.org_id,
            db,
        )

    run.completed_at = run.completed_at or finished_at
    await _verify(run, db)


async def _finalize_idle_runs(
    org_id: str, db: AsyncSession, now: datetime, result: AssembleResult
) -> None:
    rows = await db.execute(
        select(ActiveRunCache, AuditRun)
        .join(AuditRun, AuditRun.run_id == ActiveRunCache.run_id)
        .where(
            ActiveRunCache.org_id == org_id,
            AuditRun.chain_origin == "ingest",
            ActiveRunCache.last_event_at < now - IDLE_TIMEOUT,
        )
    )
    for cache, run in rows.all():
        await _finish_run(run, cache, db, cache.last_event_at or now, root=None)
        result.runs_finished.append(run.run_id)


async def _verify(run: AuditRun, db: AsyncSession) -> None:
    events = list(
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.run_id == run.run_id).order_by(AuditEvent.seq)
            )
        ).scalars().all()
    )
    ok, *_ = verify_chain(events)
    run.integrity = "verified" if ok else "failed"
    for event in events:
        event.verified = ok
    if not ok:
        try:
            from app.alerting.evaluator import fire_audit_integrity_failure

            await fire_audit_integrity_failure(
                org_id=run.org_id, agent_name=run.agent_name, project=run.project, run_id=run.run_id, db=db
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_run(db: AsyncSession, run_id: str) -> AuditRun | None:
    return (await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))).scalar_one_or_none()


async def _get_cache(db: AsyncSession, run_id: str) -> ActiveRunCache | None:
    return (
        await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run_id))
    ).scalar_one_or_none()


async def _event_exists(db: AsyncSession, event_id: str) -> bool:
    found = await db.execute(select(AuditEvent.id).where(AuditEvent.event_id == event_id))
    return found.first() is not None


def _event_id(run_id: str, key: str) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), key))


def _timestamp(ns: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(microseconds=ns // 1000)


def _actor(span: GenAISpan) -> str:
    if span.kind == "llm":
        return span.model or "llm"
    return span.name or span.kind


def _audit_payload(span: GenAISpan, cost: float) -> dict[str, Any]:
    return {
        "span_id": span.raw.span_id,
        "kind": span.kind,
        "name": span.name,
        "model": span.model,
        "input_tokens": span.input_tokens,
        "output_tokens": span.output_tokens,
        "cache_read_tokens": span.cache_read_tokens,
        "cache_write_tokens": span.cache_write_tokens,
        "cost_usd": cost,
        "tool_call_id": span.tool_call_id,
        "success": not span.failed,
        "duration_ms": span.duration_ms,
    }


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
```

```python
# server/app/routers/otlp.py
"""POST /v1/traces — OTLP/HTTP trace ingest for GenAI spans."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.database import get_db
from app.models import Organization
from app.otlp.assembler import assemble
from app.otlp.decode import JSON, PROTOBUF, DecodeError, decode_request, encode_response

router = APIRouter(tags=["otlp"])


@router.post("/v1/traces")
async def export_traces(
    request: Request,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Accept an OTLP/HTTP trace export (protobuf or JSON, optionally gzip).

    GenAI spans (OpenTelemetry GenAI semantic conventions or OpenInference) become
    agent-kit runs; other spans are accepted and ignored.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in (PROTOBUF, JSON):
        return Response(status_code=415)

    try:
        batch = decode_request(
            await request.body(), content_type, request.headers.get("content-encoding", "")
        )
    except DecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        await assemble(batch.spans, org.id, db)
        await db.commit()
    except IntegrityError:
        # Another request extended the same run concurrently; exporters retry 5xx.
        await db.rollback()
        return Response(status_code=503, headers={"Retry-After": "1"})

    return Response(
        content=encode_response(content_type, batch.rejected, batch.error_message),
        media_type=content_type,
    )
```

`server/app/main.py`:

```python
from app.routers import alerts, audit, ingest, metrics, otlp, support
...
app.include_router(otlp.router)
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest tests/test_otlp_ingest.py -v && pytest && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/otlp/assembler.py server/app/routers/otlp.py server/app/main.py server/tests/test_otlp_ingest.py
git commit -m "feat(server): POST /v1/traces assembles OTLP GenAI spans into runs"
```

---

### Task 5: Docs and end-to-end check

**Files:**
- Modify: `docs/api-reference.md`, `docs/cloud-quickstart.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/08-otlp-ingest.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: API reference** — new section after `## Ingest`:

```markdown
### POST /v1/traces

OTLP/HTTP trace export. Point any OpenTelemetry exporter at agent-kit:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.agentkit.io
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer akt_live_..."
export OTEL_RESOURCE_ATTRIBUTES="agentkit.project=support"
```

- `Content-Type: application/x-protobuf` or `application/json`; `Content-Encoding: gzip` optional.
- Spans following the OpenTelemetry GenAI semantic conventions (`gen_ai.operation.name`) or OpenInference (`openinference.span.kind`) become runs: one trace, one run. Other spans are accepted and ignored.
- `chat` / `generate_content` / `text_completion` / `LLM` spans are turns (tokens, cost); `execute_tool` / `TOOL` are tool calls; `invoke_agent` / `invoke_workflow` / `AGENT` / `CHAIN` name the run.
- A run completes when the trace's root span arrives, or after 5 minutes without new spans. A failed root span or outermost agent span records a run error.
- The audit chain is built at ingest: runs show `"chain_origin": "ingest"`.
- Prompt, completion, and tool content on spans is never stored.

**Response** `200` — `ExportTraceServiceResponse` in the request's encoding; malformed spans are reported in `partial_success.rejected_spans`. `400` undecodable body · `401` bad key · `415` unsupported content type · `503` concurrent write, retry.
```

Also add `chain_origin` to the `GET /v1/audit/runs` response example and field list.

- [ ] **Step 2: Cloud quickstart** — section `## Any OpenTelemetry-instrumented agent` before `## What is NOT sent to the cloud`, with the three environment variables, the two conventions, `chain_origin` semantics, and the 5-minute idle completion note.

- [ ] **Step 3: CHANGELOG `[Unreleased]` → `### Added`**

```markdown
- **OTLP trace ingest (`POST /v1/traces`).** Any OpenTelemetry-instrumented agent — GenAI semantic conventions or OpenInference, any language — reports runs, tool calls, tokens, cost, and failures to agent-kit Cloud with a standard OTLP/HTTP exporter. The audit chain is built at ingest and flagged `chain_origin: "ingest"`; span content is never stored. Migration `005` adds `audit_runs.chain_origin`, `active_run_cache.last_event_at`, and `active_run_cache.failure_message`.
```

- [ ] **Step 4: Specs and index**

`specs/08-otlp-ingest.md`: status `implemented`; units table migration row adds `active_run_cache.failure_message VARCHAR(500) NULL`; lifecycle step 4 notes the outermost agent span's failure is kept in `failure_message`.
`specs/06-harness-roadmap.md`: tick `3.1b`.
`PROJECT_INDEX.md`: `server/app/otlp/` tree entries, `routers/otlp.py`, migration `005`, four new server test files, spec 08 in the docs table, `POST /v1/traces` in the server API section.

- [ ] **Step 5: End-to-end against a running server, then commit**

Run a local server on a fresh migrated SQLite database, seed an API key, export spans with the real `opentelemetry-sdk` `BatchSpanProcessor` + `OTLPSpanExporter` (HTTP, protobuf) pointed at it, force-flush, then confirm `GET /v1/audit/runs` shows `chain_origin: "ingest"` + `integrity: "verified"` and `GET /v1/metrics/summary` counts the run with tokens and cost.

```bash
git add docs CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: OTLP trace ingest reference, quickstart, and changelog"
```
