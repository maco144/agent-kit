"""
Human approval for risky tools, redaction of tool output, and a hard stop.

The approver here asks on the terminal; in production it's a Slack button, an
internal approvals API, or anything else you can await.
"""

import asyncio
import re

from agent_kit import Agent, AgentConfig, tool
from agent_kit.hooks import ApprovalRequest, Decision, Hooks, ToolResultContext, deny_tools, require_approval
from agent_kit.providers import AnthropicProvider


@tool(description="Look up an order, including the card on file")
async def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "total": 129.00, "card": "4111 1111 1111 1111"}


@tool(description="Refund an order in full")
async def refund_order(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


@tool(description="Close the customer's account permanently")
async def close_account(customer_id: str) -> dict:
    return {"customer_id": customer_id, "closed": True}


CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){12}(\d{4})\b")


def redact_cards(ctx: ToolResultContext) -> Decision | None:
    """Mask card numbers in string fields before the model or memory sees them."""
    if not isinstance(ctx.output, dict):
        return None
    redacted = {
        key: CARD_NUMBER.sub(r"**** \1", value) if isinstance(value, str) else value
        for key, value in ctx.output.items()
    }
    return Decision.replace(redacted, reason="card number") if redacted != ctx.output else None


async def terminal_approver(req: ApprovalRequest) -> bool:
    answer = await asyncio.to_thread(input, f"\nApprove {req.tool_name}({req.arguments})? [y/N] ")
    return answer.strip().lower() == "y"


async def main() -> None:
    agent = Agent(
        AnthropicProvider(),
        tools=[lookup_order, refund_order, close_account],
        config=AgentConfig(
            system_prompt="You are a support agent. Use tools to resolve the request.",
            hooks=Hooks(
                before_tool=[
                    deny_tools("close_account", reason="account closure needs a human", stop_run=True),
                    require_approval("refund_order", reason="refunds move money"),
                ],
                after_tool=[redact_cards],
            ),
            approver=terminal_approver,
            approval_timeout_s=120,
        ),
    )
    result = await agent.run("Order 1042 arrived broken. Look it up and refund it.")
    print("\n" + result.output)


if __name__ == "__main__":
    asyncio.run(main())
