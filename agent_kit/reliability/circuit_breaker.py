"""Circuit breaker — closed/open/half-open state machine."""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Callable, TypeVar

from agent_kit.exceptions import CircuitOpenError
from agent_kit.types import CircuitBreakerConfig

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"      # Normal operation — calls pass through
    OPEN = "open"          # Failing fast — all calls rejected immediately
    HALF_OPEN = "half_open"  # Probing — limited calls allowed to test recovery


class CircuitBreakerStats:
    """Snapshot of circuit breaker state for observability."""

    def __init__(
        self,
        state: CircuitState,
        failure_count: int,
        success_count: int,
        last_failure_at: float | None,
    ) -> None:
        self.state = state
        self.failure_count = failure_count
        self.success_count = success_count
        self.last_failure_at = last_failure_at

    def __repr__(self) -> str:
        return (
            f"CircuitBreakerStats(state={self.state.value}, "
            f"failures={self.failure_count}, "
            f"successes={self.success_count})"
        )


class CircuitBreaker:
    """
    Standard three-state circuit breaker.

    States:
    - CLOSED: all calls pass through; consecutive failures tracked
    - OPEN: all calls fail fast with CircuitOpenError; recovery timer running
    - HALF_OPEN: limited probe calls allowed; successes close, failures re-open

    Usage::

        cb = CircuitBreaker("anthropic", CircuitBreakerConfig(failure_threshold=5))
        result = await cb.call(my_async_fn, arg1, arg2)
    """

    def __init__(self, resource: str, config: CircuitBreakerConfig | None = None) -> None:
        self._resource = resource
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Call fn through the circuit breaker.

        Raises CircuitOpenError if the circuit is OPEN.
        Records success/failure and transitions state accordingly.
        """
        async with self._lock:
            self._maybe_attempt_recovery()
            if self._state == CircuitState.OPEN:
                raise CircuitOpenError(self._resource)

        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            await self._on_success()
            return result
        except Exception:
            await self._on_failure()
            raise

    async def _on_success(self) -> None:
        async with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self._config.success_threshold:
                    self._close()
            else:
                self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_at = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._open()
            elif self._failure_count >= self._config.failure_threshold:
                self._open()

    def _maybe_attempt_recovery(self) -> None:
        """Called under lock — transition OPEN → HALF_OPEN if timeout elapsed."""
        if self._state == CircuitState.OPEN and self._last_failure_at is not None:
            elapsed = time.monotonic() - self._last_failure_at
            if elapsed >= self._config.recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._success_count = 0

    def _close(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0

    def record_success(self) -> None:
        """Manually record a success (for external tracking)."""
        asyncio.get_event_loop().run_until_complete(self._on_success())

    def record_failure(self) -> None:
        """Manually record a failure (for external tracking)."""
        asyncio.get_event_loop().run_until_complete(self._on_failure())

    def stats(self) -> CircuitBreakerStats:
        return CircuitBreakerStats(
            state=self._state,
            failure_count=self._failure_count,
            success_count=self._success_count,
            last_failure_at=self._last_failure_at,
        )

    def reset(self) -> None:
        """Force the circuit back to CLOSED (use for testing or manual recovery)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_at = None
