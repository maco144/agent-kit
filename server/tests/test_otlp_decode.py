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
