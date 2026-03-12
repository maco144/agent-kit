"""Tests for with_retry."""

from __future__ import annotations

import pytest

from agent_kit.reliability import RetryPolicyConfig, with_retry
from agent_kit.types import BackoffConfig


@pytest.mark.asyncio
async def test_retry_succeeds_first_try():
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    result = await with_retry(fn, RetryPolicyConfig(max_attempts=3))
    assert result == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("transient")
        return "ok"

    policy = RetryPolicyConfig(
        max_attempts=3,
        backoff=BackoffConfig(initial_delay_s=0.001, jitter=False),
        retryable_on=["ConnectionError"],
    )
    result = await with_retry(fn, policy)
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retry_exhausts_all_attempts():
    calls = []

    async def fn():
        calls.append(1)
        raise ConnectionError("always fails")

    policy = RetryPolicyConfig(
        max_attempts=3,
        backoff=BackoffConfig(initial_delay_s=0.001, jitter=False),
        retryable_on=["ConnectionError"],
    )
    with pytest.raises(ConnectionError):
        await with_retry(fn, policy)

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_non_retryable():
    calls = []

    async def fn():
        calls.append(1)
        raise ValueError("non-retryable")

    policy = RetryPolicyConfig(
        max_attempts=5,
        retryable_on=["ConnectionError"],  # ValueError not in list
    )
    with pytest.raises(ValueError):
        await with_retry(fn, policy)

    assert len(calls) == 1  # no retry


@pytest.mark.asyncio
async def test_retry_sync_fn():
    calls = []

    def sync_fn():
        calls.append(1)
        return "sync_ok"

    result = await with_retry(sync_fn, RetryPolicyConfig())
    assert result == "sync_ok"
    assert len(calls) == 1
