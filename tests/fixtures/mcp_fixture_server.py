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
