"""Token budget planning and context management through the loop."""

from __future__ import annotations

from agent_kit.memory.budget import message_chars, plan_trim, prompt_chars
from agent_kit.types import Message, ToolCall, ToolSchema


def test_message_chars_counts_content_native_and_arguments():
    m = Message(
        role="assistant",
        content="abc",
        tool_calls=[ToolCall(tool_name="t", arguments={"k": "v"}, call_id="1")],
        native_content=[{"type": "text", "text": "abc"}],
        native_provider="anthropic",
    )
    assert message_chars(m) == 3 + len('{"k": "v"}') + len('[{"type": "text", "text": "abc"}]')


def test_prompt_chars_includes_system_and_tools():
    tool = ToolSchema(name="get", description="desc", parameters={"type": "object"})
    msgs = [Message(role="user", content="hello")]
    assert prompt_chars("sys", [tool], msgs) == 3 + len("get") + len("desc") + len('{"type": "object"}') + 5


def test_plan_trim_under_budget():
    msgs = [Message(role="user", content="x" * 100)]
    assert plan_trim(msgs, 100, budget_tokens=50, tokens_per_char=0.25) == (0, 25, 25)


def test_plan_trim_cuts_to_half_budget():
    msgs = [Message(role="user", content="x" * 400) for _ in range(10)]  # 4000 chars = 1000 tokens
    dropped, before, after = plan_trim(msgs, 4000, budget_tokens=600, tokens_per_char=0.25)
    assert (dropped, before, after) == (7, 1000, 300)


def test_plan_trim_keeps_last_message():
    msgs = [Message(role="user", content="x" * 4000), Message(role="user", content="y" * 4000)]
    assert plan_trim(msgs, 8000, budget_tokens=10, tokens_per_char=0.25) == (1, 2000, 1000)
