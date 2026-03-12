"""Tests for SQLiteMemory."""

from __future__ import annotations

import pytest

from agent_kit.memory.sqlite import SQLiteMemory
from agent_kit.types import Message


def test_sqlite_memory_add_and_history():
    mem = SQLiteMemory(":memory:", window=50)
    mem.add(Message(role="user", content="Hello"))
    mem.add(Message(role="assistant", content="Hi there"))

    history = mem.history()
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[0].content == "Hello"
    assert history[1].role == "assistant"


def test_sqlite_memory_excludes_system_from_non_system_history():
    mem = SQLiteMemory(":memory:")
    mem.add(Message(role="system", content="Be helpful."))
    mem.add(Message(role="user", content="Hello"))

    with_system = mem.history(include_system=True)
    without_system = mem.history(include_system=False)

    assert len(with_system) == 2
    assert len(without_system) == 1
    assert without_system[0].role == "user"


def test_sqlite_memory_window_trims_oldest_non_system():
    mem = SQLiteMemory(":memory:", window=3)
    mem.add(Message(role="system", content="sys"))
    mem.add(Message(role="user", content="msg1"))
    mem.add(Message(role="user", content="msg2"))
    mem.add(Message(role="user", content="msg3"))
    # Window=3: system always kept; only 2 non-system fit
    mem.add(Message(role="user", content="msg4"))

    history = mem.history()
    contents = [m.content for m in history]
    assert "sys" in contents       # system always preserved
    assert "msg1" not in contents  # oldest non-system trimmed
    assert "msg4" in contents      # newest kept


def test_sqlite_memory_clear():
    mem = SQLiteMemory(":memory:")
    mem.add(Message(role="user", content="hello"))
    mem.clear()
    assert len(mem.history()) == 0


def test_sqlite_memory_add_many():
    mem = SQLiteMemory(":memory:")
    messages = [
        Message(role="user", content="a"),
        Message(role="assistant", content="b"),
        Message(role="user", content="c"),
    ]
    mem.add_many(messages)
    assert len(mem.history()) == 3


def test_sqlite_memory_len():
    mem = SQLiteMemory(":memory:")
    assert len(mem) == 0
    mem.add(Message(role="user", content="hi"))
    assert len(mem) == 1


def test_sqlite_memory_bool_always_true():
    mem = SQLiteMemory(":memory:")
    assert bool(mem) is True  # empty store is still truthy


def test_sqlite_memory_tool_call_id_preserved():
    mem = SQLiteMemory(":memory:")
    mem.add(Message(role="tool", content="result", tool_call_id="tc123"))
    history = mem.history()
    assert history[0].tool_call_id == "tc123"


def test_sqlite_memory_file_persistence(tmp_path):
    db_path = tmp_path / "test_memory.db"

    # Write
    mem1 = SQLiteMemory(db_path, window=50)
    mem1.add(Message(role="user", content="persisted message"))
    mem1.close()

    # Read back in new instance
    mem2 = SQLiteMemory(db_path, window=50)
    history = mem2.history()
    assert len(history) == 1
    assert history[0].content == "persisted message"
    mem2.close()
