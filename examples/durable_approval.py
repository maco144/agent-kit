"""Durable approvals: suspend a refund for human review, approve it later from another process.

    ANTHROPIC_API_KEY=... python examples/durable_approval.py start
    ANTHROPIC_API_KEY=... python examples/durable_approval.py approve <call_id>
"""

import asyncio
import sys

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.durable import SQLiteRunStore
from agent_kit.hooks import Hooks, require_approval
from agent_kit.providers import AnthropicProvider

RUN_ID = "ticket-9913"


@tool(description="Refund an order in full")
async def refund(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


def build_agent() -> Agent:
    return Agent(
        AnthropicProvider(),
        tools=[refund],
        config=AgentConfig(
            run_store=SQLiteRunStore("runs.db"),
            hooks=Hooks(before_tool=[require_approval("refund", reason="refunds need a human")]),
            approver=SUSPEND,
        ),
    )


async def main() -> None:
    agent = build_agent()
    if sys.argv[1:2] == ["start"]:
        result = await agent.run("Customer on ticket 9913 wants order A-1001 refunded.", run_id=RUN_ID)
        for pending in result.pending_approvals:
            print(f"waiting for approval: {pending.tool_name}({pending.arguments}) — call id {pending.call_id}")
    else:
        result = await agent.resume(RUN_ID, approvals={sys.argv[2]: True})
        print(result.status, "-", result.output)


if __name__ == "__main__":
    asyncio.run(main())
