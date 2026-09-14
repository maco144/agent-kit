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
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError
    from mcp.types import REQUEST_TIMEOUT, CallToolResult, PaginatedRequestParams, ToolAnnotations
except ImportError as e:
    raise ImportError(
        "MCP support requires 'mcp>=2.0'. Install it with: pip install agent-kit[mcp]"
    ) from e

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
    """
    Connects MCP servers for the duration of an ``async with`` block and exposes their tools.

    ``errlog`` receives stdio servers' stderr and must be a real file (it is handed to the
    subprocess), e.g. ``open(os.devnull, "w")``. Defaults to ``sys.stderr``.
    """

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
            # Long read timeout for streamed responses, matching the MCP SDK's own defaults.
            client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=server.headers,
                    timeout=httpx2.Timeout(server.timeout_s, read=300.0),
                    follow_redirects=True,
                )
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
