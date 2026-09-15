"""Token-budget trimming: estimate prompt size and plan how many oldest messages to drop."""

from __future__ import annotations

import json
import math

from agent_kit.types import Message, ToolSchema

DEFAULT_TOKENS_PER_CHAR = 0.25  # used until a provider has reported real prompt tokens


def message_chars(m: Message) -> int:
    chars = len(m.content)
    if m.native_content:
        chars += len(json.dumps(m.native_content, default=str))
    for tc in m.tool_calls:
        chars += len(json.dumps(tc.arguments, default=str))
    return chars


def prompt_chars(system: str, tools: list[ToolSchema], messages: list[Message]) -> int:
    tool_chars = sum(len(t.name) + len(t.description) + len(json.dumps(t.parameters)) for t in tools)
    return len(system) + tool_chars + sum(message_chars(m) for m in messages)


def plan_trim(
    messages: list[Message], chars: int, budget_tokens: int, tokens_per_char: float
) -> tuple[int, int, int]:
    """
    Return (messages_to_drop, estimated_tokens_before, estimated_tokens_after).

    Nothing is dropped under budget. Over budget, drop oldest messages until the estimate is at most
    half the budget — one cut, so the prompt prefix stays stable (and cached) until the next one.
    The last message is never dropped.
    """
    before = math.ceil(chars * tokens_per_char)
    if before <= budget_tokens:
        return 0, before, before
    target = budget_tokens // 2
    remaining, dropped = chars, 0
    while dropped < len(messages) - 1 and math.ceil(remaining * tokens_per_char) > target:
        remaining -= message_chars(messages[dropped])
        dropped += 1
    return dropped, before, math.ceil(remaining * tokens_per_char)
