"""Claude Agent SDK adapter.

Most tests drive the observer with dataclass fakes named like the SDK's message types
(the adapter dispatches on type name). The smoke tests at the bottom use the real SDK.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

import pytest

from agent_kit.exceptions import BudgetExceededError
from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str
    usage: dict[str, Any] | None = None
    message_id: str | None = None
    session_id: str | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    num_turns: int = 2
    session_id: str = "s1"
    total_cost_usd: float | None = None
    errors: list[str] | None = None


INIT = SystemMessage("init", {"session_id": "s1", "model": "claude-opus-5", "tools": ["Read"]})


def usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def hook(event: str, **fields: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": "s1", "cwd": "/", "transcript_path": "", **fields}


async def collect(observer: ClaudeAgentObserver, source: Any, prompt: str | None = "find it") -> list[Any]:
    return [m async for m in observer.observe(source, prompt=prompt)]


async def test_observe_records_a_full_run_and_passes_messages_through(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    messages = [
        INIT,
        AssistantMessage([TextBlock("Looking.")], "claude-opus-5", usage(900, 10), "msg_1", "s1"),
        AssistantMessage(
            [ToolUseBlock("tu_1", "Read", {"path": "a"})], "claude-opus-5", usage(1000, 100), "msg_1", "s1"
        ),
        AssistantMessage([TextBlock("Found.")], "claude-opus-5", usage(200, 20, cache_read=1000), "msg_2", "s1"),
        ResultMessage(num_turns=2, total_cost_usd=0.05),
    ]

    async def source():
        yield messages[0]
        yield messages[1]
        yield messages[2]
        await observer._on_hook(hook("PreToolUse", tool_name="Read", tool_use_id="tu_1", tool_input={}), "tu_1", None)
        await observer._on_hook(
            hook("PostToolUse", tool_name="Read", tool_use_id="tu_1", tool_input={}, tool_response="ok"), "tu_1", None
        )
        yield messages[3]
        yield messages[4]

    seen = await collect(observer, source())

    assert seen == messages
    assert cloud_capture.types() == [
        "run_start", "turn_complete", "turn_complete", "turn_complete", "run_complete", "audit_flush",
    ]
    (run_id,) = {e.run_id for e in cloud_capture.events}
    start = cloud_capture.of("run_start")[0]
    assert start.payload["model"] == "claude-opus-5"
    assert start.payload["harness"] == "claude-agent-sdk"
    assert start.payload["session_id"] == "s1"
    assert start.agent_name == "claude-agent"

    turn1, turn2, reconcile = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (turn1["input_tokens"], turn1["output_tokens"], turn1["tool_names"]) == (1000, 100, ["Read"])
    assert (turn2["input_tokens"], turn2["tool_names"]) == (200, [])
    assert reconcile["reconciliation"] is True
    assert turn1["cost_usd"] + turn2["cost_usd"] + reconcile["cost_usd"] == pytest.approx(0.05)

    done = cloud_capture.of("run_complete")[0].payload
    assert (done["total_turns"], done["total_cost_usd"]) == (2, pytest.approx(0.05))
    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "llm_complete", "tool_call", "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(run_id)


async def test_tool_failure_hook_records_unsuccessful_call(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="tu_9", tool_input={}), "tu_9", None)
        result = await observer._on_hook(
            hook("PostToolUseFailure", tool_name="Bash", tool_use_id="tu_9", tool_input={}, error="exit 1", is_interrupt=False),
            "tu_9",
            None,
        )
        assert result == {}
        yield ResultMessage(total_cost_usd=None)

    await collect(observer, source())

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert "tool_call" in cloud_capture.audit_types(run_id)
    cloud_capture.assert_chain_intact(run_id)


async def test_subagent_and_compaction_hooks_become_audit_events(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        await observer._on_hook(hook("SubagentStart", agent_id="a1", agent_type="researcher"), None, None)
        await observer._on_hook(
            hook("SubagentStop", agent_id="a1", agent_type="researcher", stop_hook_active=False, agent_transcript_path=""),
            None,
            None,
        )
        await observer._on_hook(hook("PreCompact", trigger="auto", custom_instructions=None), None, None)
        yield ResultMessage()

    await collect(observer, source())

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "subagent_start", "subagent_stop", "context_compaction", "agent_complete",
    ]


async def test_hooks_for_unobserved_sessions_are_ignored(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    result = await observer._on_hook(hook("PostToolUse", tool_name="Read", tool_use_id="x", tool_input={}), "x", None)
    assert result == {}
    assert cloud_capture.events == []


async def test_harness_exception_is_recorded_and_reraised_unchanged(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    failure = ConnectionError("CLI exited")

    async def source():
        yield INIT
        raise failure

    with pytest.raises(ConnectionError) as info:
        await collect(observer, source())

    assert info.value is failure
    assert cloud_capture.types() == ["run_start", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("ConnectionError", "CLI exited")


async def test_stream_ending_without_result_is_an_incomplete_run(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT

    await collect(observer, source())

    assert cloud_capture.of("run_error")[0].payload["error_type"] == "IncompleteRun"


async def test_error_result_records_run_error(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage(subtype="error_max_turns", is_error=True, errors=["hit max turns"])

    await collect(observer, source())

    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("ResultError", "hit max turns")


async def test_consumer_breaking_after_result_does_not_add_an_error(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage()
        yield SystemMessage("trailing", {"session_id": "s1"})

    async for message in observer.observe(source()):
        if isinstance(message, ResultMessage):
            break

    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


async def test_each_observe_call_is_a_separate_run(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage()

    await collect(observer, source())
    await collect(observer, source())

    assert len({e.run_id for e in cloud_capture.of("run_complete")}) == 2


# ---------------------------------------------------------------------------
# Real SDK types
# ---------------------------------------------------------------------------

requires_claude_sdk = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None, reason="claude-agent-sdk extra not installed"
)


@requires_claude_sdk
def test_with_hooks_keeps_user_hooks():
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    async def user_hook(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        return {}

    from agent_kit.cloud.reporter import CloudReporter

    observer = ClaudeAgentObserver(CloudReporter(api_key="akt_test"))
    options = observer.with_hooks(ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[user_hook])]}))

    assert options.hooks is not None
    assert options.hooks["PreToolUse"][0].hooks == [user_hook]
    assert len(options.hooks["PreToolUse"]) == 2
    assert set(options.hooks) >= {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop", "PreCompact"}


@requires_claude_sdk
async def test_observe_with_real_sdk_message_types(cloud_capture):
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock

    observer = ClaudeAgentObserver(cloud_capture.reporter)
    (post_tool_use,) = observer.hooks()["PostToolUse"]

    async def source():
        yield SystemMessage(subtype="init", data={"session_id": "real", "model": "claude-sonnet-5"})
        yield AssistantMessage(
            content=[TextBlock(text="hi"), ToolUseBlock(id="tu", name="Read", input={})],
            model="claude-sonnet-5",
            usage=usage(100, 10),
            message_id="m1",
            session_id="real",
        )
        await post_tool_use.hooks[0](
            {"hook_event_name": "PostToolUse", "session_id": "real", "tool_name": "Read", "tool_use_id": "tu",
             "tool_input": {}, "tool_response": "", "cwd": "/", "transcript_path": ""},
            "tu",
            {"signal": None},
        )
        yield ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1,
            session_id="real", total_cost_usd=0.0003,
        )

    await collect(observer, source(), prompt="hi")

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.types()[-2:] == ["run_complete", "audit_flush"]
    assert "tool_call" in cloud_capture.audit_types(run_id)
    cloud_capture.assert_chain_intact(run_id)


class TrippedGuard:
    def __init__(self, tripped: bool = True) -> None:
        self.tripped = tripped
        self.checks: list[tuple[str, str]] = []
        self.spend: list[float] = []

    async def check(self, agent_name: str, project: str) -> None:
        self.checks.append((agent_name, project))
        if self.tripped:
            raise BudgetExceededError(scope="budget", limit_usd=5.0, spent_usd=5.5, budget_name="claude daily")

    def record_spend(self, agent_name: str, project: str, usd: float) -> None:
        self.spend.append(usd)


async def test_tripped_budget_stops_claude_at_tool_boundary(cloud_capture):
    guard = TrippedGuard()
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter, enforce_budgets=True)
    outputs: list[Any] = []

    async def source():
        yield INIT
        outputs.append(await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="t1", tool_input={}), "t1", None))
        outputs.append(await observer._on_hook(hook("SubagentStart", agent_id="a", agent_type="x"), None, None))
        yield ResultMessage()

    await collect(observer, source())

    pre, sub = outputs
    assert pre["continue_"] is False and "claude daily" in pre["stopReason"]
    assert pre["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert sub["continue_"] is False
    assert guard.checks == [("claude-agent", "proj"), ("claude-agent", "proj")]
    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.audit_types(run_id).count("budget_exceeded") == 2


async def test_budget_not_tripped_lets_hooks_pass_and_records_spend(cloud_capture):
    guard = TrippedGuard(tripped=False)
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter, enforce_budgets=True)
    outputs: list[Any] = []

    async def source():
        yield INIT
        yield AssistantMessage([ToolUseBlock("t1", "Read", {})], "claude-opus-5", usage(1000, 100), "m1", "s1")
        outputs.append(await observer._on_hook(hook("PreToolUse", tool_name="Read", tool_use_id="t1", tool_input={}), "t1", None))
        yield ResultMessage()

    await collect(observer, source())

    assert outputs == [{}]
    assert guard.spend == [pytest.approx((1000 * 5 + 100 * 25) / 1_000_000)]


async def test_observer_without_enforcement_never_checks(cloud_capture):
    guard = TrippedGuard()
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        assert await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="t", tool_input={}), "t", None) == {}
        yield ResultMessage()

    await collect(observer, source())
    assert guard.checks == []


@requires_claude_sdk
def test_with_hooks_sets_native_per_run_cap():
    from claude_agent_sdk import ClaudeAgentOptions

    from agent_kit.cloud.reporter import CloudReporter

    observer = ClaudeAgentObserver(CloudReporter(api_key="akt_test"), max_run_cost_usd=1.5)
    assert observer.with_hooks(ClaudeAgentOptions()).max_budget_usd == 1.5
    assert observer.with_hooks(ClaudeAgentOptions(max_budget_usd=0.25)).max_budget_usd == 0.25
