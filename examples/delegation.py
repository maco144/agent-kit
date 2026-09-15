"""Agents as tools: a support lead delegates to a researcher and a refunds agent whose refunds need approval.

    ANTHROPIC_API_KEY=... python examples/delegation.py start
    ANTHROPIC_API_KEY=... python examples/delegation.py approve <call_id>
"""

import asyncio
import sys

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.durable import SQLiteRunStore
from agent_kit.hooks import Hooks, deny_tools, require_approval
from agent_kit.providers import AnthropicProvider

RUN_ID = "ticket-9914"


@tool(description="Look up an order by id", idempotent=True)
async def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered", "total_usd": 84.00}


@tool(description="Refund an order in full")
async def issue_refund(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


@tool(description="Close a customer account")
async def close_account(customer_id: str) -> dict:
    return {"customer_id": customer_id, "closed": True}


def build_lead() -> Agent:
    research = Agent(
        AnthropicProvider(),
        tools=[lookup_order],
        config=AgentConfig(system_prompt="Answer questions about orders using lookup_order. Be brief."),
    ).as_tool("research", "Look up facts about orders.")
    refunds = Agent(
        AnthropicProvider(),
        tools=[lookup_order, issue_refund, close_account],
        config=AgentConfig(
            system_prompt="Handle refund requests.",
            hooks=Hooks(before_tool=[require_approval("issue_refund", reason="refunds need a human")]),
        ),
    ).as_tool("refunds", "Handle a refund request end to end.")
    return Agent(
        AnthropicProvider(),
        tools=[research, refunds],
        config=AgentConfig(
            system_prompt="You lead customer support. Delegate research and refunds.",
            run_store=SQLiteRunStore("runs.db"),
            approver=SUSPEND,  # approvals from any child suspend the whole ticket
            hooks=Hooks(before_tool=[deny_tools("close_account", reason="account closure is manual")]),
            max_run_cost_usd=1.00,  # covers the lead and every delegated run
        ),
    )


async def main() -> None:
    lead = build_lead()
    if sys.argv[1:2] == ["start"]:
        result = await lead.run("Ticket 9914: customer says order A-1001 arrived broken and wants a refund.", run_id=RUN_ID)
        for pending in result.pending_approvals:
            print(f"waiting for approval: {pending.tool_name}({pending.arguments}) — call id {pending.call_id}")
    else:
        result = await lead.resume(RUN_ID, approvals={sys.argv[2]: True})
        print(result.status, "-", result.output, f"(${result.total_cost_usd:.4f} across all runs)")


if __name__ == "__main__":
    asyncio.run(main())
