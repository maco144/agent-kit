"""
AgentTracer — structured observability with zero mandatory dependencies.

Three backends:
- "noop"    (default) — all calls are no-ops; zero overhead
- "console" — structured JSON to stdout; zero extra deps
- "otlp"    — full OpenTelemetry; requires agent-kit[otel]
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

from agent_kit.types import SpanKind


class Span:
    """A tracing span. Used as a context manager."""

    def __init__(
        self,
        name: str,
        kind: SpanKind,
        trace_id: str,
        span_id: str,
        attributes: dict[str, Any],
        backend: str,
        _otel_span: Any = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes = dict(attributes)
        self._backend = backend
        self._otel_span = _otel_span
        self._start = time.monotonic()
        self._events: list[dict[str, Any]] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
        if self._otel_span:
            self._otel_span.set_attribute(key, value)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        event = {"name": name, "attributes": attributes or {}, "ts": datetime.utcnow().isoformat()}
        self._events.append(event)
        if self._otel_span:
            self._otel_span.add_event(name, attributes=attributes or {})

    def end(self) -> None:
        duration_ms = int((time.monotonic() - self._start) * 1000)
        if self._backend == "console":
            record = {
                "span": self.name,
                "kind": self.kind.value,
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "duration_ms": duration_ms,
                "attributes": self.attributes,
            }
            if self._events:
                record["events"] = self._events
            print(json.dumps(record), file=sys.stderr)
        if self._otel_span:
            self._otel_span.end()

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, *_: Any) -> None:
        self.end()


class _NoopSpan(Span):
    """Zero-overhead span for the noop backend."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def end(self) -> None:
        pass


class AgentTracer:
    """
    Thin tracing abstraction over three backends.

    Usage::

        tracer = AgentTracer()                      # noop — zero deps
        tracer = AgentTracer(backend="console")     # JSON to stderr
        tracer = AgentTracer(backend="otlp",        # OpenTelemetry OTLP
                             service_name="my-agent",
                             endpoint="http://localhost:4317")
    """

    def __init__(
        self,
        backend: str = "noop",
        service_name: str = "agent-kit",
        endpoint: str | None = None,
    ) -> None:
        self._backend = backend
        self._trace_id = str(uuid.uuid4())
        self._total_cost_usd = 0.0
        self._total_tokens = 0

        self._otel_tracer: Any = None
        if backend == "otlp":
            self._otel_tracer = self._init_otel(service_name, endpoint)

    def _init_otel(self, service_name: str, endpoint: str | None) -> Any:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource(attributes={SERVICE_NAME: service_name})
            provider = TracerProvider(resource=resource)
            exporter_kwargs: dict[str, Any] = {}
            if endpoint:
                exporter_kwargs["endpoint"] = endpoint
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**exporter_kwargs)))
            trace.set_tracer_provider(provider)
            return trace.get_tracer(service_name)
        except ImportError as e:
            raise ImportError(
                "OTLP backend requires opentelemetry packages. "
                "Install with: pip install agent-kit[otel]"
            ) from e

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.AGENT,
        **attributes: Any,
    ) -> Span:
        if self._backend == "noop":
            return _NoopSpan(
                name=name,
                kind=kind,
                trace_id=self._trace_id,
                span_id=str(uuid.uuid4()),
                attributes=attributes,
                backend="noop",
            )
        span_id = str(uuid.uuid4())
        otel_span = None
        if self._otel_tracer:
            from opentelemetry import trace
            otel_span = self._otel_tracer.start_span(
                name,
                kind=trace.SpanKind.CLIENT,
                attributes={k: str(v) for k, v in attributes.items()},
            )
        return Span(
            name=name,
            kind=kind,
            trace_id=self._trace_id,
            span_id=span_id,
            attributes=attributes,
            backend=self._backend,
            _otel_span=otel_span,
        )

    @contextmanager
    def span(
        self, name: str, kind: SpanKind = SpanKind.AGENT, **attributes: Any
    ) -> Generator[Span, None, None]:
        s = self.start_span(name, kind, **attributes)
        try:
            yield s
        finally:
            s.end()

    def record_cost(self, tokens: int, model: str, usd: float) -> None:
        self._total_tokens += tokens
        self._total_cost_usd += usd
        if self._backend == "console":
            print(
                json.dumps({
                    "cost_event": True,
                    "tokens": tokens,
                    "model": model,
                    "usd": round(usd, 6),
                    "cumulative_usd": round(self._total_cost_usd, 6),
                }),
                file=sys.stderr,
            )

    def record_tool_call(self, tool_name: str, duration_ms: int, success: bool) -> None:
        if self._backend == "console":
            print(
                json.dumps({
                    "tool_call": tool_name,
                    "duration_ms": duration_ms,
                    "success": success,
                }),
                file=sys.stderr,
            )

    def cumulative_cost_usd(self) -> float:
        return self._total_cost_usd

    def cumulative_tokens(self) -> int:
        return self._total_tokens
