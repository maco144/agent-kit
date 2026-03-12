"""Tests for CircuitBreaker state machine."""

from __future__ import annotations

import pytest

from agent_kit.exceptions import CircuitOpenError
from agent_kit.reliability import CircuitBreaker, CircuitBreakerConfig, CircuitState


@pytest.mark.asyncio
async def test_circuit_starts_closed():
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold():
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3, recovery_timeout_s=999))

    async def fail():
        raise ValueError("boom")

    for _ in range(3):
        with pytest.raises(ValueError):
            await cb.call(fail)

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_open_rejects_calls():
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=999))

    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await cb.call(fail)

    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await cb.call(fail)


@pytest.mark.asyncio
async def test_circuit_passes_success():
    cb = CircuitBreaker("test")

    async def succeed():
        return 42

    result = await cb.call(succeed)
    assert result == 42
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_reset():
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1))

    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await cb.call(fail)

    assert cb.state == CircuitState.OPEN
    cb.reset()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_half_open_closes_on_success():
    """After recovery_timeout, circuit goes HALF_OPEN and closes on success."""
    import asyncio

    cb = CircuitBreaker(
        "test",
        CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout_s=0.01,  # 10ms so test is fast
            success_threshold=1,
        ),
    )

    async def fail():
        raise ValueError("boom")

    async def succeed():
        return "ok"

    with pytest.raises(ValueError):
        await cb.call(fail)

    assert cb.state == CircuitState.OPEN

    await asyncio.sleep(0.02)  # let recovery timeout elapse

    # Next call should be allowed (HALF_OPEN) and succeed → CLOSED
    result = await cb.call(succeed)
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_stats():
    cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=5))
    stats = cb.stats()
    assert stats.state == CircuitState.CLOSED
    assert stats.failure_count == 0
