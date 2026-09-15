from __future__ import annotations

import pytest

import sqlite3

from agent_kit.memory.in_memory import InMemoryStore
from agent_kit.memory.sqlite import SQLiteMemory
from agent_kit.memory.window import window_indices
from agent_kit.types import Message, ToolCall


def test_window_under_limit_keeps_everything():
    assert window_indices(["user", "assistant"], keep=5) == [0, 1]


def test_window_starts_at_first_user_turn_inside_window():
    roles = ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert window_indices(roles, keep=3) == [4, 5]


def test_window_never_starts_on_tool_result_within_single_exchange():
    roles = ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    # Plain cut at 3 would start on an assistant; cut at 2 on a tool result.
    assert window_indices(roles, keep=4) == [0, 3, 4, 5]
    assert window_indices(roles, keep=5) == [0, 3, 4, 5]


def test_window_keeps_parallel_results_with_their_call():
    roles = ["user", "assistant", "tool", "tool", "tool"]
    assert window_indices(roles, keep=2) == [0, 1, 2, 3, 4]


def _exchange(i: int) -> list[Message]:
    return [
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(tool_name="t", arguments={}, call_id=f"c{i}")],
        ),
        Message(role="tool", content="ok", tool_call_id=f"c{i}"),
    ]


def test_in_memory_store_trims_without_orphans():
    mem = InMemoryStore(window=4)
    mem.add(Message(role="user", content="go"))
    for i in range(5):
        mem.add_many(_exchange(i))
    history = mem.history()
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[1].tool_calls[0].call_id == "c4"
    assert history[2].tool_call_id == "c4"


def test_sqlite_persists_tool_calls_and_trims_without_orphans(tmp_path):
    mem = SQLiteMemory(tmp_path / "m.db", window=4)
    mem.add(Message(role="user", content="go"))
    for i in range(5):
        mem.add_many(_exchange(i))
    history = mem.history()
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[1].tool_calls[0].call_id == "c4"


def test_sqlite_migrates_database_without_tool_calls_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, "
        "content TEXT NOT NULL, tool_call_id TEXT, metadata TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute("INSERT INTO messages (role, content) VALUES ('user', 'hello')")
    conn.commit()
    conn.close()

    mem = SQLiteMemory(path)
    assert mem.history() == [Message(role="user", content="hello")]
    mem.add(
        Message(role="assistant", content="", tool_calls=[ToolCall(tool_name="t", arguments={}, call_id="x")])
    )
    assert mem.history()[1].tool_calls[0].call_id == "x"


def _history(n: int) -> list[Message]:
    msgs: list[Message] = []
    for i in range(n):
        msgs.append(Message(role="user", content=f"q{i}"))
        msgs.extend(_exchange(i))
        msgs.append(Message(role="assistant", content=f"a{i}"))
    return msgs


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_stores_default_to_no_window(make, tmp_path):
    mem = make(tmp_path)
    mem.add_many(_history(30))
    assert len(mem.history()) == 120


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_trim_oldest_cuts_at_a_user_turn(make, tmp_path):
    mem = make(tmp_path)
    mem.add(Message(role="system", content="sys"))
    mem.add_many(_history(3))  # q0 call0 tool0 a0 | q1 ... | q2 ...

    removed = mem.trim_oldest(2)  # would start mid-exchange; extends to the next user turn

    assert removed == 4
    history = mem.history()
    assert [m.content for m in history[:2]] == ["sys", "q1"]
    assert mem.trim_oldest(0) == 0


@pytest.mark.parametrize("make", [lambda tmp: InMemoryStore(), lambda tmp: SQLiteMemory(tmp / "t.db")])
def test_trim_oldest_keeps_latest_user_turn_and_tool_pairs(make, tmp_path):
    mem = make(tmp_path)
    mem.add(Message(role="user", content="go"))
    for i in range(4):
        mem.add_many(_exchange(i))

    removed = mem.trim_oldest(6)

    history = mem.history()
    assert history[0].content == "go"
    assert removed == 6
    assert [m.role for m in history[1:]] == ["assistant", "tool"]
    assert history[1].tool_calls[0].call_id == history[2].tool_call_id
