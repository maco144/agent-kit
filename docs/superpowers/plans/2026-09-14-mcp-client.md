# MCP Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `async with MCPToolset(stdio(...), http(...)) as mcp:` exposes MCP server tools as ordinary agent-kit `Tool`s, with an opt-in approval helper for tools not marked read-only.

**Architecture:** One module, `agent_kit/tools/mcp.py`. Servers connect sequentially on enter, each inside its own `AsyncExitStack` that joins the toolset's stack only on success (so a skipped server leaves nothing open). Every listed MCP tool becomes a `Tool` whose function calls `ClientSession.call_tool` and maps the result; failures raise `MCPToolError`, which the existing `Tool` wrapper turns into `ToolResult.error`.

**Tech Stack:** `mcp>=2.0` (ClientSession, stdio_client, streamable_http_client), anyio (`fail_after`), httpx2, pytest + pytest-asyncio (auto), uvicorn (fixture HTTP server).

**Spec:** `specs/12-mcp-client.md`

## Global Constraints

- Sequential connection (spec amendment): the MCP SDK's transports hold anyio task groups that must be exited by the task that entered them, so servers are not connected from concurrent tasks.
- `anyio.fail_after` wraps only plain awaits (`initialize`, `list_tools`), never context-manager entry.
- No change to the agent loop; MCP tools are plain `Tool`s.
- Tests skip without `mcp`; no test reaches the network beyond `127.0.0.1`.
- `ruff check agent_kit tests`, `mypy agent_kit`, `pytest` clean with and without `mcp`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `agent_kit/tools/mcp.py` | Server configs, `MCPToolset`, result mapping, approval helper | Create |
| `agent_kit/exceptions.py` | `MCPConnectionError`, `MCPToolError` | Modify |
| `tests/fixtures/mcp_fixture_server.py` | Real MCP server for tests (stdio or HTTP) | Create |
| `tests/test_mcp.py` | Behaviour | Create |
| `pyproject.toml`, `.github/workflows/ci.yml` | `mcp` extra, CI install, mypy override if needed | Modify |
| `examples/mcp_tools.py`, `examples/README.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/12-mcp-client.md`, `PROJECT_INDEX.md` | Example + docs | Create / Modify |

---

### Task 1: `MCPToolset` over stdio

**Files:**
- Create: `agent_kit/tools/mcp.py`, `tests/fixtures/mcp_fixture_server.py`, `tests/test_mcp.py`
- Modify: `agent_kit/exceptions.py`, `pyproject.toml`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `StdioServer`, `HttpServer`, `stdio()`, `http()`, `MCPToolset(*servers, prefix=True, include=None, call_timeout_s=60.0, connect_timeout_s=30.0, errlog=None)` with `tools`, `annotations(name)`, `server_for(name)`, `connected_servers`, `_tool_name(server, tool)`; `require_approval_unless_read_only(toolset, reason=None)`; exceptions `MCPConnectionError`, `MCPToolError`.

- [ ] **Step 1: Write the fixture server and failing tests**

```python
# tests/fixtures/mcp_fixture_server.py
"""A real MCP server for agent-kit's MCP client tests. Run: python mcp_fixture_server.py [stdio|http PORT]."""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, ToolAnnotations
from pydantic import BaseModel

app = MCPServer("fixture")


class Inventory(BaseModel):
    sku: str
    count: int


@app.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def echo(text: str) -> str:
    """Echo text back."""
    return text


@app.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers."""
    return {"sum": a + b}


@app.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
def inventory(sku: str) -> Inventory:
    """Stock level for a SKU."""
    return Inventory(sku=sku, count=3)


@app.tool(annotations=ToolAnnotations(destructive_hint=True))
def delete_record(record_id: str) -> str:
    """Delete a record."""
    return f"deleted {record_id}"


@app.tool()
def fail(reason: str) -> str:
    """Always fails."""
    raise ValueError(reason)


@app.tool()
async def slow(seconds: float) -> str:
    """Sleep, then answer."""
    await asyncio.sleep(seconds)
    return "done"


@app.tool()
def picture() -> ImageContent:
    """A tiny image."""
    return ImageContent(type="image", data="aGk=", mime_type="image/png")


if __name__ == "__main__":
    pid_file = os.environ.get("MCP_FIXTURE_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    if len(sys.argv) > 2 and sys.argv[1] == "http":
        import uvicorn

        uvicorn.run(app.streamable_http_app(), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
    else:
        app.run("stdio")
```

