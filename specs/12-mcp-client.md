# Spec 12 — MCP Client

Status: **approved design** · Written 2026-09-14 · Roadmap item: 2.1 (`specs/06-harness-roadmap.md`)

## Goal

Agents use tools from Model Context Protocol servers — local subprocesses or remote HTTP endpoints —
exactly like local tools: the same allowlist, hooks and approvals, budgets, audit chain, Cloud
reporting, and streaming.

**Done means:** an `Agent` given `mcp.tools` from a real stdio MCP server and a real streamable HTTP MCP
server calls those tools through its normal loop; tool errors and timeouts reach the model as tool
errors; `require_approval_unless_read_only(mcp)` lets read-only tools run and routes everything else
through the approver; leaving the `async with` block terminates every server process.

## Decisions

1. **Explicit lifecycle.** `async with MCPToolset(...) as mcp:` owns connections; tools are only valid
   inside the block. No background connections owned by `Agent`.
2. **MCP tools are ordinary `Tool`s.** No special cases in the agent loop.
3. **Annotations are untrusted hints.** They're exposed, and an opt-in helper gates any tool not marked
   read-only — a server can't escape the gate by omitting hints.
4. **Transports:** stdio and streamable HTTP. SSE (deprecated in MCP) is not supported.
5. **Dependency:** optional extra `agent-kit[mcp]` → `mcp>=2.0`. Importing `agent_kit.tools.mcp`
   without it raises an `ImportError` naming the extra.

## API — `agent_kit/tools/mcp.py`

```python
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

def stdio(name: str, command: str, *args: str, env: dict[str, str] | None = None,
          cwd: str | None = None, required: bool = True) -> StdioServer
def http(name: str, url: str, headers: dict[str, str] | None = None,
         timeout_s: float = 30.0, required: bool = True) -> HttpServer


class MCPToolset:
    def __init__(self, *servers: StdioServer | HttpServer, prefix: bool = True,
                 include: Iterable[str] | None = None, call_timeout_s: float = 60.0,
                 errlog: TextIO | None = None) -> None
    async def __aenter__(self) -> MCPToolset
    async def __aexit__(self, *exc: object) -> None
    @property
    def tools(self) -> list[Tool]
    def annotations(self, tool_name: str) -> ToolAnnotations | None   # agent-kit tool name
    def server_for(self, tool_name: str) -> str | None
    @property
    def connected_servers(self) -> list[str]


class MCPConnectionError(AgentKitError): ...      # agent_kit.exceptions

def require_approval_unless_read_only(toolset: MCPToolset, reason: str | None = None) -> BeforeToolHook
```

`include` filters by agent-kit tool name (after prefixing). `errlog` receives stdio servers' stderr
(default `sys.stderr`).

## Behaviour

### Connection

- On enter, all servers connect concurrently: open the transport, `ClientSession.initialize()`, then
  `list_tools()` following `next_cursor` until exhausted.
- A `required` server that fails (transport error, initialize failure, listing failure) → close every
  already-opened server, then raise `MCPConnectionError("<server>: <cause>")`. A server with
  `required=False` is skipped with a warning log and absent from `connected_servers`.
- On exit, every session and transport closes (stdio subprocesses terminate). Exit never raises
  server-close errors (logged at debug).
- Using a tool after exit returns a tool error `"MCP toolset is closed"`.

### Naming

- `prefix=True`: `f"{server}__{tool}"`; `prefix=False`: `tool`.
- Sanitise to `[A-Za-z0-9_-]` (other characters → `_`). Names over 64 characters become the first 55
  characters + `_` + 8 hex of SHA-256 of the unsanitised name.
- Two tools resolving to the same name → `MCPConnectionError` naming both servers and the tool.

### Schema

- `ToolSchema(name=<agent-kit name>, description=tool.description or tool.title or "", parameters=tool.input_schema or {"type": "object", "properties": {}}, idempotent=bool(annotations.idempotent_hint))`.

### Calls

`session.call_tool(original_name, arguments, read_timeout_seconds=call_timeout_s)`:
- Transport timeout → tool error `"MCP tool '<name>' timed out after <N>s"`.
- Non-`CallToolResult` result (a server requesting input) → tool error `"MCP tool '<name>' requested
  interactive input, which is not supported"`.
- `is_error` → tool error with the joined text content (or `"MCP tool '<name>' failed"` when empty).
- Success output:
  - `structured_content` present → that value; if it is exactly `{"result": x}` → `x` (the MCP SDK's
    wrapping of non-object returns).
  - Otherwise join content blocks with `"\n"`: text as-is, images `[image: <mimeType>]`, audio
    `[audio: <mimeType>]`, embedded/linked resources `[resource: <uri>]`. A single text block that
    parses as a JSON object or array is returned parsed.

Tool errors surface as `ToolResult.error` via the existing `Tool` wrapper (the wrapper function
raises `MCPToolError`).

### Approval helper

`require_approval_unless_read_only(mcp)`: for a tool the toolset knows, return `Decision.ask(reason or
"<tool> is not marked read-only")` unless `annotations.read_only_hint is True`; tools the toolset does
not know → allow (so local tools are unaffected).

## Testing — `tests/test_mcp.py` (skipped without `mcp`)

Fixture server `tests/fixtures/mcp_fixture_server.py` (SDK `MCPServer`): `echo` (read-only, returns
str), `add` (returns dict), `inventory` (structured output), `delete_record` (destructive), `fail`
(raises), `slow` (sleeps), `picture` (returns an image). Runs over stdio, and over streamable HTTP via
uvicorn on a free local port for one test.

- Listing, naming with/without prefix, sanitising and long-name hashing, collision error, `include`.
- Schema mapping including `idempotent`.
- Output mapping for each fixture tool; `fail` and `slow` (short `call_timeout_s`) as tool errors.
- End to end through `Agent` with a scripted provider: tool results reach the next request; audit
  `tool_call` events present.
- `require_approval_unless_read_only`: `echo` runs without asking; `delete_record` and an un-annotated
  tool ask; a local tool is unaffected.
- `required` server with a bad command → `MCPConnectionError`, and the good server's subprocess is
  gone; `required=False` → skipped.
- After exit: subprocess terminated; calling a tool returns `"MCP toolset is closed"`.
- Streamable HTTP: connect, list, call.

## Out of scope

MCP resources, prompts, sampling, elicitation, roots; OAuth flows (header auth only); the SSE
transport; exposing agent-kit as an MCP server; reconnecting dropped servers.
