"""
Tools from an MCP server, with approval required for anything not marked read-only.

Uses the official filesystem MCP server (needs Node.js). Requires:
pip install agent-kit-ai[mcp] and ANTHROPIC_API_KEY.
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