```python
# tests/test_mcp.py
"""MCP client against a real fixture MCP server (stdio and streamable HTTP)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

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


def quiet() -> io.StringIO:
    return io.StringIO()


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
    assert contents["fixture__echo-0"] == "hi"
    assert contents["fixture__delete_record-1"] == "Error: Tool call denied: approval denied"
    assert contents["local_note-3"] == "x"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp.py -v` (in an environment with `mcp>=2.0`)
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.tools.mcp'`

- [ ] **Step 3: Implement**

`agent_kit/exceptions.py` — append:

```python
class MCPConnectionError(AgentKitError):
    """A required MCP server could not be connected, or its tools could not be registered."""


class MCPToolError(AgentKitError):
    """An MCP tool call failed; surfaced to the model as a tool error."""
```

```python
# agent_kit/tools/mcp.py
"""
Use tools from Model Context Protocol servers.

    from agent_kit.tools.mcp import MCPToolset, http, stdio

    async with MCPToolset(
        stdio("github", "npx", "-y", "@modelcontextprotocol/server-github", env={"GITHUB_TOKEN": token}),
        http("linear", "https://mcp.linear.app/mcp", headers={"Authorization": f"Bearer {key}"}),
    ) as mcp:
        agent = Agent(provider, tools=[*mcp.tools, my_tool])
        await agent.run("Open an issue for the failing test")

MCP tools are ordinary agent-kit Tools: allowed_tools, hooks and approvals, budgets, audit,
and Cloud reporting all apply. Connections live exactly as long as the ``async with`` block.
Requires ``pip install agent-kit[mcp]``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, TextIO

try:
    import anyio
    import httpx2
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    from mcp.shared.exceptions import MCPError
    from mcp.types import REQUEST_TIMEOUT, CallToolResult, PaginatedRequestParams, ToolAnnotations
except ImportError as e:
    raise ImportError("MCP support requires the 'mcp' package. Install it with: pip install agent-kit[mcp]") from e

from agent_kit.exceptions import MCPConnectionError, MCPToolError
from agent_kit.hooks import BeforeToolHook, Decision, HookResult, ToolCallContext
from agent_kit.tools.base import Tool
from agent_kit.types import ToolSchema

logger = logging.getLogger("agent_kit.mcp")

_MAX_NAME = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class StdioServer:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None
    required: bool = True


@dataclass(frozen=True)
class HttpServer:
    name: str
    url: str
    headers: dict[str, str] | None = None
    timeout_s: float = 30.0
    required: bool = True


def stdio(
    name: str,
    command: str,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    required: bool = True,
) -> StdioServer:
    """An MCP server run as a subprocess speaking stdio."""
    return StdioServer(name=name, command=command, args=tuple(args), env=env, cwd=cwd, required=required)


def http(
    name: str,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
    required: bool = True,
) -> HttpServer:
    """A remote MCP server over streamable HTTP. Authenticate with headers."""
    return HttpServer(name=name, url=url, headers=headers, timeout_s=timeout_s, required=required)


@dataclass
class _Entry:
    server: str
    original_name: str
    annotations: ToolAnnotations | None
    session: ClientSession


class MCPToolset:
    """Connects MCP servers for the duration of an ``async with`` block and exposes their tools."""

    def __init__(
        self,
        *servers: StdioServer | HttpServer,
        prefix: bool = True,
        include: Iterable[str] | None = None,
        call_timeout_s: float = 60.0,
        connect_timeout_s: float = 30.0,
        errlog: TextIO | None = None,
    ) -> None:
        seen: set[str] = set()
        for server in servers:
            if server.name in seen:
                raise ValueError(f"duplicate MCP server name {server.name!r}")
            seen.add(server.name)
        self._servers = servers
        self._prefix = prefix
        self._include = set(include) if include is not None else None
        self._call_timeout_s = call_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._errlog = errlog
        self._stack: AsyncExitStack | None = None
        self._tools: list[Tool] = []
        self._entries: dict[str, _Entry] = {}
        self._connected: list[str] = []
        self._closed = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> MCPToolset:
        if self._stack is not None:
            raise RuntimeError("MCPToolset is already open")
        stack = AsyncExitStack()
        self._stack = stack
        try:
            for server in self._servers:
                server_stack = AsyncExitStack()
                try:
                    session, listed = await self._connect(server_stack, server)
                except Exception as exc:
                    await _close_quietly(server_stack)
                    if server.required:
                        raise MCPConnectionError(f"{server.name}: {type(exc).__name__}: {exc}") from exc
                    logger.warning("MCP server %s skipped: %s: %s", server.name, type(exc).__name__, exc)
                    continue
                await stack.enter_async_context(server_stack)
                self._register(server, session, listed)
                self._connected.append(server.name)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        self._closed = False
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._closed = True
        stack, self._stack = self._stack, None
        if stack is not None:
            await _close_quietly(stack)

    async def _connect(self, stack: AsyncExitStack, server: StdioServer | HttpServer) -> tuple[ClientSession, list[Any]]:
        if isinstance(server, StdioServer):
            params = StdioServerParameters(
                command=server.command, args=list(server.args), env=server.env, cwd=server.cwd
            )
            read, write = await stack.enter_async_context(stdio_client(params, errlog=self._errlog or sys.stderr))
        else:
            client = await stack.enter_async_context(
                create_mcp_http_client(headers=server.headers, timeout=httpx2.Timeout(server.timeout_s))
            )
            streams = await stack.enter_async_context(streamable_http_client(server.url, http_client=client))
            read, write = streams[0], streams[1]
        session = await stack.enter_async_context(ClientSession(read, write))

        with anyio.fail_after(self._connect_timeout_s):
            await session.initialize()
            listed: list[Any] = []
            cursor: str | None = None
            while True:
                page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                listed.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        return session, listed

    def _register(self, server: StdioServer | HttpServer, session: ClientSession, listed: list[Any]) -> None:
        for mcp_tool in listed:
            name = self._tool_name(server.name, mcp_tool.name)
            if self._include is not None and name not in self._include:
                continue
            if name in self._entries:
                other = self._entries[name].server
                raise MCPConnectionError(
                    f"tool name collision: {name!r} from servers {other!r} and {server.name!r}"
                )
            annotations = mcp_tool.annotations
            self._entries[name] = _Entry(server.name, mcp_tool.name, annotations, session)
            schema = ToolSchema(
                name=name,
                description=mcp_tool.description or mcp_tool.title or "",
                parameters=mcp_tool.input_schema or {"type": "object", "properties": {}},
                idempotent=bool(annotations and annotations.idempotent_hint),
            )
            self._tools.append(Tool(self._caller(name), schema))

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    @property
    def connected_servers(self) -> list[str]:
        return list(self._connected)

    def annotations(self, tool_name: str) -> ToolAnnotations | None:
        entry = self._entries.get(tool_name)
        return entry.annotations if entry else None

    def server_for(self, tool_name: str) -> str | None:
        entry = self._entries.get(tool_name)
        return entry.server if entry else None

    def _tool_name(self, server: str, tool: str) -> str:
        raw = f"{server}__{tool}" if self._prefix else tool
        safe = _UNSAFE.sub("_", raw)
        if len(safe) > _MAX_NAME:
            safe = safe[: _MAX_NAME - 9] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]
        return safe

    def _caller(self, name: str) -> Any:
        async def call(**arguments: Any) -> Any:
            return await self._call(name, arguments)

        return call

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        entry = self._entries.get(name)
        if self._closed or entry is None:
            raise MCPToolError("MCP toolset is closed")
        try:
            result = await entry.session.call_tool(
                entry.original_name, arguments, read_timeout_seconds=self._call_timeout_s
            )
        except MCPError as exc:
            if exc.code == REQUEST_TIMEOUT:
                raise MCPToolError(f"MCP tool '{name}' timed out after {self._call_timeout_s:g}s") from exc
            raise MCPToolError(f"MCP tool '{name}' failed: {exc}") from exc
        if not isinstance(result, CallToolResult):
            raise MCPToolError(f"MCP tool '{name}' requested interactive input, which is not supported")
        if result.is_error:
            raise MCPToolError(_join(result.content) or f"MCP tool '{name}' failed")
        return _output(result)


def require_approval_unless_read_only(toolset: MCPToolset, reason: str | None = None) -> BeforeToolHook:
    """Ask the approver before any MCP tool not marked read-only runs. Local tools are unaffected."""

    def hook(ctx: ToolCallContext) -> HookResult:
        if toolset.server_for(ctx.tool_name) is None:
            return None
        annotations = toolset.annotations(ctx.tool_name)
        if annotations is not None and annotations.read_only_hint is True:
            return None
        return Decision.ask(reason or f"{ctx.tool_name} is not marked read-only")

    return hook


def _output(result: CallToolResult) -> Any:
    structured = result.structured_content
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    blocks = list(result.content)
    if len(blocks) == 1 and getattr(blocks[0], "type", "") == "text":
        text = str(getattr(blocks[0], "text", ""))
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        return parsed if isinstance(parsed, (dict, list)) else text
    return _join(blocks)


def _join(blocks: Iterable[Any]) -> str:
    return "\n".join(_render(block) for block in blocks)


def _render(block: Any) -> str:
    kind = getattr(block, "type", "")
    if kind == "text":
        return str(getattr(block, "text", ""))
    if kind in ("image", "audio"):
        return f"[{kind}: {getattr(block, 'mime_type', 'unknown')}]"
    if kind == "resource":
        return f"[resource: {getattr(getattr(block, 'resource', None), 'uri', '')}]"
    if kind == "resource_link":
        return f"[resource: {getattr(block, 'uri', '')}]"
    return f"[{kind or 'content'}]"


async def _close_quietly(stack: AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception as exc:
        logger.debug("MCP connection close failed: %s", exc)
```

