"""RetryPolicy and with_retry executor."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Callable

from agent_kit.types import BackoffConfig, RetryPolicyConfig


def _compute_delay(backoff: BackoffConfig, attempt: int) -> float:
    """Return delay in seconds for the given attempt number (0-indexed)."""
    delay = min(
        backoff.initial_delay_s * (backoff.multiplier ** attempt),
        backoff.max_delay_s,
    )
    if backoff.jitter:
        delay *= random.uniform(0.5, 1.5)
    return delay


def _is_retryable(exc: Exception, retryable_on: list[str]) -> bool:
    """
    Check if an exception is in the retryable set.

    We match by class name or qualified name to avoid hard import dependencies.
    """
    exc_type = type(exc)
    type_name = exc_type.__name__
    qualified_name = f"{exc_type.__module__}.{exc_type.__name__}"

    for pattern in retryable_on:
        # Match "ProviderError", "httpx.TimeoutException", etc.
        if pattern == type_name or pattern == qualified_name:
            return True
        # Also check MRO (base classes)
        for base in exc_type.__mro__:
            base_name = f"{base.__module__}.{base.__name__}"
            if pattern == base.__name__ or pattern == base_name:
                return True
    return False


async def with_retry(
    fn: Callable[..., Any],
    policy: RetryPolicyConfig,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Execute fn with retry logic defined by policy.

    - Retries only on exceptions listed in policy.retryable_on
    - Uses exponential backoff with optional jitter between attempts
    - Raises the last exception if all attempts fail

    Usage::

        result = await with_retry(my_async_fn, RetryPolicyConfig(max_attempts=3), arg1, arg2)
    """
    last_exc: Exception | None = None

    for attempt in range(policy.max_attempts):
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            else:
                return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc, policy.retryable_on):
                raise
            if attempt < policy.max_attempts - 1:
                delay = _compute_delay(policy.backoff, attempt)
                await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]
