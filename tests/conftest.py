"""Shared fixtures for agent-kit tests."""

from __future__ import annotations

import hashlib
from typing import Any, AsyncIterator

import pytest

from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, Turn


class MockProvider:
    """Deterministic mock provider for tests — no network calls."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.config = ProviderConfig(default_model="mock-model")
        self._responses = list(responses or ["Mock response."])
        self._call_count = 0
        self._calls: list[dict[str, Any]] = []

    def name(self) -> str:
        return "mock"

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: Any = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Turn:
        idx = min(self._call_count, len(self._responses) - 1)
        response_text = self._responses[idx]
        self._call_count += 1
        self._calls.append({"messages": messages, "model": model, "tools": tools})
        return Turn(
            messages_in=messages,
            message_out=Message(role="assistant", content=response_text),
            cost=CostSummary(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cost_usd=0.0001,
                model=model or "mock-model",
            ),
            duration_ms=1,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        idx = min(self._call_count, len(self._responses) - 1)
        for word in self._responses[idx].split():
            yield word + " "


@pytest.fixture(autouse=True)
def _agentkit_base_url(monkeypatch):
    """CloudReporter has no default server; tests that build one point it at a placeholder."""
    monkeypatch.setenv("AGENTKIT_BASE_URL", "http://agentkit.test")


@pytest.fixture(autouse=True)
def _no_atexit_flush(monkeypatch):
    """Reporters built in tests must not try to ship leftover events to the placeholder URL at exit."""
    monkeypatch.setattr(CloudReporter, "_flush_sync", lambda self: None)


@pytest.fixture
def mock_provider():
    return MockProvider()


@pytest.fixture
def mock_provider_factory():
    def factory(responses: list[str]) -> MockProvider:
        return MockProvider(responses)
    return factory




class CloudCapture:
    """Collects CloudEvents a reporter would send; verifies flushed audit chains."""

    def __init__(self, reporter: CloudReporter) -> None:
        self.reporter = reporter
        self.events: list[CloudEvent] = []

    def types(self) -> list[str]:
        return [e.event_type.value for e in self.events]

    def of(self, event_type: str) -> list[CloudEvent]:
        return [e for e in self.events if e.event_type.value == event_type]

    def flush_payload(self, run_id: str) -> dict[str, Any]:
        (flush,) = [e for e in self.of("audit_flush") if e.run_id == run_id]
        return flush.payload

    def audit_types(self, run_id: str) -> list[str]:
        return [e["event_type"] for e in self.flush_payload(run_id)["events"]]

    def assert_chain_intact(self, run_id: str) -> None:
        """Re-derive every link exactly as server/app/audit_chain.py does."""
        payload = self.flush_payload(run_id)
        root = "0" * 64
        for e in payload["events"]:
            expected = hashlib.sha256(
                (root + e["event_type"] + e["payload_hash"] + e["timestamp"]).encode()
            ).hexdigest()
            assert e["prev_root"] == root
            assert e["leaf_hash"] == expected
            root = e["leaf_hash"]
        assert root == payload["final_root_hash"]
        assert payload["event_count"] == len(payload["events"])


@pytest.fixture
def cloud_capture(monkeypatch):
    reporter = CloudReporter(api_key="akt_test", project="proj")
    capture = CloudCapture(reporter)
    monkeypatch.setattr(reporter, "submit_threadsafe", capture.events.append)
    return capture
