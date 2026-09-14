"""MCP client against a real fixture MCP server (stdio and streamable HTTP)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp.server.mcpserver", reason="requires mcp>=2.0")

from agent_kit import Agent, AgentConfig, tool  # noqa: E402
from agent_kit.exceptions import MCPConnectionError  # noqa: E402
from agent_kit.hooks import ApprovalRequest, Hooks  # noqa: E402
from agent_kit.providers.base import ProviderConfig  # noqa: E402
from agent_kit.tools.mcp import (  # noqa: E402
    MCPToolset,
    require_approval_unless_read_only,
    stdio,
)
from agent_kit.types import CostSummary, Message, ToolCall, Turn  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_fixture_server.py")


def fixture_server(name: str = "fixture", pid_file: Path | None = None, **kw: Any):
    env = {**os.environ, **({"MCP_FIXTURE_PID_FILE": str(pid_file)} if pid_file else {})}
    return stdio(name, sys.executable, FIXTURE, env=env, **kw)


_DEVNULL = open(os.devnull, "w")  # noqa: SIM115 - stderr for fixture subprocesses, lives for the session


def quiet():
    return _DEVNULL


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_lists_tools_with_prefixed_names_and_schemas():
    async with MCPToolset(fixture_server(), errlog=quiet()) as mcp:
        by_name = {t.schema.name: t.schema for t in mcp.tools}

        assert set(by_name) == {f"fixture__{n}" for n in ("echo", "add", "inventory", "delete_record", "fail", "slow", "picture")}
        assert by_name["fixture__add"].description == "Add two integers."
        assert set(by_name["fixture__add"].parameters["properties"]) == {"a", "b"}
        assert by_name["fixture__echo"].idempotent is True
        assert by_name["fixture__add"].idempotent is False
        assert mcp.server_for("fixture__echo") == "fixture"
        assert mcp.annotations("fixture__delete_record").destructive_hint is True
        assert mcp.annotations("fixture__add") is None
        assert mcp.connected_servers == ["fixture"]


async def test_include_and_unprefixed_names():
    async with MCPToolset(fixture_server(), prefix=False, include=["echo", "add"], errlog=quiet()) as mcp:
        assert sorted(t.schema.name for t in mcp.tools) == ["add", "echo"]


def test_tool_names_are_sanitised_and_length_capped():
    toolset = MCPToolset(stdio("my.server", "true"))
    assert toolset._tool_name("my.server", "get issue/v2") == "my_server__get_issue_v2"
    long = toolset._tool_name("s" * 40, "t" * 40)
    assert len(long) == 64 and long.startswith("s" * 40 + "__" + "t" * 13 + "_")


def test_duplicate_server_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate MCP server name"):
        MCPToolset(stdio("a", "x"), stdio("a", "y"))


async def call(mcp: MCPToolset, name: str, **arguments: Any):
    (t,) = [t for t in mcp.tools if t.schema.name == name]
    return await t(call_id="c1", **arguments)


async def test_result_mapping():
    async with MCPToolset(fixture_server(), call_timeout_s=0.5, errlog=quiet()) as mcp:
        assert (await call(mcp, "fixture__echo", text="hi")).output == "hi"
        assert (await call(mcp, "fixture__add", a=2, b=3)).output == {"sum": 5}
        assert (await call(mcp, "fixture__inventory", sku="A1")).output == {"sku": "A1", "count": 3}
        assert (await call(mcp, "fixture__picture")).output == "[image: image/png]"

        failed = await call(mcp, "fixture__fail", reason="nope")
        assert failed.output is None and "Error executing tool fail" in failed.error

        timed_out = await call(mcp, "fixture__slow", seconds=3)
        assert timed_out.error == "MCP tool 'fixture__slow' timed out after 0.5s"

        assert (await call(mcp, "fixture__echo", text="still usable")).output == "still usable"


class ScriptedProvider:
    config = ProviderConfig(default_model="mock")

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.requests: list[list[Message]] = []

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append(list(messages))
        return self.turns.pop(0)

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.requests.append(list(messages))
        yield self.turns.pop(0)


def calls(*specs: tuple[str, dict[str, Any]]) -> Turn:
    tcs = [ToolCall(tool_name=n, arguments=a, call_id=f"{n}-{i}") for i, (n, a) in enumerate(specs)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tcs), tool_calls=tcs, cost=CostSummary())


def final() -> Turn:
    return Turn(message_out=Message(role="assistant", content="done"), cost=CostSummary())


async def test_agent_uses_mcp_tools_end_to_end():
    async with MCPToolset(fixture_server(), errlog=quiet()) as mcp:
        provider = ScriptedProvider(calls(("fixture__add", {"a": 20, "b": 22})), final())
        agent = Agent(provider, tools=mcp.tools)
        result = await agent.run("add")

    tool_message = [m for m in provider.requests[1] if m.role == "tool"][0]
    assert tool_message.content == '{"sum": 42}'
    assert result.output == "done"
    assert agent.audit is not None
    assert any(e.event_type == "tool_call" and e.actor == "fixture__add" for e in agent.audit.events())


@tool(description="a local tool")
async def local_note(text: str) -> str:
    return text


async def test_require_approval_unless_read_only():
    asked: list[str] = []

    async def approver(req: ApprovalRequest) -> bool:
        asked.append(req.tool_name)
        return False

    async with MCPToolset(fixture_server(), errlog=quiet()) as mcp:
        provider = ScriptedProvider(
            calls(
                ("fixture__echo", {"text": "hi"}),
                ("fixture__delete_record", {"record_id": "9"}),
                ("fixture__add", {"a": 1, "b": 2}),
                ("local_note", {"text": "x"}),
            ),
            final(),
        )
        agent = Agent(provider, tools=[*mcp.tools, local_note], config=AgentConfig(
            hooks=Hooks(before_tool=[require_approval_unless_read_only(mcp)]),
            approver=approver,
        ))
        await agent.run("go")

    assert sorted(asked) == ["fixture__add", "fixture__delete_record"]
    contents = {m.tool_call_id: m.content for m in provider.requests[1] if m.role == "tool"}
    assert contents["fixture__echo-0"] == '"hi"'  # tool output is JSON-encoded for the model
    assert contents["fixture__delete_record-1"] == "Error: Tool call denied: approval denied"
    assert contents["local_note-3"] == '"x"'


async def test_required_server_failure_closes_started_servers(tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(MCPConnectionError, match="broken"):
        async with MCPToolset(fixture_server(pid_file=pid_file), stdio("broken", "definitely-not-a-command-xyz"),
                              errlog=quiet()):
            pass
    assert pid_file.exists()
    assert not alive(int(pid_file.read_text()))


async def test_optional_server_failure_is_skipped():
    async with MCPToolset(fixture_server(), stdio("broken", "definitely-not-a-command-xyz", required=False),
                          errlog=quiet()) as mcp:
        assert mcp.connected_servers == ["fixture"]
        assert all(t.schema.name.startswith("fixture__") for t in mcp.tools)


async def test_name_collision_fails_and_closes(tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(MCPConnectionError, match="collision"):
        async with MCPToolset(fixture_server("one", pid_file=pid_file), fixture_server("two"), prefix=False,
                              errlog=quiet()):
            pass
    assert not alive(int(pid_file.read_text()))


async def test_exit_terminates_server_and_disables_tools(tmp_path):
    pid_file = tmp_path / "pid"
    async with MCPToolset(fixture_server(pid_file=pid_file), errlog=quiet()) as mcp:
        pid = int(pid_file.read_text())
        assert alive(pid)
        echo = [t for t in mcp.tools if t.schema.name == "fixture__echo"][0]

    assert not alive(pid)
    assert (await echo(call_id="late", text="hi")).error == "MCP toolset is closed"
