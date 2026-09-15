"""SQLiteMemory — persistent conversation memory backed by stdlib sqlite3."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from agent_kit.memory.window import window_indices
from agent_kit.types import Message, ToolCall


class SQLiteMemory:
    """
    Persistent conversation memory stored in a SQLite database.

    Survives process restarts. Thread-safe via a threading.Lock.
    System messages are always preserved regardless of window size.

    Usage::

        mem = SQLiteMemory("~/.agent-kit/sessions/my_agent.db", window=100)
        mem.add(Message(role="user", content="Hello"))
        history = mem.history()

        # Share across agent instances
        agent1 = Agent(provider, memory=mem)
        agent2 = Agent(provider, memory=mem)
    """

    def __init__(self, path: str | Path = ":memory:", window: int | None = None) -> None:
        self._path = str(Path(path).expanduser()) if str(path) != ":memory:" else ":memory:"
        self._window = window
        self._lock = threading.Lock()

        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    role    TEXT    NOT NULL,
                    content TEXT    NOT NULL,
                    tool_call_id TEXT,
                    metadata TEXT   NOT NULL DEFAULT '{}',
                    tool_calls TEXT NOT NULL DEFAULT '[]',
                    native TEXT
                )
            """)
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
            if "tool_calls" not in columns:  # databases created before tool_calls existed
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN tool_calls TEXT NOT NULL DEFAULT '[]'"
                )
            if "native" not in columns:  # databases created before native content existed
                self._conn.execute("ALTER TABLE messages ADD COLUMN native TEXT")

    _INSERT = (
        "INSERT INTO messages (role, content, tool_call_id, metadata, tool_calls, native) "
        "VALUES (?,?,?,?,?,?)"
    )

    @staticmethod
    def _row(m: Message) -> tuple[str, str, str | None, str, str, str | None]:
        native = (
            json.dumps({"provider": m.native_provider, "content": m.native_content})
            if m.native_content is not None
            else None
        )
        return (
            m.role,
            m.content,
            m.tool_call_id,
            json.dumps(m.metadata),
            json.dumps([tc.model_dump() for tc in m.tool_calls]),
            native,
        )

    def add(self, message: Message) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(self._INSERT, self._row(message))
            self._trim()

    def add_many(self, messages: list[Message]) -> None:
        with self._lock:
            with self._conn:
                self._conn.executemany(self._INSERT, [self._row(m) for m in messages])
            self._trim()

    def history(self, include_system: bool = True) -> list[Message]:
        with self._lock:
            if include_system:
                rows = self._conn.execute(
                    "SELECT role, content, tool_call_id, metadata, tool_calls, native FROM messages "
                    "ORDER BY id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT role, content, tool_call_id, metadata, tool_calls, native FROM messages "
                    "WHERE role != 'system' ORDER BY id"
                ).fetchall()
            return [self._message(row) for row in rows]

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        native = json.loads(row["native"]) if row["native"] else None
        return Message(
            role=row["role"],
            content=row["content"],
            tool_call_id=row["tool_call_id"],
            tool_calls=[ToolCall(**tc) for tc in json.loads(row["tool_calls"])],
            metadata=json.loads(row["metadata"]),
            native_content=native["content"] if native else None,
            native_provider=native["provider"] if native else None,
        )

    def clear(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM messages")

    def trim_oldest(self, count: int) -> int:
        """Remove at least ``count`` oldest non-system messages at a tool-safe boundary; return how many."""
        if count <= 0:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role FROM messages WHERE role != 'system' ORDER BY id"
            ).fetchall()
            kept = set(window_indices([row[1] for row in rows], max(0, len(rows) - count)))
            drop_ids = [row[0] for i, row in enumerate(rows) if i not in kept]
            if drop_ids:
                placeholders = ",".join("?" * len(drop_ids))
                with self._conn:
                    self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", drop_ids)
            return len(drop_ids)

    def _trim(self) -> None:
        """Drop oldest non-system messages when window is exceeded, never orphaning tool results."""
        if self._window is None:
            return
        total = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total <= self._window:
            return

        system_count = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE role = 'system'"
        ).fetchone()[0]
        keep = max(0, self._window - system_count)

        rows = self._conn.execute(
            "SELECT id, role FROM messages WHERE role != 'system' ORDER BY id"
        ).fetchall()
        kept = set(window_indices([row[1] for row in rows], keep))
        drop_ids = [row[0] for i, row in enumerate(rows) if i not in kept]
        if drop_ids:
            placeholders = ",".join("?" * len(drop_ids))
            with self._conn:
                self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", drop_ids)

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"SQLiteMemory(path={self._path!r}, window={self._window})"

    def close(self) -> None:
        """Close the database connection. Call on agent shutdown if using file-backed store."""
        self._conn.close()
