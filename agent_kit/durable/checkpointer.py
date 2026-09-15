"""Serialised checkpoint writes for one agent loop."""

from __future__ import annotations

import asyncio
import logging

from agent_kit.durable.models import RunCheckpoint
from agent_kit.durable.store import RunStore

logger = logging.getLogger(__name__)


class Checkpointer:
    """Owns one run's CAS version; parallel tool coroutines write through its lock."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.current: RunCheckpoint | None = None
        self._lock = asyncio.Lock()

    async def create(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            self.current = await self.store.save(checkpoint, 0)

    async def save(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            expected = self.current.version if self.current else 0
            self.current = await self.store.save(checkpoint, expected)

    async def mark_started(self, call_id: str) -> None:
        async with self._lock:
            assert self.current is not None and self.current.pending is not None
            version = await self.store.mark_tool_started(self.current.run_id, call_id, self.current.version)
            pending = self.current.pending.model_copy(update={"started": [*self.current.pending.started, call_id]})
            self.current = self.current.model_copy(update={"version": version, "pending": pending})

    async def fail(self, error: str) -> None:
        """Mark the last saved checkpoint failed, keeping its state so resume retries from there."""
        async with self._lock:
            if self.current is None:
                return
            try:
                self.current = await self.store.save(
                    self.current.model_copy(update={"status": "failed", "error": error}), self.current.version
                )
            except Exception:  # the run is already failing; don't mask its error
                logger.warning("could not record failure for run %s", self.current.run_id, exc_info=True)
