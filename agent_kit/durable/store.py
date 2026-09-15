"""RunStore protocol and the SQLite implementation."""

from __future__ import annotations

import asyncio
import builtins
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_kit.durable.models import RunCheckpoint, RunStatus, RunSummary
from agent_kit.exceptions import CheckpointError, RunConflictError


@runtime_checkable
class RunStore(Protocol):
    """Durable storage for run checkpoints. Every write is compare-and-swap on ``version``."""

    async def save(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint: ...
    async def load(self, run_id: str) -> RunCheckpoint | None: ...
    async def mark_tool_started(self, run_id: str, call_id: str, expected_version: int) -> int: ...
    async def list(self, status: RunStatus | None = None, limit: int = 100) -> builtins.list[RunSummary]: ...
    async def delete(self, run_id: str) -> None: ...


class SQLiteRunStore:
    """
    Run checkpoints in a SQLite file, shared by every process that opens the same path.

    Usage::

        store = SQLiteRunStore("~/.agent-kit/runs.db")
        agent = Agent(provider, config=AgentConfig(run_store=store, approver=SUSPEND))
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = ":memory:" if str(path) == ":memory:" else str(Path(path).expanduser())
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                "status TEXT NOT NULL, updated_at TEXT NOT NULL, pending_approvals INTEGER NOT NULL, "
                "checkpoint TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_status ON runs (status)")

    async def save(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint:
        stored = checkpoint.model_copy(update={"version": expected_version + 1, "updated_at": datetime.utcnow()})
        data = stored.model_dump_json()  # serialise in the caller's thread
        pending = len(stored.pending.approvals) if stored.pending else 0
        await asyncio.to_thread(self._write, stored, expected_version, data, pending)
        return stored

    def _write(self, cp: RunCheckpoint, expected: int, data: str, pending: int) -> None:
        row = (cp.version, cp.status, cp.updated_at.isoformat(), pending, data)
        with self._lock, self._conn:
            if expected == 0:
                try:
                    self._conn.execute(
                        "INSERT INTO runs (version, status, updated_at, pending_approvals, checkpoint, run_id) "
                        "VALUES (?,?,?,?,?,?)",
                        (*row, cp.run_id),
                    )
                except sqlite3.IntegrityError:
                    raise RunConflictError(cp.run_id, 0, self._version(cp.run_id)) from None
                return
            cursor = self._conn.execute(
                "UPDATE runs SET version=?, status=?, updated_at=?, pending_approvals=?, checkpoint=? "
                "WHERE run_id=? AND version=?",
                (*row, cp.run_id, expected),
            )
            if cursor.rowcount != 1:
                raise RunConflictError(cp.run_id, expected, self._version(cp.run_id))

    def _version(self, run_id: str) -> int | None:
        found = self._conn.execute("SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return int(found[0]) if found else None

    async def load(self, run_id: str) -> RunCheckpoint | None:
        return await asyncio.to_thread(self._load, run_id)

    def _load(self, run_id: str) -> RunCheckpoint | None:
        with self._lock:
            found = self._conn.execute("SELECT checkpoint FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunCheckpoint.model_validate_json(found[0]) if found else None

    async def mark_tool_started(self, run_id: str, call_id: str, expected_version: int) -> int:
        return await asyncio.to_thread(self._mark, run_id, call_id, expected_version)

    def _mark(self, run_id: str, call_id: str, expected: int) -> int:
        with self._lock, self._conn:
            found = self._conn.execute("SELECT version, checkpoint FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if found is None or int(found[0]) != expected:
                raise RunConflictError(run_id, expected, int(found[0]) if found else None)
            cp = RunCheckpoint.model_validate_json(found[1])
            if cp.pending is None:
                raise CheckpointError(run_id, "no pending turn to mark a tool call started")
            cp.pending.started.append(call_id)
            cp.version = expected + 1
            self._conn.execute(
                "UPDATE runs SET version=?, checkpoint=? WHERE run_id=?",
                (cp.version, cp.model_dump_json(), run_id),
            )
            return cp.version

    async def list(self, status: RunStatus | None = None, limit: int = 100) -> builtins.list[RunSummary]:
        return await asyncio.to_thread(self._list, status, limit)

    def _list(self, status: RunStatus | None, limit: int) -> builtins.list[RunSummary]:
        query = "SELECT run_id, status, updated_at, pending_approvals FROM runs"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [
            RunSummary(run_id=r[0], status=r[1], updated_at=datetime.fromisoformat(r[2]), pending_approvals=r[3])
            for r in rows
        ]

    async def delete(self, run_id: str) -> None:
        await asyncio.to_thread(self._delete, run_id)

    def _delete(self, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))

    def close(self) -> None:
        self._conn.close()
