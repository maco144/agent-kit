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


def recent_ns() -> int:
    """A few seconds ago, so a span's minute bucket is never ahead of the metrics window."""
    return time.time_ns() - 5_000_000_000


@dataclass
class SpanSpec:
    trace_id: str
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=new_span_id)
    parent: str = ""
    start_ns: int = field(default_factory=recent_ns)
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