`pyproject.toml`: `mcp = ["mcp>=2.0"]`, add `mcp` to `all`. CI SDK install gains `mcp`.

- [ ] **Step 4: Run tests** (with `mcp` installed, and without)

Run: `pytest tests/test_mcp.py -v && pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean; skipped without `mcp`

- [ ] **Step 5: Commit**

```bash
git add agent_kit/tools/mcp.py agent_kit/exceptions.py tests/fixtures tests/test_mcp.py pyproject.toml .github/workflows/ci.yml
git commit -m "feat: MCPToolset — use MCP server tools as agent-kit tools"
```

---

### Task 2: Streamable HTTP servers

**Files:**
- Modify: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `http()` and `HttpServer` from Task 1.

- [ ] **Step 1: Write the test**

```python
# append to tests/test_mcp.py
import socket
import subprocess
import time

from agent_kit.tools.mcp import http


@pytest.fixture
def http_fixture_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, FIXTURE, "http", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("fixture HTTP MCP server did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(timeout=10)


async def test_streamable_http_server(http_fixture_server):
    async with MCPToolset(http("remote", http_fixture_server, headers={"X-Test": "1"})) as mcp:
        assert "remote__inventory" in {t.schema.name for t in mcp.tools}
        assert (await call(mcp, "remote__inventory", sku="Z9")).output == {"sku": "Z9", "count": 3}
```

- [ ] **Step 2: Run** `pytest tests/test_mcp.py -v -k http` — expected PASS with Task 1's transport code; if it fails, fix `_connect`'s HTTP branch before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp.py
git commit -m "test: MCPToolset over streamable HTTP"
```

---

### Task 3: Example and docs

**Files:**
- Create: `examples/mcp_tools.py`
- Modify: `examples/README.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/12-mcp-client.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: Example**

```python
# examples/mcp_tools.py
"""
Tools from an MCP server, with approval required for anything not marked read-only.

Uses the official filesystem MCP server (needs Node.js). Requires:
pip install agent-kit[mcp] and ANTHROPIC_API_KEY.
"""

import asyncio
import os

from agent_kit import Agent, AgentConfig
from agent_kit.hooks import ApprovalRequest, Hooks
from agent_kit.providers import AnthropicProvider
from agent_kit.tools.mcp import MCPToolset, require_approval_unless_read_only, stdio


async def approve(req: ApprovalRequest) -> bool:
    answer = await asyncio.to_thread(input, f"\nAllow {req.tool_name}({req.arguments})? [y/N] ")
    return answer.strip().lower() == "y"


async def main() -> None:
    root = os.getcwd()
    async with MCPToolset(stdio("fs", "npx", "-y", "@modelcontextprotocol/server-filesystem", root)) as mcp:
        print("MCP tools:", ", ".join(t.schema.name for t in mcp.tools))
        agent = Agent(
            AnthropicProvider(),
            tools=mcp.tools,
            config=AgentConfig(
                hooks=Hooks(before_tool=[require_approval_unless_read_only(mcp)]),
                approver=approve,
            ),
        )
        result = await agent.run("Summarise README.md in three bullet points, then save them to SUMMARY.md.")
        print("\n" + result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Docs** — README "Built in" row (`MCP tools | Tools from any MCP server over stdio or streamable HTTP, governed by the same allowlist, hooks, budgets, and audit`) and a `## MCP tools` section with the toolset example, naming rule, result mapping summary, and the approval helper; `examples/README.md` row; CHANGELOG `[Unreleased]` → `### Added` MCP entry (extra `agent-kit[mcp]`); spec 12 status `implemented` plus the sequential-connection amendment; roadmap 2.1 ticked and the "MCP client" capability row updated; `PROJECT_INDEX.md` (`tools/mcp.py`, fixture + test, spec 12, example count).

- [ ] **Step 3: Gates and commit**

Run: `pytest && ruff check agent_kit tests && mypy agent_kit && python -m compileall -q examples/`

```bash
git add examples README.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: MCP tools example and guide"
```
