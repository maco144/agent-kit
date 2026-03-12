"""Shared fixtures for agent-kit tests."""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

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


@pytest.fixture
def mock_provider():
    return MockProvider()


@pytest.fixture
def mock_provider_factory():
    def factory(responses: list[str]) -> MockProvider:
        return MockProvider(responses)
    return factory
