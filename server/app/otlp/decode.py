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
        return response.SerializeToString()
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
