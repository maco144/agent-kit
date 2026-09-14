"""Hooks and approval gates."""

from __future__ import annotations

from typing import Any

from agent_kit.exceptions import RunStoppedByHookError
from agent_kit.hooks import (
    Decision,
    ToolCallContext,
    allow_only,
    deny_tools,
    require_approval,
    run_hook,
)


def tool_ctx(name: str = "refund", **extra: Any) -> ToolCallContext:
    return ToolCallContext(run_id="r", turn=1, tool_name=name, arguments={"order_id": "1"}, call_id="c1",
                           context=extra)


async def test_run_hook_accepts_sync_async_and_none():
    async def async_deny(ctx):
        return Decision.deny("async no")

    assert (await run_hook(lambda ctx: None, tool_ctx())).kind == "allow"
    assert (await run_hook(lambda ctx: Decision.ask("why"), tool_ctx())).reason == "why"
    assert (await run_hook(async_deny, tool_ctx())).reason == "async no"


async def test_run_hook_fails_closed():
    def broken(ctx):
        raise ValueError("bad policy")

    decision = await run_hook(broken, tool_ctx())
    assert (decision.kind, decision.reason) == ("deny", "hook error: ValueError: bad policy")

    odd = await run_hook(lambda ctx: "yes", tool_ctx())
    assert (odd.kind, odd.reason) == ("deny", "hook error: returned str, expected Decision or None")


def test_decision_constructors():
    assert Decision.deny("x", stop_run=True).stop_run is True
    assert Decision.replace({"a": 1}, "redacted").output == {"a": 1}
    assert Decision.allow().kind == "allow"


async def test_helpers():
    approval = require_approval("refund", "wire")
    assert (await run_hook(approval, tool_ctx("refund"))).kind == "ask"
    assert (await run_hook(approval, tool_ctx("lookup"))).kind == "allow"

    blocked = deny_tools("delete_account", reason="never", stop_run=True)
    decision = await run_hook(blocked, tool_ctx("delete_account"))
    assert (decision.kind, decision.reason, decision.stop_run) == ("deny", "never", True)

    only = allow_only("lookup")
    assert (await run_hook(only, tool_ctx("lookup"))).kind == "allow"
    assert (await run_hook(only, tool_ctx("refund"))).reason == "tool not permitted by policy"


def test_run_stopped_error_message():
    err = RunStoppedByHookError("before_tool", "never", tool_name="delete_account")
    assert (err.stage, err.reason, err.tool_name) == ("before_tool", "never", "delete_account")
    assert "delete_account" in str(err) and "never" in str(err)
